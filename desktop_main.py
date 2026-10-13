"""Windows native desktop entry point (source and frozen portable package)."""
from __future__ import annotations

import argparse
import multiprocessing
import os
from pathlib import Path


def main() -> int:
    from app_version import VERSION
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--preferences-file", type=Path, help="Override desktop preferences location (for isolated testing)")
    parser.add_argument("--smoke-test", type=Path, help="Run offline native acceptance in a new isolated directory")
    parser.add_argument("--verify-launch", type=Path, help="Verify normal locked startup/event loop, save evidence and close; requires isolated --data-dir or --preferences-file")
    args = parser.parse_args()
    if args.verify_launch and ((not args.data_dir and not args.preferences_file) or args.verify_launch.exists()):
        parser.error("--verify-launch requires isolated --data-dir or --preferences-file and a new evidence directory")
    from desktop.installer_guard import InstallerGuard
    # Hold the marker before importing Qt or spawning the business process.
    # Acquiring it only after startup leaves a race where an upgrade replaces
    # DLLs while the application is still loading them.
    guard = InstallerGuard()
    try:
        return run_application(args, VERSION)
    finally:
        guard.close()


def run_application(args, version) -> int:
    from PySide6.QtWidgets import QApplication
    from desktop.runtime import BusinessRuntime
    from desktop.window import MainWindow
    application = QApplication([])
    application.setStyle("Fusion")
    from PySide6.QtGui import QIcon
    application.setWindowIcon(QIcon(str(Path(__file__).resolve().parent / "desktop/assets/app.svg")))
    application.setApplicationName("TTS Dataset Studio")
    application.setApplicationVersion(version)
    application.setOrganizationName("TTS Dataset Studio")
    if args.smoke_test:
        from desktop.smoke import run_smoke
        return run_smoke(application, args.smoke_test)
    from PySide6.QtWidgets import QMessageBox
    from desktop.preferences import DesktopPreferences
    preferences = DesktopPreferences(args.preferences_file)
    try:
        directory = args.data_dir or preferences.data_directory() or Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local")) / "TTS Dataset Studio" / "data"
    except (OSError, ValueError):
        QMessageBox.warning(None, "数据目录配置不可用", "无法读取已保存的数据目录配置，请检查配置文件或用 --data-dir 指定目录。")
        return 1
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        QMessageBox.warning(None, "数据目录不可用", "无法创建或写入数据目录，请检查路径与访问权限。")
        return 1
    # A per-directory lock prevents a second desktop process from changing
    # startup task state under a running first instance. Never remove another
    # process's lock; Qt checks its PID and handles stale process locks.
    from desktop.session import DirectorySession
    try:
        session = DirectorySession.open(directory, preferences)
    except (ValueError, OSError, RuntimeError):
        if args.verify_launch:
            import json
            args.verify_launch.mkdir(parents=True)
            (args.verify_launch / "result.json").write_text(json.dumps({"ok": False, "reason": "data-directory-lock-unavailable"}), encoding="utf-8")
        else:
            QMessageBox.warning(None, "数据目录不可用", "该数据目录已由另一实例使用或无法写入，请关闭原窗口或选择其他目录。")
        return 1
    runtime = session.runtime
    try:
        window = MainWindow(runtime, preferences, session)
        window.show()
        if args.verify_launch:
            from desktop.launch_check import attach_launch_check
            check_timer = attach_launch_check(application, window, args.verify_launch)
        return application.exec()
    finally:
        session.close()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
