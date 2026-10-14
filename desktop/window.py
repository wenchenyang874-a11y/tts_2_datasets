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
    def __init__(self, runtime, preferences=None, session=None):
        super().__init__()
        from .form_wheel import FormWheelGuard
        self.form_wheel_guard = FormWheelGuard(self)
        self.runtime = runtime
        self.preferences = preferences
        from .session import DirectorySession, acquire_directory_lock
        self.session = session or (DirectorySession(runtime, preferences, acquire_directory_lock(runtime.data_directory)) if preferences else None)
        self.switching = False
        self.epoch = 0
        self.jobs = {}
        self.selected_samples = set()
        self.project = {}
        self.ready = False
        self.closing = False
        self.playback_epoch = 0
        self.playback_resource = None
        self.cache = tempfile.TemporaryDirectory(prefix="tts-desktop-media-")
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="desktop-commands")
        # Serialize UI-originated commands. Core generation still uses its real
        # task pool. A single pipe never interleaves request/reply pairs, and
        # timers cannot create overlapping polling calls on a slow backend.
        self.setWindowTitle("语音数据集工坊")
        self.resize(1260, 880)
        self.setMinimumSize(980, 680)
        self.setStyleSheet(STYLE)
        center = QWidget()
        center.setObjectName("canvas")
        outer = QHBoxLayout(center)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(184)
        navigation = QVBoxLayout(sidebar)
        navigation.setContentsMargins(14, 28, 14, 20)
        navigation.setSpacing(7)
        brand = QLabel("语音工坊")
        brand.setObjectName("brand")
        navigation.addWidget(brand)
        caption = QLabel("DATASET STUDIO")
        caption.setObjectName("muted")
        navigation.addWidget(caption)
        navigation.addSpacing(24)
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
        main.setContentsMargins(24, 18, 24, 14)
        header = QHBoxLayout()
        self.project_label = QLabel("你好，小助手")
        self.project_label.setTextFormat(Qt.TextFormat.PlainText)
        self.project_label.setObjectName("projectChip")
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
        self.build_pages()
        main.addWidget(self.stack)
        self.player = QMediaPlayer(self)
        self.output = QAudioOutput(self)
        self.output.setVolume(.7)
        self.player.setAudioOutput(self.output)
        self.player.errorOccurred.connect(lambda error, message: self.set_status("音频播放失败，请检查输出设备与文件", True))
        self.player.playbackStateChanged.connect(self.update_playback_controls)
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

    def build_pages(self):
        while self.stack.count():
            old = self.stack.widget(0)
            self.stack.removeWidget(old)
            old.deleteLater()
        self.pages = [cls(self) for cls in [GeneratePage, SamplesPage, RecordingPage, NoisesPage,
                                           TasksPage, ExportsPage, SettingsPage]]
        for page in self.pages:
            if isinstance(page, GeneratePage):
                # The composer is a bounded workspace with tabs and independent
                # lists; wrapping it in a page scroll area hides its submit action
                # and shrinks the catalog behind unrelated parameter forms.
                self.stack.addWidget(page)
                continue
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(page)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.stack.addWidget(scroll)

    def submit(self, function, callback=None, error=None, *, switching=False):
        if self.closing or (self.switching and not switching):
            return
        identifier = uuid.uuid4().hex
        self.jobs[identifier] = (self.pool.submit(function), callback, error, self.epoch)

    def drain_completions(self):
        for identifier, (future, _, _, _) in list(self.jobs.items()):
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
        _, success, failure, epoch = entry
        if epoch != self.epoch:
            return  # Old dataset replies must never populate the new pages.
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
        if self.switching:
            return
        if index != self.stack.currentIndex():
            self.stop_playback()
        self.stack.setCurrentIndex(index)
        for i, item in enumerate(self.nav_buttons):
            item.setChecked(i == index)
        if self.ready:
            self.pages[index].refresh()

    def poll(self):
        if self.ready and not self.closing and not self.switching and not self.jobs:
            if self.stack.currentIndex() == 4:
                self.pages[4].refresh()
            elif self.stack.currentIndex() == 5:
                self.pages[5].refresh()

    def open_data_directory(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.runtime.data_directory)))

    def switch_directory(self, value, mode="empty"):
        if self.switching or not self.ready:
            return
        if self.session is None:
            self.error("当前启动方式无法管理数据目录")
            return
        if self.pages[2].source is not None:
            self.error("请先停止录音，再切换数据目录")
            return
        self.switching = True
        self.epoch += 1
        self.stack.setEnabled(False)
        for item in self.nav_buttons:
            item.setEnabled(False)
        self.player.stop()
        self.player.setSource(QUrl())
        self.set_status("正在迁移数据…" if mode == "move" else "正在切换数据目录…")
        self.submit(lambda: self.session.switch(value, mode), self.directory_switched, self.directory_failed, switching=True)

    def enable_after_switch(self):
        self.runtime = self.session.runtime
        self.switching = False
        self.stack.setEnabled(True)
        for item in self.nav_buttons:
            item.setEnabled(True)

    def directory_switched(self, result):
        self.enable_after_switch()
        self.selected_samples = set()
        self.project = {}
        self.build_pages()
        self.navigate(6)
        self.pages[0].refresh()
        self.set_status(result["warning"] or "数据目录已切换", bool(result["warning"]))

    def directory_failed(self, error):
        self.enable_after_switch()
        # Rollback may recreate the service, so stale loading flags and old
        # forms must be reset even though the dataset identity stayed the same.
        self.build_pages()
        self.navigate(6)
        self.pages[0].refresh()
        self.error(str(error))

    def play_resource(self, kind, identifier, label="音频"):
        # Each playback gets a new snapshot: on Windows the current media
        # backend can still hold the old file while it releases its source.
        # Replacing the same cache filename made repeated/hot playback fail.
        path = Path(self.cache.name) / (identifier + "-" + uuid.uuid4().hex + ".wav")
        self.playback_epoch += 1
        page, epoch, playback_epoch = self.stack.currentIndex(), self.epoch, self.playback_epoch
        self.submit(lambda: self.runtime.call(op="copy", kind=kind, id=identifier, destination=str(path)),
                    lambda result: self.play_file(Path(result), label, (kind, identifier)) if page == self.stack.currentIndex() and epoch == self.epoch and playback_epoch == self.playback_epoch else None)

    def play_file(self, path, label="录音", resource=None):
        self.player.stop()
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.playback_resource = resource
        self.player.play()
        page = self.pages[self.stack.currentIndex()]
        if hasattr(page, "playback_label"):
            page.playback_label.setText("试听 · " + label[:35])

    def stop_playback(self):
        # Invalidate queued copies too. Leaving and returning to the same page
        # before a copy finishes must not restart audio after the user stopped.
        self.playback_epoch += 1
        self.player.stop()
        self.update_playback_controls()

    def update_playback_controls(self, *args):
        active = self.player.playbackState() != QMediaPlayer.PlaybackState.StoppedState
        if not active:
            self.playback_resource = None
        for index, page in enumerate(self.pages):
            if hasattr(page, "playback_controls"):
                page.playback_controls.setVisible(active and index == self.stack.currentIndex())
            if hasattr(page, "update_row_playback"):
                page.update_row_playback()

    def closeEvent(self, event):
        if self.switching:
            self.set_status("正在切换数据目录，完成后即可关闭")
            event.ignore()
            return
        self.closing = True
        self.poll_timer.stop()
        self.completion_timer.stop()
        self.pages[2].stop()
        self.player.stop()
        self.player.setSource(QUrl())
        self.output.setMuted(True)
        self.pool.shutdown(wait=False, cancel_futures=True)
        if self.session:
            self.session.close()
        else:
            self.runtime.close()
        try:
            self.cache.cleanup()
        except OSError:
            # Audio device teardown can briefly keep a Windows handle. Only
            # our own temp directory is involved; never delete source audio.
            pass
        event.accept()
