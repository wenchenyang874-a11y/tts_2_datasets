"""Training export UI: explicit selection scope and actual artifact copying."""
from pathlib import Path
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QCheckBox, QLabel, QTableWidgetItem, QFileDialog
from .base import Page
from .widgets import field, spin, combo, table


class ExportsPage(Page):
    def __init__(self, shell):
        super().__init__(shell, "训练集导出")
        self.rows = []
        self.name = QLineEdit("kws_dataset")
        self.format = combo([("通用 JSONL", "generic"), ("JSONL 兼容", "jsonl"), ("分类目录", "folders"), ("WeKWS", "wekws")])
        self.scope = combo([("样本库所选", "selection"), ("当前唤醒词全部", "keyword")])
        row = QHBoxLayout()
        for label, widget, tip in [("名称", self.name, "仅作为导出名称，不覆盖原始音频。"),
                                   ("格式", self.format, "WeKWS 包含 <FILLER>/关键词字典、text、wav.scp、wav.dur。"),
                                   ("范围", self.scope, "所选样本保留跨页选择；当前唤醒词全部按当前项目 ID 查询。")]:
            row.addWidget(field(label, widget, tip))
        self.layout.addLayout(row)
        ratios = QHBoxLayout()
        self.train = spin(.01, .99, .8, True)
        self.dev = spin(0, .99, .1, True)
        self.seed = spin(0, 2**31-1, 20260804)
        ratios.addWidget(field("Train 比例", self.train, "同一说话人或供应商音色组不会跨划分；既有明确划分保留。"))
        ratios.addWidget(field("Dev 比例", self.dev, "Test 为剩余比例。分组粒度导致实际数量不一定等于比例。"))
        ratios.addWidget(field("随机种子", self.seed))
        self.layout.addLayout(ratios)
        self.include_audio = QCheckBox("将音频复制到导出包")
        self.include_audio.setChecked(True)
        self.layout.addWidget(field("音频文件", self.include_audio, "关闭复制时，清单引用本机数据路径，训练环境必须可读取同一路径。"))
        self.summary = QLabel()
        self.layout.addWidget(self.summary)
        self.actions(("创建导出", self.create, True), ("查看任务", lambda: shell.navigate(4), False))
        self.deleted = QCheckBox("显示已删除导出")
        self.deleted.toggled.connect(self.refresh)
        self.layout.addWidget(self.deleted)
        self.grid = table(["名称", "格式", "样本数量", "创建时间", "状态"])
        self.grid.setMinimumHeight(260)
        self.layout.addWidget(self.grid)
        self.actions(("刷新", self.refresh, False), ("另存为 ZIP", self.save, True), ("删除 / 恢复", self.delete_restore, False))
        self.layout.addStretch()

    def refresh(self):
        self.summary.setText(f"跨页已选 {len(self.shell.selected_samples)} 条 · 当前唤醒词：{self.shell.project.get('display_text', '')}")
        self.request("/api/exports", params={"include_deleted": self.deleted.isChecked()}, callback=self.show_rows)

    def show_rows(self, rows):
        self.rows = rows
        self.grid.setRowCount(len(rows))
        for index, row in enumerate(rows):
            values = [row["name"], row.get("format", ""), row.get("sample_count", ""), row["created_at"], "已删除" if row.get("deleted_at") else "可用"]
            for column, value in enumerate(values):
                self.grid.setItem(index, column, QTableWidgetItem(str(value)))

    def create(self):
        selected = self.scope.currentData() == "selection"
        if selected and not self.shell.selected_samples:
            self.shell.error("请先在样本库选择样本，或切换导出范围")
            return
        payload = {"name": self.name.text(), "format": self.format.currentData(), "include_audio": self.include_audio.isChecked(),
                   "train_ratio": self.train.value(), "dev_ratio": self.dev.value(), "seed": self.seed.value(),
                   "sample_ids": sorted(self.shell.selected_samples) if selected else [],
                   "keyword_id": "" if selected else self.shell.project["keyword_id"]}
        self.request("/api/exports", "POST", json=payload,
                     callback=lambda result: (self.notify("导出任务已创建"), self.shell.navigate(4)))

    def current(self):
        index = self.grid.currentRow()
        return self.rows[index] if 0 <= index < len(self.rows) else None

    def save(self):
        row = self.current()
        if not row or row.get("deleted_at"):
            return
        filename, _ = QFileDialog.getSaveFileName(self, "保存导出包", Path(str(row["name"])).name + ".zip", "ZIP (*.zip)")
        if filename:
            self.request(op="copy", kind="exports", id=row["id"], destination=filename,
                         path="", callback=lambda result: self.notify("导出包已保存"))

    def delete_restore(self):
        row = self.current()
        if row and self.confirm("恢复所选导出包？" if row.get("deleted_at") else "软删除所选导出包？原始样本不受影响。"):
            deleted = bool(row.get("deleted_at"))
            self.mutate(f"/api/exports/{row['id']}" + ("/restore" if deleted else ""), "POST" if deleted else "DELETE")
