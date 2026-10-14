"""Native model-backed combo popups placed before their first visible frame."""
from PySide6.QtCore import Qt, QPoint, QRect, QEvent
from PySide6.QtWidgets import (QComboBox, QFrame, QVBoxLayout, QListView,
                               QStyledItemDelegate, QSizePolicy, QStyleOptionComboBox, QStyle, QApplication)


class ComboPopup(QFrame):
    def __init__(self, owner):
        super().__init__(owner, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.owner = owner
        self.setObjectName('comboPopup')
        self.setStyleSheet('QFrame#comboPopup {background:white; border:1px solid #dce2ee; border-radius:8px;}')

    def mousePressEvent(self, event):
        # Qt replays an outside click after dismissing a popup. A click on a
        # different combo should open that combo immediately; a click on this
        # combo should merely close it instead of reopening on replay.
        local = self.owner.mapFromGlobal(event.globalPosition().toPoint())
        self.setAttribute(Qt.WidgetAttribute.WA_NoMouseReplay, self.owner.rect().contains(local))
        super().mousePressEvent(event)


class AnchoredComboBox(QComboBox):
    def __init__(self):
        super().__init__()
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(7)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMaxVisibleItems(8)
        # Never call QComboBox.showPopup then reparent its private container:
        # Qt has already started showing/animating the old container by then.
        # On Windows that painted option fragments over the original control,
        # and reparenting showed it a second time. Own one stable native popup
        # with a standard QListView sharing the combo's authoritative model.
        self.popup_window = ComboPopup(self)
        layout = QVBoxLayout(self.popup_window)
        layout.setContentsMargins(5, 5, 5, 5)
        self.popup_view = QListView(self.popup_window)
        # QAbstractScrollArea's generic ~70px minimum hint would expand a
        # one-row popup after positioning and could cross the screen edge.
        # This popup's size is explicitly derived from its rows, not that hint.
        self.popup_view.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.popup_view.setProperty('comboPopupView', True)
        self.popup_view.setStyleSheet('QListView {background:white; color:#26334a; border:0; padding:0; outline:0; selection-background-color:#eeedff; selection-color:#5245bc;} QListView::item {min-height:24px; padding:6px 10px; border:0; border-radius:5px;} QListView::item:hover {background:#f5f4ff;} QListView::item:selected {background:#eeedff; color:#5245bc;}')
        self.popup_view.setItemDelegate(QStyledItemDelegate(self.popup_view))
        self.popup_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.popup_view.setUniformItemSizes(True)
        self.popup_view.setMouseTracking(True)
        self.popup_view.setModel(self.model())
        self.popup_view.clicked.connect(self.choose)
        self.popup_view.installEventFilter(self)
        layout.addWidget(self.popup_view)

    def view(self):
        return self.popup_view if hasattr(self, 'popup_view') else super().view()

    def required_text_width(self):
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        rect = self.style().subControlRect(QStyle.ComplexControl.CC_ComboBox, option,
                                          QStyle.SubControl.SC_ComboBoxEditField, self)
        text = max((self.fontMetrics().horizontalAdvance(self.itemText(i)) for i in range(self.count())), default=0)
        return text + self.width()-rect.width()+4

    def choose(self, index):
        if not index.isValid() or not index.flags() & Qt.ItemFlag.ItemIsEnabled:
            return
        row = index.row()
        self.hidePopup()
        self.setCurrentIndex(row)
        self.setFocus(Qt.FocusReason.PopupFocusReason)
        # setCurrentIndex emits change signals, not user-activation signals.
        # Preserve both contracts, once, including re-selecting the same row.
        self.activated.emit(row)
        self.textActivated.emit(self.currentText())

    def eventFilter(self, watched, event):
        if watched is self.popup_view and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.choose(self.popup_view.currentIndex())
                return True
            if key in (Qt.Key.Key_Escape, Qt.Key.Key_F4):
                self.hidePopup()
                self.setFocus(Qt.FocusReason.PopupFocusReason)
                return True
            if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
                self.hidePopup()
                self.setFocus(Qt.FocusReason.PopupFocusReason)
                self.focusNextPrevChild(key == Qt.Key.Key_Tab)
                return True
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if self.rect().contains(event.position().toPoint()):
                self.hidePopup() if self.popup_window.isVisible() else self.showPopup()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def showPopup(self):
        if not self.count() or not self.isEnabled():
            return
        active = QApplication.activePopupWidget()
        if isinstance(active, ComboPopup) and active is not self.popup_window:
            active.hide()  # One combo list at a time, including direct UI actions.
        view = self.popup_view
        view.setModel(self.model())
        view.setRootIndex(self.rootModelIndex())
        view.setModelColumn(self.modelColumn())
        view.doItemsLayout()
        index = self.model().index(self.currentIndex(), self.modelColumn(), self.rootModelIndex())
        view.setCurrentIndex(index)
        screen = self.screen().availableGeometry()
        below = self.mapToGlobal(QPoint(0, self.height()+4))
        top = self.mapToGlobal(QPoint(0, 0)).y()
        height = min(max(32, view.sizeHintForRow(0))*min(self.count(), self.maxVisibleItems())+10, screen.height())
        y = below.y() if below.y()+height <= screen.bottom()+1 else top-height-4
        y = max(screen.top(), min(y, screen.bottom()-height+1))
        width = min(self.width(), screen.width())
        x = max(screen.left(), min(below.x(), screen.right()-width+1))
        # Set geometry, model selection and scroll position while hidden. The
        # first Show/Paint already has final bounds; no native popup animation
        # or visible reparent/resize can flash options through the combo below.
        self.popup_window.setAttribute(Qt.WidgetAttribute.WA_NoMouseReplay, False)
        self.popup_window.setGeometry(QRect(x, y, width, height))
        self.popup_window.layout().activate()
        view.scrollTo(index)
        self.popup_window.show()
        view.setFocus(Qt.FocusReason.PopupFocusReason)

    def hidePopup(self):
        if hasattr(self, 'popup_window'):
            self.popup_window.hide()
        super().hidePopup()

    def hideEvent(self, event):
        self.hidePopup()
        super().hideEvent(event)
