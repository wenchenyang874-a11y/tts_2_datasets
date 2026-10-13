"""Tests for persistent KWS project settings and deterministic metadata."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import project_settings  # type: ignore  # noqa: E402


def test_default_project_settings_do_not_require_a_file(tmp_path: Path) -> None:
    path = tmp_path / "project_settings.json"
    store = project_settings.ProjectSettingsStore(path)

    assert store.get() == {
        "display_text": "你好，小助手",
        "label": "你好小助手",
        "keyword_id": "ni_hao_xiao_zhu_shou",
        "updated_at": "",
    }
    assert not path.exists()


def test_update_derives_and_persists_custom_project_settings(tmp_path: Path) -> None:
    path = tmp_path / "project_settings.json"
    store = project_settings.ProjectSettingsStore(path)
    updated = store.update("  测试，唤 醒！  ")
    expected_id = "kws_" + hashlib.sha256("测试唤醒".encode()).hexdigest()[:12]

    assert updated["display_text"] == "测试，唤 醒！"
    assert updated["label"] == "测试唤醒"
    assert updated["keyword_id"] == expected_id
    assert updated["updated_at"]
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert project_settings.ProjectSettingsStore(path).get() == updated
    assert not list(tmp_path.glob("*.tmp"))


def test_unicode_and_ascii_punctuation_map_to_canonical_default() -> None:
    settings = project_settings.derive_project_settings("  你  好,\u3000小助手？！  ")

    assert settings["display_text"] == "你 好, 小助手？！"
    assert settings["label"] == "你好小助手"
    assert settings["keyword_id"] == "ni_hao_xiao_zhu_shou"


@pytest.mark.parametrize(
    "value", ["", " \t\n", "，！？", "x" * 101, "唤\n醒", "唤\x00醒"]
)
def test_invalid_display_text_is_rejected_without_overwriting(
    tmp_path: Path, value: str,
) -> None:
    store = project_settings.ProjectSettingsStore(tmp_path / "project_settings.json")
    original = store.update("可用唤醒词")

    with pytest.raises(ValueError):
        store.update(value)

    assert store.get() == original


def test_concurrent_updates_leave_a_consistent_json_document(tmp_path: Path) -> None:
    path = tmp_path / "project_settings.json"
    store = project_settings.ProjectSettingsStore(path)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda index: store.update(f"唤醒词 {index}！"), range(40)))

    persisted = json.loads(path.read_text(encoding="utf-8"))
    current = store.get()
    assert persisted["version"] == 1
    assert persisted["display_text"] == current["display_text"]
    assert persisted["label"] == current["label"]
    assert persisted["keyword_id"] == current["keyword_id"]
    assert project_settings.ProjectSettingsStore(path).get() == current
