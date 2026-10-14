"""Route wheel gestures over editable controls to the containing page."""
from PySide6.QtCore import QObject, QEvent, QCoreApplication
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import (QApplication, QWidget, QComboBox, QAbstractSpinBox,
                               QLineEdit, QPlainTextEdit, QTextEdit, QScrollArea)


class FormWheelGuard(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        QApplication.instance().installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() != QEvent.Type.Wheel or not isinstance(watched, QWidget):
            return False
        owner = watched
        while owner and owner is not self.window:
            owner = owner.parentWidget()
        # QWidget.isAncestorOf stops at top-level dialog boundaries, even when
        # the QObject/widget parent is this shell. Walk ownership explicitly so
        # dynamically opened metadata dialogs receive the same input protection.
        if owner is None:
            return False
        control = watched
        while control and control is not self.window:
            if isinstance(control, (QComboBox, QAbstractSpinBox, QLineEdit)):
                break
            if isinstance(control, (QPlainTextEdit, QTextEdit)):
                if control.isReadOnly():
                    return False  # Task logs are readers, not editable form fields.
                break
            control = control.parentWidget()
        else:
            return False
        # Focus-only policies still change a focused spin/combo while the user
        # scrolls the page. Catch descendants too (spin line edit/text viewport),
        # including controls created later in dialogs or after a dataset switch.
        # Consume the original gesture exactly once and send a mapped copy to
        # the outer page viewport; swallowing it alone creates scrolling traps.
        page = control.parentWidget()
        while page and not isinstance(page, QScrollArea):
            page = page.parentWidget()
        if page:
            viewport = page.viewport()
            if not event.pixelDelta().isNull():
                # Qt's QScrollArea/scrollbar path ignored pixel-only gestures
                # in the Windows regression. Apply their already-oriented pixel
                # deltas once; do not convert them into lossy 120-unit notches.
                delta = event.pixelDelta()
                page.verticalScrollBar().setValue(page.verticalScrollBar().value() - delta.y())
                page.horizontalScrollBar().setValue(page.horizontalScrollBar().value() - delta.x())
            else:
                forwarded = QWheelEvent(viewport.mapFromGlobal(event.globalPosition()),
                                        event.globalPosition(), event.pixelDelta(), event.angleDelta(),
                                        event.buttons(), event.modifiers(), event.phase(), event.inverted(),
                                        event.source(), event.pointingDevice())
                QCoreApplication.sendEvent(viewport, forwarded)
        # A standalone dialog has no page to scroll, so its input remains
        # unchanged. Explicit arrows, typing and keyboard navigation still work.
        event.accept()
        return True
