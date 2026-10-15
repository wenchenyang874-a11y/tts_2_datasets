"""Native device capture; device rate is normalized by the existing importer."""
from __future__ import annotations

import time
import wave
from pathlib import Path
from PySide6.QtCore import QTimer, Qt
from PySide6.QtMultimedia import QAudioSource, QMediaDevices, QAudio
from PySide6.QtWidgets import (QLineEdit, QHBoxLayout, QListWidget, QProgressBar,
                               QFileDialog, QLabel)
from .base import Page
from .widgets import combo, field, button
from .generate import TARGETS, AGES
from .audio_capture import capture_format, pcm16_frames


class RecordingPage(Page):
    def __init__(self, shell):
        super().__init__(shell, "真人录音")
        self.paths = []
        self.source = None
        self.device_stream = None
        self.writer = None
        self.started = 0.0
        self.recorded_path = None
        self.pending_bytes = b""
        self.uploading = False
        self.device = combo([])
        self.devices = []
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.tick)
        self.media_devices = QMediaDevices(self)
        self.media_devices.audioInputsChanged.connect(self.reload_devices)
        self.layout.addWidget(field("输入设备", self.device, "Windows 设置中需允许桌面应用使用麦克风。没有设备或权限被拒绝时会明确报错。"))
        self.actions(("开始录音", self.start, True), ("停止", self.stop, False),
                     ("试听录音", self.preview, False), ("重录", self.rerecord, False),
                     ("刷新设备", self.reload_devices, False))
        self.add_playback_controls()
        self.clock = QLabel("00:00")
        self.layout.addWidget(self.clock)
        self.level = QProgressBar()
        self.level.setRange(0, 100)
        self.level.setTextVisible(False)
        self.layout.addWidget(self.level)
        row = QHBoxLayout()
        self.speaker = combo([])
        self.speaker.setEditable(True)
        self.text = QLineEdit("你好，小助手")
        self.kind = combo(TARGETS)
        row.addWidget(field("说话人 ID", self.speaker, "可选已有 ID 或输入新 ID。同一人保持相同 ID，避免训练划分泄漏。"))
        row.addWidget(field("文本", self.text))
        row.addWidget(field("样本目标", self.kind))
        self.layout.addLayout(row)
        row2 = QHBoxLayout()
        self.split = combo([("未分配", "unassigned"), ("Train", "train"), ("Dev", "dev"), ("Test", "test")])
        self.gender = combo([("未知", "unknown"), ("女", "female"), ("男", "male")])
        self.age = combo([("未知", "unknown")] + AGES[:-1])
        for label, widget in [("数据划分", self.split), ("性别", self.gender), ("年龄", self.age)]:
            row2.addWidget(field(label, widget))
        self.layout.addLayout(row2)
        self.files = QListWidget()
        self.files.setMinimumHeight(150)
        self.layout.addWidget(self.files)
        actions = self.actions(("添加本地音频", self.choose_files, False), ("清空文件", self.clear_files, False),
                               ("导入并校验", self.upload, True))
        self.upload_button = actions.itemAt(2).widget()
        self.result = QLabel()
        self.result.setWordWrap(True)
        self.result.setTextFormat(Qt.TextFormat.PlainText)
        self.layout.addWidget(self.result)
        self.layout.addStretch()
        self.reload_devices()

    def reload_devices(self):
        if self.source:
            return
        self.devices = QMediaDevices.audioInputs()
        self.device.clear()
        for index, device in enumerate(self.devices):
            self.device.addItem(device.description(), index)
        if not self.devices:
            self.device.addItem("无可用输入设备", None)

    def refresh(self):
        self.request("/api/samples/speakers", callback=self.set_speakers)

    def set_speakers(self, rows):
        value = self.speaker.currentText()
        self.speaker.clear()
        for row in rows:
            self.speaker.addItem(row["speaker_id"])
        self.speaker.setEditText(value)

    def start(self):
        if self.source:
            return
        index = self.device.currentData()
        if index is None or not 0 <= index < len(self.devices):
            self.shell.error("没有可用麦克风，请检查设备与 Windows 权限")
            return
        device = self.devices[index]
        try:
            format = capture_format(device)
        except ValueError as exc:
            self.shell.error(str(exc))
            return
        # Capture the actual supported rate/channel format. Pretending 48 kHz
        # bytes are 16 kHz corrupts duration/pitch; import performs conversion.
        # Float device samples are explicitly converted to PCM16 before WAV.
        self.capture_format = format
        self.pending_bytes = b""
        self.recorded_path = Path(self.shell.cache.name) / f"recording-{time.time_ns()}.wav"
        self.writer = wave.open(str(self.recorded_path), "wb")
        self.writer.setnchannels(format.channelCount())
        self.writer.setsampwidth(2)
        self.writer.setframerate(format.sampleRate())
        self.source = QAudioSource(device, format, self)
        self.source.stateChanged.connect(self.state_changed)
        self.started = time.monotonic()
        self.device_stream = self.source.start()
        if self.device_stream is None:
            self.stop()
            self.shell.error("无法启动麦克风，请检查 Windows 权限和设备占用")
            return
        self.device_stream.readyRead.connect(self.read_audio)
        self.timer.start()

    def read_audio(self):
        if not self.device_stream or not self.writer:
            return
        data, self.pending_bytes, peak = pcm16_frames(bytes(self.device_stream.readAll()), self.capture_format, self.pending_bytes)
        if not data:
            return
        self.writer.writeframesraw(data)
        self.level.setValue(peak)

    def state_changed(self, state):
        if self.source and self.source.error() != QAudio.Error.NoError:
            self.result.setText("录音失败，请检查麦克风权限、连接或占用状态")
            QTimer.singleShot(0, self.stop)

    def tick(self):
        elapsed = time.monotonic() - self.started
        self.clock.setText(f"00:{int(elapsed):02d}")
        if elapsed >= 30:
            self.stop()

    def stop(self):
        self.timer.stop()
        if self.source:
            self.read_audio()
            source, self.source = self.source, None
            source.stop()
            source.deleteLater()
        self.device_stream = None
        if self.writer:
            self.writer.close()
            self.writer = None
        self.level.setValue(0)
        if self.recorded_path and self.recorded_path.is_file():
            with wave.open(str(self.recorded_path)) as recording:
                duration = recording.getnframes() / recording.getframerate()
            if duration >= .2:
                if str(self.recorded_path) not in self.paths:
                    self.paths.append(str(self.recorded_path))
                    self.files.addItem(self.recorded_path.name)
            else:
                self.recorded_path.unlink(missing_ok=True)
                self.result.setText("录音不足 0.2 秒，请重录")
                self.recorded_path = None

    def preview(self):
        if self.source:
            self.stop()
        if self.recorded_path:
            self.shell.play_file(self.recorded_path)

    def rerecord(self):
        self.stop()
        self.shell.player.stop()
        if self.recorded_path:
            path = str(self.recorded_path)
            self.paths = [item for item in self.paths if item != path]
            self.recorded_path = None
            self.files.clear()
            self.files.addItems([Path(item).name for item in self.paths])
        self.start()

    def choose_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "选择音频", "", "音频 (*.wav *.flac *.mp3 *.ogg *.opus *.m4a *.webm)")
        for path in paths:
            if path not in self.paths:
                self.paths.append(path)
                self.files.addItem(Path(path).name)

    def clear_files(self):
        self.paths.clear()
        self.files.clear()

    def upload(self):
        if self.uploading:
            return
        self.stop()
        if not self.paths or not self.speaker.currentText().strip():
            self.shell.error("请选择音频并填写说话人 ID")
            return
        paths = self.paths[:]
        project = self.shell.project
        data = {"speaker_id": self.speaker.currentText(), "text": self.text.text(),
                "label": project["label"], "keyword_id": project["keyword_id"],
                "target_kind": self.kind.currentData(), "split": self.split.currentData(),
                "gender": self.gender.currentData(), "age_group": self.age.currentData()}
        self.uploading = True
        self.upload_button.setEnabled(False)
        self.request("/api/samples/upload", "POST", files=[("files", path) for path in paths], data=data,
                     callback=lambda result: self.uploaded(result, paths), error=self.upload_failed)

    def upload_failed(self, error):
        self.uploading = False
        self.upload_button.setEnabled(True)
        self.shell.error(error)

    def uploaded(self, result, submitted):
        self.uploading = False
        self.upload_button.setEnabled(True)
        # Continuous import keeps identity/label settings; only the files that
        # actually belonged to this request are cleared, not later additions.
        self.paths = [path for path in self.paths if path not in submitted]
        self.files.clear()
        self.files.addItems([Path(path).name for path in self.paths])
        failures = "；".join(f"{item['file']}: {item['error']}" for item in result["failures"])
        self.result.setText(f"已导入 {result['imported']} 条 · 失败 {len(result['failures'])} 条" + ("\n" + failures if failures else ""))
        self.refresh()
