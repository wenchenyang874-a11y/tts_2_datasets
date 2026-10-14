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


def verify_sample_row_first_paint(application, destination):
    """Inspect incremental paint events, not a grab() that forces a full render."""
    from PySide6.QtCore import Qt, QObject, QEvent
    from PySide6.QtGui import QRegion
    from PySide6.QtWidgets import QTableWidgetItem
    from PySide6.QtTest import QTest
    from .sample_table import SampleTable
    from .widgets import STYLE
    paints = []

    class PaintTracker(QObject):
        def eventFilter(self, watched, event):
            if event.type() == QEvent.Type.Paint:
                paints.append(QRegion(event.region()))
            return False

    table = SampleTable()
    table.set_multi_select(True)
    table.setStyleSheet(STYLE)
    table.setWindowTitle("样本即时重绘验收")
    table.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
    table.resize(850, 240)
    table.move(application.primaryScreen().availableGeometry().topLeft())
    table.setRowCount(2)
    for row in range(2):
        for column, text in enumerate(("样本即时重绘", "离线测试 / Mock", "原音", "已通过", "1.0s", "未分配", "")):
            item = QTableWidgetItem(text)
            table.setItem(row, column, item)
            if column == 0:
                item.setData(Qt.ItemDataRole.UserRole, str(row))
                item.setCheckState(Qt.CheckState.Unchecked)
    evidence = []
    try:
        table.show()
        table.setFocus()
        table.setCurrentCell(0, 0)
        # Settle initial show/focus/layout paints before observing the click.
        application.processEvents()
        application.processEvents()
        tracker = PaintTracker(table)
        table.viewport().installEventFilter(tracker)
        centers = [table.visualItemRect(table.item(0, column)).center() for column in range(table.columnCount())]
        other_row = table.visualItemRect(table.item(1, 0)).center()
        click = lambda: QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=centers[0])
        actions = [("mouse-check", click, True), ("mouse-uncheck", click, False),
                   ("space-check", lambda: QTest.keyClick(table, Qt.Key.Key_Space), True),
                   ("space-uncheck", lambda: QTest.keyClick(table, Qt.Key.Key_Space), False),
                   ("double-click-check", lambda: QTest.mouseDClick(table.viewport(), Qt.MouseButton.LeftButton, pos=centers[0]), True),
                   ("programmatic-uncheck", lambda: table.item(0, 0).setCheckState(Qt.CheckState.Unchecked), False)]
        for name, action, checked in actions:
            paints.clear()
            action()
            application.processEvents()
            assert (table.item(0, 0).checkState() == Qt.CheckState.Checked) == checked
            assert paints and all(paints[0].contains(point) for point in centers), name
            assert not paints[0].contains(other_row), name
            rect = paints[0].boundingRect()
            evidence.append({"action": name, "checked": checked, "first_paint_all_columns": True,
                             "unrelated_row_repainted": False, "region": [rect.x(), rect.y(), rect.width(), rect.height()]})
            # QScreen captures the presented window, unlike QWidget.grab(),
            # which would repaint stale cells and falsely pass this regression.
            if name == "mouse-check":
                table.screen().grabWindow(table.winId()).save(str(destination / "sample-first-frame.png"))
        (destination / "sample-first-paint.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    finally:
        table.close()


def run_smoke(application, destination: Path) -> int:
    from PySide6.QtCore import Qt, QTimer, QPoint
    from PySide6.QtWidgets import QPushButton, QWidget
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
        # Shared machines may receive unrelated physical pointer movement.
        # Retry only a short bounded positioning attempt, and require an actual
        # hit at the moment the popup appears; never inject a Help event.
        hover_passed = False
        for attempt in range(4):
            QCursor.setPos(window.mapToGlobal(window.rect().topLeft()))
            application.processEvents()
            QTest.mouseMove(visible_help, visible_help.rect().center())
            QCursor.setPos(visible_help.mapToGlobal(visible_help.rect().center()))
            application.processEvents()
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                application.processEvents()
                if application.widgetAt(QCursor.pos()) is not visible_help:
                    break
                if visible_help.popup is not None and visible_help.popup.isVisible():
                    hover_passed = True
                    break
                time.sleep(.02)
            if hover_passed:
                break
        assert hover_passed and application.widgetAt(QCursor.pos()) is visible_help, "Real hover unavailable: pointer moved away or popup did not appear"
        assert visible_help.popup.findChild(QLabel).text() == visible_help.toolTip()
        visible_help.popup.grab().save(str(destination / "hover-help.png"))
        QCursor.setPos(window.mapToGlobal(window.rect().topLeft()))
        pump_until(lambda: not visible_help.popup.isVisible(), 3)
        visible_help.setFocus(Qt.FocusReason.TabFocusReason)
        pump_until(lambda: visible_help.popup.isVisible(), 3)
        visible_help.clearFocus()
        pump_until(lambda: not visible_help.popup.isVisible(), 3)
        check("actual-mouse-hover-help-focus-and-leave")
        from .control_acceptance import verify_controls
        verify_controls(application, window, destination, idle)
        check("mouse-tab-switch-silent-help-full-filter-labels-all-pages-single-show-popup-first-frames")
        from PySide6.QtWidgets import QTabWidget
        # The shared-control probe visits every page and restores the composer.
        # Explicitly enter settings before continuing its visible-button checks.
        window.navigate(6)
        idle(window)
        settings = window.pages[6]
        settings.findChildren(QTabWidget)[0].setCurrentIndex(2)
        settings.fields["xiaomi"]["api_key"].setText("native-smoke-test-only-key")
        action(settings, "检查配置")
        idle(window)
        assert "格式通过" in window.status.text()
        assert not runtime.call("/api/settings/providers")["providers"][0]["api_key"]["configured"]
        action(settings, "保存配置")
        idle(window)
        persisted = (destination / "data/provider_settings.json").read_text(encoding="utf-8")
        assert "native-smoke-test-only-key" not in persisted
        assert "windows-dpapi-user-v1" in persisted
        assert not settings.fields["xiaomi"]["api_key"].text()
        assert len(runtime.call("/api/voices", params={"provider": "xiaomi"})) == 9
        assert "已获取 9" in settings.state["xiaomi"].text()
        action(settings, "检查配置")
        idle(window)
        assert "格式通过" in window.status.text()
        check("native-provider-form-save-dpapi-secret-not-returned")
        check("unsaved-form-validation-retains-saved-secret-and-auto-catalog")
        old_directory = runtime.data_directory
        old_process = runtime.process
        new_directory = destination / "自定义 数据目录"
        new_directory.mkdir()
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        original_picker = QFileDialog.getExistingDirectory
        QFileDialog.getExistingDirectory = staticmethod(lambda *args: str(new_directory))
        def choose_empty_mode():
            dialog = application.activeModalWidget()
            assert isinstance(dialog, QMessageBox)
            chosen = next(item for item in dialog.buttons() if item.text() == "使用空目录")
            QTest.mouseClick(chosen, Qt.MouseButton.LeftButton)
        try:
            QTimer.singleShot(250, choose_empty_mode)
            action(settings, "更换目录")
        finally:
            QFileDialog.getExistingDirectory = original_picker
        idle(window)
        runtime = window.runtime
        assert window.preferences.data_directory() == new_directory.resolve()
        assert runtime.data_directory == new_directory.resolve()
        assert not old_process.is_alive() and window.isVisible()
        assert (new_directory / "tts_dataset.sqlite3").exists()
        assert (old_directory / "tts_dataset.sqlite3").exists()
        assert not any(item.text() == "保存目录" for item in window.findChildren(QPushButton))
        assert window.pages[6].location.text() == str(new_directory.resolve())
        check("directory-auto-saved-and-live-switched-without-window-restart")
        window.pages[6].use_directory(old_directory, "existing")
        idle(window)
        runtime = window.runtime
        assert runtime.call("/api/settings/providers")["providers"]
        window.navigate(0)
        generation = window.pages[0]
        idle(window)
        # Open the real shared Qt popup at different current selections. Its
        # anchor must remain fixed rather than centering the selected menu row.
        popup_positions = []
        for selection in (0, 3, generation.age.count() - 1):
            generation.age.setCurrentIndex(selection)
            idle(window)
            window.stack.currentWidget().ensureWidgetVisible(generation.age)
            application.processEvents()
            QTest.mouseClick(generation.age, Qt.MouseButton.LeftButton, pos=QPoint(generation.age.width() - 14, generation.age.height() // 2))
            application.processEvents()
            popup = generation.age.view().window()
            assert popup.isVisible(), {"selection": selection, "age_visible": generation.age.isVisible(), "enabled": generation.age.isEnabled(), "size": (generation.age.width(), generation.age.height()), "popup": (popup.x(), popup.y()), "active": window.isActiveWindow()}
            popup_positions.append((popup.x(), popup.y(), popup.width()))
            assert popup.width() == generation.age.width()
            popup.grab().save(str(destination / f"dropdown-{selection}.png"))
            QTest.keyClick(generation.age.view(), Qt.Key.Key_Escape)
            application.processEvents()
        assert len(set(popup_positions)) == 1, popup_positions
        generation.age.setCurrentIndex(0)
        idle(window)
        QTest.mouseClick(generation.age, Qt.MouseButton.LeftButton)
        view = generation.age.view()
        row_index = view.model().index(1, 0)
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view.visualRect(row_index).center())
        assert generation.age.currentData() == "young" and not view.window().isVisible()
        generation.age.setCurrentIndex(0)
        idle(window)
        QTest.mouseClick(generation.age, Qt.MouseButton.LeftButton)
        QTest.keyClick(view, Qt.Key.Key_Down)
        QTest.keyClick(view, Qt.Key.Key_Return)
        assert generation.age.currentData() == "young"
        generation.age.setCurrentIndex(0)
        idle(window)
        check("dropdown-stable-anchor-width-first-middle-last-and-keyboard-close")
        generation.provider.setCurrentIndex(generation.provider.findData("xiaomi"))
        idle(window)
        generation.language.setCurrentIndex(generation.language.findData("foreign"))
        idle(window)
        assert generation.voices.count() == 4
        item = generation.voices.item(0)
        bounds = generation.voices.visualItemRect(item)
        # Clicking the name/blank side and the actual checkbox each toggles
        # once. The same row rectangle survives every visual selection state.
        from PySide6.QtCore import QPoint
        for x in (bounds.center().x(), bounds.left() + 18, bounds.right() - 12):
            before = item.checkState()
            QTest.mouseClick(generation.voices.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(x, bounds.center().y()))
            assert item.checkState() != before
            assert generation.voices.visualItemRect(item) == bounds
        assert item.text() == generation.voice_rows[item.data(Qt.ItemDataRole.UserRole)]["name"]
        assert item.data(Qt.ItemDataRole.UserRole + 1)["language"] == "外语"
        QTest.keyClick(generation.voices, Qt.Key.Key_Space)
        assert item.checkState() == Qt.CheckState.Unchecked
        check("formatted-voice-rows-whole-row-checkbox-keyboard-toggle-no-inset-change")
        generation.voices.item(0).setCheckState(Qt.CheckState.Checked)
        chosen_foreign = set(generation.selected)
        generation.instruction.setText("轻快自然，强调关键词")
        generation.emotion.setCurrentIndex(generation.emotion.findData("happy"))
        assert generation.payload()["emotion"] == "happy"
        assert generation.instruction.isEnabled() and not generation.scale_field.isVisible()
        generation.ensureWidgetVisible(generation.instruction)
        window.grab().save(str(destination / "voice-foreign-expression.png"))
        generation.language.setCurrentIndex(generation.language.findData("chinese"))
        idle(window)
        assert generation.voices.count() == 4 and generation.selected == chosen_foreign
        assert generation.chosen_voices.count() == len(chosen_foreign)
        assert generation.instruction.text() == "轻快自然，强调关键词"
        generation.language.setCurrentIndex(0)
        generation.provider.setCurrentIndex(generation.provider.findData("aliyun"))
        generation.capability_filter.setCurrentIndex(generation.capability_filter.findData("instruction"))
        idle(window)
        assert generation.voices.count() == 5
        generation.capability_filter.setCurrentIndex(0)
        idle(window)
        check("voice-language-capability-filters-and-visible-emotion-controls")
        generation.clear_voices()
        generation.provider.setCurrentIndex(generation.provider.findData("volcengine_v1"))
        idle(window)
        generation.voices.item(0).setCheckState(Qt.CheckState.Checked)
        generation.voices.item(1).setCheckState(Qt.CheckState.Checked)
        selected_before = set(generation.selected)
        tasks_before = len(runtime.call("/api/tasks"))
        modal_text = []
        def dismiss_expression_warning():
            dialog = application.activeModalWidget()
            if isinstance(dialog, QMessageBox):
                modal_text.append(dialog.text())
                dialog.grab().save(str(destination / "expression-warning.png"))
                dialog.accept()
        QTimer.singleShot(200, dismiss_expression_warning)
        action(generation, "开始合成")
        idle(window)
        assert modal_text and "不支持表达指令" in modal_text[0]
        assert generation.voices.item(0).text() in modal_text[0]
        assert generation.voices.item(1).text() in modal_text[0]
        assert generation.selected == selected_before and generation.instruction.text() == "轻快自然，强调关键词"
        assert len(runtime.call("/api/tasks")) == tasks_before and not generation.submitting
        generation.instruction.clear()
        generation.clear_voices()
        check("start-expression-modal-preserves-form-before-submission")
        original_size = window.size()
        window.resize(980, 680)
        pump_until(lambda: generation.width() <= window.stack.currentWidget().viewport().width(), 3)
        window.grab().save(str(destination / "generation-minimum-window.png"))
        for control in (generation.provider, generation.age, generation.capability_filter, generation.voices):
            point = control.mapTo(window, control.rect().topRight())
            assert point.x() < window.width() - 10, (point, window.width(), generation.width(), generation.minimumSizeHint(), generation.body_layout.minimumSize())
        from PySide6.QtWidgets import QScrollArea
        assert window.stack.currentWidget() is generation and not generation.findChildren(QScrollArea)
        for index in range(generation.editor_tabs.count()):
            generation.editor_tabs.setCurrentIndex(index)
            application.processEvents()
            assert generation.start_button.isVisible()
            assert generation.rect().contains(generation.start_button.mapTo(generation, generation.start_button.rect().bottomRight()))
            assert all(not generation.editor_tabs.widget(other).isVisible() for other in range(4) if other != index)
            window.grab().save(str(destination / f"composer-minimum-tab-{index}.png"))
        generation.editor_tabs.setCurrentIndex(0)
        window.grab().save(str(destination / "generation-minimum-window.png"))
        window.resize(original_size)
        pump_until(lambda: generation.width() <= window.stack.currentWidget().viewport().width(), 3)
        check("minimum-window-width-voice-filters-without-horizontal-clipping")
        generation.provider.setCurrentIndex(generation.provider.findData("volcengine_v2"))
        action(generation, "获取音色")
        idle(window)
        assert generation.voices.count() >= 2
        assert all(generation.voice_rows[generation.voices.item(i).data(Qt.ItemDataRole.UserRole)]["model"] == "seed-tts-2.0" for i in range(generation.voices.count()))
        generation.voices.item(0).setCheckState(Qt.CheckState.Checked)
        before_count = len(runtime.call("/api/tasks"))
        action(generation, "开始合成")
        idle(window)
        assert "API Key" in generation.problem.text()
        assert len(runtime.call("/api/tasks")) == before_count
        action(generation, "去配置")
        idle(window)
        assert window.stack.currentIndex() == 6 and window.pages[6].tabs.currentIndex() == 1
        assert "access_token" not in window.pages[6].fields["volcengine_v2"]
        assert "api_key" in window.pages[6].fields["volcengine_v1"]
        for service, controls in window.pages[6].fields.items():
            assert not set(controls) & {"endpoint", "v3_endpoint", "catalog_endpoint", "customization_endpoint", "access_token", "cluster"}
        window.grab().save(str(destination / "doubao2-settings.png"))
        check("separate-doubao-settings-and-missing-v2-key-blocks-before-task-creation")
        settings = window.pages[6]
        settings.select_service("aliyun")
        region = settings.fields["aliyun"]["region"]
        assert [region.itemData(i) for i in range(region.count())] == ["cn-beijing", "ap-southeast-1"]
        region.setCurrentIndex(region.findData("ap-southeast-1"))
        settings.fields["aliyun"]["api_key"].setText("native-region-fixture")
        action(settings, "检查配置")
        idle(window)
        assert "配置格式通过" in settings.state["aliyun"].text()
        assert settings.form_payload("aliyun")["region"] == "ap-southeast-1"
        # Save only via the internal config API: UI save intentionally fetches
        # the real account catalog, which an offline acceptance must not call.
        runtime.call("/api/settings/providers/aliyun", "PUT", json={"api_key": "native-region-fixture", "region": "ap-southeast-1", "workspace_id": "ws-native-fixture"})
        settings.dirty.discard("aliyun")
        settings.refresh()
        idle(window)
        assert region.currentData() == "ap-southeast-1"
        window.grab().save(str(destination / "region-settings.png"))
        runtime.call("/api/settings/providers/aliyun", "DELETE")
        check("native-region-dropdown-validation-and-persistence-no-cloud")
        window.navigate(0)
        idle(window)
        generation.provider.setCurrentIndex(generation.provider.findData("mock"))
        generation.clear_voices()
        generation.refresh_catalog()
        idle(window)
        assert not generation.selected
        assert generation.voices.count() >= 2
        generation.voices.item(0).setCheckState(Qt.CheckState.Checked)
        generation.voices.item(1).setCheckState(Qt.CheckState.Checked)
        mock_selection = set(generation.selected)
        generation.provider.setCurrentIndex(generation.provider.findData("xiaomi"))
        idle(window)
        generation.voices.item(0).setCheckState(Qt.CheckState.Checked)
        idle(window)
        assert len(generation.selected) == 3 and generation.chosen_voices.count() == 3
        assert set(generation.payload()["provider_audio"]) == {"mock", "xiaomi"}
        window.grab().save(str(destination / "composer-cross-supplier-basket.png"))
        cross_identifier = next(iter(generation.selected-mock_selection))
        basket_item = next(generation.chosen_voices.item(i) for i in range(generation.chosen_voices.count())
                           if generation.chosen_voices.item(i).data(Qt.ItemDataRole.UserRole) == cross_identifier)
        basket_rect = generation.chosen_voices.visualItemRect(basket_item)
        QTest.mouseClick(generation.chosen_voices.viewport(), Qt.MouseButton.LeftButton,
                         pos=QPoint(basket_rect.right()-20, basket_rect.top()+21))
        idle(window)
        assert cross_identifier not in generation.selected and generation.chosen_voices.count() == 2
        generation.provider.setCurrentIndex(generation.provider.findData("mock"))
        idle(window)
        assert generation.selected == mock_selection
        check("cross-supplier-basket-keeps-filtered-selections-and-separate-audio-profiles")
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
        assert window.stack.currentIndex() == 0 and generation.selected == mock_selection
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
        assert not library.grid.multi_select
        # Re-run sizing after stylesheet polish, just as window/column changes
        # do. Initial cell-widget sizing hid the beta.6 button clipping defect.
        library.grid.resizeRowsToContents()
        application.processEvents()
        control = next(iter(library.play_buttons.values()))
        assert control.visibleRegion().boundingRect() == control.rect()
        bottom = QPoint(control.width() // 2, control.height()-4)
        assert library.grid.viewport().childAt(control.mapTo(library.grid.viewport(), bottom)) is control
        original_size = window.size()
        for width in (980, original_size.width()):
            window.resize(width, original_size.height())
            application.processEvents()
            library.toolbar.reflow()
            application.processEvents()
            for item in library.toolbar.controls:
                if item.isVisible():
                    assert library.toolbar.rect().contains(item.geometry())
            assert control.visibleRegion().boundingRect() == control.rect()
        check("sample-play-control-full-visible-region-and-bottom-hit-after-resize")
        for row in (0, 1):
            QTest.mouseClick(library.grid.viewport(), Qt.MouseButton.LeftButton,
                             pos=library.grid.visualItemRect(library.grid.item(row, 0)).center())
            assert library.grid.currentRow() == row and not library.selected
            idle(window)
            identifier = library.rows[row]['id']
            expected = runtime.call(f"/api/samples/{identifier}/analysis")
            assert library.analysis_loaded_identifier == identifier
            assert library.waveform.values == expected['waveform']
            assert library.waveform.x_values == expected['waveform_time']
            assert min(library.waveform.values) < 0 < max(library.waveform.values)
            assert library.spectrum.values == expected['spectrum']['magnitude_db']
        for row in (0, 1, 0):
            QTest.mouseClick(library.grid.viewport(), Qt.MouseButton.LeftButton,
                             pos=library.grid.visualItemRect(library.grid.item(row, 0)).center())
        idle(window)
        assert library.analysis_loaded_identifier == library.rows[0]['id']
        window.stack.currentWidget().ensureWidgetVisible(library.spectrum)
        application.processEvents()
        window.screen().grabWindow(window.winId()).save(str(destination / "sample-auto-analysis.png"))
        window.stack.currentWidget().ensureWidgetVisible(library.grid)
        application.processEvents()
        check("sample-single-click-auto-waveform-spectrum-latest-selection-wins")
        window.grab().save(str(destination / "sample-single-selection.png"))
        check("sample-default-single-current-row-no-hidden-checkbox-selections")
        action(library, "多选")
        assert library.grid.multi_select
        library.toolbar.reflow()
        application.processEvents()
        assert control.visibleRegion().boundingRect() == control.rect()
        for item in library.toolbar.controls:
            if item.isVisible():
                assert library.toolbar.rect().contains(item.geometry())
        verify_sample_row_first_paint(application, destination)
        check("sample-check-uncheck-all-columns-first-paint-no-forced-render")
        window.stack.currentWidget().ensureWidgetVisible(library.grid)
        application.processEvents()
        from .sample_table import SampleDelegate
        assert library.grid.columnCount() == 7 and library.grid.horizontalHeaderItem(0).text() == "文本"
        first_item = library.grid.item(0, 0)
        first_id = first_item.data(Qt.ItemDataRole.UserRole)
        first_rect = library.grid.visualItemRect(first_item)
        indicator = SampleDelegate.checkbox_rect(first_rect)
        # Hit the actual painted checkbox's edges and center, the adjacent
        # gap, text, other information columns and their empty side. Every
        # target is the same row selection, with no native second hit rectangle.
        points = [indicator.topLeft() + QPoint(1, 1), indicator.center(), indicator.bottomRight() - QPoint(1, 1),
                  QPoint(indicator.right() + 5, indicator.center().y()), first_rect.center(),
                  library.grid.visualItemRect(library.grid.item(0, 1)).center(),
                  library.grid.visualItemRect(library.grid.item(0, 5)).bottomRight() - QPoint(5, 5)]
        for point in points:
            before = first_item.checkState()
            QTest.mouseClick(library.grid.viewport(), Qt.MouseButton.LeftButton, pos=point)
            assert first_item.checkState() != before
            assert (first_id in library.selected) == (first_item.checkState() == Qt.CheckState.Checked)
            assert library.grid.visualItemRect(first_item) == first_rect
        first_item.setCheckState(Qt.CheckState.Checked)
        application.processEvents()
        rendered = library.grid.viewport().grab().toImage()
        ratio = rendered.devicePixelRatio()
        pixel = indicator.topLeft() + QPoint(4, 3)
        assert rendered.pixelColor(int(pixel.x() * ratio), int(pixel.y() * ratio)).name() == "#6563df"
        library.grid.setFocus()
        QTest.keyClick(library.grid, Qt.Key.Key_Space)
        assert first_id not in library.selected
        # Press on one row and release on another/empty space is not a click.
        QTest.mousePress(library.grid.viewport(), Qt.MouseButton.LeftButton, pos=first_rect.center())
        QTest.mouseRelease(library.grid.viewport(), Qt.MouseButton.LeftButton, pos=library.grid.visualItemRect(library.grid.item(1, 0)).center())
        assert first_id not in library.selected
        original_rows = list(library.rows)
        selection_before = set(library.selected)
        QTest.mousePress(library.grid.viewport(), Qt.MouseButton.LeftButton, pos=first_rect.center())
        library.show_rows({"items": [original_rows[1], original_rows[0], *original_rows[2:]], "page": 1, "total": 8})
        QTest.mouseRelease(library.grid.viewport(), Qt.MouseButton.LeftButton, pos=first_rect.center())
        assert library.selected == selection_before
        library.show_rows({"items": original_rows[:1], "page": 1, "total": 8})
        application.processEvents()
        blank = QPoint(20, library.grid.viewport().height() - 5)
        assert not library.grid.indexAt(blank).isValid()
        QTest.mouseClick(library.grid.viewport(), Qt.MouseButton.LeftButton, pos=blank)
        assert library.selected == selection_before
        library.show_rows({"items": original_rows, "page": 1, "total": 8})
        window.grab().save(str(destination / "sample-row-selection.png"))
        check("sample-integrated-checkbox-painted-pixels-full-row-hit-and-single-toggle")
        library.grid.setCurrentCell(0, 0)
        identifier = library.rows[0]["id"]
        action(library, "切换审核")
        idle(window)
        assert runtime.call("/api/samples", params={"quality": "rejected"})["total"] == 1
        assert runtime.call("/api/samples", params={"quality": "accepted"})["total"] == 7
        action(library, "音频分析")
        idle(window)
        assert library.waveform.values and library.spectrum.values
        assert library.waveform.x_values[-1] > 0
        assert library.waveform.axis_ranges()[1:] == (-1.0, 1.0)
        assert library.spectrum.x_values[-1] > 7000
        window.stack.currentWidget().ensureWidgetVisible(library.spectrum)
        application.processEvents()
        window.grab().save(str(destination / "sample-chart-axes.png"))
        check("sample-chart-time-amplitude-and-frequency-db-axes")
        selection_before = set(library.selected)
        QTest.mouseClick(library.play_buttons[identifier], Qt.MouseButton.LeftButton)
        idle(window)
        assert library.selected == selection_before
        assert not any(button.text() == "试听" for button in library.findChildren(QPushButton))
        first_playback = window.player.source().toLocalFile()
        assert Path(first_playback).is_file()
        # Exercise the actual native popup and submenu with stable ID actions.
        # Playback never checks a batch box; direct export writes one WAV only.
        position = library.grid.visualItemRect(library.grid.item(0, 0)).center()
        library.open_context_menu(position)
        application.processEvents()
        menu = library.context_menu
        assert menu.isVisible()
        menu.grab().save(str(destination / "sample-context-menu.png"))
        review_menu = menu.review_menu
        review_menu.popup(menu.mapToGlobal(menu.actionGeometry(menu.review_action).topRight()))
        application.processEvents()
        assert review_menu.isVisible()
        review_menu.grab().save(str(destination / "sample-review-submenu.png"))
        review_menu.hide()
        menu.hide()
        from unittest.mock import patch
        output = destination / "single-wav-export"
        output.mkdir()
        with patch("desktop.samples.QFileDialog.getExistingDirectory", return_value=str(output)):
            library.export_audio(identifier)
        idle(window)
        assert len(list(output.iterdir())) == 1 and (output / f"sample-{identifier}.wav").is_file()
        assert library.selected == selection_before
        check("sample-native-context-submenu-per-row-play-button-and-direct-wav-export")
        cell = library.grid.visualItemRect(library.grid.item(0, 1))
        window.stack.currentWidget().ensureWidgetVisible(library.grid)
        application.processEvents()
        QTest.mouseClick(library.grid.viewport(), Qt.MouseButton.LeftButton, pos=cell.center())
        QTest.mouseDClick(library.grid.viewport(), Qt.MouseButton.LeftButton, pos=cell.center())
        idle(window)
        assert window.player.source().toLocalFile() != first_playback
        assert Path(window.player.source().toLocalFile()).is_file()
        assert first_id in library.selected and library.grid.item(0, 0).checkState() == Qt.CheckState.Checked
        assert runtime.call("/api/samples", params={"quality": "rejected"})["total"] == 1
        from PySide6.QtWidgets import QStyledItemDelegate
        assert isinstance(library.grid.itemDelegate(), QStyledItemDelegate)
        # Long display fixture on a real sample ID; compare row height before
        # and after focus, with both screenshots for visual wrap acceptance.
        display_rows = [dict(row) for row in library.rows]
        display_rows[0]["text"] = "较长的文本换行验收：勾选框与文本位于同一列，整行点击都可选中，也不裁切内容。" * 2
        display_rows[0]["voice_name"] = "音色名称较长的换行显示验收：选中后保留全部信息"
        library.show_rows({"items": display_rows, "page": 1, "total": len(display_rows)})
        library.grid.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
        application.processEvents()
        height_before = library.grid.rowHeight(0)
        window.grab().save(str(destination / "sample-long-unselected.png"))
        QTest.mouseClick(library.grid.viewport(), Qt.MouseButton.LeftButton, pos=library.grid.visualItemRect(library.grid.item(0, 0)).center())
        application.processEvents()
        assert library.grid.rowHeight(0) == height_before and height_before > 43
        window.grab().save(str(destination / "sample-long-selected.png"))
        library.load_page(1)
        idle(window)
        window.navigate(0)
        idle(window)
        assert not any(item.text() == "停止播放" and item.isVisible() for item in window.findChildren(QPushButton))
        from PySide6.QtMultimedia import QMediaPlayer
        assert window.player.playbackState() == QMediaPlayer.PlaybackState.StoppedState
        window.navigate(1)
        idle(window)
        check("sample-double-click-selects-and-plays-integrated-text-wrap-local-controls")
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
        assert "已导入 1" in recording.result.text() and "重复" not in recording.result.text()
        assert runtime.call("/api/samples", params={"provider": "real"})["total"] == 2
        check("native-import-48khz-continuous-parameters-repeat-creates-new-sample")
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
        window.navigate(4)
        idle(window)
        window.poll_timer.stop()
        tasks_page = window.pages[4]
        failure_fixture = {"id":"owned-failed-display", "type":"generate_batch", "status":"failed", "total":1, "completed":0, "failed":1,
                           "children":[{"id":"owned-failed-child", "type":"generate", "status":"failed", "total":1, "completed":0, "failed":1,
                                        "message":"豆包 2.0 未配置 API Key，请打开设置 → 豆包 2.0"}]}
        tasks_page.show_tasks([failure_fixture])
        tasks_page.tree.setCurrentItem(tasks_page.tree.topLevelItem(0))
        application.processEvents()
        assert "API Key" in tasks_page.detail.text()
        assert tasks_page.detail.height() >= tasks_page.detail.heightForWidth(tasks_page.detail.width())
        assert "API Key" in tasks_page.tree.topLevelItem(0).text(5)
        window.grab().save(str(destination / "task-failure.png"))
        check("failure-reason-visible-without-json-dialog")
        log_fixture = {**failure_fixture, "payload": {f"fixture_{i}": "reading-position"*4 for i in range(90)}}
        tasks_page.show_tasks([log_fixture])
        tasks_page.tree.setCurrentItem(tasks_page.tree.topLevelItem(0))
        tasks_page.raw_detail.parentWidget().show()
        window.stack.currentWidget().ensureWidgetVisible(tasks_page.raw_detail)
        application.processEvents()
        cursor = tasks_page.raw_detail.textCursor()
        cursor.setPosition(120)
        cursor.setPosition(140, cursor.MoveMode.KeepAnchor)
        tasks_page.raw_detail.setTextCursor(cursor)
        anchor, cursor_position = cursor.anchor(), cursor.position()
        tasks_page.raw_detail.verticalScrollBar().setValue(20)
        position = tasks_page.raw_detail.verticalScrollBar().value()
        assert position > 0
        for count in range(3):
            tasks_page.show_tasks([{**log_fixture, "completed": count}])
            application.processEvents()
            assert tasks_page.raw_detail.verticalScrollBar().value() == position
            assert tasks_page.raw_detail.textCursor().anchor() == anchor
            assert tasks_page.raw_detail.textCursor().position() == cursor_position
        window.grab().save(str(destination / "task-log-preserved.png"))
        check("technical-detail-poll-retains-scroll-and-text-selection")
        from PySide6.QtGui import QWheelEvent
        from PySide6.QtCore import QPointF
        from PySide6.QtWidgets import QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit, QPlainTextEdit
        tested = 0
        for page_index in range(7):
            window.navigate(page_index)
            idle(window)
            page = window.pages[page_index]
            for control in page.findChildren(QWidget):
                if not isinstance(control, (QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit, QPlainTextEdit)):
                    continue
                if not control.isVisible() or not control.isEnabled():
                    continue
                if isinstance(control, QPlainTextEdit) and control.isReadOnly():
                    continue
                control.setFocus()
                window.stack.currentWidget().ensureWidgetVisible(control)
                application.processEvents()
                state = lambda: control.currentIndex() if isinstance(control, QComboBox) else control.value() if isinstance(control, (QSpinBox, QDoubleSpinBox)) else control.toPlainText() if isinstance(control, QPlainTextEdit) else control.text()
                before = state()
                target = control.viewport() if isinstance(control, QPlainTextEdit) else control
                event = QWheelEvent(QPointF(target.rect().center()), target.mapToGlobal(QPointF(target.rect().center())),
                                    QPoint(), QPoint(0, -120), Qt.MouseButton.NoButton,
                                    Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
                application.sendEvent(target, event)
                application.processEvents()
                assert state() == before
                tested += 1
        assert tested >= 12
        check("visible-page-form-wheel-gestures-do-not-edit-focused-controls")
        window.poll_timer.start()
        # Repeat the exact same synthesis twice. Both explicit requests must
        # own new rows/files, including identical deterministic Mock output.
        repeated_keyword = "native-repeat-takes"
        voice = runtime.call("/api/voices", params={"provider": "mock"})[0]
        payload = {"voice_ids": [voice["id"]], "text": "重复合成验收", "label": "重复合成验收",
                   "keyword_id": repeated_keyword, "count": 2, "seed": 20261014,
                   "speed": 1.0, "pitch": 1.0, "volume": 50}
        for _ in range(2):
            created = runtime.call("/api/tasks/generate", "POST", json=payload)
            repeated_task = wait_task(runtime, created["task_id"])
            assert repeated_task["completed"] == 2 and repeated_task["failed"] == 0
            assert "重复" not in repeated_task["message"]
        repeated_rows = runtime.call("/api/samples", params={"keyword_id": repeated_keyword})["items"]
        assert len(repeated_rows) == 4 and len({row["id"] for row in repeated_rows}) == 4
        assert len({row["sha256"] for row in repeated_rows}) < 4
        window.navigate(4)
        window.pages[4].refresh()
        idle(window)
        window.grab().save(str(destination / "repeat-synthesis-task.png"))
        check("same-request-and-identical-audio-create-new-samples-without-reuse")
        expected_samples = runtime.call("/api/samples")["total"]
        assert expected_samples == 22
        previous_directory = runtime.data_directory
        moved_directory = destination / "迁移 数据"
        moved_directory.mkdir()
        window.pages[6].use_directory(moved_directory, "move")
        idle(window)
        runtime = window.runtime
        assert runtime.data_directory == moved_directory.resolve()
        assert runtime.call("/api/samples")["total"] == expected_samples
        assert not (previous_directory / "tts_dataset.sqlite3").exists()
        assert not (previous_directory / "samples").exists()
        assert window.preferences.data_directory() == moved_directory.resolve()
        check("live-move-preserves-samples-and-removes-old-data-not-a-copy")
        window.navigate(6)
        idle(window)
        window.grab().save(str(destination / "settings-after-move.png"))
        window.close()
        pump_until(lambda: not runtime.process.is_alive())
        check("native-close-no-business-child-left")
        imported = copy_dataset(runtime.data_directory, destination / "导入 数据")
        runtime2, window2 = start(imported)
        assert runtime2.call("/api/samples")["total"] == expected_samples
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
