import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from utils.error_reports import (ErrorReportManager, encode_report, guard_ui_callbacks,
                                 make_report, redact)
from ui.components.error_report import ErrorReportDialog
from utils.debug_console import DebugConsoleManager


class ErrorReportTests(unittest.TestCase):
    def test_pubsub_dispatch_serializes_control_updates(self):
        from ui.app_message_handler import AppMessageHandler
        handler = AppMessageHandler.__new__(AppMessageHandler)
        handler._closing = False
        handler._dispatch_lock = threading.RLock()
        first_entered, release_first, second_entered = (threading.Event() for _ in range(3))
        def first(message):
            first_entered.set()
            release_first.wait(2)
        handler._routes = {'first': first, 'second': lambda message: second_entered.set()}
        threads = [threading.Thread(target=handler.handle, args=({'type': kind},))
                   for kind in ('first', 'second')]
        threads[0].start()
        self.assertTrue(first_entered.wait(2))
        threads[1].start()
        try:
            self.assertFalse(second_entered.wait(0.05))
        finally:
            release_first.set()
            for thread in threads:
                thread.join(2)
        self.assertTrue(second_entered.is_set())

    def report_page(self, visible=True):
        page = SimpleNamespace(window=SimpleNamespace(visible=visible), overlay=[], update=Mock())
        def show_dialog(control):
            control.open = True
            control.update = Mock()
        page.show_dialog = Mock(side_effect=show_dialog)
        return page

    def test_ui_callback_failures_are_captured(self):
        import flet as ft
        capture = Mock()
        def fail(e):
            raise ValueError('callback')
        button = ft.TextButton('test', on_click=fail)
        guard_ui_callbacks([button], capture)
        button.on_click(None)
        self.assertIsInstance(capture.call_args.args[0], ValueError)

    def test_redaction_and_stack_excludes_source(self):
        try:
            raise ValueError('token=abc password=xyz AA:BB:CC:DD:EE:FF user@example.com https://host/private')
        except ValueError as error:
            report = make_report(error)
        self.assertNotIn('abc', report['message'])
        self.assertNotIn('xyz', report['message'])
        self.assertNotIn('AA:BB', report['message'])
        self.assertNotIn('raise ValueError', report['stack'])
        self.assertNotIn(str(Path.home()), redact(str(Path.home()) + '/private'))
        self.assertIn('test_error_reports.py:', report['stack'])

    def test_unicode_budget(self):
        report = make_report(ValueError('\U0001f600' * 10000))
        report['details']['description'] = '\U0001f600' * 4000
        self.assertLessEqual(len(encode_report(report)), 65536)
        with self.assertRaises(ValueError):
            encode_report({'message': '\U0001f600' * 20000})

    def test_deduplication_and_reporter_failure(self):
        publish = Mock()
        manager = ErrorReportManager(publish=publish, app_version='v1.0', get_device=lambda: '')
        manager.capture(ValueError('same'))
        manager.capture(ValueError('same'))
        self.assertEqual(publish.call_count, 1)
        publish.side_effect = RuntimeError('reporter failed')
        manager.capture(ValueError('different'))

    def test_dialog_hidden_preview(self):
        page = self.report_page(visible=False)
        dialog = ErrorReportDialog(page)
        dialog.receive(make_report(ValueError('test')))
        self.assertIsNone(dialog.active)
        page.window.visible = True
        dialog.show_pending()
        controls = dialog.active.content.content.controls
        field = next(child for child in controls if hasattr(child, 'on_change'))
        field.value = 'I clicked connect'
        field.on_change(None)
        preview = next(child for child in controls if hasattr(child, 'controls')).controls[0]
        self.assertEqual(json.loads(preview.value)['details']['description'], field.value)

    def test_pending_dialog_waits_for_native_dismissal(self):
        page = self.report_page()
        manager = ErrorReportDialog(page)
        manager.receive(make_report(ValueError('first')))
        first = manager.active
        manager.receive(make_report(ValueError('second')))
        first.actions[0].on_click(None)
        page.show_dialog.assert_called_once_with(first)
        first.on_dismiss(None)
        self.assertIsNot(manager.active, first)
        self.assertEqual(page.show_dialog.call_count, 2)

    def test_hooks_preserve_original(self):
        callback = Mock()
        manager = DebugConsoleManager(on_exception=callback)
        manager._orig_excepthook = Mock()
        error = ValueError('test')
        manager._handle_exception(ValueError, error, None)
        callback.assert_called_once_with(error, tb=None)
        manager._orig_excepthook.assert_called_once()
        manager._orig_threading_hook = Mock()
        manager._handle_thread_exception(SimpleNamespace(exc_type=ValueError, exc_value=error, exc_traceback=None))
        self.assertEqual(callback.call_count, 2)


if __name__ == '__main__':
    unittest.main()
