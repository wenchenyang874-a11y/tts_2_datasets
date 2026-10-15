"""Offline microphone stream/format/lifetime regressions; no real capture."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import struct
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtCore import QObject, Signal, QBuffer, QIODevice, QEvent, QCoreApplication, QByteArray
from PySide6.QtMultimedia import QAudio, QAudioFormat
from PySide6.QtWidgets import QApplication, QDialog, QWidget
from desktop.audio_capture import ReferenceCapture, pcm16_frames
from desktop.reference_recording import ReferenceRecordingDialog
from desktop.custom_voices import CreationDialog


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


class Device:
    def id(self):
        return b"offline-microphone"
    def description(self):
        return "离线模拟麦克风"
    def isFormatSupported(self, format):
        return True


class Source(QObject):
    stateChanged = Signal(object)
    def __init__(self, device, format, parent):
        super().__init__(parent)
        self.buffer = QBuffer(self)
        self.buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        self.closed = False
        self.error_value = QAudio.Error.NoError
    def start(self):
        return self.buffer
    def error(self):
        return self.error_value
    def stop(self):
        self.closed = True


@pytest.fixture
def fake_capture(monkeypatch, application):
    monkeypatch.setattr("desktop.audio_capture.QAudioSource", Source)
    monkeypatch.setattr("desktop.reference_recording.QMediaDevices.audioInputs", lambda: [Device()])
    yield
    for widget in application.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def feed(capture, seconds):
    rate = capture.format.sampleRate()
    capture.stream.close()
    capture.stream.setData(QByteArray(b"\x10\x00"*int(rate*seconds)))
    capture.stream.open(QIODevice.OpenModeFlag.ReadOnly)
    capture.stream.readyRead.emit()


@pytest.mark.parametrize("kind,raw", [(QAudioFormat.SampleFormat.Int16, struct.pack("<hhhh", 1000,-1000,2000,1000)),
                                      (QAudioFormat.SampleFormat.Int32, struct.pack("<iiii", 1000,-1000,2000,1000)),
                                      (QAudioFormat.SampleFormat.Float, struct.pack("<ffff", .5,-.5,float("nan"),.2)),
                                      (QAudioFormat.SampleFormat.UInt8, bytes([200,56,128,255]))])
def test_pcm_conversion_keeps_partial_channel_frames(kind, raw):
    format = QAudioFormat()
    format.setSampleRate(48000)
    format.setChannelCount(2)
    format.setSampleFormat(kind)
    full, remainder, _ = pcm16_frames(raw, format, mono=True)
    first, pending, _ = pcm16_frames(raw[:3], format, mono=True)
    second, pending, peak = pcm16_frames(raw[3:], format, pending, mono=True)
    assert first+second == full and pending == remainder == b""
    assert len(full) == 4 and 0 <= peak <= 100
    assert np.isfinite(np.frombuffer(full, dtype="<i2")).all()


def test_capture_stops_at_exact_60_seconds_and_releases_source(fake_capture, application, tmp_path):
    capture = ReferenceCapture()
    path = tmp_path/"limit.wav"
    capture.start(Device(), path)
    source = capture.source
    feed(capture, 61)
    application.processEvents()
    assert source.closed and capture.source is None and capture.writer is None
    with wave.open(str(path), "rb") as wav:
        assert wav.getnframes() == wav.getframerate()*60 and wav.getnchannels() == 1
        assert wav.getsampwidth() == 2


def test_short_take_rejected_and_accepted_take_survives_until_parent_closes(fake_capture, application, tmp_path):
    destination = tmp_path/"accepted"
    destination.mkdir()
    dialog = ReferenceRecordingDialog(None, destination)
    dialog.show()
    dialog.start()
    feed(dialog.capture, 1)
    dialog.stop()
    assert not dialog.valid and not dialog.use_button.isEnabled()
    dialog.start()
    feed(dialog.capture, 4)
    source = dialog.capture.source
    dialog.stop()
    assert dialog.valid and source.closed and dialog.play_button.isEnabled()
    owned = Path(dialog.cache.name)
    dialog.use()
    assert dialog.accepted_path.is_file() and not owned.exists()
    with wave.open(str(dialog.accepted_path), "rb") as wav:
        assert wav.getnframes()/wav.getframerate() == 4


def test_device_failure_never_accepts_partial_take_and_cancel_cleans(fake_capture, application, tmp_path):
    dialog = ReferenceRecordingDialog(None, tmp_path)
    dialog.start()
    feed(dialog.capture, 4)
    source = dialog.capture.source
    source.error_value = QAudio.Error.IOError
    source.stateChanged.emit(QAudio.State.StoppedState)
    application.processEvents()
    assert source.closed and not dialog.valid and not dialog.use_button.isEnabled()
    root = Path(dialog.cache.name)
    dialog.reject()
    assert not root.exists() and dialog.accepted_path is None


def test_no_device_and_close_during_recording_are_safe(fake_capture, application, monkeypatch, tmp_path):
    dialog = ReferenceRecordingDialog(None, tmp_path)
    dialog.start()
    source = dialog.capture.source
    root = Path(dialog.cache.name)
    dialog.reject()
    assert source.closed and dialog.capture.writer is None and not root.exists()
    monkeypatch.setattr("desktop.reference_recording.QMediaDevices.audioInputs", lambda: [])
    empty = ReferenceRecordingDialog(None, tmp_path)
    assert not empty.record_button.isEnabled()
    empty.start()
    assert "麦克风" in empty.problem.text()
    empty.reject()


def test_reference_selection_preserved_on_cancel_and_form_owns_recording(fake_capture, monkeypatch, application, tmp_path):
    page = QWidget()
    page.shell = SimpleNamespace(pages=[None,None,SimpleNamespace(source=None)], stop_playback=lambda: None)
    form = CreationDialog(page, "clone")
    original = tmp_path/"existing.wav"
    original.write_bytes(b"external audio must not be deleted")
    form.path = original
    def cancel(dialog):
        assert "阳光" in dialog.reading_text.toPlainText()
        dialog.reading_text.setPlainText("我的自定义朗读文稿")
        dialog.start()
        assert dialog.reading_text.isReadOnly()
        feed(dialog.capture, 4)
        dialog.reject()
        return QDialog.DialogCode.Rejected
    monkeypatch.setattr(ReferenceRecordingDialog, "exec", cancel)
    form.record_reference()
    assert form.path == original and original.is_file()
    def accept(dialog):
        assert dialog.reading_text.toPlainText() == "我的自定义朗读文稿"
        dialog.start()
        feed(dialog.capture, 4)
        dialog.stop()
        dialog.use()
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(ReferenceRecordingDialog, "exec", accept)
    form.record_reference()
    accepted = form.path
    assert accepted != original and accepted.is_file() and "现场录音" in form.file_label.text()
    form.reject()
    assert not accepted.exists() and original.is_file()


def test_reading_script_language_and_recording_state(fake_capture, application, tmp_path):
    dialog = ReferenceRecordingDialog(None, tmp_path, language="en")
    assert "sunlight" in dialog.reading_text.toPlainText()
    assert not dialog.reading_text.isReadOnly()
    dialog.start()
    assert dialog.reading_text.isReadOnly()
    feed(dialog.capture, 4)
    dialog.stop()
    assert not dialog.reading_text.isReadOnly()
    assert "sunlight" in dialog.reading_text.toPlainText()
    dialog.reject()


def test_aliyun_recording_save_cancel_and_explicit_local_export(fake_capture, monkeypatch, application, tmp_path):
    dialog = ReferenceRecordingDialog(None, tmp_path, export_required=True)
    dialog.start()
    feed(dialog.capture, 4)
    dialog.stop()
    monkeypatch.setattr("desktop.reference_recording.QFileDialog.getSaveFileName", lambda *args: ("", ""))
    dialog.use()
    assert dialog.valid and dialog.accepted_path is None and Path(dialog.cache.name).exists()
    target = tmp_path/"saved reference.wav"
    monkeypatch.setattr("desktop.reference_recording.QFileDialog.getSaveFileName", lambda *args: (str(target), ""))
    dialog.use()
    assert dialog.accepted_path == target and target.is_file() and not Path(dialog.cache.name).exists()
    with wave.open(str(target), "rb") as wav:
        assert wav.getnframes()/wav.getframerate() == 4 and wav.getnchannels() == 1
