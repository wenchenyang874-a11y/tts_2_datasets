"""Thread-safe, file-backed KWS project settings.

The display text is the only user-controlled source value.  Training labels and
internal IDs are derived deterministically so punctuation variants cannot create
accidentally inconsistent dataset metadata.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_DISPLAY_TEXT = "你好，小助手"
DEFAULT_LABEL = "你好小助手"
DEFAULT_KEYWORD_ID = "ni_hao_xiao_zhu_shou"
MAX_DISPLAY_TEXT_LENGTH = 100


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_display_text(value: str) -> str:
    """Validate and normalize a user-facing wake-word string."""

    if not isinstance(value, str):
        raise ValueError("display_text 必须是字符串")
    normalized = unicodedata.normalize("NFC", value)
    if any(unicodedata.category(character).startswith("C") for character in normalized):
        raise ValueError("display_text 不能包含控制字符")
    display_text = " ".join(normalized.split())
    if not display_text:
        raise ValueError("display_text 不能为空")
    if len(display_text) > MAX_DISPLAY_TEXT_LENGTH:
        raise ValueError(
            f"display_text 不能超过 {MAX_DISPLAY_TEXT_LENGTH} 个字符"
        )
    if not derive_label(display_text):
        raise ValueError("display_text 不能只包含标点或空白")
    return display_text


def derive_label(display_text: str) -> str:
    """Remove Unicode punctuation and whitespace from a display string."""

    return "".join(
        character
        for character in unicodedata.normalize("NFC", display_text)
        if not character.isspace()
        and not unicodedata.category(character).startswith("P")
    )


def derive_keyword_id(label: str) -> str:
    """Return the canonical default ID or a stable ASCII custom-word ID."""

    if label == DEFAULT_LABEL:
        return DEFAULT_KEYWORD_ID
    digest = hashlib.sha256(label.encode("utf-8")).hexdigest()[:12]
    return f"kws_{digest}"


def derive_project_settings(display_text: str, updated_at: str = "") -> dict[str, str]:
    normalized = normalize_display_text(display_text)
    label = derive_label(normalized)
    return {
        "display_text": normalized,
        "label": label,
        "keyword_id": derive_keyword_id(label),
        "updated_at": updated_at,
    }


class ProjectSettingsStore:
    """Persist one internally consistent set of KWS project settings."""

    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._data = self._load()

    @staticmethod
    def _default() -> dict[str, str]:
        return derive_project_settings(DEFAULT_DISPLAY_TEXT)

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return self._default()
        try:
            raw: Any = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"项目配置文件无法读取：{self.path.name}") from exc
        if not isinstance(raw, dict):
            raise RuntimeError(f"项目配置文件结构无效：{self.path.name}")
        try:
            settings = derive_project_settings(
                raw.get("display_text"), str(raw.get("updated_at") or "")
            )
        except ValueError as exc:
            raise RuntimeError(f"项目配置文件结构无效：{self.path.name}") from exc
        try:
            os.chmod(self.path, 0o600)
        except OSError as exc:
            raise RuntimeError(f"无法保护项目配置文件权限：{self.path.name}") from exc
        return settings

    def _write_locked(self) -> None:
        payload = json.dumps(
            {"version": 1, **self._data}, ensure_ascii=False, indent=2
        ).encode("utf-8") + b"\n"
        descriptor = -1
        temporary_path = ""
        try:
            descriptor, temporary_path = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
            )
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                descriptor = -1
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = ""
            os.chmod(self.path, 0o600)
            try:
                directory_fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                # Some filesystems do not support fsync on directory handles.
                pass
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_path:
                try:
                    os.unlink(temporary_path)
                except FileNotFoundError:
                    pass

    def get(self) -> dict[str, str]:
        with self._lock:
            return copy.deepcopy(self._data)

    def update(self, display_text: str) -> dict[str, str]:
        with self._lock:
            self._data = derive_project_settings(display_text, _utc_now())
            self._write_locked()
            return copy.deepcopy(self._data)


__all__ = [
    "DEFAULT_DISPLAY_TEXT",
    "DEFAULT_KEYWORD_ID",
    "DEFAULT_LABEL",
    "MAX_DISPLAY_TEXT_LENGTH",
    "ProjectSettingsStore",
    "derive_keyword_id",
    "derive_label",
    "derive_project_settings",
    "normalize_display_text",
]
