"""Native shell with queued, bounded business commands and owned media."""
from __future__ import annotations

import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                               QStackedWidget, QPushButton, QScrollArea, QMessageBox)
from .widgets import STYLE
from .generate import GeneratePage
from .samples import SamplesPage
from .recording import RecordingPage
from .noises import NoisesPage
from .tasks import TasksPage
from .exports import ExportsPage
from .settings import SettingsPage


class MainWindow(QMainWindow):
    def __init__(self, runtime):
        super().__init__()
        self.runtime = runtime
        self.jobs = {}
        self.selected_samples = set()
        self.project = {}
        self.ready = False
        self.closing = False
        self.cache = tempfile.TemporaryDirectory(prefix="tts-desktop-media-")
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="desktop-commands")
        # Serialize UI-originated commands. Core generation still uses its real
        # task pool. A single pipe never interleaves request/reply pairs, and
        # timers cannot create overlapping polling calls on a slow backend.
        self.setWindowTitle("语音数据集工坊")
        self.resize(1280, 900)
        self.setMinimumSize(1060, 720)
        self.setStyleSheet(STYLE)
        center = QWidget()
        center.setObjectName("canvas")
        outer = QHBoxLayout(center)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(204)
        navigation = QVBoxLayout(sidebar)
        navigation.setContentsMargins(14, 28, 14, 20)
        navigation.setSpacing(7)
        brand = QLabel("语音数据集\n工坊")
        brand.setObjectName("brand")
        navigation.addWidget(brand)
        navigation.addSpacing(30)
        self.nav_buttons = []
        labels = ["合成创作", "样本库", "真人录音", "噪声增强", "任务中心", "训练集导出", "设置"]
        for index, label in enumerate(labels):
            item = QPushButton(label)
            item.setCheckable(True)
            item.clicked.connect(lambda checked=False, i=index: self.navigate(i))
            navigation.addWidget(item)
            self.nav_buttons.append(item)
        navigation.addStretch()
        data = QPushButton("打开数据目录")
        data.clicked.connect(self.open_data_directory)
        navigation.addWidget(data)
        outer.addWidget(sidebar)
        main = QVBoxLayout()
        main.setContentsMargins(32, 22, 32, 20)
        header = QHBoxLayout()
        self.project_label = QLabel("语音数据集工坊")
        self.project_label.setTextFormat(Qt.TextFormat.PlainText)
        self.project_label.setObjectName("section")
        header.addWidget(self.project_label)
        header.addStretch()
        self.status = QLabel("正在启动")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setMaximumWidth(540)
        self.status.setWordWrap(True)
        self.status.setObjectName("muted")
        header.addWidget(self.status)
        main.addLayout(header)
        main.addSpacing(16)
        self.stack = QStackedWidget()
        self.pages = [cls(self) for cls in [GeneratePage, SamplesPage, RecordingPage, NoisesPage,
                                           TasksPage, ExportsPage, SettingsPage]]
        for page in self.pages:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(page)
            self.stack.addWidget(scroll)
        main.addWidget(self.stack)
        self.player = QMediaPlayer(self)
        self.output = QAudioOutput(self)
        self.output.setVolume(.7)
        self.player.setAudioOutput(self.output)
        self.player.errorOccurred.connect(lambda error, message: self.set_status("音频播放失败，请检查输出设备与文件", True))
        footer = QHBoxLayout()
        self.playback = QLabel("")
        self.playback.setTextFormat(Qt.TextFormat.PlainText)
        self.playback.setObjectName("muted")
        footer.addWidget(self.playback)
        footer.addStretch()
        stop = QPushButton("停止播放")
        stop.clicked.connect(self.player.stop)
        footer.addWidget(stop)
        main.addLayout(footer)
        outer.addLayout(main, 1)
        self.setCentralWidget(center)
        self.stack.setEnabled(False)
        self.nav_buttons[0].setChecked(True)
        self.submit(runtime.ready, self.started)
        # Workers only resolve Python Futures; a GUI-owned timer drains them.
        # No worker constructs, emits through or destroys a Qt object. This
        # avoids QObject lifetime coupling to the worker and guarantees that
        # page updates, errors and navigation all run on the GUI thread.
        self.completion_timer = QTimer(self)
        self.completion_timer.setInterval(20)
        self.completion_timer.timeout.connect(self.drain_completions)
        self.completion_timer.start()
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(1800)
        self.poll_timer.timeout.connect(self.poll)
        self.poll_timer.start()

    def submit(self, function, callback=None, error=None):
        if self.closing:
            return
        identifier = uuid.uuid4().hex
        self.jobs[identifier] = (self.pool.submit(function), callback, error)

    def drain_completions(self):
        for identifier, (future, _, _) in list(self.jobs.items()):
            if future.done():
                try:
                    result = future.result()
                except Exception as exc:
                    self.finished(identifier, None, exc)
                else:
                    self.finished(identifier, result, None)

    def finished(self, identifier, result, error):
        entry = self.jobs.pop(identifier, None)
        if not entry or self.closing:
            return
        _, success, failure = entry
        if error is not None:
            (failure or self.error)(error)
        elif success:
            try:
                success(result)
            except Exception:
                self.error("界面更新失败，请刷新并检查任务实际状态")

    def started(self, result):
        self.ready = True
        self.stack.setEnabled(True)
        self.set_status("已就绪")
        self.pages[0].refresh()

    def set_status(self, text, error=False):
        self.status.setText(str(text))
        self.status.setToolTip(str(text))
        self.status.setStyleSheet("color: #c64e68;" if error else "color: #7a8497;")

    def error(self, error):
        self.set_status(str(error), True)
        if not self.runtime.process.is_alive():
            self.ready = False
            self.stack.setEnabled(False)
        QMessageBox.warning(self, "操作未完成", str(error))

    def navigate(self, index):
        self.stack.setCurrentIndex(index)
        for i, item in enumerate(self.nav_buttons):
            item.setChecked(i == index)
        if self.ready:
            self.pages[index].refresh()

    def poll(self):
        if self.ready and not self.closing and not self.jobs:
            if self.stack.currentIndex() == 4:
                self.pages[4].refresh()
            elif self.stack.currentIndex() == 5:
                self.pages[5].refresh()

    def open_data_directory(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.runtime.data_directory)))

    def play_resource(self, kind, identifier, label="音频"):
        # Each playback gets a new snapshot: on Windows the current media
        # backend can still hold the old file while it releases its source.
        # Replacing the same cache filename made repeated/hot playback fail.
        path = Path(self.cache.name) / (identifier + "-" + uuid.uuid4().hex + ".wav")
        self.submit(lambda: self.runtime.call(op="copy", kind=kind, id=identifier, destination=str(path)),
                    lambda result: self.play_file(Path(result), label))

    def play_file(self, path, label="录音"):
        self.player.stop()
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.play()
        self.playback.setText("试听 · " + label[:35])

    def closeEvent(self, event):
        self.closing = True
        self.poll_timer.stop()
        self.completion_timer.stop()
        self.pages[2].stop()
        self.player.stop()
        self.player.setSource(QUrl())
        self.output.setMuted(True)
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.runtime.close()
        try:
            self.cache.cleanup()
        except OSError:
            # Audio device teardown can briefly keep a Windows handle. Only
            # our own temp directory is involved; never delete source audio.
            pass
        event.accept()
