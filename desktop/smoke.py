"""Repeatable offline acceptance of the actual native UI and isolated runtime.

This entry deliberately refuses an existing output tree. It never opens the
normal user dataset, saves cloud credentials or performs a paid cloud call.
"""
from __future__ import annotations

import io
import json
import math
import platform
import time
import wave
import zipfile
from pathlib import Path


def run_smoke(application, destination: Path) -> int:
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import QPushButton
    from PySide6.QtTest import QTest
    from .runtime import BusinessRuntime, RuntimeErrorDetail
    from .window import MainWindow
    from .widgets import HelpButton
    from .data_transfer import copy_dataset
    from .preferences import DesktopPreferences
    destination = destination.resolve()
    if destination.exists():
        raise ValueError("自检输出目录必须不存在，以隔离真实数据")
    destination.mkdir(parents=True)
    import faulthandler
    trace = (destination / "hang-trace.txt").open("w", encoding="utf-8")
    faulthandler.dump_traceback_later(40, repeat=True, file=trace)
    result = {"platform": platform.platform(), "checks": [], "errors": []}
    windows = []

    def pump_until(predicate, timeout=45):
        deadline = time.monotonic() + timeout
        while not predicate():
            application.processEvents()
            if time.monotonic() >= deadline:
                raise AssertionError("native UI operation timed out")
            # QTest.qWait enters a nested Qt waiting loop and stalled during
            # repeated page transitions in this Windows/PySide build. Pump the
            # normal event queue explicitly, then yield Python briefly so the
            # deadline remains enforceable and background commands can finish.
            time.sleep(.02)
        application.processEvents()

    def idle(window):
        pump_until(lambda: not window.jobs)
        assert not result["errors"], result["errors"]

    def action(page, text):
        matches = [item for item in page.findChildren(QPushButton) if item.text() == text and item.isVisible()]
        assert matches, text
        page.shell.stack.currentWidget().ensureWidgetVisible(matches[0])
        application.processEvents()
        QTest.mouseClick(matches[0], Qt.MouseButton.LeftButton)

    def check(name):
        result["checks"].append(name)
        (destination / "progress.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    def wait_task(runtime, identifier):
        deadline = time.monotonic() + 60
        while True:
            task = runtime.call(f"/api/tasks/{identifier}")
            if task["status"] not in ("running", "queued"):
                assert task["status"] in ("completed", "done"), task
                return task
            assert time.monotonic() < deadline, task
            application.processEvents()
            time.sleep(.04)

    def start(directory):
        runtime = BusinessRuntime(directory)
        window = MainWindow(runtime, DesktopPreferences(destination / "desktop_preferences.json"))
        # Keep this owned test window on the primary screen and above any
        # unrelated user's window during pointer acceptance. widgetAt() must
        # identify our actual control; synthetic Help events are not proof.
        window.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        window.move(application.primaryScreen().availableGeometry().topLeft())
        windows.append(window)
        # Collect errors instead of leaving an unattended modal dialog open.
        window.error = lambda error: result["errors"].append(str(error))
        window.show()
        pump_until(lambda: window.ready)
        idle(window)
        return runtime, window

    try:
        runtime, window = start(destination / "data")
        check("native-window-start-and-ready")
        helps = window.findChildren(HelpButton)
        assert len(helps) >= 15 and all(item.toolTip() for item in helps)
        for index in range(7):
            QTest.mouseClick(window.nav_buttons[index], Qt.MouseButton.LeftButton)
            idle(window)
            assert window.stack.currentIndex() == index
            if not window.grab().save(str(destination / f"page-{index}.png")):
                raise AssertionError("screenshot save failed")
        check("seven-native-pages-and-help-controls")
        from PySide6.QtGui import QCursor
        from PySide6.QtWidgets import QLabel
        visible_help = next(item for item in helps if item.isVisible())
        window.stack.currentWidget().ensureWidgetVisible(visible_help)
        window.activateWindow()
        window.raise_()
        pump_until(lambda: window.isActiveWindow(), 4)
        QCursor.setPos(window.mapToGlobal(window.rect().topLeft()))
        application.processEvents()
        QTest.mouseMove(visible_help, visible_help.rect().center())
        QCursor.setPos(visible_help.mapToGlobal(visible_help.rect().center()))
        application.processEvents()
        assert application.widgetAt(QCursor.pos()) is visible_help, "Actual cursor is not over the owned help control"
        try:
            pump_until(lambda: visible_help.popup is not None and visible_help.popup.isVisible(), 4)
        except AssertionError:
            result["hover_debug"] = {"active": window.isActiveWindow(), "window": [window.x(),window.y(),window.width(),window.height()], "hovered": visible_help.hovered, "under_mouse": visible_help.underMouse(), "popup_created": visible_help.popup is not None,
                                     "widget_at_cursor": type(application.widgetAt(QCursor.pos())).__name__, "cursor": [QCursor.pos().x(), QCursor.pos().y()], "target": [visible_help.mapToGlobal(visible_help.rect().center()).x(), visible_help.mapToGlobal(visible_help.rect().center()).y()]}
            raise
        assert visible_help.popup.findChild(QLabel).text() == visible_help.toolTip()
        visible_help.popup.grab().save(str(destination / "hover-help.png"))
        QCursor.setPos(window.mapToGlobal(window.rect().topLeft()))
        pump_until(lambda: not visible_help.popup.isVisible(), 3)
        visible_help.setFocus(Qt.FocusReason.TabFocusReason)
        pump_until(lambda: visible_help.popup.isVisible(), 3)
        visible_help.clearFocus()
        pump_until(lambda: not visible_help.popup.isVisible(), 3)
        check("actual-mouse-hover-help-focus-and-leave")
        from PySide6.QtWidgets import QTabWidget
        settings = window.pages[6]
        settings.findChildren(QTabWidget)[0].setCurrentIndex(2)
        settings.fields["xiaomi"]["api_key"].setText("native-smoke-test-only-key")
        action(settings, "验证格式")
        idle(window)
        assert "格式通过" in window.status.text()
        assert not runtime.call("/api/settings/providers")["providers"][0]["api_key"]["configured"]
        action(settings, "保存")
        idle(window)
        persisted = (destination / "data/provider_settings.json").read_text(encoding="utf-8")
        assert "native-smoke-test-only-key" not in persisted
        assert "windows-dpapi-user-v1" in persisted
        assert not settings.fields["xiaomi"]["api_key"].text()
        assert len(runtime.call("/api/voices", params={"provider": "xiaomi"})) == 9
        assert "已获取 9" in settings.state["xiaomi"].text()
        action(settings, "验证格式")
        idle(window)
        assert "格式通过" in window.status.text()
        check("native-provider-form-save-dpapi-secret-not-returned")
        check("unsaved-form-validation-retains-saved-secret-and-auto-catalog")
        new_directory = destination / "自定义 数据目录"
        settings.data_directory.setText(str(new_directory))
        action(settings, "保存目录")
        idle(window)
        assert window.preferences.data_directory() == new_directory.resolve()
        assert runtime.data_directory != new_directory.resolve()
        assert not (new_directory / "tts_dataset.sqlite3").exists()
        check("custom-data-directory-saved-live-dataset-unchanged")
        window.navigate(0)
        generation = window.pages[0]
        generation.provider.setCurrentIndex(generation.provider.findData("volcengine"))
        generation.model.setCurrentIndex(generation.model.findData("seed-tts-2.0"))
        action(generation, "获取音色列表")
        idle(window)
        assert generation.voices.count() >= 2
        assert all("豆包 2.0" in generation.voices.item(i).text() for i in range(generation.voices.count()))
        check("visible-catalog-button-and-doubao2-model-filter")
        generation.provider.setCurrentIndex(generation.provider.findData("mock"))
        generation.refresh_catalog()
        idle(window)
        assert generation.voices.count() >= 2
        generation.voices.item(0).setCheckState(Qt.CheckState.Checked)
        generation.voices.item(1).setCheckState(Qt.CheckState.Checked)
        generation.texts.setPlainText("你好，小助手\n请开灯")
        generation.count.setValue(2)
        heartbeat = []
        timer = QTimer()
        timer.setInterval(10)
        timer.timeout.connect(lambda: heartbeat.append(time.monotonic()))
        timer.start()
        action(generation, "开始合成")
        idle(window)
        tasks = runtime.call("/api/tasks")
        task = wait_task(runtime, tasks[0]["id"])
        assert len(task["children"]) == 2
        samples = runtime.call("/api/samples", params={"page_size": 200})["items"]
        assert len(samples) == 8
        timer.stop()
        assert heartbeat, "UI event loop did not run during generation"
        check("native-batch-action-two-voices-two-texts-eight-wavs-ui-responsive")
        for row in samples:
            path = destination / (row["id"] + ".wav")
            runtime.call(op="copy", kind="samples", id=row["id"], destination=str(path))
            with wave.open(str(path)) as audio:
                assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (16000, 1, 2)
        check("actual-wav-pcm16-mono-16khz")
        window.navigate(1)
        idle(window)
        library = window.pages[1]
        library.grid.setCurrentCell(0, 1)
        identifier = library.rows[0]["id"]
        action(library, "切换审核")
        idle(window)
        assert runtime.call("/api/samples", params={"quality": "rejected"})["total"] == 1
        assert runtime.call("/api/samples", params={"quality": "accepted"})["total"] == 7
        action(library, "音频分析")
        idle(window)
        assert library.waveform.values and library.spectrum.values
        action(library, "试听")
        idle(window)
        first_playback = window.player.source().toLocalFile()
        assert Path(first_playback).is_file()
        action(library, "试听")
        idle(window)
        assert window.player.source().toLocalFile() != first_playback
        assert Path(window.player.source().toLocalFile()).is_file()
        library.selected.update(row["id"] for row in samples)
        library.load_page(2)
        idle(window)
        assert len(library.selected) == 8
        library.load_page(1)
        idle(window)
        check("native-review-analysis-playback-source-cross-page-selection")
        window.grab().save(str(destination / "review.png"))
        # Import a deterministic local recording through the same native action.
        tone = destination / "真人 测试.wav"
        with wave.open(str(tone), "wb") as audio:
            audio.setframerate(48000)
            audio.setnchannels(1)
            audio.setsampwidth(2)
            import struct
            audio.writeframes(b"".join(struct.pack("<h", int(4000 * math.sin(2 * math.pi * 330 * i / 48000))) for i in range(48000)))
        window.navigate(2)
        idle(window)
        recording = window.pages[2]
        if not recording.devices:
            recording.start()
            assert result["errors"] and "麦克风" in result["errors"][-1]
            result["errors"].clear()
            check("missing-microphone-reported-without-fake-recording")
        recording.speaker.setEditText("smoke-person")
        recording.paths = [str(tone)]
        recording.files.addItem(tone.name)
        action(recording, "导入并校验")
        idle(window)
        assert runtime.call("/api/samples", params={"provider": "real"})["total"] == 1
        assert recording.speaker.currentText() == "smoke-person" and not recording.paths
        recording.paths = [str(tone)]
        action(recording, "导入并校验")
        idle(window)
        assert "重复 1" in recording.result.text()
        check("native-import-48khz-continuous-parameters-deduplication")
        noise = runtime.call("/api/noises/upload", "POST", files=[("file", str(tone))],
                             data={"name": "smoke-noise", "min_snr": 5, "max_snr": 15})
        window.navigate(3)
        idle(window)
        noises = window.pages[3]
        noises.selected.add(noise["id"])
        action(noises, "增强所选样本")
        idle(window)
        task = wait_task(runtime, runtime.call("/api/tasks")[0]["id"])
        augmented = runtime.call("/api/samples", params={"sample_type": "augmented", "page_size": 200})["items"]
        assert len(augmented) == 8 and all(row["quality_status"] == "pending" for row in augmented)
        check("native-augmentation-eight-pending-samples")
        for format in ("generic", "jsonl", "folders", "wekws"):
            window.navigate(5)
            idle(window)
            exports = window.pages[5]
            exports.format.setCurrentIndex(exports.format.findData(format))
            exports.name.setText("smoke-" + format)
            action(exports, "创建导出")
            idle(window)
            wait_task(runtime, runtime.call("/api/tasks")[0]["id"])
            export = next(row for row in runtime.call("/api/exports") if row["name"] == "smoke-" + format)
            archive = destination / (format + ".zip")
            runtime.call(op="copy", kind="exports", id=export["id"], destination=str(archive))
            with zipfile.ZipFile(archive) as zip_file:
                names = zip_file.namelist()
                assert "SHA256SUMS" in names
                assert any(name.endswith(".wav") for name in names)
                assert any(name.endswith(".jsonl") for name in names)
                if format == "wekws":
                    assert any(name.endswith("wav.dur") for name in names)
                    assert any(name.endswith("words.txt") for name in names)
                groups = {}
                for name in names:
                    if name.endswith(".jsonl"):
                        split = Path(name).stem
                        for line in zip_file.read(name).decode("utf-8").splitlines():
                            value = json.loads(line)
                            group = (value.get("provider"), value.get("voice_id"), value.get("model"))
                            groups.setdefault(group, set()).add(split)
                assert all(len(splits) == 1 for splits in groups.values()), groups
            runtime.call(f"/api/exports/{export['id']}", "DELETE")
            runtime.call(f"/api/exports/{export['id']}/restore", "POST")
        check("four-real-export-zips-contract-group-isolation-soft-delete-restore")
        window.navigate(5)
        idle(window)
        window.grab().save(str(destination / "exports.png"))
        runtime.call(f"/api/samples/{identifier}", "DELETE")
        runtime.call(f"/api/samples/{identifier}/restore", "POST")
        runtime.call("/api/settings/project", "PUT", json={"display_text": "你好，小助手"})
        check("sample-delete-restore-project-settings-persist")
        window.resize(1060, 720)
        window.navigate(0)
        idle(window)
        window.grab().save(str(destination / "compact.png"))
        window.close()
        pump_until(lambda: not runtime.process.is_alive())
        check("native-close-no-business-child-left")
        imported = copy_dataset(destination / "data", destination / "导入 数据")
        runtime2, window2 = start(imported)
        assert runtime2.call("/api/samples")["total"] == 17
        assert runtime2.call("/api/exports")
        window2.close()
        check("copied-dataset-rebased-paths-second-cold-start-and-close")
        from PySide6.QtMultimedia import QMediaDevices
        result["audio_inputs"] = [device.description() for device in QMediaDevices.audioInputs()]
        result["audio_outputs"] = [device.description() for device in QMediaDevices.audioOutputs()]
        result["hardware_recording_verified"] = False
        result["real_cloud_verified"] = False
        result["ok"] = True
    except Exception as exc:
        result["ok"] = False
        result["failure"] = repr(exc)
        import traceback
        result["traceback"] = traceback.format_exc()
    finally:
        for window in windows:
            if not window.closing:
                window.close()
        (destination / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        faulthandler.cancel_dump_traceback_later()
        trace.close()
    return 0 if result["ok"] else 1
