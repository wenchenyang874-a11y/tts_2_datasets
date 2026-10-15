"""A bounded native composition workspace; only its content lists scroll."""
from PySide6.QtCore import Qt, QTimer, Signal, QRect, QSize
from PySide6.QtGui import QColor, QPainter, QPen, QFont, QFontMetrics
from PySide6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QGridLayout, QLabel,
    QLineEdit, QPlainTextEdit, QCheckBox, QSplitter, QTabWidget, QStackedWidget,
    QListWidget, QListWidgetItem, QStyledItemDelegate, QAbstractItemView, QStyle)
from .widgets import combo, spin, button, HelpButton, field, CheckableVoiceList
from .services import SERVICES, service_for_voice


class ExpressionEdit(QPlainTextEdit):
    def text(self):
        return self.toPlainText()

    def setText(self, text):
        self.setPlainText(text)


class BasketDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        width = max(100, self.parent().viewport().width()-50)
        font = QFont(option.font)
        font.setPixelSize(13)
        font.setBold(True)
        bounds = QFontMetrics(font).boundingRect(QRect(0, 0, width, 1000), Qt.TextFlag.TextWordWrap, index.data())
        return QSize(width+50, max(58, bounds.height()+32))

    def paint(self, painter, option, index):
        rect = option.rect
        info = index.data(Qt.ItemDataRole.UserRole+1)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setClipRect(rect)
        painter.fillRect(rect, QColor('#f1efff' if option.state & QStyle.StateFlag.State_MouseOver else 'white'))
        font = QFont(option.font)
        font.setPixelSize(13)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor('#26334a'))
        painter.drawText(QRect(rect.left()+12, rect.top()+7, rect.width()-50, rect.height()-30), Qt.TextFlag.TextWordWrap, index.data())
        font.setPixelSize(12)
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QColor('#7a8497'))
        painter.drawText(QRect(rect.left()+12, rect.bottom()-22, rect.width()-40, 18), Qt.AlignmentFlag.AlignVCenter, info['service'])
        remove = QRect(rect.right()-33, rect.top()+8, 26, 26)
        painter.setPen(QPen(QColor('#8a87a4'), 1.5))
        center = remove.center()
        painter.drawLine(center.x()-4, center.y()-4, center.x()+4, center.y()+4)
        painter.drawLine(center.x()-4, center.y()+4, center.x()+4, center.y()-4)
        painter.setPen(QColor('#f0f2f7'))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        painter.restore()


class VoiceBasket(QListWidget):
    removeRequested = Signal(object)
    locateRequested = Signal(object)

    def __init__(self):
        super().__init__()
        self.setItemDelegate(BasketDelegate(self))
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def mouseReleaseEvent(self, event):
        item = self.itemAt(event.position().toPoint())
        if item and event.button() == Qt.MouseButton.LeftButton:
            self.setCurrentItem(item)
            identifier = item.data(Qt.ItemDataRole.UserRole)
            rect = self.visualItemRect(item)
            remove = QRect(rect.right()-33, rect.top()+8, 26, 26)
            (self.removeRequested if remove.contains(event.position().toPoint()) else self.locateRequested).emit(identifier)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        item = self.currentItem()
        if item and event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.removeRequested.emit(item.data(Qt.ItemDataRole.UserRole))
            event.accept()
            return
        if item and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.locateRequested.emit(item.data(Qt.ItemDataRole.UserRole))
            event.accept()
            return
        super().keyPressEvent(event)


def pane(title):
    widget = QWidget()
    widget.setObjectName('composerPane')
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(12, 12, 12, 12)
    layout.setSpacing(8)
    label = QLabel(title)
    label.setObjectName('section')
    layout.addWidget(label)
    return widget, layout


