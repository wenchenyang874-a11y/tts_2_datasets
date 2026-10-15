"""Record, inspect and explicitly accept a local voice-cloning reference."""
from __future__ import annotations

import shutil
import tempfile
import time
import uuid
from pathlib import Path
from PySide6.QtCore import QTimer, QUrl, Qt
from PySide6.QtMultimedia import QMediaDevices, QMediaPlayer, QAudioOutput
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPlainTextEdit,
                               QProgressBar, QFileDialog, QWidget)
from .audio_capture import ReferenceCapture
from .widgets import button, combo, field, HelpButton

READING_SCRIPTS = {
    "zh": "你好，很高兴认识你。今天的天气很好，阳光透过窗户照进房间。我准备出门走走，看看街边的花，听听周围的声音。你平时喜欢做些什么？不着急，我们可以慢慢聊。希望接下来的每一天，都能遇见一些让人开心的小事。",
    "en": "Hello, it is nice to meet you. The sunlight is coming through the window, and it looks like a lovely day for a walk. I might stop by a small cafe, look at the flowers, and enjoy the sounds of the city. What do you like to do in your free time? There is no need to hurry. We can take our time and have a relaxed conversation.",
}


class ReferenceRecordingDialog(QDialog):
    def __init__(self, parent, destination, *, export_required=False, language="zh", reading_text=None):
        super().__init__(parent)
        self.destination = Path(destination)
        self.export_required = export_required
        self.accepted_path = None
        self.capture_path = None
        self.valid = False
        self.failed_take = False
        self.disposed = False
        self.cache = tempfile.TemporaryDirectory(prefix="tts-reference-recording-")
        self.capture = ReferenceCapture(self)
        self.capture.updated.connect(self.update_level)
        self.capture.stopped.connect(self.stopped)
        self.capture.failed.connect(self.failed)
        self.player = QMediaPlayer(self)
        self.output = QAudioOutput(self)
        self.output.setVolume(.7)
        self.player.setAudioOutput(self.output)
        self.player.playbackStateChanged.connect(self.playback_changed)
        self.player.errorOccurred.connect(lambda *_: self.problem.setText("无法播放这段录音，请检查输出设备后重试"))
        self.setWindowTitle("录制参考音频")
        self.setStyleSheet("QDialog { background: #f4f6fb; }")
        self.resize(600, 580)
        self.setMinimumWidth(520)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 18)
        root.setSpacing(16)
        device_row = QHBoxLayout()
        self.device = combo([])
        device_row.addWidget(field("麦克风", self.device, "使用 Windows 允许桌面应用访问的输入设备。参考音频需清晰、单人、无背景音乐；录音不会自动上传或写入样本库。"), 1)
        self.refresh_button = button("刷新设备", self.reload_devices)
        device_row.addWidget(self.refresh_button, alignment=Qt.AlignmentFlag.AlignBottom)
        root.addLayout(device_row)
        # This is a reading aid, not an automatically verified transcript.
        # Keep it separate from preview/synthesis text and do not claim that
        # editing it transcribes the recording or changes a provider payload.
        self.reading_text = QPlainTextEdit(READING_SCRIPTS.get(language, READING_SCRIPTS["zh"]) if reading_text is None else reading_text)
        self.reading_text.setMinimumHeight(120)
        root.addWidget(field("朗读文稿", self.reading_text,
                             "录音前可以修改文稿；录音时用自然、清晰的声音照着读，避免背景音乐与多人说话。此文稿用于辅助朗读，不是待合成的预览文本。"), 1)
        meter = QWidget()
        meter.setObjectName("composerPane")
        meter_layout = QVBoxLayout(meter)
        meter_layout.setContentsMargins(20, 16, 20, 16)
        self.clock = QLabel("00:00")
        self.clock.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.clock.setStyleSheet("font-size: 32px; font-weight: 600; color: #6155cc;")
        meter_layout.addWidget(self.clock)
        info = QHBoxLayout()
        info.addStretch()
        self.status = QLabel("准备录音")
        self.status.setObjectName("muted")
        info.addWidget(self.status)
        info.addWidget(HelpButton("3–60 秒，60 秒自动停止。豆包 2.0 建议 14–30 秒清晰单人录音，小米建议 10–20 秒。录制完成后可播放检查或重录；选择使用后仍需在创建音色时确认云费用。"))
        info.addStretch()
        meter_layout.addLayout(info)
        self.level = QProgressBar()
        self.level.setRange(0, 100)
        self.level.setTextVisible(False)
        self.level.setFixedHeight(8)
        self.level.setStyleSheet("QProgressBar { border: 0; border-radius: 4px; background: #eeedf8; } QProgressBar::chunk { background: #7771e5; border-radius: 4px; }")
        meter_layout.addWidget(self.level)
        root.addWidget(meter)
        controls = QHBoxLayout()
        self.record_button = button("开始录音", self.start, True)
        self.stop_button = button("停止录音", self.stop)
        self.play_button = button("播放录音", self.play)
        controls.addWidget(self.record_button)
        controls.addWidget(self.stop_button)
        controls.addWidget(self.play_button)
        root.addLayout(controls)
        self.problem = QLabel()
        self.problem.setWordWrap(True)
        self.problem.setTextFormat(Qt.TextFormat.PlainText)
        self.problem.setStyleSheet("color: #bd455d;")
        root.addWidget(self.problem)
        root.addStretch()
        footer = QHBoxLayout()
        footer.addStretch()
        footer.addWidget(button("取消", self.reject))
        self.use_button = button("保存录音…" if export_required else "使用这段录音", self.use, True)
        footer.addWidget(self.use_button)
        root.addLayout(footer)
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.tick)
        self.media_devices = QMediaDevices(self)
        self.media_devices.audioInputsChanged.connect(self.devices_changed)
        self.reload_devices()
        self.update_controls()

    def reload_devices(self):
        if self.capture.source:
            return
        selected = self.device.currentData()
        self.devices = QMediaDevices.audioInputs()
        self.device.clear()
        for device in self.devices:
            self.device.addItem(device.description(), bytes(device.id()))
        if not self.devices:
            self.device.addItem("没有可用麦克风", None)
        index = self.device.findData(selected)
        if index >= 0:
            self.device.setCurrentIndex(index)
        self.update_controls()

    def devices_changed(self):
        active_id = getattr(self, "active_device", None)
        if self.capture.source and active_id not in [bytes(device.id()) for device in QMediaDevices.audioInputs()]:
            self.failed("麦克风已断开，请重新选择设备并重录")
        if not self.capture.source:
            self.reload_devices()

    def update_controls(self):
        active = self.capture.source is not None
        self.reading_text.setReadOnly(active)
        self.device.setEnabled(not active)
        self.refresh_button.setEnabled(not active)
        self.record_button.setEnabled(not active and bool(self.devices))
        self.record_button.setText("重新录制" if self.capture_path else "开始录音")
        self.stop_button.setEnabled(active)
        self.play_button.setEnabled(not active and self.valid)
        self.use_button.setEnabled(not active and self.valid)

    def stop_playback(self):
        self.player.stop()
        self.player.setSource(QUrl())

    def start(self):
        if self.capture.source:
            return
        device = next((item for item in self.devices if bytes(item.id()) == self.device.currentData()), None)
        if device is None:
            self.problem.setText("没有可用麦克风，请检查 Windows 权限并刷新设备")
            return
        self.stop_playback()
        self.valid, self.failed_take = False, False
        self.problem.clear()
        self.active_device = bytes(device.id())
        self.capture_path = Path(self.cache.name)/(uuid.uuid4().hex+".wav")
        self.clock.setText("00:00")
        try:
            self.capture.start(device, self.capture_path)
        except (ValueError, OSError) as exc:
            self.failed(str(exc))
            return
        self.started = time.monotonic()
        self.status.setText("正在录音")
        self.timer.start()
        self.update_controls()

    def update_level(self, duration, peak):
        self.clock.setText(f"00:{int(duration):02d}")
        self.level.setValue(peak)

    def tick(self):
        if time.monotonic()-self.started >= 60:
            self.stop()

    def stop(self):
        self.capture.stop()

    def stopped(self):
        self.timer.stop()
        self.level.setValue(0)
        duration = self.capture.duration
        self.valid = bool(not self.failed_take and not self.capture.error_message and self.capture_path and self.capture_path.is_file() and 3 <= duration <= 60)
        self.status.setText(f"已录制 {duration:.1f} 秒")
        if not self.valid and not self.failed_take:
            self.problem.setText(self.capture.error_message or ("录音不足 3 秒，请重录" if duration < 3 else "参考录音无效，请重录"))
        self.update_controls()

    def failed(self, message):
        self.failed_take = True
        self.capture.stop()
        self.valid = False
        self.status.setText("录音未完成")
        self.problem.setText(message)
        self.update_controls()

    def play(self):
        if not self.valid or self.capture.source:
            return
        if self.player.playbackState() != QMediaPlayer.PlaybackState.StoppedState:
            self.stop_playback()
        else:
            self.player.setSource(QUrl.fromLocalFile(str(self.capture_path)))
            self.player.play()

    def playback_changed(self, *_):
        playing = self.player.playbackState() != QMediaPlayer.PlaybackState.StoppedState
        self.play_button.setText("停止播放" if playing else "播放录音")

    def use(self):
        if not self.valid or self.capture.source:
            return
        self.stop_playback()
        if self.export_required:
            filename, _ = QFileDialog.getSaveFileName(self, "保存参考录音", "参考录音.wav", "WAV 音频 (*.wav)")
            if not filename:
                return
            target = Path(filename)
        else:
            target = self.destination/("reference-"+uuid.uuid4().hex+".wav")
        try:
            shutil.copyfile(self.capture_path, target)
        except OSError:
            self.problem.setText("无法保存录音，请检查目标目录权限")
            return
        self.accepted_path = target
        self.accept()

    def done(self, result):
        if not self.disposed:
            self.disposed = True
            self.timer.stop()
            self.capture.stop()
            self.stop_playback()
            self.output.setMuted(True)
            # Release the media source BEFORE deleting our temporary WAVs.
            # The accepted file belongs to the parent form (or Save dialog).
            try:
                self.cache.cleanup()
            except OSError:
                # Backends can briefly retain a handle after setSource.
                # Retry on the event loop; never delete outside our cache.
                QTimer.singleShot(500, self.cleanup_cache)
        super().done(result)

    def cleanup_cache(self):
        try:
            self.cache.cleanup()
        except OSError:
            pass  # A backend still holds the owned temporary file.
