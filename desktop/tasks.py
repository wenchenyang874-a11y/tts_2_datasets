"""Task hierarchy, actual timings, explicit retries and honest cost display."""
import json
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem, QPlainTextEdit, QDialog, QVBoxLayout, QLabel
from .base import Page
from .widgets import Card, Disclosure

STATUS = {"queued": "排队", "running": "执行中", "completed": "已完成", "done": "已完成",
          "failed": "失败", "partial": "部分失败", "cancelled": "已取消"}


class TasksPage(Page):
    def __init__(self, shell):
        super().__init__(shell, "任务中心")
        self.loading = False
        self.detail_identifier = None
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["任务", "状态", "进度", "耗时", "预估费用", "结果 / 失败原因"])
        self.tree.setColumnWidth(0, 190)
        self.tree.setColumnWidth(1, 80)
        self.tree.setColumnWidth(2, 80)
        self.tree.setColumnWidth(3, 130)
        self.tree.setMinimumHeight(280)
        self.tree.setColumnWidth(4, 110)
        self.tree.itemDoubleClicked.connect(lambda item, column: self.details())
        self.actions(("刷新", self.refresh, False), ("查看明细", self.details, False), ("重新合成", self.retry, False))
        self.layout.addWidget(self.tree, 1)
        detail = Card("任务详情")
        self.detail = QLabel("选择一个任务查看结果")
        self.detail.setTextFormat(Qt.TextFormat.PlainText)
        self.detail.setWordWrap(True)
        self.detail.setMinimumHeight(54)
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        detail.body.addWidget(self.detail)
        technical = Disclosure("技术详情")
        self.raw_detail = QPlainTextEdit()
        self.raw_detail.setReadOnly(True)
        self.raw_detail.setMaximumHeight(180)
        technical.body.addWidget(self.raw_detail)
        detail.body.addWidget(technical)
        self.layout.addWidget(detail)
        self.tree.itemSelectionChanged.connect(self.details)
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
        scroll = self.tree.verticalScrollBar().value()
        # Clearing/reselecting emits transient "no task" signals. Suppress them
        # while rebuilding so a poll cannot clear the technical document first.
        self.tree.blockSignals(True)
        self.tree.clear()
        for row in rows:
            parent = self.item(row)
            self.tree.addTopLevelItem(parent)
            parent.setExpanded(row["id"] in expanded or (identifier is None and row["status"] in {"failed", "partial"}))
            if row["id"] == identifier:
                self.tree.setCurrentItem(parent)
            for child in row.get("children", []):
                item = self.item(child)
                parent.addChild(item)
                if child["id"] == identifier:
                    self.tree.setCurrentItem(item)

        self.tree.blockSignals(False)
        self.tree.verticalScrollBar().setValue(scroll)
        self.details()

    def failure_reason(self, row):
        if row["status"] in {"failed", "partial"}:
            for child in row.get("children", []):
                if child["status"] in {"failed", "partial"}:
                    return str(child.get("message") or "失败原因未记录")
        return str(row.get("message") or "")

    def item(self, row):
        payload = row.get("payload") or {}
        name = str(payload.get("text") or {"generate_batch": "批量合成", "generate": "语音合成", "augment": "噪声增强", "export": "训练集导出"}.get(row.get("type"), row.get("type") or row["id"]))
        timings = [row.get(key + "_duration_seconds") for key in ("queue", "execution", "total")]
        duration = " / ".join("—" if value is None else f"{value:.1f}s" for value in timings)
        cost = row.get("cost_by_currency", {})
        if cost:
            amount = " · ".join(f"{currency} {value}" for currency, value in cost.items())
        else:
            amount = "未配置" if not row.get("cost_available") else "本地处理"
        item = QTreeWidgetItem([name[:55], STATUS.get(row["status"], row["status"]),
                               f"{row.get('completed', 0)} / {row.get('total', 0)}", duration, amount, self.failure_reason(row)])
        item.setData(0, Qt.ItemDataRole.UserRole, row)
        item.setToolTip(0, name)
        item.setToolTip(1, self.failure_reason(row))
        item.setToolTip(5, self.failure_reason(row))
        if row["status"] in {"failed", "partial"}:
            from PySide6.QtGui import QColor
            item.setForeground(1, QColor("#b54050"))
            item.setForeground(5, QColor("#b54050"))
        item.setToolTip(4, "预估费用，最终金额以供应商账单为准。未配置价格不表示免费。")
        return item

    def selected_row(self):
        item = self.tree.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else None

    def details(self):
        row = self.selected_row()
        if row:
            status = STATUS.get(row["status"], row["status"])
            self.detail.setText(f"{status} · 完成 {row.get('completed', 0)} / {row.get('total', 0)}\n" + self.failure_reason(row))
            # QLabel minimumSizeHint can underestimate a multiline wrapped
            # result at 150% DPI. Reserve the actual height-for-width before
            # the card layout squeezes the second line behind the disclosure.
            self.detail.setMinimumHeight(max(54, self.detail.heightForWidth(max(260, self.detail.width()))))
            self.detail.setStyleSheet("color:#b54050;" if row["status"] in {"failed", "partial"} else "")
            self.update_technical_detail(row)
        else:
            self.detail.setText("选择一个任务查看结果")
            self.detail_identifier = None
            self.raw_detail.clear()

    def update_technical_detail(self, row):
        text = json.dumps(row, ensure_ascii=False, indent=2)
        same_task = self.detail_identifier == row["id"]
        if same_task and text == self.raw_detail.toPlainText():
            return  # setPlainText resets scroll/cursor even for identical text.
        cursor = self.raw_detail.textCursor()
        position, anchor = cursor.position(), cursor.anchor()
        vertical = self.raw_detail.verticalScrollBar().value()
        horizontal = self.raw_detail.horizontalScrollBar().value()
        self.raw_detail.setPlainText(text)
        self.detail_identifier = row["id"]
        if same_task:
            # Running-task JSON changes on each poll; keep the reader's position
            # and selection too, bounded to the shortened document if necessary.
            limit = self.raw_detail.document().characterCount() - 1
            cursor = self.raw_detail.textCursor()
            cursor.setPosition(min(anchor, limit))
            cursor.setPosition(min(position, limit), cursor.MoveMode.KeepAnchor)
            self.raw_detail.setTextCursor(cursor)
            self.raw_detail.verticalScrollBar().setValue(vertical)
            self.raw_detail.horizontalScrollBar().setValue(horizontal)

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
