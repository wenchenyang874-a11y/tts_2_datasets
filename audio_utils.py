"""Audio normalization, analysis and deterministic augmentation helpers."""

from __future__ import annotations

import hashlib
import math
import random
import shutil
import subprocess
import wave
from pathlib import Path
from typing import Any

import numpy as np


class AudioError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def probe_wav(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        with wave.open(str(source), "rb") as wav:
            frames = wav.getnframes()
            rate = wav.getframerate()
            return {
                "sample_rate": rate,
                "channels": wav.getnchannels(),
                "sample_width": wav.getsampwidth(),
                "frames": frames,
                "duration": frames / rate if rate else 0.0,
            }
    except (wave.Error, EOFError, OSError) as exc:
        raise AudioError(f"Invalid WAV file: {source.name}: {exc}") from exc


def normalize_wav(src: str | Path, dst: str | Path, sample_rate: int = 16000) -> dict[str, Any]:
    source, target = Path(src), Path(dst)
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
        "-ac", "1", "-ar", str(sample_rate), "-c:a", "pcm_s16le", str(target),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    if result.returncode != 0:
        target.unlink(missing_ok=True)
        raise AudioError(f"ffmpeg normalization failed: {result.stderr.strip()[-500:]}")
    return probe_wav(target)


def _read_pcm16(path: Path) -> tuple[np.ndarray, int]:
    info = probe_wav(path)
    if info["channels"] != 1 or info["sample_width"] != 2:
        raise AudioError("Audio must be normalized to mono PCM16 before processing")
    with wave.open(str(path), "rb") as wav:
        values = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").astype(np.float32)
    return values / 32768.0, int(info["sample_rate"])


def _write_pcm16(path: Path, values: np.ndarray, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(values, -1.0, 0.999969) * 32768.0).astype("<i2")
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm.tobytes())


def _read_noise_segment(
    path: Path, target_frames: int, rng: random.Random,
) -> tuple[np.ndarray, int, int]:
    """Read only the noise frames needed for one mix operation."""

    info = probe_wav(path)
    if info["channels"] != 1 or info["sample_width"] != 2:
        raise AudioError("Audio must be normalized to mono PCM16 before processing")
    frame_count = int(info["frames"])
    if frame_count <= 0:
        raise AudioError("Cannot mix empty audio")
    max_start = max(0, frame_count - target_frames)
    start = rng.randint(0, max_start) if max_start else 0
    try:
        with wave.open(str(path), "rb") as wav:
            if start:
                wav.setpos(start)
            raw = wav.readframes(min(frame_count, target_frames))
    except (wave.Error, EOFError, OSError) as exc:
        raise AudioError(f"Invalid WAV file: {path.name}: {exc}") from exc
    values = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if not len(values):
        raise AudioError("Cannot mix empty audio")
    if len(values) < target_frames:
        values = np.tile(values, math.ceil(target_frames / len(values)))
    return values[:target_frames].copy(), int(info["sample_rate"]), start


def mix_noise(
    clean: str | Path,
    noise: str | Path,
    out: str | Path,
    snr_db: float,
    seed: int = 0,
) -> dict[str, Any]:
    clean_audio, sample_rate = _read_pcm16(Path(clean))
    if not len(clean_audio):
        raise AudioError("Cannot mix empty audio")
    rng = random.Random(seed)
    segment, noise_rate, start = _read_noise_segment(
        Path(noise), len(clean_audio), rng
    )
    if noise_rate != sample_rate:
        raise AudioError("Clean and noise sample rates differ; normalize both first")

    clean_rms = float(np.sqrt(np.mean(np.square(clean_audio), dtype=np.float64)))
    noise_rms = float(np.sqrt(np.mean(np.square(segment), dtype=np.float64)))
    if noise_rms < 1e-8:
        raise AudioError("Selected noise segment is silent")
    target_noise_rms = max(clean_rms, 1e-6) / (10.0 ** (snr_db / 20.0))
    mixed = clean_audio + segment * (target_noise_rms / noise_rms)
    peak = float(np.max(np.abs(mixed)))
    if peak > 0.999:
        mixed *= 0.999 / peak
    _write_pcm16(Path(out), mixed, sample_rate)
    info = probe_wav(out)
    info.update({"snr_db": snr_db, "seed": seed, "noise_offset": start})
    return info


def waveform_points(path: str | Path, max_points: int = 1000) -> list[float]:
    audio, _ = _read_pcm16(Path(path))
    if not len(audio):
        return []
    step = max(1, math.ceil(len(audio) / max_points))
    usable = len(audio) // step * step
    if not usable:
        return audio.tolist()
    blocks = audio[:usable].reshape(-1, step)
    return np.max(np.abs(blocks), axis=1).astype(float).tolist()


def spectrum_points(path: str | Path, max_points: int = 512) -> dict[str, list[float]]:
    audio, sample_rate = _read_pcm16(Path(path))
    if not len(audio):
        return {"frequency": [], "magnitude_db": []}
    size = min(16384, max(512, 1 << int(math.ceil(math.log2(len(audio))))))
    segment = audio[:size]
    if len(segment) < size:
        segment = np.pad(segment, (0, size - len(segment)))
    windowed = segment * np.hanning(size)
    magnitude = np.abs(np.fft.rfft(windowed))
    db = 20.0 * np.log10(np.maximum(magnitude, 1e-8))
    frequencies = np.fft.rfftfreq(size, 1.0 / sample_rate)
    step = max(1, math.ceil(len(db) / max_points))
    return {
        "frequency": frequencies[::step].astype(float).tolist(),
        "magnitude_db": db[::step].astype(float).tolist(),
    }


def audio_metrics(path: str | Path) -> dict[str, float]:
    """Return lightweight quality indicators for UI review, not model scoring."""
    audio, _ = _read_pcm16(Path(path))
    if not len(audio):
        return {
            "peak": 0.0, "rms": 0.0, "dc_offset": 0.0,
            "clipping_ratio": 0.0, "silence_ratio": 1.0,
        }
    absolute = np.abs(audio)
    return {
        "peak": float(np.max(absolute)),
        "rms": float(np.sqrt(np.mean(np.square(audio), dtype=np.float64))),
        "dc_offset": float(np.mean(audio, dtype=np.float64)),
        "clipping_ratio": float(np.mean(absolute >= 0.999)),
        "silence_ratio": float(np.mean(absolute < 10.0 ** (-50.0 / 20.0))),
    }


def stage_uploaded_audio(src: Path, dst: Path) -> dict[str, Any]:
    """Normalize an uploaded audio file and clean its temporary source."""
    try:
        return normalize_wav(src, dst)
    finally:
        src.unlink(missing_ok=True)
