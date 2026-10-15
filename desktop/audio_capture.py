"""Native capture primitives shared by recording and clone reference dialogs."""
from __future__ import annotations

import wave
import numpy as np
from PySide6.QtCore import QObject, Signal, QTimer
from PySide6.QtMultimedia import QAudio, QAudioFormat, QAudioSource


def capture_format(device, rate=16000):
    result = QAudioFormat()
    result.setSampleRate(rate)
    result.setChannelCount(1)
    result.setSampleFormat(QAudioFormat.SampleFormat.Int16)
    if not device.isFormatSupported(result):
        result = device.preferredFormat()
    if result.sampleFormat() not in (QAudioFormat.SampleFormat.Int16, QAudioFormat.SampleFormat.Int32,
                                      QAudioFormat.SampleFormat.UInt8, QAudioFormat.SampleFormat.Float):
        raise ValueError("麦克风采样格式不受支持")
    if result.sampleRate() <= 0 or result.channelCount() <= 0:
        raise ValueError("麦克风采样率或声道无效")
    return result


def pcm16_frames(data, format, pending=b"", *, mono=False):
    """Keep partial interleaved frames; convert true device samples, never rates.

    Float/Int32/unsigned devices must not be relabeled as signed PCM16. Splitting
    on sample rather than whole channel frames corrupts stereo and adds clicks.
    """
    kind = format.sampleFormat()
    dtype, scale = {QAudioFormat.SampleFormat.Int16: ("<i2", 32768),
                    QAudioFormat.SampleFormat.Int32: ("<i4", 2147483648),
                    QAudioFormat.SampleFormat.UInt8: ("u1", 128),
                    QAudioFormat.SampleFormat.Float: ("<f4", 1)}[kind]
    data = pending+data
    frame_size = np.dtype(dtype).itemsize*format.channelCount()
    length = len(data)//frame_size*frame_size
    remainder = data[length:]
    if not length:
        return b"", remainder, 0
    values = np.frombuffer(data[:length], dtype=dtype).astype(np.float64)
    if kind == QAudioFormat.SampleFormat.UInt8:
        values -= 128
    values = np.clip(np.nan_to_num(values/scale), -1, 1)
    if mono and format.channelCount() > 1:
        values = values.reshape(-1, format.channelCount()).mean(axis=1)
    pcm = (values*32767).astype("<i2").tobytes()
    return pcm, remainder, min(100, int(np.max(np.abs(values))*100))


class ReferenceCapture(QObject):
    updated = Signal(float, int)
    failed = Signal(str)
    stopped = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.source = self.stream = self.writer = None
        self.pending = b""
        self.frames = 0
        self.format = None
        self.failure_scheduled = False
        self.epoch = 0
        self.error_message = ""

    @property
    def duration(self):
        return self.frames/self.format.sampleRate() if self.format else 0

    def start(self, device, path):
        if self.source:
            raise ValueError("录音尚未停止")
        self.epoch += 1
        self.format = capture_format(device, 24000)
        if self.format.sampleRate() < 16000:
            raise ValueError("参考录音需要至少 16 kHz 的麦克风采样率，请选择其他设备")
        self.frames, self.pending, self.failure_scheduled = 0, b"", False
        self.error_message = ""
        try:
            self.writer = wave.open(str(path), "wb")
            self.writer.setnchannels(1)
            self.writer.setsampwidth(2)
            self.writer.setframerate(self.format.sampleRate())
            self.source = QAudioSource(device, self.format, self)
            self.source.stateChanged.connect(self.state_changed)
            self.stream = self.source.start()
            if self.stream is None:
                raise ValueError("无法启动麦克风，请检查 Windows 权限或设备占用")
            self.stream.readyRead.connect(self.read)
        except Exception:
            self.stop()
            raise

    def read(self):
        if self.stream is None or self.writer is None:
            return
        try:
            pcm, self.pending, peak = pcm16_frames(bytes(self.stream.readAll()), self.format, self.pending, mono=True)
            # Bound the actual number of saved frames, not just a UI timer.
            # A delayed timer or a large final readyRead must not exceed 60s.
            remaining = max(0, self.format.sampleRate()*60-self.frames)
            pcm = pcm[:remaining*2]
            self.writer.writeframesraw(pcm)
            self.frames += len(pcm)//2
            self.updated.emit(self.duration, peak)
            if self.duration >= 60 and not self.failure_scheduled:
                self.failure_scheduled = True
                epoch = self.epoch
                QTimer.singleShot(0, lambda: self.stop() if epoch == self.epoch else None)
        except (OSError, wave.Error, ValueError):
            self.schedule_failure("录音写入失败，请检查临时目录和麦克风")

    def state_changed(self, state):
        if self.source and self.source.error() != QAudio.Error.NoError:
            self.schedule_failure("麦克风连接中断或权限被拒绝，请检查设备后重录")

    def schedule_failure(self, message):
        if self.failure_scheduled:
            return
        self.failure_scheduled = True
        self.error_message = message
        # Device callbacks can arrive during QAudioSource.start/stop. Queue
        # cleanup instead of destroying a native object inside its own signal.
        epoch = self.epoch
        QTimer.singleShot(0, lambda: (self.stop(), self.failed.emit(message)) if epoch == self.epoch else None)

    def stop(self):
        self.epoch += 1
        source, self.source = self.source, None
        active = source is not None or self.writer is not None
        if source:
            # Disconnect before teardown: Windows backends can emit another
            # state change while stop releases the input device.
            source.stateChanged.disconnect(self.state_changed)
            if self.stream:
                self.read()
                self.stream.readyRead.disconnect(self.read)
            source.stop()
            source.deleteLater()
        self.stream = None
        if self.writer:
            writer, self.writer = self.writer, None
            try:
                writer.close()
            except (OSError, wave.Error):
                self.failed.emit("无法完成录音文件，请重录")
        if active:
            self.stopped.emit()
