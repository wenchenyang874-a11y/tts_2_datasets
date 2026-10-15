"""Native voice creation and management, sharing the composer's catalog."""
from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path
from PySide6.QtCore import Qt, QTimer, QSize
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QWidget, QVBoxLayout, QHBoxLayout,
                               QLineEdit, QPlainTextEdit, QFileDialog, QLabel, QTableWidget,
                               QTableWidgetItem, QHeaderView, QAbstractItemView, QMessageBox,
                               QCheckBox, QScrollArea)
from .base import Page
from .widgets import combo, field, button
from .services import SERVICES

STATES = {"creating": "创建中", "pending": "待审核 / 训练中", "ready": "可用", "unknown": "结果待确认", "failed": "未通过", "missing": "云端不存在", "removed": "已移除"}
MODES = {"clone": "声音复刻", "design": "音色设计", "imported": "云端导入"}
LABELS = {key: label for label, key in SERVICES}
PREVIEW = "你好，很高兴认识你。欢迎体验我的声音。"


class CreationDialog(QDialog):
    def __init__(self, page, mode):
        super().__init__(page)
        self.page, self.mode, self.path = page, mode, None
        self.request_id = uuid.uuid4().hex
        self.submitting = False
        self.reference_cache = tempfile.TemporaryDirectory(prefix="tts-clone-reference-")
        self.finished.connect(self.release_reference)
        self.setWindowTitle("新建"+MODES[mode])
        self.resize(640, 700)
        self.setMinimumWidth(500)
        self.setStyleSheet("QDialog { background: #f4f6fb; }")
        root = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("composerPane")
        form = QVBoxLayout(body)
        form.setSpacing(14)
        self.name = QLineEdit()
        self.name.setMaxLength(80)
        self.name.setPlaceholderText("为这个声音取个名字")
        self.service = combo(SERVICES)
        self.service.setCurrentIndex(self.service.findData("volcengine_v2"))
        form.addWidget(field("音色名称", self.name))
        form.addWidget(field("供应商", self.service))
        self.model = combo([(model, model) for model in ("cosyvoice-v3.5-plus", "cosyvoice-v3.5-flash", "cosyvoice-v3-flash", "cosyvoice-v3-plus")])
        self.model_field = field("合成模型", self.model, "创建后绑定此模型，后续合成不能换成其他模型。声音设计仅支持北京地域。")
        form.addWidget(self.model_field)
        self.speaker_id = QLineEdit()
        self.speaker_id.setPlaceholderText("S_…")
        self.speaker_field = field("Speaker ID", self.speaker_id, "填写豆包控制台已购买的空闲音色槽位 ID。创建会消耗训练/设计次数，可能覆盖该槽位的云端音色；本版本不自动购买或使用后付费槽位。")
        form.addWidget(self.speaker_field)
        self.reference = QWidget()
        reference_layout = QHBoxLayout(self.reference)
        reference_layout.setContentsMargins(0, 0, 0, 0)
        self.file_label = QLabel("未选择音频")
        self.file_label.setTextFormat(Qt.TextFormat.PlainText)
        self.file_label.setWordWrap(True)
        reference_layout.addWidget(self.file_label, 1)
        reference_layout.addWidget(button("选择音频", self.choose_file))
        self.record_button = button("现场录制", self.record_reference)
        reference_layout.addWidget(self.record_button)
        self.reference_field = field("参考音频", self.reference, "本地 WAV、MP3、M4A 等，3–60 秒、不超过 10 MB；建议 10–20 秒清晰单人录音。文件会保存在数据目录，并发送给所选供应商用于复刻。")
        form.addWidget(self.reference_field)
        self.url = QLineEdit()
        self.url.setPlaceholderText("https://…/reference.wav")
        url_input = QWidget()
        url_layout = QHBoxLayout(url_input)
        url_layout.setContentsMargins(0, 0, 0, 0)
        url_layout.addWidget(self.url, 1)
        url_layout.addWidget(button("现场录制", self.record_reference))
        self.url_field = field("参考音频链接", url_input, "阿里云 CosyVoice 要求公网可访问的 HTTPS 音频 URL，建议 10–20 秒。现场录音可先保存到本地，再由你上传并填写链接；应用不会自动上传到第三方存储。")
        form.addWidget(self.url_field)
        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText("例如：温暖沉稳的青年女性，声音清晰自然，适合轻松的日常交流")
        self.prompt.setFixedHeight(100)
        self.prompt_field = field("声音描述", self.prompt, "描述年龄、性别、音色和说话风格。豆包最多 200 字，其他供应商最多 500 字。小米设计后会保存预览作为后续复刻参考。")
        form.addWidget(self.prompt_field)
        row = QHBoxLayout()
        self.language = combo([("中文", "zh"), ("英语", "en")])
        self.group = QLineEdit()
        self.group.setPlaceholderText("可选，说话人 ID")
        row.addWidget(field("语言", self.language))
        row.addWidget(field("声源标识", self.group, "同一真人的不同录音、不同供应商复刻应填写相同说话人 ID，避免训练与测试泄漏。留空时，相同本地文件自动使用相同标识。"), 2)
        form.addLayout(row)
        self.preview = QPlainTextEdit(PREVIEW)
        self.preview.setFixedHeight(75)
        form.addWidget(field("预览文本", self.preview, "创建时使用这段文字生成预览，可能计费；阿里云复刻不自动调用合成，待状态可用后在创作页生成。最多 200 字，阿里设计至少 15 字，豆包复刻至少 4 字。"))
        form.addStretch()
        scroll.setWidget(body)
        root.addWidget(scroll, 1)
        self.problem = QLabel()
        self.problem.setWordWrap(True)
        self.problem.setTextFormat(Qt.TextFormat.PlainText)
        self.problem.setStyleSheet("color: #bd455d;")
        root.addWidget(self.problem)
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(button("取消", self.reject))
        self.create_button = button("创建音色", self.submit, True)
        actions.addWidget(self.create_button)
        root.addLayout(actions)
        self.service.currentIndexChanged.connect(self.update_fields)
        self.update_fields()

    def update_fields(self):
        service = self.service.currentData()
        self.model_field.setVisible(service == "aliyun")
        self.speaker_field.setVisible(service in {"volcengine_v1", "volcengine_v2"})
        self.reference_field.setVisible(self.mode == "clone" and service != "aliyun")
        self.url_field.setVisible(self.mode == "clone" and service == "aliyun")
        self.prompt_field.setVisible(self.mode == "design")

    def choose_file(self):
        filename, _ = QFileDialog.getOpenFileName(self, "选择参考音频", "", "音频 (*.wav *.mp3 *.m4a *.ogg *.aac *.flac);;所有文件 (*)")
        if filename:
            self.path = Path(filename)
            self.file_label.setText(self.path.name)

    def release_reference(self, *_):
        # A successful callback means the backend has already copied the
        # multipart reference into its managed data directory. Cancelled forms
        # only remove their own recordings, never a user-selected local file.
        self.reference_cache.cleanup()

    def record_reference(self):
        from .reference_recording import ReferenceRecordingDialog
        if self.submitting:
            return
        if self.page.shell.pages[2].source:
            self.problem.setText("真人录音正在录制，请先停止后再录制参考音频")
            return
        self.page.shell.stop_playback()
        dialog = ReferenceRecordingDialog(self, self.reference_cache.name, export_required=self.service.currentData() == "aliyun")
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.accepted_path:
            if self.service.currentData() == "aliyun":
                self.url.clear()
                self.problem.setText("参考录音已保存；请上传后填写可公网访问的 HTTPS 音频链接")
            else:
                self.path = dialog.accepted_path
                self.file_label.setText(f"现场录音 · {dialog.capture.duration:.1f} 秒")
                self.problem.clear()
        dialog.deleteLater()

    def values(self):
        return {"request_id": self.request_id, "name": self.name.text().strip(), "service": self.service.currentData(), "mode": self.mode,
                "model": self.model.currentData() if self.service.currentData() == "aliyun" else "", "speaker_id": self.speaker_id.text().strip(),
                "speaker_group": self.group.text().strip(), "prompt": self.prompt.toPlainText().strip() if self.mode == "design" else "",
                "preview_text": self.preview.toPlainText().strip(), "language": self.language.currentData(), "reference_url": self.url.text().strip() if self.mode == "clone" and self.service.currentData() == "aliyun" else ""}

    def submit(self):
        if self.submitting:
            return
        values = self.values()
        if not values["name"] or not values["preview_text"] or len(values["preview_text"]) > 200:
            self.problem.setText("请填写音色名称与预览文本（最多 200 字）")
            return
        if self.mode == "design" and not values["prompt"]:
            self.problem.setText("请填写声音描述")
            return
        service = values["service"]
        if service.startswith("volcengine") and not values["speaker_id"].startswith("S_"):
            self.problem.setText("请填写控制台已购买的 Speaker ID（S_ 开头）")
            return
        if service == "aliyun" and self.mode == "clone" and not values["reference_url"].startswith("https://"):
            self.problem.setText("请填写可公网访问的 HTTPS 参考音频链接")
            return
        if service == "aliyun" and self.mode == "design" and len(values["preview_text"]) < 15:
            self.problem.setText("阿里云设计的预览文本至少 15 字")
            return
        files = []
        if self.mode == "clone" and values["service"] != "aliyun":
            if not self.path or not self.path.is_file() or self.path.stat().st_size > 10*1024*1024:
                self.problem.setText("请选择不超过 10 MB 的本地音频")
                return
            files = [("audio", str(self.path))]
        if values["service"] != "mock":
            text = f"将向{LABELS[values['service']]}提交 1 次{MODES[self.mode]}请求，可能产生创建或预览费用。\n参考素材将发送给所选供应商，失败或超时不会自动重试。"
            if values["service"].startswith("volcengine"):
                text += "\n\n请确认 Speaker ID 是可使用的槽位，本次操作会消耗训练/设计次数，并可能覆盖槽位中的原音色。"
            if QMessageBox.question(self, "确认云端创建", text) != QMessageBox.StandardButton.Yes:
                return
        values["cost_acknowledged"] = values["service"] != "mock"
        self.problem.setText("正在提交…")
        self.submitting = True
        self.create_button.setEnabled(False)
        self.page.request("/api/custom-voices", "POST", data={"options": json.dumps(values, ensure_ascii=False)}, files=files,
                          callback=self.created, error=self.failed)

    def created(self, result):
        self.submitting = False
        self.page.notify("音色已加入创建队列，可在列表查看状态")
        self.page.refresh()
        self.accept()

    def failed(self, error):
        self.submitting = False
        self.problem.setText(str(error))
        self.create_button.setEnabled(True)

    def reject(self):
        if not self.submitting:
            super().reject()

    def closeEvent(self, event):
        if self.submitting:
            event.ignore()
        else:
            super().closeEvent(event)


