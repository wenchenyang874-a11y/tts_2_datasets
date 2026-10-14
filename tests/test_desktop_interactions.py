"""Form gestures, task readers and sample actions with isolated native widgets."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from copy import deepcopy
from pathlib import Path
import pytest
from PySide6.QtCore import Qt, QPoint, QPointF
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QScrollArea,
                               QLineEdit, QPlainTextEdit, QTableWidgetItem, QDialog, QPushButton)
from desktop.form_wheel import FormWheelGuard
from desktop.widgets import combo, spin
from desktop.sample_table import SampleTable
from desktop.tasks import TasksPage
from desktop.samples import SamplesPage
from desktop.generate import GeneratePage


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


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
    QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=points[0])
    QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=points[1])
    assert all(table.item(row, 0).checkState() == Qt.CheckState.Checked for row in range(2))
    table.set_multi_select(False)
    QTest.mouseDClick(table.viewport(), Qt.MouseButton.LeftButton, pos=points[0])
    assert table.currentRow() == 0
    table.close()


def sample(identifier):
    return {"id": identifier, "text": identifier, "provider": "mock", "voice_name": "Mock", "quality_status": "accepted", "duration": 2.0}


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
