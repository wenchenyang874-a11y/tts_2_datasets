"""Security and concurrency tests for the private provider settings store."""

from __future__ import annotations

import json
import os
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
        "api_key": "unit-test-server-side-only-1234",
        "workspace_id": "ws-unit-test",
        "region": "cn-beijing",
    })

    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["providers"]["aliyun"]["api_key"] == "unit-test-server-side-only-1234"
    public = store.public_provider("aliyun")
    rendered = json.dumps(public, ensure_ascii=False)
    assert public["api_key"] == {"configured": True, "masked": "****1234"}
    assert "unit-test-server-side-only-1234" not in rendered
    assert not list(tmp_path.glob("*.tmp"))


def test_empty_secret_preserves_value_and_delete_clears_provider(tmp_path: Path) -> None:
    store = settings_store.ProviderSettingsStore(tmp_path / "provider_settings.json")
    store.update_provider("volcengine", {
        "app_id": "app-id",
        "api_key": "synthesis-token",
        "access_key_id": "catalog-ak",
        "access_key_secret": "catalog-sk",
    })
    store.update_provider("volcengine", {
        "api_key": "",
        "access_key_secret": "",
        "app_id": "changed-app",
    })

    stored = store.get_provider("volcengine")
    assert stored["api_key"] == "synthesis-token"
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


@pytest.mark.parametrize("provider", ["aliyun", "volcengine"])
def test_pricing_fields_are_non_secret_public_strings(
    tmp_path: Path, provider: str,
) -> None:
    store = settings_store.ProviderSettingsStore(tmp_path / "provider_settings.json")

    stored = store.update_provider(provider, {
        "pricing_unit": "per_1k_chars",
        "pricing_rate": 2,
        "pricing_currency": "CNY",
    })

    assert stored["pricing_unit"] == "per_1k_chars"
    assert stored["pricing_rate"] == "2"
    assert stored["pricing_currency"] == "CNY"
    public = store.public_provider(provider)
    assert public["pricing_unit"] == "per_1k_chars"
    assert public["pricing_rate"] == "2"
    assert public["pricing_currency"] == "CNY"


def test_legacy_settings_without_pricing_fields_remain_compatible(tmp_path: Path) -> None:
    path = tmp_path / "provider_settings.json"
    path.write_text(
        json.dumps({
            "version": 1,
            "providers": {
                "aliyun": {"region": "cn-beijing"},
                "volcengine": {"cluster": "volcano_tts"},
            },
        }),
        encoding="utf-8",
    )

    store = settings_store.ProviderSettingsStore(path)

    for provider in ("aliyun", "volcengine"):
        public = store.public_provider(provider)
        assert public["pricing_unit"] == ""
        assert public["pricing_rate"] == ""
        assert public["pricing_currency"] == ""


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("pricing_unit", "per_character", "pricing_unit"),
        ("pricing_rate", "not-a-number", "非负有限数值"),
        ("pricing_rate", "-0.1", "非负有限数值"),
        (
            "pricing_rate",
            "1" * (settings_store.MAX_PRICING_RATE_LENGTH + 1),
            "pricing_rate",
        ),
        (
            "pricing_currency",
            "C" * (settings_store.MAX_PRICING_CURRENCY_LENGTH + 1),
            "pricing_currency",
        ),
    ],
)
def test_invalid_pricing_fields_are_rejected(
    tmp_path: Path, field: str, value: str, message: str,
) -> None:
    store = settings_store.ProviderSettingsStore(tmp_path / "provider_settings.json")

    with pytest.raises(ValueError, match=message):
        store.update_provider("aliyun", {field: value})


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
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
