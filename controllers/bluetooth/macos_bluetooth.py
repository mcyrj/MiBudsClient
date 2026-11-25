"""macOS Bluetooth Classic discovery and RFCOMM via Apple's IOBluetooth API."""

import ctypes
import queue
import socket
import threading
import time
from uuid import UUID

from Foundation import NSData, NSObject
from IOBluetooth import IOBluetoothDevice, IOBluetoothSDPUUID
import objc
from CoreFoundation import CFRunLoopRunInMode, kCFRunLoopDefaultMode

_main_loop = None
_main_thread_id = None
_main_loop_lock = threading.Lock()


def install_iobluetooth_runloop(loop):
    """Pump IOBluetooth callbacks from the application's main event loop."""
    global _main_loop, _main_thread_id
    with _main_loop_lock:
        if _main_loop is loop:
            return
        _main_loop = loop
        _main_thread_id = threading.get_ident()

    def pump():
        if loop.is_closed():
            return
        CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.002, True)
        loop.call_later(0.01, pump)

    loop.call_soon(pump)


def _on_main_thread(callback, timeout=8):
    with _main_loop_lock:
        loop, thread_id = _main_loop, _main_thread_id
    if loop is None or threading.get_ident() == thread_id:
        return callback()

    done = threading.Event()
    result = []

    def invoke():
        try:
            result.append((True, callback()))
        except BaseException as error:
            result.append((False, error))
        finally:
            done.set()

    loop.call_soon_threadsafe(invoke)
    if not done.wait(timeout):
        raise TimeoutError("macOS Bluetooth main-thread dispatch timed out")
    succeeded, value = result[0]
    if not succeeded:
        raise value
    return value


def _device(address):
    device = IOBluetoothDevice.deviceWithAddressString_(address)
    if device is None:
        raise OSError(f"Bluetooth device is not paired or unavailable: {address}")
    return device


def connected_devices():
    """Return paired Classic devices that macOS currently reports connected."""
    def read_devices():
        devices = []
        for device in IOBluetoothDevice.pairedDevices() or ():
            if device.isConnected():
                name = str(device.name() or "")
                address = str(device.addressString() or "")
                if name and address:
                    devices.append((name, address))
        return tuple(devices)

    return _on_main_thread(read_devices)


class _DeviceConnectObserver(NSObject):
    @objc.typedSelector(b"v@:@@")
    def deviceConnected_device_(self, notification, device):
        callback = self.callback
        if callback is not None:
            callback(str(device.name() or ""), str(device.addressString() or ""))


def watch_device_connections(callback):
    """Subscribe on the IOBluetooth run loop and return a cleanup callback."""
    observer = _DeviceConnectObserver.alloc().init()
    observer.callback = callback
    notification = IOBluetoothDevice.registerForConnectNotifications_selector_(
        observer, b"deviceConnected:device:"
    )
    if notification is None:
        raise OSError("Could not register macOS Bluetooth connection notifications")

    def stop():
        # Keep the observer alive until the native subscription is removed.
        def unregister():
            observer.callback = None
            notification.unregister()

        if _main_loop is not None and _main_loop.is_running():
            _on_main_thread(unregister)

    return stop


class _SDPDelegate(NSObject):
    def init(self):
        self = objc.super(_SDPDelegate, self).init()
        if self is None:
            return None
        self.done = threading.Event()
        self.status = None
        return self

    def sdpQueryComplete_status_(self, device, status):
        self.status = int(status)
        self.done.set()


def _sdp_uuid(value):
    raw = UUID(value).bytes
    data = NSData.dataWithBytes_length_(raw, len(raw))
    return IOBluetoothSDPUUID.uuidWithData_(data)


def _service_channel(record):
    result = record.getRFCOMMChannelID_(None)
    if not isinstance(result, tuple) or len(result) < 2:
        raise OSError("PyObjC did not return the RFCOMM service channel")
    return int(result[0]), int(result[1])


