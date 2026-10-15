"""Native recording workflow using simulated PCM, never the user's microphone."""
import time
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QObject, Signal, QBuffer, QByteArray, QIODevice, QTimer, Qt
from PySide6.QtMultimedia import QAudio
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from .reference_recording import ReferenceRecordingDialog


class OfflineDevice:
    def id(self):
        return b"owned-acceptance-input"
    def description(self):
        return "离线模拟输入（验收）"
    def isFormatSupported(self, format):
        return True


class OfflineSource(QObject):
    stateChanged = Signal(object)
    def __init__(self, device, format, parent):
        super().__init__(parent)
        self.stream = QBuffer(self)
        self.stream.open(QIODevice.OpenModeFlag.ReadOnly)
        self.closed = False
    def start(self):
        return self.stream
    def error(self):
        return QAudio.Error.NoError
    def stop(self):
        self.closed = True


def verify_reference_recording(application, window, form, destination):
    errors = []
    recorded = []
    def drive():
        dialog = QApplication.activeModalWidget()
        try:
            assert isinstance(dialog, ReferenceRecordingDialog)
            QTest.mouseClick(dialog.record_button, Qt.MouseButton.LeftButton)
            assert dialog.capture.source and not dialog.use_button.isEnabled()
            def feed(seconds):
                stream = dialog.capture.stream
                stream.close()
                stream.setData(QByteArray(b"\x10\x00"*int(24000*seconds)))
                stream.open(QIODevice.OpenModeFlag.ReadOnly)
                stream.readyRead.emit()
                application.processEvents()
            feed(1)
            QTest.mouseClick(dialog.stop_button, Qt.MouseButton.LeftButton)
            assert not dialog.use_button.isEnabled() and "3 秒" in dialog.problem.text()
            QTest.mouseClick(dialog.record_button, Qt.MouseButton.LeftButton)
            feed(4)
            application.processEvents()
            time.sleep(.05)
            window.screen().grabWindow(dialog.winId()).save(str(destination/"reference-recording-active.png"))
            source = dialog.capture.source
            QTest.mouseClick(dialog.stop_button, Qt.MouseButton.LeftButton)
            assert source.closed and dialog.valid and dialog.capture.writer is None
            QTest.mouseClick(dialog.play_button, Qt.MouseButton.LeftButton)
            deadline = time.monotonic()+10
            while dialog.player.playbackState() == dialog.player.PlaybackState.StoppedState:
                assert time.monotonic() < deadline, "recording playback did not start"
                application.processEvents()
                time.sleep(.02)
            QTest.mouseClick(dialog.play_button, Qt.MouseButton.LeftButton)
            assert dialog.player.playbackState() == dialog.player.PlaybackState.StoppedState
            application.processEvents()
            window.screen().grabWindow(dialog.winId()).save(str(destination/"reference-recording-ready.png"))
            QTest.mouseClick(dialog.use_button, Qt.MouseButton.LeftButton)
            recorded.append(dialog.accepted_path)
        except Exception as exc:
            errors.append(repr(exc))
            if dialog:
                dialog.reject()
    # Only the input source is simulated. Dialog events, WAV writer, playback,
    # form handoff and backend multipart import use their production paths.
    with patch("desktop.audio_capture.QAudioSource", OfflineSource), patch("desktop.reference_recording.QMediaDevices.audioInputs", return_value=[OfflineDevice()]):
        QTimer.singleShot(0, drive)
        QTest.mouseClick(form.record_button, Qt.MouseButton.LeftButton)
    assert not errors, errors
    assert recorded and form.path == recorded[0] and form.path.is_file()
    assert "现场录音" in form.file_label.text()
    application.processEvents()
    window.screen().grabWindow(form.winId()).save(str(destination/"reference-recording-selected-in-form.png"))
    return Path(form.path)
