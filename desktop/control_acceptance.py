"""Offline acceptance of shared controls in their actual native page hosts."""
import json
import time
from PySide6.QtCore import QObject, QEvent, Qt
from PySide6.QtWidgets import QStyleOptionComboBox, QStyle
from PySide6.QtTest import QTest
from .widgets import AnchoredComboBox, HelpButton


def verify_controls(application, window, destination, idle):
    generation = window.pages[0]
    window.navigate(0)
    idle(window)
    generation.editor_tabs.setCurrentIndex(0)
    QTest.mouseClick(generation.texts.viewport(), Qt.MouseButton.LeftButton)
    for index in (2, 3, 0, 2):
        QTest.mouseClick(generation.editor_tabs.tabBar(), Qt.MouseButton.LeftButton,
                         pos=generation.editor_tabs.tabBar().tabRect(index).center())
        application.processEvents()
        assert not any(help.popup and help.popup.isVisible() for help in generation.findChildren(HelpButton))
        if index in (2, 3):
            window.screen().grabWindow(window.winId()).save(str(destination / f"tab-{index}-without-stuck-help.png"))
    generation.editor_tabs.setCurrentIndex(0)

    evidence = []
    class Tracker(QObject):
        def eventFilter(self, watched, event):
            if event.type() in (QEvent.Type.Show, QEvent.Type.ParentChange):
                states.append((event.type().name, [watched.x(), watched.y(), watched.width(), watched.height()]))
            return False
    for page_index in range(len(window.pages)):
        window.navigate(page_index)
        idle(window)
        tabs = range(4) if page_index == 0 else (None,)
        for tab in tabs:
            if tab is not None:
                generation.editor_tabs.setCurrentIndex(tab)
                application.processEvents()
            for control in window.pages[page_index].findChildren(AnchoredComboBox):
                if not control.isVisible() or not control.isEnabled() or not control.count():
                    continue
                window.stack.currentWidget().ensureWidgetVisible(control)
                application.processEvents()
                text, selected = control.currentText(), control.currentIndex()
                popup = control.view().window()
                states = []
                tracker = Tracker(control)
                popup.installEventFilter(tracker)
                try:
                    for repeat in range(2):
                        states.clear()
                        QTest.mouseClick(control, Qt.MouseButton.LeftButton)
                        application.processEvents()
                        assert popup.isVisible() and len(states) == 1 and states[0][0] == 'Show', states
                        assert states[0][1] == [popup.x(), popup.y(), popup.width(), popup.height()]
                        assert control.currentText() == text and control.currentIndex() == selected
                        assert popup.width() == control.width()
                        assert popup.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
                        assert popup.windowFlags() & Qt.WindowType.NoDropShadowWindowHint
                        # Capture presented frames at the same scale as the user's
                        # GIF, not QWidget.grab() which would force a repaint and
                        # hide a transient private-container animation defect.
                        if repeat == 0 and page_index == 0 and tab == 0:
                            for frame in range(3):
                                application.processEvents()
                                window.screen().grabWindow(0, window.x(), window.y(), window.width(), window.height()).save(
                                    str(destination / f"popup-{len(evidence)}-frame-{frame}.png"))
                                time.sleep(.04)
                        # Pixel-alpha verification forces a render, so run it
                        # only AFTER the presented first-frame evidence above.
                        rendered = popup.grab().toImage()
                        assert rendered.pixelColor(rendered.width()-1, rendered.height()-1).alpha() == 0
                        evidence.append({'page': page_index, 'tab': tab, 'text': text,
                                         'repeat': repeat, 'shows': 1, 'visible_reparent': False,
                                         'first_show_geometry': list(states[0][1]), 'selection_unchanged': True})
                        QTest.keyClick(control.view(), Qt.Key.Key_Escape)
                        application.processEvents()
                        assert not popup.isVisible()
                finally:
                    popup.removeEventFilter(tracker)
                    tracker.deleteLater()
    window.navigate(0)
    generation.editor_tabs.setCurrentIndex(0)
    idle(window)
    for control in generation.filter_controls:
        option = QStyleOptionComboBox()
        control.initStyleOption(option)
        rect = control.style().subControlRect(QStyle.ComplexControl.CC_ComboBox, option,
                                             QStyle.SubControl.SC_ComboBoxEditField, control)
        longest = max(control.fontMetrics().horizontalAdvance(control.itemText(i)) for i in range(control.count()))
        assert rect.width() >= longest+2, (control.currentText(), rect.width(), longest)
    window.screen().grabWindow(window.winId()).save(str(destination / 'filter-labels-full-width.png'))
    (destination / 'shared-controls.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
