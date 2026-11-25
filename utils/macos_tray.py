"""Run pystray's macOS icon alongside the Flet asyncio loop."""

import AppKit
import objc
from Foundation import NSDate, NSDefaultRunLoopMode
from pystray._darwin import Icon


class MacOSTrayIcon(Icon):
    def __init__(self, *args, **kwargs):
        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
        app.finishLaunching()
        super().__init__(*args, darwin_nsapplication=app, **kwargs)
        self._status_item.setAutosaveName_("MiBudsClient")
        self._status_item.button().setAccessibilityLabel_("MiBudsClient")
        self._status_item.button().sendActionOn_(
            AppKit.NSEventMaskLeftMouseUp | AppKit.NSEventMaskRightMouseUp
        )

    def _update_menu(self):
        # Present the same menu for both buttons via the status button action.
        callbacks = []
        self._menu_handle = (self._create_menu(self.menu, callbacks), callbacks)

    def __call__(self):
        menu, _ = self._menu_handle
        if menu is not None:
            self._status_item.popUpStatusItemMenu_(menu)

    def start(self, loop):
        # pystray invokes setup on a worker, but AppKit requires the main thread.
        self.run_detached(setup=lambda icon: None)
        self.visible = True

        def pump():
            if not self._running:
                return
            # Flet's Python process owns the icon but has no NSApplication loop.
            with objc.autorelease_pool():
                while (event := self._app.nextEventMatchingMask_untilDate_inMode_dequeue_(
                    AppKit.NSEventMaskAny, NSDate.distantPast(), NSDefaultRunLoopMode, True
                )) is not None:
                    self._app.sendEvent_(event)
            loop.call_later(0.04, pump)

        loop.call_soon(pump)

    def _stop(self):
        # Only remove our item when pystray is running detached.
        self._status_bar.removeStatusItem_(self._status_item)
