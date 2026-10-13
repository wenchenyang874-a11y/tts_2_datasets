"""Shared native controls and visual tokens; explanatory text lives in help."""
from __future__ import annotations
from pathlib import Path

from PySide6.QtCore import Qt, QPointF, QEvent, QTimer, QPoint
from PySide6.QtGui import QPainter, QPen, QColor
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                               QComboBox, QSpinBox, QDoubleSpinBox, QTableWidget,
                               QHeaderView, QToolTip, QFrame)

STYLE = """
QWidget { font-family: 'Segoe UI', 'Microsoft YaHei UI'; font-size: 14px; color: #26334a; }
QMainWindow, QWidget#canvas, QWidget#page { background: #f4f6fb; }
QWidget#sidebar { background: #ffffff; border-right: 1px solid #e6eaf3; }
QWidget#sidebar QLabel { color: #6155cc; background: transparent; }
QWidget#sidebar QPushButton { color: #68758b; background: transparent; border: 0; text-align: left; padding: 13px 18px; border-radius: 8px; }
QWidget#sidebar QPushButton:hover { background: #f3f2fd; color: #5245bc; }
QWidget#sidebar QPushButton:checked { background: #efedff; color: #5245bc; font-weight: 600; }
QLabel#brand { font-size: 20px; font-weight: 700; }
QLabel#title { font-size: 24px; font-weight: 700; color: #17263d; }
QLabel#section { font-size: 15px; font-weight: 600; }
QLabel#muted { color: #7a8497; }
QPushButton { background: white; border: 1px solid #dce2ee; border-radius: 7px; padding: 8px 14px; }
QPushButton:hover { background: #f0f1fe; border-color: #a6a4e8; }
QPushButton:pressed { background: #e3e4ff; }
QPushButton#primary { background: #6563df; color: white; border: 0; font-weight: 600; }
QPushButton#primary:hover { background: #5553c9; }
QPushButton:disabled { color: #a4acbb; background: #eef0f5; }
QPushButton#help { border: 1px solid #a0a9bd; border-radius: 10px; padding: 0; color: #7a8497; background: transparent; font-size: 12px; }
QLineEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox { background: white; border: 1px solid #dce2ee; border-radius: 6px; padding: 7px; selection-background-color: #6563df; }
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus { border-color: #6563df; }
QComboBox::drop-down { border: 0; width: 24px; }
QComboBox::down-arrow { image: url("__ASSETS__/chevron.svg"); width: 14px; height: 14px; }
QSpinBox::up-button, QDoubleSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::down-button { border: 0; width: 18px; background: #f8f9fc; }
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow { image: url("__ASSETS__/chevron-up.svg"); width: 12px; height: 12px; }
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow { image: url("__ASSETS__/chevron.svg"); width: 12px; height: 12px; }
QGroupBox { background: white; border: 1px solid #e1e6f0; border-radius: 10px; margin-top: 14px; padding: 18px 14px 10px; }
QGroupBox::title { subcontrol-origin: margin; left: 16px; padding: 0 5px; font-weight: 600; }
QTableWidget, QListWidget, QTreeWidget { background: white; border: 1px solid #e1e6f0; border-radius: 8px; outline: 0; gridline-color: #eef0f5; }
QTableWidget::item, QListWidget::item, QTreeWidget::item { padding: 8px; }
QTableWidget::item:selected, QListWidget::item:selected, QTreeWidget::item:selected { background: #eeedff; color: #383691; }
QHeaderView::section { background: #f8f9fc; color: #768196; border: 0; border-bottom: 1px solid #e6eaf2; padding: 10px; font-weight: 600; }
QScrollArea { border: 0; background: transparent; }
QScrollBar:vertical { background: transparent; width: 10px; }
QScrollBar::handle:vertical { background: #c9cfdd; border-radius: 5px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QToolTip { background: #25334b; color: white; border: 0; padding: 9px; }
QTabWidget::pane { border: 1px solid #e1e6f0; background: white; border-radius: 8px; padding: 8px; }
QTabBar::tab { background: transparent; border: 0; padding: 9px 18px; margin-right: 4px; border-top-left-radius: 7px; border-top-right-radius: 7px; }
QTabBar::tab:selected { background: #efedff; color: #5245bc; font-weight: 600; }
QFrame#card { background: white; border: 1px solid #e6eaf3; border-radius: 12px; }
QPushButton#disclosure { background: transparent; border: 0; text-align: left; padding: 8px 0; color: #68758b; }
QLabel#batchSummary { background: #eeecfa; color: #6155a0; border-radius: 8px; padding: 12px 16px; }
QLabel#errorBanner { color: #b54050; }
QLabel#formStatus { color: #68758b; padding: 4px 0; }
QLabel#projectChip { color: #6f648f; background: #eeecfa; border-radius: 6px; padding: 6px 12px; font-size: 12px; }
QCheckBox { spacing: 8px; }
QCheckBox::indicator { width: 17px; height: 17px; }
QProgressBar { border: 0; background: #e8ebf4; border-radius: 5px; height: 12px; text-align: center; }
QProgressBar::chunk { background: #6563df; border-radius: 5px; }
"""
STYLE = STYLE.replace("__ASSETS__", (Path(__file__).resolve().parent / "assets").as_posix())


