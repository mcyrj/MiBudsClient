"""Prepare a report for the user to post on GitHub."""
import asyncio
import copy
import json
import threading
import webbrowser
from functools import wraps
from textwrap import indent

import flet as ft
from utils.i18n import t
from utils.error_reports import redact
from ui.constants import COLOR_TEXT_PRIMARY, GITHUB_URL


def serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call


class ErrorReportDialog:
    def __init__(self, page, *, ui_lock=None):
        self.page = page
        self._clipboard = ft.Clipboard()
        self.pending = []
        self.active = None
        self._lock = ui_lock if ui_lock is not None else threading.RLock()

    @serialized
    def receive(self, report):
        if len(self.pending) < 20:
            self.pending.append(copy.deepcopy(report))
        self.show_pending()

    @serialized
    def show_pending(self):
        if self.active is not None or not self.pending or self.page.window.visible is False:
            return
        report = self.pending.pop(0)
        description = ft.TextField(label=t('report.description'), multiline=True,
                                   min_lines=3, max_lines=5, max_length=4000)
        preview = ft.Text('', selectable=True, size=11)
        feedback = ft.Text('', color=ft.Colors.RED, visible=False)
        def synchronized(callback):
            @wraps(callback)
            def call(e=None):
                with self._lock:
                    return callback(e)
            return call

        @synchronized
        def update_preview(e=None):
            report['details']['description'] = redact(description.value or '')[:4000]
            preview.value = json.dumps(report, ensure_ascii=False, indent=2)
            if e is not None:
                self.page.update()
        description.on_change = update_preview
        update_preview()

        @synchronized
        def close(e=None):
            dialog.open = False
            # Keep the control mounted until Flutter acknowledges dismissal.
            # Removing an open dialog from overlay leaves its native route alive.
            dialog.update()

        @synchronized
        def dismissed(e):
            if self.active is dialog:
                self.active = None
            self.show_pending()

        async def open_issue(e):
            with self._lock:
                if send_button.disabled:
                    return
                send_button.disabled = True
                cancel_button.disabled = True
                feedback.visible = False
                update_preview()
                body = '\n\n' + indent(preview.value, '    ')
                dialog.update()
            try:
                await self._clipboard.set(body)
                opened = await asyncio.to_thread(
                    webbrowser.open, f'{GITHUB_URL}/issues/new?template=bug_report.md')
            except Exception:
                opened = False
            with self._lock:
                if self.active is not dialog or not dialog.open:
                    return
                send_button.disabled = False
                cancel_button.disabled = False
                feedback.value = t('report.copied' if opened else 'report.open_failed')
                feedback.color = COLOR_TEXT_PRIMARY if opened else ft.Colors.RED
                feedback.visible = True
                dialog.update()

        send_button = ft.ElevatedButton(t('report.open_github'), on_click=open_issue)
        cancel_button = ft.TextButton(t('report.cancel'), on_click=close)
        dialog = ft.AlertDialog(
            modal=True, title=ft.Text(t('report.title')),
            content=ft.Container(width=420, content=ft.Column([
                ft.Text(t('report.consent')),
                ft.Text(report['message'], selectable=True, size=12),
                description, ft.Text(t('report.privacy'), size=12),
                ft.ExpansionTile(title=ft.Text(t('report.preview')), controls=[preview]),
                feedback,
            ], tight=True, scroll=ft.ScrollMode.AUTO)),
            actions=[cancel_button, send_button],
        )
        self.active = dialog
        dialog.on_dismiss = dismissed
        self.page.show_dialog(dialog)
