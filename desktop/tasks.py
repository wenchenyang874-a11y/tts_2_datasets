"""Task hierarchy, actual timings, explicit retries and honest cost display."""
import json
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem, QPlainTextEdit, QDialog, QVBoxLayout
from .base import Page

STATUS = {"queued": "排队", "running": "执行中", "completed": "已完成", "done": "已完成",
          "failed": "失败", "partial": "部分失败", "cancelled": "已取消"}


class TasksPage(Page):
    def __init__(self, shell):
        super().__init__(shell, "任务中心")
        self.loading = False
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["任务", "状态", "进度", "排队 / 执行 / 总耗时", "预估费用"])
        self.tree.setColumnWidth(0, 290)
        self.tree.setColumnWidth(1, 100)
        self.tree.setColumnWidth(2, 110)
        self.tree.setColumnWidth(3, 210)
        self.tree.setMinimumHeight(450)
        self.tree.itemDoubleClicked.connect(lambda item, column: self.details())
        self.actions(("刷新", self.refresh, False), ("查看明细", self.details, False), ("显式重试", self.retry, False))
        self.layout.addWidget(self.tree)
        self.layout.addStretch()

    def refresh(self):
        if self.loading:
            return
        self.loading = True
        self.request("/api/tasks", params={"limit": 500}, callback=self.show_tasks, error=self.failed)

    def failed(self, error):
        self.loading = False
        self.shell.set_status(str(error), True)

    def show_tasks(self, rows):
        self.loading = False
        current = self.tree.currentItem()
        identifier = current.data(0, Qt.ItemDataRole.UserRole)["id"] if current else None
        expanded = {self.tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)["id"]
                    for i in range(self.tree.topLevelItemCount()) if self.tree.topLevelItem(i).isExpanded()}
        self.tree.clear()
        for row in rows:
            parent = self.item(row)
            self.tree.addTopLevelItem(parent)
            parent.setExpanded(row["id"] in expanded)
            if row["id"] == identifier:
                self.tree.setCurrentItem(parent)
            for child in row.get("children", []):
                item = self.item(child)
                parent.addChild(item)
                if child["id"] == identifier:
                    self.tree.setCurrentItem(item)

    def item(self, row):
        payload = row.get("payload") or {}
        name = str(payload.get("text") or row.get("type") or row["id"])
        timings = [row.get(key + "_duration_seconds") for key in ("queue", "execution", "total")]
        duration = " / ".join("—" if value is None else f"{value:.1f}s" for value in timings)
        cost = row.get("cost_by_currency", {})
        if cost:
            amount = " · ".join(f"{currency} {value}" for currency, value in cost.items())
        else:
            amount = "未配置" if not row.get("cost_available") else "本地处理"
        item = QTreeWidgetItem([name[:55], STATUS.get(row["status"], row["status"]),
                               f"{row.get('completed', 0)} / {row.get('total', 0)}", duration, amount])
        item.setData(0, Qt.ItemDataRole.UserRole, row)
        item.setToolTip(0, name)
        item.setToolTip(1, str(row.get("message", "")))
        item.setToolTip(4, "预估费用，最终金额以供应商账单为准。未配置价格不表示免费。")
        return item

    def selected_row(self):
        item = self.tree.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else None

    def details(self):
        row = self.selected_row()
        if row:
            dialog = QDialog(self)
            dialog.setWindowTitle("任务明细")
            dialog.resize(720, 520)
            layout = QVBoxLayout(dialog)
            text = QPlainTextEdit(json.dumps(row, ensure_ascii=False, indent=2))
            text.setReadOnly(True)
            layout.addWidget(text)
            dialog.exec()

    def retry(self):
        row = self.selected_row()
        if not row:
            return
        if row["status"] in ("running", "queued"):
            self.shell.error("任务尚未结束")
            return
        if self.confirm("将创建一个新任务。云合成可能再次计费，超时不能证明原调用未执行。确认重试？"):
            self.request(f"/api/tasks/{row['id']}/retry", "POST", json={"cost_acknowledged": True},
                         callback=lambda result: (self.notify("重试任务已创建"), self.refresh()))
