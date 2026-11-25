"""Collect and redact diagnostics for user-reviewed error reports."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import threading
import traceback
import functools
import inspect
from contextlib import nullcontext
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone


def redact(value):
    text = str(value)
    for root in {str(Path.home()), os.environ.get('USERPROFILE', ''), os.environ.get('HOME', '')}:
        if root:
            text = re.sub(re.escape(root), '[user]', text, flags=re.I)
    text = re.sub(r'(?i)\b(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b', '[address]', text)
    text = re.sub(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', '[address]', text)
    text = re.sub(r'(?i)\b(?:https?://|www\.)\S+', '[url]', text)
    text = re.sub(r'(?i)\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b', '[email]', text)
    text = re.sub(r'(?i)(\b(?:password|passwd|token|secret|api[_-]?key|authorization)\b[\s\"\x27:=]+)(?:Bearer\s+)?[^\s,;\"\x27}]+', r'\1[redacted]', text)
    text = re.sub(r'(?i)\bBearer\s+\S+', 'Bearer [redacted]', text)
    text = re.sub(r'(?i)[a-z]:[\\/]users[\\/][^\\/\s]+|/home/[^/\s]+', '[user]', text)
    return text


def encode_report(report):
    body = json.dumps(report, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(body) > 65536:
        raise ValueError('Report exceeds UTF-8 body limit')
    return body


def make_report(error, *, app_version='', device='', action='', tb=None, identity=None):
    frames = traceback.extract_tb(tb if tb is not None else error.__traceback__)
    stack = '\n'.join(f'{Path(frame.filename).name}:{frame.lineno} in {frame.name}' for frame in frames)
    # Preserve handled transport causes and safe metadata within the existing
    # API schema. Never collect socket data, auth challenges, or frame locals.
    causes, current, visited = [], error, set()
    while current is not None and id(current) not in visited and len(causes) < 8:
        visited.add(id(current))
        causes.append(f'{type(current).__name__}: {str(current)[:256]} '
                      f'(errno={getattr(current, "errno", None)}, winerror={getattr(current, "winerror", None)})')
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    diagnostics = getattr(error, 'bluetooth_diagnostics', None)
    if diagnostics is not None:
        stack += '\n\nBluetooth diagnostics:\n' + json.dumps(diagnostics, ensure_ascii=False, default=str)
        stack += '\nCauses:\n' + '\n'.join(causes)
    if identity is not None and diagnostics is None:
        stack += '\n\nDevice identity:\n' + json.dumps(identity, ensure_ascii=False, default=str)
    report = {
        'message': redact(str(error) or type(error).__name__)[:4096],
        'name': type(error).__name__[:128], 'stack': redact(stack)[-16000:],
        'app_version': app_version.lstrip('v')[:64],
        'os': f'{platform.system()} {platform.release()}'[:256],
        'device': redact(device)[:512],
        'occurred_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'details': {'action': redact(action)[:128], 'description': ''},
    }
    # Keep the report compact while leaving room for an optional description.
    while len(json.dumps(report, ensure_ascii=False).encode('utf-8')) > 48000 and report['stack']:
        report['stack'] = report['stack'][1000:]
    return report


def publish_exception(publish, error, action):
    """Report caught programming errors without changing existing result callbacks."""
    if isinstance(error, (OSError, TimeoutError)):
        return
    try:
        publish({'type': 'report_exception', 'error': error, 'action': action})
    except Exception:
        pass


def guard_ui_callbacks(root, capture, *, ui_lock=None):
    """Catch Python handler failures before Flet consumes their exceptions."""
    visited = set()
    def visit(value):
        if id(value) in visited:
            return
        visited.add(id(value))
        if isinstance(value, (list, tuple)):
            for child in value:
                visit(child)
        elif is_dataclass(value) and not isinstance(value, type):
            for field in fields(value):
                if field.name.startswith('_') or field.name in {'parent', 'data'}:
                    continue
                handler = getattr(value, field.name, None)
                if field.name.startswith('on_') and callable(handler):
                    def wrap(callback, action):
                        if inspect.iscoroutinefunction(callback):
                            @functools.wraps(callback)
                            async def guarded(*args, **kwargs):
                                try:
                                    return await callback(*args, **kwargs)
                                except Exception as exc:
                                    capture(exc, action)
                        else:
                            @functools.wraps(callback)
                            def guarded(*args, **kwargs):
                                with ui_lock if ui_lock is not None else nullcontext():
                                    try:
                                        return callback(*args, **kwargs)
                                    except Exception as exc:
                                        capture(exc, action)
                        return guarded
                    setattr(value, field.name, wrap(handler, f'{type(value).__name__}.{field.name}'))
                else:
                    visit(handler)
    visit(root)


class ErrorReportManager:
    def __init__(self, *, publish, app_version, get_device, get_identity=None):
        self.publish, self.app_version, self.get_device = publish, app_version, get_device
        self.get_identity = get_identity
        self.seen = set()
        self.lock = threading.Lock()

    def capture(self, error, action='', tb=None):
        signature = None
        try:
            report = make_report(error, app_version=self.app_version,
                                 device=self.get_device(), action=action, tb=tb,
                                 identity=self.get_identity() if self.get_identity is not None else None)
            signature = hashlib.sha256(encode_report({key: report[key] for key in ('name', 'message', 'stack')})).hexdigest()
            with self.lock:
                if signature in self.seen:
                    print(redact(f'[Error reports] Duplicate report skipped: action={action}'))
                    return
                self.seen.add(signature)
            self.publish({'type': 'error_report', 'report': report})
        except Exception as exc:
            print(f'[Error reports] Could not prepare report: {redact(exc)}')
            if signature is not None:
                with self.lock:
                    self.seen.discard(signature)