class HelpButton(QPushButton):
    def __init__(self, explanation: str):
        super().__init__("?")
        self.setObjectName("help")
        self.setFixedSize(20, 20)
        self.setToolTip(explanation)
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips)
        self.setAccessibleName("帮助：" + explanation)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.popup = None
        self.hovered = False
        self.hover_timer = QTimer(self)
        self.hover_timer.setSingleShot(True)
        self.hover_timer.setInterval(180)
        self.hover_timer.timeout.connect(self.show_help)

    def show_help(self):
        if not self.isVisible() or not (self.hovered or self.hasFocus()):
            return
        # Do not rely only on QEvent.ToolTip: Windows did not deliver that
        # delayed event consistently, while the old smoke manually injected it
        # and falsely passed. Enter/hover and keyboard focus drive an owned
        # non-activating Qt tooltip window, with plain text and bounded width.
        if self.popup is None:
            self.popup = QFrame(self, Qt.WindowType.ToolTip)
            self.popup.setObjectName("helpPopup")
            self.popup.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            self.popup.setStyleSheet("QFrame#helpPopup {background:#25334b; border:1px solid #52617a; border-radius:5px;} QLabel {color:white; background:transparent;}")
            layout = QVBoxLayout(self.popup)
            layout.setContentsMargins(12, 10, 12, 10)
            label = QLabel(self.toolTip())
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            label.setFixedWidth(330)
            layout.addWidget(label)
        self.popup.adjustSize()
        position = self.mapToGlobal(QPoint(0, self.height() + 5))
        screen = self.screen().availableGeometry()
        position.setX(max(screen.left(), min(position.x(), screen.right() - self.popup.width())))
        position.setY(max(screen.top(), min(position.y(), screen.bottom() - self.popup.height())))
        self.popup.move(position)
        self.popup.show()

    def hide_help(self):
        self.hover_timer.stop()
        if self.popup:
            self.popup.hide()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.show_help()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.hide_help()

    def event(self, event):
        if not hasattr(self, "hover_timer"):
            return super().event(event)
        if event.type() in (QEvent.Type.Enter, QEvent.Type.HoverEnter):
            self.hovered = True
            self.hover_timer.start()
        elif event.type() in (QEvent.Type.Leave, QEvent.Type.HoverLeave):
            self.hovered = False
            self.hide_help()
        elif event.type() in (QEvent.Type.Hide, QEvent.Type.WindowDeactivate):
            self.hide_help()
        elif event.type() == QEvent.Type.ToolTip:
            self.show_help()
            return True
        return super().event(event)


def field(label: str, widget: QWidget, explanation: str = "") -> QWidget:
    container = QWidget()
    layout = QVBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    row = QHBoxLayout()
    row.addWidget(QLabel(label))
    if explanation:
        row.addWidget(HelpButton(explanation))
    row.addStretch()
    layout.addLayout(row)
    layout.addWidget(widget)
    return container


class Card(QFrame):
    def __init__(self, title=""):
        super().__init__()
        self.setObjectName("card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(20, 18, 20, 18)
        self.body.setSpacing(14)
        if title:
            heading = QLabel(title)
            heading.setObjectName("section")
            self.body.addWidget(heading)


class Disclosure(QWidget):
    """A short optional section, physically collapsed rather than disabled."""
    def __init__(self, title):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.toggle = QPushButton(title + "  ▸")
        self.toggle.setObjectName("disclosure")
        self.toggle.setCheckable(True)
        self.content = QWidget()
        self.body = QVBoxLayout(self.content)
        self.body.setContentsMargins(0, 8, 0, 4)
        self.body.setSpacing(12)
        self.toggle.toggled.connect(lambda visible: (self.content.setVisible(visible), self.toggle.setText(title + ("  ▾" if visible else "  ▸"))))
        layout.addWidget(self.toggle)
        layout.addWidget(self.content)
        self.content.hide()


def button(text, callback, primary=False):
    item = QPushButton(text)
    if primary:
        item.setObjectName("primary")
    item.clicked.connect(callback)
    return item


def combo(options):
    item = QComboBox()
    for label, value in options:
        item.addItem(label, value)
    return item


def spin(minimum, maximum, value, decimal=False):
    item = QDoubleSpinBox() if decimal else QSpinBox()
    item.setRange(minimum, maximum)
    item.setValue(value)
    if decimal:
        item.setDecimals(2)
        item.setSingleStep(0.05)
    return item


def table(headers):
    item = QTableWidget(0, len(headers))
    item.setHorizontalHeaderLabels(headers)
    item.verticalHeader().hide()
    item.verticalHeader().setDefaultSectionSize(43)
    item.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    item.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    item.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    item.setShowGrid(False)
    return item


class Plot(QWidget):
    def __init__(self, title: str):
        super().__init__()
        self.title = title
        self.values = []
        self.setMinimumHeight(115)

    def set_values(self, values):
        self.values = list(values)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#ffffff"))
        painter.setPen(QColor("#7a8497"))
        painter.drawText(12, 20, self.title)
        if len(self.values) < 2:
            return
        signed = self.title == "波形"
        minimum = min(self.values)
        values = self.values if signed else [float(v) - minimum for v in self.values]
        maximum = max(max(abs(float(v)) for v in values), 0.001)
        base = self.height() / 2 + 10 if signed else self.height() - 14
        scale = (self.height() - 44) / (2 if signed else 1) / maximum
        painter.setPen(QPen(QColor("#7774e5"), 1.5))
        last = None
        for index, value in enumerate(values):
            point = QPointF(12 + index * (self.width() - 24) / (len(self.values) - 1), base - float(value) * scale)
            if last is not None:
                painter.drawLine(last, point)
            last = point
