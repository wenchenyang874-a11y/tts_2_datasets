"""Unit tests for local audio processing; no cloud API is called."""

from __future__ import annotations

import math
import shutil
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

try:
    from audio_utils import (
        AudioError, audio_metrics,
        ffmpeg_executable,
        mix_noise,
        normalize_wav,
        probe_wav,
        spectrum_points,
        waveform_points,
    )
except ModuleNotFoundError:
    # Also support running pytest from the workspace root.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from audio_utils import (  # type: ignore[no-redef]
        AudioError, audio_metrics,
        ffmpeg_executable,
        mix_noise,
        normalize_wav,
        probe_wav,
        spectrum_points,
        waveform_points,
    )


def _write_wav(path: Path, samples: np.ndarray, sample_rate: int) -> None:
    clipped = np.clip(samples, -1.0, 1.0 - 1.0 / 32768.0)
    pcm = np.rint(clipped * 32768.0).astype("<i2")
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm.tobytes())


def _read_wav(path: Path) -> tuple[np.ndarray, int, int, int]:
    with wave.open(str(path), "rb") as wav_file:
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        sample_rate = wav_file.getframerate()
        frames = wav_file.readframes(wav_file.getnframes())
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    return samples, sample_rate, channels, sample_width


@pytest.fixture
def tone_wav(tmp_path: Path) -> Path:
    sample_rate = 16_000
    time = np.arange(sample_rate, dtype=np.float64) / sample_rate
    path = tmp_path / "tone.wav"
    _write_wav(path, 0.25 * np.sin(2.0 * np.pi * 440.0 * time), sample_rate)
    return path


def test_probe_wav_reports_audio_contract(tone_wav: Path) -> None:
    info = probe_wav(tone_wav)

    assert info == {
        "sample_rate": 16_000,
        "channels": 1,
        "sample_width": 2,
        "frames": 16_000,
        "duration": pytest.approx(1.0, abs=1.0 / 16_000),
    }


def test_waveform_points_are_bounded(tone_wav: Path) -> None:
    points = waveform_points(tone_wav, max_points=400)

    assert 2 <= len(points) <= 400
    assert min(points) >= 0.0
    assert max(points) == pytest.approx(0.25, abs=2e-4)


def test_audio_metrics_report_tone_levels(tone_wav: Path) -> None:
    metrics = audio_metrics(tone_wav)

    assert metrics["peak"] == pytest.approx(0.25, abs=2e-4)
    assert metrics["rms"] == pytest.approx(0.25 / math.sqrt(2.0), abs=2e-4)
    assert abs(metrics["dc_offset"]) < 1e-4
    assert metrics["clipping_ratio"] == 0.0
    assert 0.0 <= metrics["silence_ratio"] < 0.02


def test_spectrum_points_find_tone_frequency(tone_wav: Path) -> None:
    spectrum = spectrum_points(tone_wav, max_points=512)

    frequencies = np.asarray(spectrum["frequency"])
    magnitude_db = np.asarray(spectrum["magnitude_db"])
    assert len(frequencies) == len(magnitude_db)
    assert 2 <= len(frequencies) <= 512
    assert frequencies[0] >= 0.0
    assert frequencies[-1] <= 8_000.0
    assert frequencies[np.argmax(magnitude_db)] == pytest.approx(440.0, abs=40.0)


def _ffmpeg_available():
    try:
        ffmpeg_executable()
        return True
    except AudioError:
        return False


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg is not installed or bundled")
def test_normalize_wav_writes_kws_audio_contract(tmp_path: Path) -> None:
    source_rate = 22_050
    time = np.arange(source_rate, dtype=np.float64) / source_rate
    source = tmp_path / "source.wav"
    destination = tmp_path / "normalized.wav"
    _write_wav(source, 0.2 * np.sin(2.0 * np.pi * 330.0 * time), source_rate)

    result = normalize_wav(source, destination, sample_rate=16_000)

    samples, sample_rate, channels, sample_width = _read_wav(destination)
    assert result["sample_rate"] == 16_000
    assert sample_rate == 16_000
    assert channels == 1
    assert sample_width == 2
    assert len(samples) == pytest.approx(16_000, abs=2)
    assert np.max(np.abs(samples)) <= 1.0


def test_mix_noise_is_reproducible_and_hits_requested_snr(tmp_path: Path) -> None:
    sample_rate = 16_000
    time = np.arange(sample_rate, dtype=np.float64) / sample_rate
    clean = tmp_path / "clean.wav"
    noise = tmp_path / "noise.wav"
    first = tmp_path / "mixed-first.wav"
    second = tmp_path / "mixed-second.wav"

    _write_wav(clean, 0.08 * np.sin(2.0 * np.pi * 440.0 * time), sample_rate)
    rng = np.random.default_rng(7)
    _write_wav(noise, rng.normal(0.0, 0.08, sample_rate * 3), sample_rate)

    first_result = mix_noise(clean, noise, first, snr_db=10.0, seed=1234)
    second_result = mix_noise(clean, noise, second, snr_db=10.0, seed=1234)

    assert first.read_bytes() == second.read_bytes()
    assert first_result["noise_offset"] == second_result["noise_offset"]
    assert first_result["snr_db"] == 10.0

    clean_samples, _, _, _ = _read_wav(clean)
    mixed, output_rate, channels, sample_width = _read_wav(first)
    residual = mixed - clean_samples
    clean_rms = float(np.sqrt(np.mean(np.square(clean_samples), dtype=np.float64)))
    noise_rms = float(np.sqrt(np.mean(np.square(residual), dtype=np.float64)))
    measured_snr = 20.0 * math.log10(clean_rms / noise_rms)

    assert measured_snr == pytest.approx(10.0, abs=0.35)
    assert output_rate == sample_rate
    assert channels == 1
    assert sample_width == 2
    assert len(mixed) == sample_rate
    assert np.max(np.abs(mixed)) <= 1.0


def test_probe_wav_rejects_corrupt_input(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.wav"
    corrupt.write_bytes(b"this is not a wav file")

    with pytest.raises(AudioError):
        probe_wav(corrupt)
