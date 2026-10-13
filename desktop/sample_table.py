"""Sample selection uses one row state, with no separate checkbox cell."""
from PySide6.QtCore import Qt, QRect, QPointF, QSize
from PySide6.QtGui import QColor, QPen, QTextLayout, QTextOption
from PySide6.QtWidgets import (QTableWidget, QAbstractItemView, QHeaderView,
                               QStyledItemDelegate, QStyle, QStyleOptionViewItem)


class SampleDelegate(QStyledItemDelegate):
    @staticmethod
    def checkbox_rect(rect):
        return QRect(rect.left() + 12, rect.center().y() - 8, 16, 16)

    @staticmethod
    def text_layout(text, font, width):
        layout = QTextLayout(text, font)
        options = QTextOption()
        options.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(options)
        layout.beginLayout()
        height = 0
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(max(1, width))
            line.setPosition(QPointF(0, height))
            height += line.height()
        layout.endLayout()
        return layout, height

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        option.features &= ~QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator
        # Stylesheet checkbox painting and the native indicator hit rectangle
        # disagreed in beta.5. A native focus panel also occupied the remaining
        # checkbox cell. Selection now belongs to the row, not the current cell;
        # never ask Qt to draw or hit-test a second checkbox/focus panel.
        option.state &= ~(QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_HasFocus)
        checked = index.siblingAtColumn(0).data(Qt.ItemDataRole.CheckStateRole)
        if checked == Qt.CheckState.Checked.value:
            option.state |= QStyle.StateFlag.State_Selected

    def paint(self, painter, option, index):
        if index.column() != 0:
            super().paint(painter, option, index)
            return
        styled = QStyleOptionViewItem(option)
        self.initStyleOption(styled, index)
        checked = bool(styled.state & QStyle.StateFlag.State_Selected)
        painter.save()
        painter.setClipRect(styled.rect)
        painter.setRenderHint(painter.RenderHint.Antialiasing)
        painter.fillRect(styled.rect, QColor("#eeedff" if checked else "white"))
        indicator = self.checkbox_rect(styled.rect)
        painter.setPen(QPen(QColor("#6563df" if checked else "#bac3d2"), 1))
        painter.setBrush(QColor("#6563df" if checked else "white"))
        painter.drawRoundedRect(indicator, 3, 3)
        if checked:
            painter.setPen(QPen(QColor("white"), 1.7))
            painter.drawLine(indicator.left() + 3, indicator.top() + 8, indicator.left() + 7, indicator.top() + 12)
            painter.drawLine(indicator.left() + 7, indicator.top() + 12, indicator.left() + 13, indicator.top() + 4)
        # The same layout/width is used for row height and drawing, including
        # checked/focused states. Plain text cannot turn sample content into HTML.
        layout, height = self.text_layout(styled.text, styled.font, styled.rect.width() - 50)
        painter.setPen(QColor("#383691" if checked else "#26334a"))
        layout.draw(painter, QPointF(styled.rect.left() + 38, styled.rect.top() + (styled.rect.height() - height) / 2))
        painter.restore()

    def sizeHint(self, option, index):
        if index.column() != 0:
            return super().sizeHint(option, index)
        styled = QStyleOptionViewItem(option)
        self.initStyleOption(styled, index)
        width = self.parent().columnWidth(0)
        _, height = self.text_layout(styled.text, styled.font, width - 50)
        return QSize(width, max(44, int(height + 16)))

    def editorEvent(self, event, model, option, index):
        return False  # Mouse/space toggling is handled exactly once by the table.


class SampleTable(QTableWidget):
    def __init__(self):
        super().__init__(0, 6)
        self.setHorizontalHeaderLabels(["文本", "来源 / 音色", "类型", "审核", "时长", "划分"])
        self.setItemDelegate(SampleDelegate(self))
        self.verticalHeader().hide()
        self.verticalHeader().setMinimumSectionSize(44)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for column, width in ((2, 60), (3, 80), (4, 70), (5, 80)):
            self.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            self.setColumnWidth(column, width)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setWordWrap(True)
        self.setShowGrid(False)
        self.pressed_identifier = None
        self.horizontalHeader().sectionResized.connect(lambda *args: self.resizeRowsToContents())
        self.model().dataChanged.connect(self.refresh_checked_rows)

    def refresh_checked_rows(self, top_left, bottom_right, roles):
        if top_left.column() != 0 or (roles and Qt.ItemDataRole.CheckStateRole not in roles):
            return
        # Every cell derives its background from column 0, but Qt invalidates
        # only the changed cell. Without this dependency invalidation the text
        # cell turns purple first, and the other columns wait for an unrelated
        # paint seconds later. Queue the visible row region for the same next
        # paint; do not use repaint(), timers, network refresh or a whole-table
        # redraw. Model-level handling also covers space/double-click/API updates
        # and remains active while SamplesPage blocks itemChanged on refresh.
        first = self.rowViewportPosition(top_left.row())
        last = self.rowViewportPosition(bottom_right.row()) + self.rowHeight(bottom_right.row())
        region = QRect(0, first, self.viewport().width(), last - first).intersected(self.viewport().rect())
        if not region.isEmpty():
            self.viewport().update(region)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        index = self.indexAt(event.position().toPoint())
        self.pressed_identifier = None
        if index.isValid():
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self.setCurrentCell(index.row(), 0)
            self.pressed_identifier = self.item(index.row(), 0).data(Qt.ItemDataRole.UserRole)
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            super().mouseReleaseEvent(event)
            return
        identifier, self.pressed_identifier = self.pressed_identifier, None
        index = self.indexAt(event.position().toPoint())
        if identifier is not None and index.isValid():
            item = self.item(index.row(), 0)
            # Compare stable IDs, not a cached row/item pointer: a background
            # refresh may replace/reorder rows between the press and release.
            if item.data(Qt.ItemDataRole.UserRole) == identifier:
                item.setCheckState(Qt.CheckState.Unchecked if item.checkState() == Qt.CheckState.Checked else Qt.CheckState.Checked)
        event.accept()

    def mouseDoubleClickEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            super().mouseDoubleClickEvent(event)
            return
        self.pressed_identifier = None
        index = self.indexAt(event.position().toPoint())
        if index.isValid():
            self.setCurrentCell(index.row(), 0)
            # The first click already toggled. Double-click selects the sample
            # for playback and must not undo that choice on its second release.
            self.item(index.row(), 0).setCheckState(Qt.CheckState.Checked)
            self.cellDoubleClicked.emit(index.row(), index.column())
        event.accept()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Space and self.currentRow() >= 0:
            item = self.item(self.currentRow(), 0)
            item.setCheckState(Qt.CheckState.Unchecked if item.checkState() == Qt.CheckState.Checked else Qt.CheckState.Checked)
            event.accept()
            return
        super().keyPressEvent(event)
