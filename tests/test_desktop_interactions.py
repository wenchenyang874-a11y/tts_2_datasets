"""Form gestures, task readers and sample actions with isolated native widgets."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from copy import deepcopy
from pathlib import Path
import pytest
from PySide6.QtCore import Qt, QPoint, QPointF, QCoreApplication, QEvent
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QScrollArea,
                               QLineEdit, QPlainTextEdit, QTableWidgetItem, QDialog, QPushButton)
from desktop.form_wheel import FormWheelGuard
from desktop.widgets import combo, spin, STYLE
from desktop.sample_table import SampleTable
from desktop.tasks import TasksPage
from desktop.samples import SamplesPage
from desktop.generate import GeneratePage


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def release_test_windows(application):
    yield
    # close() only hides these isolated widgets; callback ownership cycles can
    # otherwise keep their native children alive until QApplication teardown.
    # Release them through Qt while its event dispatcher is still running.
    for widget in application.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def wheel(application, widget, delta=-120, pixel=False):
    position = QPointF(widget.rect().center())
    event = QWheelEvent(position, widget.mapToGlobal(position), QPoint(0, delta) if pixel else QPoint(),
                        QPoint() if pixel else QPoint(0, delta), Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(widget, event)
    application.processEvents()


def test_focused_inputs_forward_wheel_without_changing_values(application):
    root = QWidget()
    layout = QVBoxLayout(root)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    layout.addWidget(scroll)
    page = QWidget()
    fields = QVBoxLayout(page)
    controls = [combo([("A", "a"), ("B", "b"), ("C", "c")]), spin(0, 20, 8),
                spin(0, 20, 8, True), QLineEdit("keep"), QPlainTextEdit("line\n"*80)]
    for control in controls:
        fields.addWidget(control)
    page.setMinimumHeight(1200)
    scroll.setWidget(page)
    guard = FormWheelGuard(root)
    root.resize(500, 320)
    root.show()
    application.processEvents()
    try:
        for control in controls:
            control.setFocus()
            scroll.verticalScrollBar().setValue(100)
            application.processEvents()
            before = scroll.verticalScrollBar().value()
            target = control.viewport() if isinstance(control, QPlainTextEdit) else control
            wheel(application, target)
            assert scroll.verticalScrollBar().value() > before
        assert controls[0].currentIndex() == 0
        assert controls[1].value() == controls[2].value() == 8
        assert controls[3].text() == "keep"
        assert controls[4].verticalScrollBar().value() == 0
        # Child line edits and high-resolution touchpad deltas need the same
        # contract; checking only wheelEvent on a spin's outer widget misses it.
        scroll.verticalScrollBar().setValue(100)
        wheel(application, controls[1].findChild(QLineEdit), -24, pixel=True)
        assert controls[1].value() == 8 and scroll.verticalScrollBar().value() > 100
        QTest.keyClick(controls[1], Qt.Key.Key_Up)
        assert controls[1].value() == 9
        dialog = QDialog(root)
        child = spin(0, 20, 8)
        QVBoxLayout(dialog).addWidget(child)
        dialog.show()
        application.processEvents()
        wheel(application, child)
        assert child.value() == 8
        dialog.close()
    finally:
        QApplication.instance().removeEventFilter(guard)
        root.close()


class Shell(QWidget):
    def __init__(self):
        super().__init__()
        self.selected_samples = set()
        self.playback_resource = None
        self.calls = []
        self.status = ""

    def submit(self, operation, callback=None, error=None):
        self.calls.append((operation, callback))

    def set_status(self, text, error=False):
        self.status = text

    def stop_playback(self):
        self.playback_resource = None

    def play_resource(self, kind, identifier, text):
        self.playback_resource = (kind, identifier)


def test_task_poll_preserves_reader_position_selection_and_task_identity(application):
    shell = Shell()
    page = TasksPage(shell)
    page.raw_detail.setMaximumHeight(100)
    page.raw_detail.show()
    page.show()
    row = {"id": "task-one", "type": "generate", "status": "running", "payload": {str(i): "x"*25 for i in range(90)}}
    page.show_tasks([row])
    page.tree.setCurrentItem(page.tree.topLevelItem(0))
    page.raw_detail.resize(300, 100)
    application.processEvents()
    cursor = page.raw_detail.textCursor()
    cursor.setPosition(120)
    cursor.setPosition(140, cursor.MoveMode.KeepAnchor)
    page.raw_detail.setTextCursor(cursor)
    page.raw_detail.verticalScrollBar().setValue(20)
    before = page.raw_detail.verticalScrollBar().value()
    assert before > 0
    for _ in range(3):
        page.show_tasks([deepcopy(row)])
        application.processEvents()
        assert page.raw_detail.verticalScrollBar().value() == before
        assert page.raw_detail.textCursor().selectedText() == cursor.selectedText()
    changed = deepcopy(row)
    changed["completed"] = 2
    page.show_tasks([changed])
    application.processEvents()
    assert page.raw_detail.verticalScrollBar().value() == before
    wheel(application, page.raw_detail.viewport())
    assert page.raw_detail.verticalScrollBar().value() > before
    other = deepcopy(row)
    other["id"] = "task-two"
    page.show_tasks([other])
    page.tree.setCurrentItem(page.tree.topLevelItem(0))
    assert page.detail_identifier == "task-two"
    assert page.raw_detail.verticalScrollBar().value() == 0
    page.close()
    shell.close()


def test_single_and_multi_sample_modes_click_and_double_click(application):
    table = SampleTable()
    table.setRowCount(2)
    for row in range(2):
        for column in range(7):
            item = QTableWidgetItem(str(row))
            table.setItem(row, column, item)
            if column == 0:
                item.setData(Qt.ItemDataRole.UserRole, str(row))
                item.setCheckState(Qt.CheckState.Unchecked)
    table.show()
    application.processEvents()
    points = [table.visualItemRect(table.item(row, 0)).center() for row in range(2)]
    QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=points[0])
    QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=points[1])
    assert table.currentRow() == 1
    assert all(table.item(row, 0).checkState() == Qt.CheckState.Unchecked for row in range(2))
    table.set_multi_select(True)
    points = [table.visualItemRect(table.item(row, 0)).center() for row in range(2)]
    QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=points[0])
    QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=points[1])
    assert all(table.item(row, 0).checkState() == Qt.CheckState.Checked for row in range(2))
    table.set_multi_select(False)
    points = [table.visualItemRect(table.item(row, 0)).center() for row in range(2)]
    QTest.mouseDClick(table.viewport(), Qt.MouseButton.LeftButton, pos=points[0])
    assert table.currentRow() == 0
    table.close()


def sample(identifier):
    return {"id": identifier, "text": identifier, "provider": "mock", "voice_name": "Mock", "quality_status": "accepted", "duration": 2.0}


def test_play_button_remains_fully_visible_after_styled_row_resize(application):
    shell = Shell()
    page = SamplesPage(shell)
    page.setStyleSheet(STYLE)
    page.resize(1000, 830)
    page.show_rows({"items": [sample("one"), sample("two")], "page": 1, "total": 2})
    page.show()
    application.processEvents()
    try:
        # Initial setCellWidget sizing can fit the control; the later polished
        # delegate sizing and stylesheet item padding used to crop its bottom.
        # Exercise the same second resize caused by column/window/mode changes.
        for multi, width in ((False, 1000), (True, 740), (False, 1000)):
            page.multi_button.setChecked(multi)
            page.toggle_multi()
            page.resize(width, 830)
            page.grid.resizeRowsToContents()
            application.processEvents()
            assert page.width() == width
            control = page.play_buttons["one"]
            assert control.visibleRegion().boundingRect() == control.rect()
            bottom = QPoint(control.width() // 2, control.height() - 4)
            assert page.grid.viewport().childAt(control.mapTo(page.grid.viewport(), bottom)) is control
            QTest.mouseClick(control, Qt.MouseButton.LeftButton, pos=bottom)
            assert shell.playback_resource == ("samples", "one")
            shell.stop_playback()
            # The action strip shares one row at ordinary width and wraps at
            # narrow widths; every visible action remains inside its parent.
            for action in page.toolbar.findChildren(QPushButton):
                if action.isVisible():
                    assert page.toolbar.rect().contains(action.geometry())
    finally:
        page.close()
        shell.close()


def test_audio_plot_uses_duration_frequency_and_absolute_amplitude(application):
    shell = Shell()
    page = SamplesPage(shell)
    page.show_analysis({"info": {"duration": 2.0}, "waveform": [0.1, 0.2, 0.0],
                        "spectrum": {"frequency": [0, 1000, 8000], "magnitude_db": [-90, -20, -60]},
                        "metrics": {"peak": 0.2}})
    assert page.waveform.x_values == [0, 1, 2]
    assert page.waveform.axis_ranges() == (2, 0, 1)
    assert page.spectrum.x_values == [0, 1000, 8000]
    assert page.spectrum.axis_ranges() == (8000, -100, -20)
    # A constant spectrum must have a nonzero plotted range, without division
    # by zero; its numeric values still retain the API's actual dB reference.
    page.spectrum.set_values([-40, -40], [0, 8000])
    assert page.spectrum.axis_ranges() == (8000, -40, -20)
    page.close()
    shell.close()


def test_per_row_playback_context_targets_and_direct_wav_export(application, monkeypatch, tmp_path):
    shell = Shell()
    page = SamplesPage(shell)
    page.show_rows({"items": [sample("one"), sample("two")], "page": 1, "total": 2})
    page.show()
    application.processEvents()
    QTest.mouseClick(page.play_buttons["two"], Qt.MouseButton.LeftButton)
    assert shell.playback_resource == ("samples", "two")
    assert not shell.selected_samples
    assert not any(control.text() == "试听" for control in page.findChildren(QPushButton))
    menu = page.build_context_menu("one")
    assert [action.text() for action in menu.actions()] == ["播放", "频谱分析", "审核", "", "导出音频文件…", "删除"]
    assert [action.text() for action in menu.actions()[2].menu().actions()] == ["未审核", "已通过", "已拒绝"]
    requests = []
    monkeypatch.setattr(page, "request", lambda *args, **kwargs: requests.append((args, kwargs)))
    menu.actions()[2].menu().actions()[0].trigger()
    assert requests[-1][0][0] == "/api/samples/one"
    assert requests[-1][1]["json"] == {"quality_status": "pending"}
    monkeypatch.setattr('desktop.samples.QFileDialog.getExistingDirectory', lambda *args: str(tmp_path))
    (tmp_path / 'sample-one.wav').write_bytes(b'preserve existing file')
    copies = []
    class Runtime:
        def call(self, **kwargs):
            copies.append(kwargs)
            Path(kwargs['destination']).write_bytes(b'RIFF-isolated-audio')
            return kwargs['destination']
    shell.runtime = Runtime()
    page.export_audio("one")
    operation, callback = shell.calls[-1]
    callback(operation())
    assert copies[0]['op'] == 'copy' and copies[0]['id'] == 'one'
    assert Path(copies[0]['destination']).suffix == '.wav'
    assert (tmp_path / 'sample-one.wav').read_bytes() == b'preserve existing file'
    assert not list(tmp_path.glob('*.zip'))
    menu.close()
    page.close()
    shell.close()


def test_removed_billing_checkbox_acknowledges_only_confirmed_snapshot(application, monkeypatch):
    shell = Shell()
    shell.project = {"keyword_id": "fixture", "label": "fixture"}
    page = GeneratePage(shell)
    page.set_capabilities({})
    assert not hasattr(page, "billing")
    payload = {"texts": ["fixture"], "voice_ids": [1], "count": 1,
               "instruction": "ASMR", "cost_acknowledged": False}
    requests = []
    monkeypatch.setattr(page, "request", lambda *args, **kwargs: requests.append((args, kwargs)))
    monkeypatch.setattr(page, "confirm", lambda message: False)
    page.preflight_ready(payload, True)
    assert not requests and payload["cost_acknowledged"] is False
    monkeypatch.setattr(page, "confirm", lambda message: True)
    page.preflight_ready(payload, True)
    assert requests[-1][0] == ("/api/tasks/generate-batch", "POST")
    assert requests[-1][1]["json"]["cost_acknowledged"] is True
    assert requests[-1][1]["json"]["instruction"] == "ASMR"
    page.close()
    shell.close()


def test_composer_keeps_cross_supplier_basket_and_uses_each_audio_profile(application, monkeypatch):
    from voice_features import expression_features
    shell = Shell()
    shell.project = {"keyword_id": "fixture", "label": "fixture"}
    page = GeneratePage(shell)
    monkeypatch.setattr(page, "request", lambda *args, **kwargs: None)
    caps = {key: {"audio_formats": ["wav", "mp3"], "sample_rates": [16000, 24000],
                  "speed": True, "pitch": True, "volume": True} for key in ("mock", "xiaomi")}
    caps["xiaomi"]["pitch"] = False
    page.set_capabilities(caps)
    def voice(identifier, provider):
        row = {"id": identifier, "name": "A long voice name "+str(identifier), "provider": provider,
               "voice_id": "fixture", "model": "mimo-v2.5-tts" if provider == "xiaomi" else "mock",
               "languages": ["zh"], "gender": "female", "age_group": "young"}
        row["expression_features"] = expression_features(row)
        return row
    page.show_voices([voice(1, "mock")])
    page.voices.item(0).setCheckState(Qt.CheckState.Checked)
    page.provider.setCurrentIndex(page.provider.findData("xiaomi"))
    page.show_voices([voice(2, "xiaomi")])
    page.voices.item(0).setCheckState(Qt.CheckState.Checked)
    application.processEvents()
    assert page.selected == {1, 2} and page.chosen_voices.count() == 2
    assert not page.pitch.isEnabled()
    page.audio_supplier.setCurrentIndex(page.audio_supplier.findData("xiaomi"))
    page.audio_format.setCurrentIndex(page.audio_format.findData("mp3"))
    page.sample_rate.setCurrentIndex(page.sample_rate.findData(24000))
    payload = page.payload()
    assert payload["voice_ids"] == [1, 2]
    assert payload["provider_audio"] == {"mock": {"format": "wav", "sample_rate": 16000},
                                         "xiaomi": {"format": "mp3", "sample_rate": 24000}}
    page.language.setCurrentIndex(page.language.findData("foreign"))
    page.show_voices([])
    application.processEvents()
    assert page.selected == {1, 2} and page.chosen_voices.count() == 2
    page.remove_voice(2)
    application.processEvents()
    assert page.selected == {1} and page.chosen_voices.count() == 1 and page.pitch.isEnabled()
    page.created({})
    assert page.selected == {1}  # Submission keeps the draft and basket.


def test_composer_minimum_viewport_has_no_page_scroll_and_all_tabs_fit(application, monkeypatch):
    shell = Shell()
    page = GeneratePage(shell)
    monkeypatch.setattr(page, "request", lambda *args, **kwargs: None)
    page.setStyleSheet(STYLE)
    page.resize(748, 550)  # The available composer area in the 980x680 shell.
    page.show()
    application.processEvents()
    assert not page.findChildren(QScrollArea)
    for index in range(page.editor_tabs.count()):
        page.editor_tabs.setCurrentIndex(index)
        application.processEvents()
        for control in (page.start_button, page.chosen_voices, page.voices, page.editor_tabs):
            assert control.isVisible()
            assert page.rect().contains(control.mapTo(page, control.rect().topLeft()))
            assert page.rect().contains(control.mapTo(page, control.rect().bottomRight()))
        assert page.editor_tabs.currentWidget().isVisible()
        assert all(not page.editor_tabs.widget(other).isVisible() for other in range(4) if other != index)
    page.ensureWidgetVisible(page.instruction)
    assert page.editor_tabs.currentIndex() == 1
