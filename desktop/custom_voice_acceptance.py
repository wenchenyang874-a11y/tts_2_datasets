"""Exercise custom voice dialogs through an isolated Mock runtime."""
import json
import time
import wave
from PySide6.QtCore import Qt, QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QPushButton
from .custom_voices import CreationDialog


def verify_custom_voices(application, window, destination, idle):
    page = window.pages[7]
    window.navigate(7)
    idle(window)
    # This harness explicitly polls the backend. Disable the page's periodic
    # poll so its older queued snapshot cannot race our assertion-only reads.
    page.timer.stop()
    def wait(predicate):
        deadline = time.monotonic()+20
        while not predicate():
            assert time.monotonic() < deadline, "custom voice acceptance timed out"
            application.processEvents()
            time.sleep(.02)
        application.processEvents()

    # Check each real provider's actual form, without submitting a cloud call.
    for mode in ("clone", "design"):
        dialog = CreationDialog(page, mode)
        dialog.show()
        application.processEvents()
        for service in ("volcengine_v1", "volcengine_v2", "xiaomi", "aliyun", "mock"):
            dialog.service.setCurrentIndex(dialog.service.findData(service))
            application.processEvents()
            assert dialog.speaker_field.isVisible() == service.startswith("volcengine")
            assert dialog.model_field.isVisible() == (service == "aliyun")
            assert dialog.url_field.isVisible() == (mode == "clone" and service == "aliyun")
            assert dialog.reference_field.isVisible() == (mode == "clone" and service != "aliyun")
            assert dialog.prompt_field.isVisible() == (mode == "design")
        dialog.service.setCurrentIndex(dialog.service.findData("mock"))
        dialog.name.setText("离线"+("复刻" if mode == "clone" else "设计")+"验收")
        if mode == "clone":
            reference = destination / "custom-reference.wav"
            with wave.open(str(reference), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(24000)
                wav.writeframes(b"\x01\x00\xff\xff"*48000)
            dialog.path = reference
            dialog.file_label.setText(reference.name)
        else:
            dialog.prompt.setPlainText("温暖清晰的青年声音")
        application.processEvents()
        window.screen().grabWindow(dialog.winId()).save(str(destination / f"custom-{mode}-dialog.png"))
        QTest.mouseClick(dialog.create_button, Qt.MouseButton.LeftButton)
        wait(lambda: not dialog.isVisible())
        idle(window)
        dialog.deleteLater()
        def completed():
            rows = window.runtime.call("/api/custom-voices")
            page.loaded(rows)
            return rows and all(not row["busy"] and row["status"] == "ready" and row["has_preview"] for row in rows)
        wait(completed)
        assert all(row["status"] == "ready" and row["has_preview"] for row in page.rows.values())
    page.table.setCurrentCell(0, 0)
    current = page.current()
    assert current and current["voice_db_id"]
    application.processEvents()
    time.sleep(.08)
    application.processEvents()
    for index in range(page.table.rowCount()):
        control = page.table.cellWidget(index, 5).findChildren(QPushButton)[0]
        assert control.visibleRegion().contains(control.rect()), "custom preview button clipped"
    play = page.play_buttons[current["id"]]
    QTest.mouseClick(play, Qt.MouseButton.LeftButton, pos=play.rect().bottomRight()-QPoint(5, 5))
    idle(window)
    wait(lambda: window.playback_resource == ("custom_voices", current["id"]))
    assert play.toolTip() == "停止播放"
    QTest.mouseClick(play, Qt.MouseButton.LeftButton)
    application.processEvents()
    assert window.playback_resource is None
    window.screen().grabWindow(window.winId()).save(str(destination / "custom-voices-ready.png"))
    window.runtime.call(f"/api/custom-voices/{current['id']}", "PATCH", json={"name": "自定义音色验收", "tags": "温暖,清晰", "enabled": True})
    page.refresh()
    idle(window)
    page.table.setCurrentCell(0, 0)
    page.use()
    idle(window)
    assert current["voice_db_id"] in window.pages[0].selected
    row = window.pages[0].voice_rows[current["voice_db_id"]]
    assert row["name"] == "自定义音色验收" and row["source"] == "managed_custom"
    application.processEvents()
    wait(lambda: any(window.pages[0].chosen_voices.item(index).data(Qt.ItemDataRole.UserRole) == current["voice_db_id"] for index in range(window.pages[0].chosen_voices.count())))
    window.screen().grabWindow(window.winId()).save(str(destination / "custom-voice-in-composer.png"))
    # Use the confined playback route, not raw disk filenames or vendor URLs.
    preview = destination / "custom-preview-copy.wav"
    window.runtime.call(op="copy", kind="custom_voices", id=current["id"], destination=str(preview))
    with wave.open(str(preview), "rb") as wav:
        assert wav.getframerate() == 24000 and wav.getnchannels() == 1
    window.pages[0].selected.discard(current["voice_db_id"])
    window.pages[0].update_summary()
    window.navigate(7)
    idle(window)
    window.runtime.call(f"/api/custom-voices/{current['id']}", "DELETE")
    page.archived.setChecked(True)
    idle(window)
    selected_row = next(i for i in range(page.table.rowCount()) if page.table.item(i, 0).data(Qt.ItemDataRole.UserRole) == current["id"])
    page.table.setCurrentCell(selected_row, 0)
    application.processEvents()
    assert page.voice_actions[3].text() == "恢复音色"
    QTest.mouseClick(page.voice_actions[3], Qt.MouseButton.LeftButton)
    idle(window)
    assert page.rows[current["id"]]["status"] == "ready"
    page.archived.setChecked(False)
    idle(window)
    page.timer.start()
    (destination / "custom-voices-evidence.json").write_text(json.dumps({"ok": True, "created": 2, "real_cloud_calls": 0,
        "checks": ["provider-specific-dialog-fields", "mock-clone-and-design-create", "preview-button-unclipped-bottom-hit-play-and-stop", "ready-preview-playback-copy", "edit-name-tags", "select-custom-voice-in-composer", "show-removed-and-restore-without-new-cloud-call"]}, indent=2), encoding="utf-8")
