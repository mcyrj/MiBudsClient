"""Battery telemetry from complete target-info and status RCSP frames."""
import threading

from .rcsp import RcspStreamParser, is_supported_target, parse_info_fields
from .protocol import BatteryStatus


class BatteryStreamParser:
    def __init__(self):
        self._parser = RcspStreamParser()
        self._lock = threading.Lock()
        self._values = [255, 255, 255]

    def reset(self):
        with self._lock:
            self._parser.reset()
            self._values = [255, 255, 255]

    def feed(self, data):
        return self.feed_packets(self._parser.feed(data))

    def feed_packets(self, packets):
        statuses = []
        with self._lock:
            for packet in packets:
                if packet.opcode == 0x02 and not packet.flags & 0x80:
                    if len(packet.payload) < 2 or packet.payload[0]:
                        continue
                    data, kind = packet.payload[2:], 7
                elif packet.opcode == 0x0E and packet.flags & 0x80:
                    if not packet.payload:
                        continue
                    data, kind = packet.payload[1:], 0
                else:
                    continue
                source = f"opcode=0x{packet.opcode:02X} flags=0x{packet.flags:02X}"
                try:
                    fields = parse_info_fields(data)
                except ValueError:
                    print(f"[Bluetooth] Battery fields {source}: malformed attributes")
                    continue
                values = fields.get(kind)
                layout = ", ".join(f"0x{key:02X}/{len(value)}" for key, value in fields.items())
                if values is None:
                    raw = "missing"
                elif not 1 <= len(values) <= 3:
                    raw = "unexpected length"
                else:
                    raw = values.hex(" ").upper()
                print(f"[Bluetooth] Battery fields {source}: attributes={layout or '-'} raw={raw}")
                if not is_supported_target(packet.flags):
                    continue
                if values is None or not 1 <= len(values) <= 3:
                    continue
                if any(value != 255 and (value & 127) > 100 for value in values):
                    continue
                # Omitted components retain their last value; FF explicitly
                # means unknown, and zero is a valid empty battery.
                self._values[:len(values)] = values
                statuses.append(BatteryStatus(*self._values))
        return statuses
