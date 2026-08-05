"""Security and concurrency tests for the private provider settings store."""

from __future__ import annotations

import json
import stat
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import settings_store  # type: ignore  # noqa: E402


def test_settings_file_is_atomic_private_and_secrets_are_masked(tmp_path: Path) -> None:
    path = tmp_path / "provider_settings.json"
    store = settings_store.ProviderSettingsStore(path)
    store.update_provider("aliyun", {
        "api_key": "TEST_ONLY_SERVER_SECRET_1234",
        "workspace_id": "ws-unit-test",
        "region": "cn-beijing",
    })

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["providers"]["aliyun"]["api_key"] == "TEST_ONLY_SERVER_SECRET_1234"
    public = store.public_provider("aliyun")
    rendered = json.dumps(public, ensure_ascii=False)
    assert public["api_key"] == {"configured": True, "masked": "****1234"}
    assert "TEST_ONLY_SERVER_SECRET_1234" not in rendered
    assert not list(tmp_path.glob("*.tmp"))


def test_empty_secret_preserves_value_and_delete_clears_provider(tmp_path: Path) -> None:
    store = settings_store.ProviderSettingsStore(tmp_path / "provider_settings.json")
    store.update_provider("volcengine", {
        "app_id": "app-id",
        "access_token": "synthesis-token",
        "access_key_id": "catalog-ak",
        "access_key_secret": "catalog-sk",
    })
    store.update_provider("volcengine", {
        "access_token": "",
        "access_key_secret": "",
        "app_id": "changed-app",
    })

    stored = store.get_provider("volcengine")
    assert stored["access_token"] == "synthesis-token"
    assert stored["access_key_secret"] == "catalog-sk"
    assert stored["app_id"] == "changed-app"
    public = store.public_provider("volcengine")
    assert public["synthesis_configured"] is True
    assert public["catalog_configured"] is True

    store.clear_provider("volcengine")
    assert store.get_provider("volcengine") == {}
    assert store.public_provider("volcengine")["configured"] is False


def test_removed_volc_project_name_is_not_accepted(tmp_path: Path) -> None:
    store = settings_store.ProviderSettingsStore(tmp_path / "provider_settings.json")

    with pytest.raises(ValueError, match="project_name"):
        store.update_provider("volcengine", {"project_name": "unused"})


def test_concurrent_updates_always_leave_valid_json(tmp_path: Path) -> None:
    path = tmp_path / "provider_settings.json"
    store = settings_store.ProviderSettingsStore(path)

    def update(index: int) -> None:
        store.update_provider("aliyun", {
            "api_key": f"secret-{index:04d}",
            "workspace_id": f"workspace-{index:04d}",
        })
        assert store.get_provider("aliyun")["api_key"]

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(update, range(50)))

    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["version"] == 1
    assert persisted["providers"]["aliyun"]["api_key"].startswith("secret-")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
