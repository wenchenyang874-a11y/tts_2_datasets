"""Native noise management and augmentation using the existing task model."""
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QLabel, QTableWidgetItem,
                               QDialog, QVBoxLayout, QLineEdit, QDialogButtonBox, QFileDialog)
from .base import Page
from .widgets import table, spin, field


class NoisesPage(Page):
    def __init__(self, shell):
        super().__init__(shell, "噪声增强")
        self.rows = []
        self.selected = set()
        self.deleted = QCheckBox("显示已删除")
        self.deleted.toggled.connect(self.refresh)
        self.layout.addWidget(self.deleted)
        self.grid = table(["选择", "噪声", "分类", "时长", "权重", "SNR 范围", "状态"])
        self.grid.setMinimumHeight(330)
        self.grid.itemChanged.connect(self.select)
        self.layout.addWidget(self.grid)
        self.actions(("导入噪声", self.upload, True), ("试听", self.play, False), ("编辑", self.edit, False),
                     ("删除 / 恢复", self.delete_restore, False), ("加入背景样本", self.materialize, False))
        self.add_playback_controls()
        row = QHBoxLayout()
        self.count = spin(1, 100, 1)
        self.seed = spin(0, 2**31 - 1, 20260804)
        row.addWidget(field("每条增强份数", self.count, "在样本库跨页勾选原音，再选择噪声。新增强输出均为未审核。"))
        row.addWidget(field("随机种子", self.seed, "相同源音频、噪声、SNR 和 seed 可复现本地增强。"))
        self.layout.addLayout(row)
        self.summary = QLabel()
        self.layout.addWidget(self.summary)
        self.actions(("增强所选样本", self.augment, True), ("选择源样本", lambda: shell.navigate(1), False))
        self.layout.addStretch()

    def refresh(self):
        self.summary.setText(f"已选源样本 {len(self.shell.selected_samples)} 条")
        self.request("/api/noises", params={"include_deleted": self.deleted.isChecked()}, callback=self.show_rows)

    def show_rows(self, rows):
        self.rows = rows
        valid = {row["id"] for row in rows if not row.get("deleted_at") and row.get("enabled")}
        self.selected.intersection_update(valid)
        self.grid.blockSignals(True)
        self.grid.setRowCount(len(rows))
        for index, row in enumerate(rows):
            check = QTableWidgetItem()
            check.setData(Qt.ItemDataRole.UserRole, row["id"])
            check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled if row["id"] in valid else Qt.ItemFlag.NoItemFlags)
            check.setCheckState(Qt.CheckState.Checked if row["id"] in self.selected else Qt.CheckState.Unchecked)
            self.grid.setItem(index, 0, check)
            values = [row["name"], row["category"], f"{row['duration']:.2f}s", row["weight"],
                      f"{row['min_snr']} ~ {row['max_snr']} dB", "已删除" if row.get("deleted_at") else "启用" if row["enabled"] else "禁用"]
            for column, value in enumerate(values, 1):
                self.grid.setItem(index, column, QTableWidgetItem(str(value)))
        self.grid.blockSignals(False)

    def select(self, item):
        if item.column() == 0:
            identifier = item.data(Qt.ItemDataRole.UserRole)
            if item.checkState() == Qt.CheckState.Checked:
                self.selected.add(identifier)
            else:
                self.selected.discard(identifier)

    def current(self):
        index = self.grid.currentRow()
        return self.rows[index] if 0 <= index < len(self.rows) else None

    def parameters(self, row=None):
        row = row or {}
        dialog = QDialog(self)
        dialog.setWindowTitle("噪声参数")
        layout = QVBoxLayout(dialog)
        widgets = {}
        for key, label, default in [("name", "名称", ""), ("category", "分类", "other"),
                                    ("source", "来源", ""), ("license", "许可", "")]:
            widget = QLineEdit(str(row.get(key) or row.get("metadata", {}).get(key) or default))
            if row and key in ("source", "license"):
                widget.setReadOnly(True)
                widget.setToolTip("导入时的来源与许可记录，当前接口不支持修改")
            layout.addWidget(field(label, widget))
            widgets[key] = widget
        for key, label, minimum, maximum, default in [("weight", "采样权重", .001, 10000, 1),
                                                      ("min_snr", "最低 SNR", -60, 100, 0),
                                                      ("max_snr", "最高 SNR", -60, 100, 20)]:
            widget = spin(minimum, maximum, row.get(key, default), True)
            layout.addWidget(field(label, widget, "SNR 越低，背景噪声相对越强。" if key == "min_snr" else ""))
            widgets[key] = widget
        enabled = QCheckBox("启用")
        enabled.setChecked(bool(row.get("enabled", True)))
        layout.addWidget(enabled)
        controls = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        controls.accepted.connect(dialog.accept)
        controls.rejected.connect(dialog.reject)
        layout.addWidget(controls)
        if not dialog.exec():
            return None
        result = {key: widget.text() if isinstance(widget, QLineEdit) else widget.value() for key, widget in widgets.items()}
        result["enabled"] = enabled.isChecked()
        return result

    def upload(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "选择噪声音频", "", "音频 (*.wav *.flac *.mp3 *.ogg *.m4a *.webm)")
        if not paths:
            return
        parameters = self.parameters()
        if parameters is None:
            return
        parameters.pop("enabled")
        def operation():
            results = []
            failures = []
            for path in paths:
                try:
                    results.append(self.shell.runtime.call("/api/noises/upload", "POST", files=[("file", path)], data=parameters))
                except Exception:
                    failures.append(Path(path).name)
            return results, failures
        self.shell.submit(operation, lambda result: (self.notify(f"已导入 {len(result[0])} 条噪声 · 失败 {len(result[1])} 条" + ("：" + "、".join(result[1]) if result[1] else "")), self.refresh()))

    def edit(self):
        row = self.current()
        if row and not row.get("deleted_at"):
            parameters = self.parameters(row)
            if parameters is not None:
                parameters.pop("source")
                parameters.pop("license")
                self.mutate(f"/api/noises/{row['id']}", "PATCH", json=parameters)

    def play(self):
        row = self.current()
        if row and not row.get("deleted_at"):
            self.shell.play_resource("noises", row["id"], row["name"])

    def delete_restore(self):
        row = self.current()
        if row and self.confirm("恢复所选噪声？" if row.get("deleted_at") else "软删除所选噪声？"):
            deleted = bool(row.get("deleted_at"))
            self.mutate(f"/api/noises/{row['id']}" + ("/restore" if deleted else ""), "POST" if deleted else "DELETE")

    def materialize(self):
        if not self.selected:
            self.shell.error("请选择噪声")
            return
        self.request("/api/noises/materialize", "POST", json={"noise_ids": sorted(self.selected), "keyword_id": self.shell.project["keyword_id"]},
                     callback=lambda result: (self.notify("背景样本任务已创建"), self.shell.navigate(4)))

    def augment(self):
        if not self.shell.selected_samples or not self.selected:
            self.shell.error("请在样本库选择源样本，并勾选可用噪声")
            return
        self.request("/api/augment", "POST", json={"sample_ids": sorted(self.shell.selected_samples),
                     "noise_ids": sorted(self.selected), "keyword_id": self.shell.project["keyword_id"],
                     "count": self.count.value(), "seed": self.seed.value()},
                     callback=lambda result: (self.notify("增强任务已创建"), self.shell.navigate(4)))
