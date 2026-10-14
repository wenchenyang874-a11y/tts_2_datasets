"""Sample review with stable cross-page selections and API-owned provenance."""
from pathlib import Path
import uuid
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QHBoxLayout, QLineEdit, QLabel, QTableWidgetItem,
                               QDialog, QVBoxLayout, QDialogButtonBox, QFormLayout, QWidget, QMenu, QFileDialog)
from .base import Page
from .widgets import combo, spin, button, Plot, field, Disclosure
from .services import SERVICES
from .generate import TARGETS, AGES
from .sample_table import SampleTable

QUALITY = {"pending": "未审核", "accepted": "已通过", "rejected": "已拒绝",
           "unchecked": "未审核", "pass": "已通过", "reject": "已拒绝", "review": "未审核"}


class SamplesPage(Page):
    def __init__(self, shell):
        super().__init__(shell, "样本库")
        self.page = 1
        self.total = 0
        self.selected = shell.selected_samples
        self.rows = []
        self.play_buttons = {}
        self.deleted_ids = []
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索文本、音色或说话人")
        self.quality = combo([("全部审核", ""), ("未审核", "pending"), ("已通过", "accepted"), ("已拒绝", "rejected")])
        self.provider = combo([("全部来源", "")] + SERVICES + [("真人", "real")])
        self.kind = combo([("全部目标", "")] + TARGETS)
        self.variant = combo([("全部类型", ""), ("原音", "clean"), ("增强", "augmented")])
        self.split = combo([("全部划分", ""), ("未分配", "unassigned"), ("Train", "train"), ("Dev", "dev"), ("Test", "test")])
        self.keyword = combo([("全部唤醒词", "")])
        self.task_filter = combo([("全部来源任务", "")])
        self.gender = combo([("全部性别", ""), ("女", "female"), ("男", "male"), ("未知", "unknown")])
        self.age = combo([("全部年龄", "")] + AGES)
        for control in (self.provider, self.quality, self.kind, self.variant, self.keyword, self.task_filter, self.split, self.gender, self.age):
            control.setMinimumContentsLength(4)
        more = Disclosure("更多筛选")
        for index, widgets in enumerate(([self.search, self.provider, self.quality, self.kind, self.variant],
                        [self.keyword, self.task_filter, self.split, self.gender, self.age])):
            row = QHBoxLayout()
            for widget in widgets:
                row.addWidget(widget, 2 if widget is self.search else 1)
            (self.layout if index == 0 else more.body).addLayout(row)
        self.layout.addWidget(more)
        for widget in [self.provider, self.quality, self.kind, self.variant, self.split, self.gender, self.age, self.keyword, self.task_filter]:
            widget.currentIndexChanged.connect(self.search_first)
        self.search.returnPressed.connect(self.search_first)
        actions = self.actions(("筛选", self.search_first, True))
        self.multi_button = button("多选", self.toggle_multi)
        self.multi_button.setCheckable(True)
        actions.insertWidget(1, self.multi_button)
        self.batch_buttons = [button("全选筛选结果", self.select_filtered),
                              button("清空选择", self.clear_selection),
                              button("导出所选", lambda: shell.navigate(5))]
        for control in self.batch_buttons:
            actions.insertWidget(actions.count()-1, control)
            control.hide()
        self.grid = SampleTable()
        self.grid.setMinimumHeight(270)
        self.grid.itemChanged.connect(self.selection_changed)
        self.grid.cellDoubleClicked.connect(lambda row, column: self.play_row(row))
        self.grid.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(self.open_context_menu)
        self.layout.addWidget(self.grid)
        row = self.actions(("音频分析", self.analyze_current, False),
                           ("切换审核", self.review_current, False), ("编辑元数据", self.edit, False),
                           ("删除所选", self.delete, False), ("撤销最近删除", self.restore, False))
        self.delete_button = row.itemAt(3).widget()
        self.delete_button.hide()
        self.add_playback_controls()
        self.summary = QLabel()
        self.summary.setObjectName("muted")
        self.layout.addWidget(self.summary)
        paging = QHBoxLayout()
        paging.addWidget(button("上一页", lambda: self.change_page(-1)))
        self.page_number = spin(1, 999999, 1)
        paging.addWidget(self.page_number)
        paging.addWidget(button("跳转", lambda: self.load_page(self.page_number.value())))
        paging.addWidget(button("下一页", lambda: self.change_page(1)))
        paging.addStretch()
        self.layout.addLayout(paging)
        self.metrics = QLabel()
        self.layout.addWidget(self.metrics)
        charts = QHBoxLayout()
        self.waveform = Plot("波形")
        self.spectrum = Plot("频谱 · dB")
        charts.addWidget(self.waveform)
        charts.addWidget(self.spectrum)
        self.layout.addLayout(charts)

    def filters(self):
        return {"search": self.search.text(), "provider": self.provider.currentData(),
                "quality": self.quality.currentData(), "target_kind": self.kind.currentData(),
                "sample_type": self.variant.currentData(), "split": self.split.currentData(),
                "keyword_id": self.keyword.currentData(), "origin_job_id": self.task_filter.currentData(),
                "gender": self.gender.currentData(), "age_group": self.age.currentData()}

    def refresh(self):
        self.load_page(self.page)
        self.request("/api/samples/keywords", callback=lambda rows: self.fill_filter(self.keyword, rows, "keyword_id", "label"))
        self.request("/api/tasks", params={"limit": 500}, callback=lambda rows: self.fill_filter(self.task_filter, rows, "id", "text"))

    def fill_filter(self, widget, rows, key, label):
        value = widget.currentData()
        widget.blockSignals(True)
        while widget.count() > 1:
            widget.removeItem(1)
        for row in rows:
            widget.addItem(str(row.get(label) or row.get("type") or row[key])[:35], row[key])
        index = widget.findData(value)
        widget.setCurrentIndex(max(0, index))
        widget.blockSignals(False)

    def search_first(self):
        self.load_page(1)

    def change_page(self, delta):
        self.load_page(max(1, self.page + delta))

    def load_page(self, page):
        self.page = page
        self.request("/api/samples", params={**self.filters(), "page": page, "page_size": 24}, callback=self.show_rows)

    def show_rows(self, result):
        current = self.current()
        identifier = current["id"] if current else None
        self.rows = result["items"]
        self.total = result["total"]
        self.grid.blockSignals(True)
        self.grid.setRowCount(len(self.rows))
        self.play_buttons = {}
        for index, row in enumerate(self.rows):
            check = QTableWidgetItem(row["text"])
            check.setToolTip(row["text"])
            check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
            check.setCheckState(Qt.CheckState.Checked if row["id"] in self.selected else Qt.CheckState.Unchecked)
            check.setData(Qt.ItemDataRole.UserRole, row["id"])
            self.grid.setItem(index, 0, check)
            provider = "volcengine_v2" if row["provider"] == "volcengine" and row.get("model") == "seed-tts-2.0" else "volcengine_v1" if row["provider"] == "volcengine" else row["provider"]
            source_name = dict((value, label) for label, value in SERVICES + [("真人", "real"), ("噪声", "noise")]).get(provider, provider)
            values = [f"{source_name} / {row['voice_name']}",
                      "增强" if row.get("variant_kind") == "augmented" else "原音",
                      QUALITY.get(row.get("quality_status"), row.get("quality_status", "")),
                      f"{row['duration']:.2f}s", {"unassigned": "未分配"}.get(row.get("split"), row.get("split", ""))]
            for column, value in enumerate(values, 1):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                self.grid.setItem(index, column, item)
            # A real, keyboard-accessible button has a 40x34 click target. Bind
            # its stable sample ID rather than the row number: polling/filtering
            # can reorder rows, and playback must never toggle batch selection.
            self.grid.setItem(index, 6, QTableWidgetItem())
            control = button("", lambda checked=False, identifier=row["id"]: self.play_sample(identifier))
            control.setObjectName("rowPlay")
            control.setFixedSize(40, 34)
            control.setIconSize(QSize(18, 18))
            host = QWidget()
            layout = QHBoxLayout(host)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(control, 0, Qt.AlignmentFlag.AlignCenter)
            self.grid.setCellWidget(index, 6, host)
            self.play_buttons[row["id"]] = control
            if row["id"] == identifier:
                self.grid.setCurrentCell(index, 0)
        if identifier is None or not any(row["id"] == identifier for row in self.rows):
            self.grid.setCurrentCell(-1, -1)
        self.grid.blockSignals(False)
        self.grid.resizeRowsToContents()
        self.update_row_playback()
        self.page_number.setValue(result["page"])
        self.update_selection_summary()

    def update_selection_summary(self):
        text = f"共 {self.total} 条 · 第 {self.page} 页"
        if self.grid.multi_select:
            text += f" · 已选 {len(self.selected)} 条"
        self.summary.setText(text)

    def toggle_multi(self):
        enabled = self.multi_button.isChecked()
        self.multi_button.setText("退出多选" if enabled else "多选")
        self.grid.set_multi_select(enabled)
        for control in self.batch_buttons:
            control.setVisible(enabled)
        self.delete_button.setVisible(enabled)
        if not enabled:
            # Do not leave invisible batch selections active after returning to
            # ordinary single-row navigation. The current row remains separate.
            self.selected.clear()
            for row in range(self.grid.rowCount()):
                self.grid.item(row, 0).setCheckState(Qt.CheckState.Unchecked)
        self.update_selection_summary()

    def build_context_menu(self, identifier):
        row = next((row for row in self.rows if row["id"] == identifier), None)
        if row is None:
            return None
        menu = QMenu(self)
        menu.setMinimumWidth(174)
        menu.addAction(QIcon(str(Path(__file__).resolve().parent / "assets/play.svg")), "播放", lambda: self.play_sample(identifier))
        menu.addAction("频谱分析", lambda: self.request(f"/api/samples/{identifier}/analysis", callback=self.show_analysis))
        review = QMenu("审核", menu)
        # Give the submenu an explicit parent and retain both wrappers. The
        # native-created addMenu(title) path did not survive the returned menu's
        # later action inspection in PySide 6.8.3; avoid relying on that ownership.
        menu.review_menu = review
        menu.review_action = menu.addMenu(review)
        current = QUALITY.get(row.get("quality_status"), "未审核")
        for label, status in [("未审核", "pending"), ("已通过", "accepted"), ("已拒绝", "rejected")]:
            action = review.addAction(label, lambda checked=False, status=status: self.mutate(f"/api/samples/{identifier}", "PATCH", json={"quality_status": status}))
            action.setCheckable(True)
            action.setChecked(label == current)
        menu.addSeparator()
        menu.addAction("导出音频文件…", lambda: self.export_audio(identifier))
        menu.addAction("删除", lambda: self.delete_audio(identifier))
        return menu

    def open_context_menu(self, position):
        index = self.grid.indexAt(position)
        if not index.isValid():
            return
        self.grid.setCurrentCell(index.row(), 0)
        identifier = self.grid.item(index.row(), 0).data(Qt.ItemDataRole.UserRole)
        menu = self.build_context_menu(identifier)
        if menu:
            # Nonblocking popup keeps background UI completions alive. Actions
            # capture the ID, so a refresh while the menu is open cannot act on a
            # different sample that happens to occupy its former row.
            self.context_menu = menu
            menu.aboutToHide.connect(menu.deleteLater)
            menu.popup(self.grid.viewport().mapToGlobal(position))

    def export_audio(self, identifier):
        directory = QFileDialog.getExistingDirectory(self, "选择音频导出目录")
        if not directory:
            return
        path = Path(directory) / f"sample-{identifier}.wav"
        if path.exists():
            path = path.with_name(f"sample-{identifier}-{uuid.uuid4().hex[:8]}.wav")
        self.shell.submit(lambda: self.shell.runtime.call(op="copy", kind="samples", id=identifier, destination=str(path)),
                          lambda result: self.notify("音频已导出：" + str(path)))

    def delete_audio(self, identifier):
        if self.confirm("删除这条音频？可通过“撤销最近删除”恢复。"):
            self.request("/api/samples/delete", "POST", json={"ids": [identifier]}, callback=lambda result: self.after_delete([identifier]))

    def selection_changed(self, item):
        if item.column() != 0:
            return
        identifier = item.data(Qt.ItemDataRole.UserRole)
        if item.checkState() == Qt.CheckState.Checked:
            self.selected.add(identifier)
        else:
            self.selected.discard(identifier)
        self.update_selection_summary()

    def select_filtered(self):
        self.request("/api/samples/ids", params=self.filters(),
                     callback=lambda result: (self.selected.update(result["ids"]), self.load_page(self.page)))

    def clear_selection(self):
        self.selected.clear()
        self.load_page(self.page)

    def current(self):
        index = self.grid.currentRow()
        return self.rows[index] if 0 <= index < len(self.rows) else None

    def play_sample(self, identifier):
        if self.shell.playback_resource == ("samples", identifier):
            self.shell.stop_playback()
            return
        row = next((row for row in self.rows if row["id"] == identifier), None)
        if row:
            self.shell.play_resource("samples", identifier, row["text"])

    def update_row_playback(self):
        assets = Path(__file__).resolve().parent / "assets"
        for identifier, control in self.play_buttons.items():
            playing = self.shell.playback_resource == ("samples", identifier)
            control.setIcon(QIcon(str(assets / ("stop.svg" if playing else "play.svg"))))
            control.setAccessibleName("停止播放音频" if playing else "播放音频")
            control.setToolTip("停止播放" if playing else "播放")

    def play_row(self, index):
        # Double-click explicitly uses the clicked row, even if the current
        # cell changed during selection. It never changes review status.
        if 0 <= index < len(self.rows):
            row = self.rows[index]
            self.shell.play_resource("samples", row["id"], row["text"])

    def analyze_current(self):
        self.analyze(self.grid.currentRow())

    def analyze(self, index):
        if 0 <= index < len(self.rows):
            self.request(f"/api/samples/{self.rows[index]['id']}/analysis", callback=self.show_analysis)

    def show_analysis(self, result):
        self.waveform.set_values(result["waveform"])
        self.spectrum.set_values(result["spectrum"]["magnitude_db"])
        metrics = result["metrics"]
        labels = {"peak": "峰值", "rms": "RMS", "dc_offset": "直流偏移",
                  "clipping_ratio": "削波", "silence_ratio": "静音"}
        self.metrics.setText(" · ".join(f"{labels.get(name, name)} {value*100:.1f}%" if name.endswith("_ratio")
                                       else f"{labels.get(name, name)} {value:.3f}" for name, value in metrics.items()))

    def review_current(self):
        self.review(self.grid.currentRow())

    def review(self, index):
        if not 0 <= index < len(self.rows):
            return
        row = self.rows[index]
        status = {"pending": "accepted", "accepted": "rejected", "rejected": "pending",
                  "pass": "rejected", "reject": "pending"}.get(row.get("quality_status"), "accepted")
        self.mutate(f"/api/samples/{row['id']}", "PATCH", json={"quality_status": status})

    def edit(self):
        row = self.current()
        if not row:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("编辑样本")
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        fields = {}
        for key, label in [("text", "文本"), ("label", "标签"), ("keyword_id", "唤醒词 ID"),
                           ("speaker_id", "说话人 ID"), ("notes", "备注")]:
            widget = QLineEdit(str(row.get(key) or row.get("metadata", {}).get(key) or ""))
            form.addRow(label, widget)
            fields[key] = widget
        kind = combo(TARGETS)
        kind.setCurrentIndex(max(0, kind.findData(row.get("target_kind"))))
        split = combo([("未分配", "unassigned"), ("Train", "train"), ("Dev", "dev"), ("Test", "test")])
        split.setCurrentIndex(max(0, split.findData(row.get("split"))))
        form.addRow("样本目标", kind)
        form.addRow("数据划分", split)
        gender = combo([("未知", "unknown"), ("女", "female"), ("男", "male")])
        age = combo(AGES)
        gender.setCurrentIndex(max(0, gender.findData(row.get("metadata", {}).get("gender", "unknown"))))
        age.setCurrentIndex(max(0, age.findData(row.get("metadata", {}).get("age_group", "unknown"))))
        form.addRow("性别", gender)
        form.addRow("年龄", age)
        tags = QLineEdit(", ".join(str(value) for value in row.get("metadata", {}).get("tags", [])))
        form.addRow("标签（逗号分隔）", tags)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec():
            payload = {key: widget.text() for key, widget in fields.items()}
            if not payload["speaker_id"]:
                payload.pop("speaker_id")
            payload.update(target_kind=kind.currentData(), split=split.currentData())
            payload.update(gender=gender.currentData(), age_group=age.currentData(),
                           tags=[tag.strip() for tag in tags.text().split(",") if tag.strip()])
            self.mutate(f"/api/samples/{row['id']}", "PATCH", json=payload)

    def delete(self):
        identifiers = list(self.selected)
        if identifiers and self.confirm(f"软删除所选 {len(identifiers)} 条样本？"):
            self.request("/api/samples/delete", "POST", json={"ids": identifiers},
                         callback=lambda result: self.after_delete(identifiers))

    def after_delete(self, identifiers):
        self.deleted_ids = identifiers
        self.selected.difference_update(identifiers)
        self.notify("已软删除，可撤销最近删除")
        self.refresh()

    def restore(self):
        identifiers = self.deleted_ids[:]
        if not identifiers:
            self.notify("没有本次会话的删除记录")
            return
        def operation():
            for identifier in identifiers:
                self.shell.runtime.call(f"/api/samples/{identifier}/restore", "POST")
        self.shell.submit(operation, lambda result: (self.deleted_ids.clear(), self.refresh(), self.notify("已恢复")))