def resolve_rfcomm_channel(address, service_uuid, timeout, cancel):
    """Resolve RFCOMM only from the requested service record on this device."""
    if cancel.is_set():
        raise OSError("SDP lookup cancelled")
    device = _on_main_thread(lambda: _device(address))
    uuid = _on_main_thread(lambda: _sdp_uuid(service_uuid))
    delegate = _on_main_thread(lambda: _SDPDelegate.alloc().init())
    status = _on_main_thread(lambda: int(device.performSDPQuery_(delegate)))
    if status != 0:
        raise OSError(f"macOS SDP query could not start (IOBluetooth status {status})")
    deadline = time.monotonic() + timeout
    while not delegate.done.is_set():
        if cancel.is_set():
            raise OSError("SDP lookup cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("macOS SDP lookup timed out")
        if cancel.wait(min(0.05, remaining)):
            raise OSError("SDP lookup cancelled")
    if delegate.status != 0:
        raise OSError(f"macOS SDP query failed (IOBluetooth status {delegate.status})")
    record = _on_main_thread(lambda: device.getServiceRecordForUUID_(uuid))
    if record is None:
        return None
    status, channel = _on_main_thread(lambda: _service_channel(record))
    if status != 0:
        raise OSError(f"macOS RFCOMM channel lookup failed (IOBluetooth status {status})")
    if not 1 <= channel <= 30:
        raise OSError("macOS SDP returned an invalid RFCOMM channel")
    return channel


class _RFCOMMDelegate(NSObject, protocols=[objc.protocolNamed("IOBluetoothRFCOMMChannelDelegate")]):
    def init(self):
        self = objc.super(_RFCOMMDelegate, self).init()
        if self is None:
            return None
        self.incoming = queue.Queue()
        self.closed = threading.Event()
        self.opened = threading.Event()
        self.open_status = None
        self.channel = None
        self.abandoned = False
        self.lock = threading.Lock()
        return self

    def rfcommChannelOpenComplete_status_(self, channel, status):
        with self.lock:
            self.channel = channel
            self.open_status = int(status)
            abandoned = self.abandoned
        self.opened.set()
        if abandoned:
            channel.closeChannel()

    @objc.typedSelector(b"v@:@^vQ")
    def rfcommChannelData_data_length_(self, channel, data, length):
        if not 0 < length <= 0xFFFF or not data:
            return
        if isinstance(data, int):
            payload = ctypes.string_at(data, length)
        else:
            payload = bytes(data[:length])
        self.incoming.put(payload)

    def rfcommChannelClosed_(self, channel):
        self.closed.set()
        self.incoming.put(None)


class MacRFCOMMSocket:
    """Socket-shaped adapter over IOBluetoothRFCOMMChannel for shared transport."""

    def __init__(self):
        self._delegate = _on_main_thread(lambda: _RFCOMMDelegate.alloc().init())
        self._channel = None
        self._timeout = None
        self._pending = bytearray()

    def settimeout(self, timeout):
        self._timeout = timeout

    def connect(self, endpoint):
        address, channel_id = endpoint
        status, channel = _on_main_thread(
            lambda: _device(address).openRFCOMMChannelAsync_withChannelID_delegate_(
                None, int(channel_id), self._delegate
            ),
            timeout=self._timeout if self._timeout is not None else 18,
        )
        status = int(status)
        if status != 0 or channel is None:
            raise ConnectionError(f"macOS RFCOMM open failed (IOBluetooth status {status})")
        self._channel = channel

        deadline = time.monotonic() + (self._timeout if self._timeout is not None else 18)
        while not self._delegate.opened.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("timed out opening macOS RFCOMM channel")
            if self._delegate.opened.wait(min(0.05, remaining)):
                break
        if self._delegate.open_status != 0:
            self._channel = None
            raise ConnectionError(
                f"macOS RFCOMM open failed (IOBluetooth status {self._delegate.open_status})"
            )
        if self._delegate.channel is not None:
            self._channel = self._delegate.channel

    def sendall(self, data):
        if self._channel is None:
            raise ConnectionError("RFCOMM channel is closed")
        payload = bytes(data)
        for offset in range(0, len(payload), 0xFFFF):
            part = payload[offset:offset + 0xFFFF]
            status = int(_on_main_thread(
                lambda part=part: self._channel.writeSync_length_(part, len(part)),
                timeout=self._timeout if self._timeout is not None else 18,
            ))
            if status != 0:
                raise OSError(f"macOS RFCOMM write failed (IOBluetooth status {status})")

    def recv(self, size):
        while not self._pending:
            try:
                data = self._delegate.incoming.get(timeout=self._timeout)
            except queue.Empty as error:
                raise socket.timeout("timed out") from error
            if data is None:
                return b""
            self._pending.extend(data)
        result = bytes(self._pending[:size])
        del self._pending[:size]
        return result

    def shutdown(self, how):
        self.close()

    def close(self):
        channel, self._channel = self._channel, None
        if channel is not None:
            def close_native():
                if channel.isOpeningRFCOMM() and not channel.isOpen():
                    with self._delegate.lock:
                        self._delegate.abandoned = True
                        opened = self._delegate.opened.is_set()
                    if opened and self._delegate.channel is not None:
                        self._delegate.channel.closeChannel()
                    return
                channel.closeChannel()

            _on_main_thread(close_native)
