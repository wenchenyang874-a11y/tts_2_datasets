"""Bounded verification of the normal QApplication.exec / locked entry path."""
import json
import platform
import time
from PySide6.QtCore import QTimer
from .widgets import HelpButton


def attach_launch_check(application, window, destination):
    destination.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    timer = QTimer(window)
    timer.setInterval(100)
    report = {"platform": platform.platform(), "errors": []}
    window.error = lambda error: report["errors"].append(str(error))

    def finish():
        if window.ready and not window.jobs and time.monotonic() - started >= 2:
            report["ok"] = not report["errors"]
            report["window_size"] = [window.width(), window.height()]
            report["help_controls"] = len(window.findChildren(HelpButton))
            report["data_directory_locked"] = True
            window.grab().save(str(destination / "window.png"))
        elif time.monotonic() - started < 45 and not report["errors"]:
            return
        else:
            report["ok"] = False
        timer.stop()
        window.close()
        report["business_process_closed"] = not window.runtime.process.is_alive()
        (destination / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        application.exit(0 if report["ok"] and report["business_process_closed"] else 1)

    timer.timeout.connect(finish)
    timer.start()
    return timer