def build_workspace(page, title, targets, ages):
    heading = QHBoxLayout()
    heading.addWidget(title)
    heading.addStretch()
    page.display_text = QLineEdit('你好，小助手')
    page.display_text.setAccessibleName('项目唤醒词')
    page.display_text.setPlaceholderText('项目唤醒词')
    page.display_text.setMaximumWidth(260)
    heading.addWidget(page.display_text)
    heading.addWidget(HelpButton('保存项目唤醒词后统一标签；已有样本不重写。'))
    heading.addWidget(button('保存唤醒词', page.save_project))
    page.layout.addLayout(heading)
    page.layout.setSpacing(12)

    workspace = QSplitter(Qt.Orientation.Horizontal)
    workspace.setHandleWidth(10)
    page.workspace = workspace
    page.body_layout = QHBoxLayout()
    page.body_layout.setContentsMargins(0, 0, 0, 0)
    page.body_layout.addWidget(workspace)
    page.layout.addLayout(page.body_layout, 1)
    editor, editor_layout = pane('创作设置')
    editor.setMinimumWidth(230)
    page.editor_tabs = QTabWidget()
    page.editor_tabs.setObjectName('composerTabs')
    editor_layout.addWidget(page.editor_tabs, 1)
    text_tab = QWidget()
    text_layout = QVBoxLayout(text_tab)
    text_layout.setContentsMargins(8, 12, 8, 8)
    text_layout.setSpacing(8)
    page.texts = QPlainTextEdit('你好，小助手')
    page.texts.setPlaceholderText('每行一条合成文本')
    page.texts.setMinimumHeight(80)
    page.texts.textChanged.connect(page.update_summary)
    text_layout.addWidget(page.texts, 1)
    page.kind, page.count, page.concurrency = combo(targets), spin(1, 1000, 1), spin(1, 16, 3)
    page.count.valueChanged.connect(page.update_summary)
    text_layout.addWidget(field('样本类型', page.kind, '负样本的训练目标为 0。'))
    amounts = QHBoxLayout()
    amounts.setSpacing(8)
    amounts.addWidget(field('份数', page.count, '每条文本 × 每个已选音色的份数。每份实际合成并入库。'))
    amounts.addWidget(field('并发', page.concurrency, '同时执行的文本任务数，仍受业务线程池限制。'))
    text_layout.addLayout(amounts)
    page.editor_tabs.addTab(text_tab, '文本')

    expression = QWidget()
    expression_layout = QVBoxLayout(expression)
    expression_layout.setContentsMargins(8, 12, 8, 8)
    page.instruction = ExpressionEdit()
    page.instruction.setPlaceholderText('例如：轻快、自然，重点强调唤醒词')
    expression_layout.addWidget(field('表达指令', page.instruction, '作用于所有已选音色；不支持的音色会在生成前提示。豆包使用自然语言，小米支持自然语言要求，阿里云部分音色需要固定格式。'), 1)
    page.emotion = combo([('默认情感', '')])
    page.emotion.currentIndexChanged.connect(page.update_expression_scale)
    expression_layout.addWidget(field('情感', page.emotion, '只列出所有已选音色共同支持的情感。'))
    page.emotion_scale = spin(1, 5, 4, True)
    page.scale_field = field('强度', page.emotion_scale, '仅用于支持豆包情感参数的音色，范围 1–5。')
    expression_layout.addWidget(page.scale_field)
    page.expression_panel = expression
    page.editor_tabs.addTab(expression, '表达')

    parameters = QWidget()
    parameters_layout = QVBoxLayout(parameters)
    parameters_layout.setContentsMargins(8, 12, 8, 8)
    parameters_layout.setSpacing(8)
    page.speed, page.pitch, page.volume = spin(.5, 2, 1, True), spin(.5, 2, 1, True), spin(0, 100, 50)
    page.random_fields = {}
    for name, label, minimum, maximum, low, high, control in [
        ('speed', '语速', .5, 2, .8, 1.2, page.speed), ('pitch', '音高', .5, 2, .9, 1.1, page.pitch),
        ('volume', '音量', 0, 100, 40, 60, page.volume)]:
        row = QHBoxLayout()
        row.addWidget(QLabel(label))
        row.addWidget(HelpButton('固定值或随机范围；只允许所有已选供应商共同支持的参数。'))
        row.addStretch()
        enable = QCheckBox('随机')
        row.addWidget(enable)
        parameters_layout.addLayout(row)
        start, end = spin(minimum, maximum, low, name != 'volume'), spin(minimum, maximum, high, name != 'volume')
        range_widget = QWidget()
        range_layout = QHBoxLayout(range_widget)
        range_layout.setContentsMargins(0, 0, 0, 0)
        range_layout.setSpacing(6)
        for widget in (start, QLabel('—'), end):
            range_layout.addWidget(widget)
        stack = QStackedWidget()
        stack.addWidget(control)
        stack.addWidget(range_widget)
        stack.setFixedHeight(38)
        enable.toggled.connect(lambda value, stack=stack: stack.setCurrentIndex(int(value)))
        enable.toggled.connect(start.setEnabled)
        enable.toggled.connect(end.setEnabled)
        start.setEnabled(False)
        end.setEnabled(False)
        parameters_layout.addWidget(stack)
        page.random_fields[name] = enable, start, end
    page.seed = spin(0, 2**31-1, 20260804)
    parameters_layout.addWidget(field('随机种子', page.seed, '控制可复现参数；相同种子不会复用已有音频。'))
    parameters_layout.addStretch()
    page.advanced = parameters
    page.editor_tabs.addTab(parameters, '参数')

    audio = QWidget()
    audio_layout = QVBoxLayout(audio)
    audio_layout.setContentsMargins(8, 12, 8, 8)
    page.audio_supplier = combo([])
    page.audio_format, page.sample_rate = combo([]), combo([])
    page.audio_supplier.currentIndexChanged.connect(page.update_audio_controls)
    page.audio_format.currentIndexChanged.connect(page.store_audio_options)
    page.sample_rate.currentIndexChanged.connect(page.store_audio_options)
    audio_layout.addWidget(field('供应商', page.audio_supplier, '逐供应商设置请求音频格式，跨厂商合成时分别使用。'))
    audio_layout.addWidget(field('请求格式', page.audio_format, '入库统一为 PCM16 WAV。'))
    audio_layout.addWidget(field('请求采样率', page.sample_rate, '入库与导出统一为 16 kHz mono。'))
    audio_layout.addStretch()
    page.editor_tabs.addTab(audio, '音频')

    browser, browser_layout = pane('音色目录')
    page.voice_pane = browser
    browser.setMinimumWidth(280)
    browser.installEventFilter(page)
    top = QHBoxLayout()
    top.setSpacing(8)
    page.provider = combo([('全部供应商', '')]+SERVICES)
    top.addWidget(page.provider, 1)
    page.catalog_button = button('获取音色', page.refresh_catalog, True)
    top.addWidget(page.catalog_button)
    page.catalog_button.setToolTip('获取音色列表；预置目录不代表账号权限。')
    browser_layout.addLayout(top)
    page.search = QLineEdit()
    page.search.setPlaceholderText('搜索音色名称或 ID')
    browser_layout.addWidget(page.search)
    page.gender = combo([('全部性别', ''), ('女', 'female'), ('男', 'male')])
    page.age = combo([('全部年龄', '')]+ages)
    page.language = combo([('全部语言', ''), ('中文', 'chinese'), ('外语', 'foreign'), ('语言未知', 'unknown')])
    page.capability_filter = combo([('全部能力', ''), ('支持指令', 'instruction'), ('支持情感', 'emotion'), ('无指令控制', 'no_instruction')])
    page.voice_kind = combo([('全部类型', ''), ('预置音色', 'preset'), ('复刻音色', 'clone'), ('设计音色', 'design'), ('其他自定义', 'custom')])
    page.filter_grid = QGridLayout()
    page.filter_grid.setSpacing(6)
    page.filter_controls = (page.voice_kind, page.language, page.gender, page.age, page.capability_filter)
    for index, control in enumerate(page.filter_controls):
        control.setMinimumContentsLength(4)
        page.filter_grid.addWidget(control, index//2, index%2)
    browser_layout.addLayout(page.filter_grid)
    page.voices = CheckableVoiceList()
    page.voices.setMinimumHeight(100)
    page.voices.itemChanged.connect(page.voice_changed)
    browser_layout.addWidget(page.voices, 1)
    actions = QHBoxLayout()
    page.visible_count = QLabel('0 个音色')
    page.visible_count.setObjectName('muted')
    actions.addWidget(page.visible_count)
    actions.addStretch()
    actions.addWidget(button('全选当前', page.select_all))
    actions.addWidget(button('清空已选', page.clear_voices))
    browser_layout.addLayout(actions)
    page.provider.currentIndexChanged.connect(page.provider_changed)
    for control in page.filter_controls:
        control.currentIndexChanged.connect(page.selection_filter_changed)
    page.search.textChanged.connect(page.refresh_voices)

    selected, selected_layout = pane('已选音色')
    selected.setMinimumWidth(180)
    page.selected_count = QLabel('0 个音色')
    page.selected_count.setObjectName('muted')
    selected_layout.addWidget(page.selected_count)
    page.chosen_voices = VoiceBasket()
    page.chosen_voices.setMinimumHeight(80)
    page.chosen_voices.removeRequested.connect(page.remove_voice)
    page.chosen_voices.locateRequested.connect(page.locate_voice)
    selected_layout.addWidget(page.chosen_voices, 1)
    page.selection_timer = QTimer(page)
    page.selection_timer.setSingleShot(True)
    page.selection_timer.timeout.connect(page.render_selected)
    for widget in (editor, browser, selected):
        workspace.addWidget(widget)
    workspace.setChildrenCollapsible(False)
    workspace.setSizes([280, 540, 210])
    workspace.setStretchFactor(1, 1)

    footer = QWidget()
    footer.setObjectName('composerFooter')
    footer_layout = QVBoxLayout(footer)
    footer_layout.setContentsMargins(14, 10, 14, 10)
    footer_layout.setSpacing(6)
    issue = QHBoxLayout()
    page.problem = QLabel()
    page.problem.setWordWrap(True)
    page.problem.setTextFormat(Qt.TextFormat.PlainText)
    page.problem.setObjectName('errorBanner')
    page.problem.hide()
    page.configure_button = button('去配置', page.open_settings)
    page.configure_button.hide()
    issue.addWidget(page.problem, 1)
    issue.addWidget(page.configure_button)
    footer_layout.addLayout(issue)
    summary = QHBoxLayout()
    page.selection_count = QLabel()
    page.selection_count.setObjectName('composerSummary')
    summary.addWidget(page.selection_count, 1)
    summary.addWidget(button('任务中心', lambda: page.shell.navigate(4)))
    page.start_button = button('开始合成', page.generate, True)
    page.start_button.setMinimumWidth(112)
    summary.addWidget(page.start_button)
    footer_layout.addLayout(summary)
    page.layout.addWidget(footer)
    page.update_summary()