class CustomVoicesPage(Page):
    def __init__(self, shell):
        super().__init__(shell, "我的音色")
        self.rows, self.loading = {}, False
        self.play_buttons = {}
        toolbar = QHBoxLayout()
        toolbar.addWidget(button("声音复刻", lambda: self.create("clone"), True))
        toolbar.addWidget(button("设计音色", lambda: self.create("design")))
        toolbar.addStretch()
        toolbar.addWidget(button("刷新列表", self.refresh))
        toolbar.addWidget(button("同步云端音色", self.sync_catalog))
        self.layout.addLayout(toolbar)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索名称、供应商、模型或标签")
        self.search.textChanged.connect(self.filter_rows)
        search_row = QHBoxLayout()
        search_row.addWidget(self.search, 1)
        self.archived = QCheckBox("显示已移除")
        self.archived.toggled.connect(self.refresh)
        search_row.addWidget(self.archived)
        self.layout.addLayout(search_row)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["音色", "供应商 / 模型", "来源", "状态", "标签", "预览"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        for i in range(6):
            header.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(5, 76)
        self.table.currentCellChanged.connect(self.show_detail)
        self.table.cellDoubleClicked.connect(lambda *_: self.edit())
        self.layout.addWidget(self.table, 1)
        self.details = QLabel()
        self.details.setTextFormat(Qt.TextFormat.PlainText)
        self.details.setWordWrap(True)
        self.details.setObjectName("muted")
        self.layout.addWidget(self.details)
        actions = self.actions(("用于合成", self.use, True), ("刷新音色状态", self.query, False), ("编辑音色", self.edit, False), ("移除音色", self.remove, False))
        self.voice_actions = [actions.itemAt(i).widget() for i in range(4)]
        self.add_playback_controls()
        self.timer = QTimer(self)
        self.timer.setInterval(1500)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        self.show_detail()

    def poll(self):
        if self.shell.ready and self.shell.stack.currentIndex() == 7 and any(v["busy"] for v in self.rows.values()):
            self.refresh()

    def ensureWidgetVisible(self, widget):
        pass  # Bounded page: only the table scrolls; actions stay in view.

    def refresh(self):
        if self.loading:
            return
        self.loading = True
        archived = self.archived.isChecked()
        def loaded(rows):
            if archived != self.archived.isChecked():
                self.loading = False
                self.refresh()
            else:
                self.loaded(rows)
        self.request("/api/custom-voices", params={"include_removed": archived}, callback=loaded, error=self.failed)

    def failed(self, error):
        self.loading = False
        self.shell.error(error)

    def loaded(self, rows):
        current = self.current()
        self.rows = {row["id"]: row for row in rows}
        self.play_buttons = {}
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            state = "处理中" if row["busy"] else STATES.get(row["status"], row["status"])
            if row["status"] == "ready" and not row["desired_enabled"]:
                state = "已停用"
            for column, text in enumerate((row["name"], LABELS.get(row["service"], row["service"])+"\n"+row["model"], MODES[row["mode"]], state, row["tags"], "")):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, row["id"])
                item.setToolTip(text)
                self.table.setItem(index, column, item)
            self.table.setRowHeight(index, 62)
            container = QWidget()
            layout = QHBoxLayout(container)
            # Item padding already removes 8px on each side. Additional host
            # margins clipped fixed-size play buttons inside the final column.
            # Reserve the column explicitly and center within its real rect.
            layout.setContentsMargins(0, 0, 0, 0)
            play = button("", lambda checked=False, identity=row["id"]: self.play(identity))
            play.setObjectName("rowPlay")
            play.setIcon(QIcon(str(Path(__file__).parent / "assets" / "play.svg")))
            play.setIconSize(QSize(18, 18))
            play.setFixedSize(38, 38)
            play.setAccessibleName("播放 "+row["name"])
            play.setToolTip("播放预览")
            play.setEnabled(row["has_preview"])
            layout.addStretch()
            layout.addWidget(play)
            layout.addStretch()
            self.table.setCellWidget(index, 5, container)
            self.play_buttons[row["id"]] = play
            if current and current["id"] == row["id"]:
                self.table.setCurrentCell(index, 0)
        self.loading = False
        self.filter_rows()
        self.show_detail()
        self.update_row_playback()

    def play(self, identifier):
        if self.shell.playback_resource == ("custom_voices", identifier):
            self.shell.stop_playback()
        elif identifier in self.rows:
            self.shell.play_resource("custom_voices", identifier, self.rows[identifier]["name"])

    def update_row_playback(self):
        for identifier, control in self.play_buttons.items():
            playing = self.shell.playback_resource == ("custom_voices", identifier)
            control.setIcon(QIcon(str(Path(__file__).parent / "assets" / ("stop.svg" if playing else "play.svg"))))
            control.setToolTip("停止播放" if playing else "播放预览")

    def current(self):
        item = self.table.item(self.table.currentRow(), 0)
        return self.rows.get(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def filter_rows(self):
        text = self.search.text().strip().lower()
        for index in range(self.table.rowCount()):
            content = " ".join(self.table.item(index, col).text() for col in range(5)).lower()
            self.table.setRowHidden(index, bool(text and text not in content))

    def show_detail(self, *_):
        row = self.current()
        self.details.setText("" if not row else (row["error"] or (f"剩余训练 / 设计次数：{row['remaining']}" if row["remaining"] is not None else "")))
        self.details.setVisible(bool(self.details.text()))
        if hasattr(self, "voice_actions"):
            usable = bool(row and not row["busy"] and row["status"] != "removed")
            for action in self.voice_actions:
                action.setEnabled(usable)
            self.voice_actions[0].setEnabled(bool(usable and row["status"] == "ready" and row["desired_enabled"]))
            self.voice_actions[3].setText("恢复音色" if row and row["status"] == "removed" else "移除音色")
            self.voice_actions[3].setEnabled(bool(row and not row["busy"]))

    def create(self, mode):
        dialog = CreationDialog(self, mode)
        dialog.exec()
        dialog.deleteLater()

    def query(self):
        row = self.current()
        if row:
            self.request(f"/api/custom-voices/{row['id']}/refresh", "POST", callback=lambda _: self.refresh())

    def sync_catalog(self):
        def synced(result):
            self.refresh()
            self.notify("云端目录已同步" + ("；"+"；".join(result["warnings"]) if result.get("warnings") else ""))
        self.request("/api/voices/refresh", "POST", json={}, callback=synced)

    def use(self):
        row = self.current()
        if not row:
            return
        if row["status"] != "ready" or not row["desired_enabled"] or not row["voice_db_id"]:
            self.notify("请先等待音色可用并启用")
            return
        composer = self.shell.pages[0]
        self.shell.navigate(0)
        # Catalog refresh is asynchronous. The page owns the callback so the
        # exact stable DB ID is selected only after the fresh catalog arrives.
        def select(rows):
            composer.voice_rows.update({v["id"]: v for v in rows})
            composer.selected.add(row["voice_db_id"])
            composer.locate_voice(row["voice_db_id"])
            composer.update_summary()
            composer.update_capabilities()
        self.request("/api/voices", callback=select)

    def edit(self):
        row = self.current()
        if not row or row["busy"]:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("编辑音色")
        dialog.setMinimumWidth(440)
        layout = QVBoxLayout(dialog)
        name, tags = QLineEdit(row["name"]), QLineEdit(row["tags"])
        enabled = QCheckBox("在合成目录中启用")
        enabled.setChecked(bool(row["desired_enabled"]))
        layout.addWidget(field("音色名称", name))
        layout.addWidget(field("标签", tags))
        layout.addWidget(enabled)
        actions = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        actions.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        actions.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        actions.accepted.connect(dialog.accept)
        actions.rejected.connect(dialog.reject)
        layout.addWidget(actions)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.request(f"/api/custom-voices/{row['id']}", "PATCH", json={"name": name.text(), "tags": tags.text(), "enabled": enabled.isChecked()}, callback=lambda _: self.refresh())
        dialog.deleteLater()

    def remove(self):
        row = self.current()
        if row and row["status"] == "removed":
            self.request(f"/api/custom-voices/{row['id']}/restore", "POST", callback=lambda _: (self.refresh(), self.notify("已恢复；云端音色请刷新状态后使用")))
            return
        if row and not row["busy"] and self.confirm("从本地音色列表移除“"+row["name"]+"”？\n已有样本会保留，云端音色与槽位不会删除。"):
            self.shell.stop_playback()
            self.request(f"/api/custom-voices/{row['id']}", "DELETE", callback=lambda _: self.refresh())
