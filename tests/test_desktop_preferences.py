import sys

import pytest

from desktop.preferences import DesktopPreferences


def test_selected_directory_persists_without_initializing_or_moving_data(tmp_path):
    preferences = DesktopPreferences(tmp_path / "preferences.json")
    assert preferences.data_directory() is None
    directory = tmp_path / "自定义 数据"
    directory.mkdir()
    sentinel = directory / "my-data.txt"
    sentinel.write_text("owned test data", encoding="utf-8")
    preferences.save_data_directory(directory)
    assert DesktopPreferences(preferences.path).data_directory() == directory.resolve()
    assert list(directory.iterdir()) == [sentinel]


def test_unwritable_or_invalid_target_keeps_previous_preference(tmp_path):
    preferences = DesktopPreferences(tmp_path / "preferences.json")
    preferences.save_data_directory(tmp_path / "valid")
    before = preferences.path.read_bytes()
    invalid = tmp_path / "file"
    invalid.write_text("not a folder")
    with pytest.raises(OSError):
        preferences.save_data_directory(invalid)
    assert preferences.path.read_bytes() == before


def test_frozen_installation_tree_cannot_be_data_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "app/TTS.exe"))
    with pytest.raises(ValueError, match="安装目录"):
        DesktopPreferences(tmp_path / "prefs.json").save_data_directory(tmp_path / "app/data")


def test_invalid_preference_does_not_silently_open_empty_default_data(tmp_path):
    path = tmp_path / "preferences.json"
    path.write_text('{"data_directory":"relative"}')
    with pytest.raises(ValueError):
        DesktopPreferences(path).data_directory()
