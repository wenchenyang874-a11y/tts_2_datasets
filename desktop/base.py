"""Page contract and asynchronous actions shared by native pages."""
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QHBoxLayout, QMessageBox
from .widgets import button


class Page(QWidget):
    def __init__(self, shell, title):
        super().__init__()
        self.setObjectName("page")
        self.shell = shell
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(18)
        heading = QLabel(title)
        heading.setObjectName("title")
        self.layout.addWidget(heading)

    def request(self, path, method="GET", callback=None, error=None, **options):
        self.shell.submit(lambda: self.shell.runtime.call(path, method, **options), callback, error)

    def actions(self, *items):
        row = QHBoxLayout()
        for text, handler, primary in items:
            row.addWidget(button(text, handler, primary))
        row.addStretch()
        self.layout.addLayout(row)
        return row

    def refresh(self):
        pass

    def confirm(self, text):
        return QMessageBox.question(self, "确认操作", text) == QMessageBox.StandardButton.Yes

    def notify(self, text):
        self.shell.set_status(text)

    def mutate(self, path, method="POST", **options):
        self.request(path, method, callback=lambda result: (self.notify("已保存"), self.refresh()), **options)
