"""Lifecycle diagnostics are isolated and never serialize user input."""
import sys
import faulthandler
from PySide6.QtCore import qWarning
from desktop.diagnostics import NativeDiagnostics, event


def test_native_diagnostics_capture_phase_and_frames_without_exception_values(tmp_path):
    before_hook, before_fault = sys.excepthook, faulthandler.is_enabled()
    with NativeDiagnostics(tmp_path, 'fixture-version'):
        event('generation.submit')
        qWarning('fixture-secret-not-for-logs')
        try:
            raise ValueError('fixture-secret-not-for-logs')
        except ValueError:
            sys.excepthook(*sys.exc_info())
    assert sys.excepthook is before_hook and faulthandler.is_enabled() == before_fault
    content = next(tmp_path.glob('*.log')).read_text(encoding='utf-8')
    assert 'generation.submit' in content and 'python ValueError' in content and 'frame ' in content
    assert 'fixture-secret-not-for-logs' not in content and 'shutdown' in content


def test_unwritable_diagnostics_do_not_replace_hooks_or_prevent_startup(tmp_path):
    not_directory = tmp_path / 'file'
    not_directory.write_text('preserve')
    before_hook = sys.excepthook
    with NativeDiagnostics(not_directory, 'fixture-version'):
        event('generation.submit')
    assert sys.excepthook is before_hook and not_directory.read_text() == 'preserve'
