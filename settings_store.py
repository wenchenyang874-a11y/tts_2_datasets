"""Thread-safe, file-backed provider settings for the TTS dataset application.

Cloud credentials intentionally live outside SQLite and environment variables.  The
JSON file is private to the server process (mode 0600), and each update is written to
a temporary file before an atomic replace so readers never observe partial JSON.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

try:
    from .private_settings import decode_settings, encode_settings
except ImportError:
    from private_settings import decode_settings, encode_settings


PRICING_UNITS = frozenset({"per_call", "per_1k_chars"})
PRICING_FIELDS = frozenset({"pricing_unit", "pricing_rate", "pricing_currency"})
MAX_PRICING_RATE_LENGTH = 64
MAX_PRICING_CURRENCY_LENGTH = 16

PROVIDER_FIELDS: dict[str, frozenset[str]] = {
    "xiaomi": frozenset({"api_key", "endpoint", *PRICING_FIELDS}),
    "aliyun": frozenset(
        {
            "api_key",
            "workspace_id",
            "region",
            "endpoint",
            "customization_endpoint",
            *PRICING_FIELDS,
        }
    ),
    "volcengine": frozenset(
        {
            "app_id",
            "access_token",
            "cluster",
            "endpoint",
            "access_key_id",
            "access_key_secret",
            "catalog_endpoint",
            "api_key",
            "v3_endpoint",
            *PRICING_FIELDS,
        }
    ),
}
SECRET_FIELDS: dict[str, frozenset[str]] = {
    "xiaomi": frozenset({"api_key"}),
    "aliyun": frozenset({"api_key"}),
    "volcengine": frozenset({"access_token", "access_key_secret", "api_key"}),
}
REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    "xiaomi": frozenset({"api_key"}),
    "aliyun": frozenset({"api_key"}),
    "volcengine": frozenset({"app_id", "access_token"}),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mask_secret(value: str) -> str:
    """Return a useful fingerprint without disclosing a credential."""

    if not value:
        return ""
    if len(value) <= 4:
        return "****"
    return f"****{value[-4:]}"


def _validate_pricing_field(field: str, value: str) -> None:
    if field == "pricing_unit" and value not in PRICING_UNITS:
        allowed = ", ".join(sorted(PRICING_UNITS))
        raise ValueError(f"pricing_unit 仅支持：{allowed}")
    if field == "pricing_rate":
        if len(value) > MAX_PRICING_RATE_LENGTH:
            raise ValueError(
                f"pricing_rate 超过 {MAX_PRICING_RATE_LENGTH} 字符限制"
            )
        try:
            rate = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("pricing_rate 必须是非负有限数值") from exc
        if not rate.is_finite() or rate < 0:
            raise ValueError("pricing_rate 必须是非负有限数值")
    if field == "pricing_currency" and len(value) > MAX_PRICING_CURRENCY_LENGTH:
        raise ValueError(
            f"pricing_currency 超过 {MAX_PRICING_CURRENCY_LENGTH} 字符限制"
        )


class ProviderSettingsStore:
    """Persist provider configuration with process-local reader/writer locking."""

    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._data = self._load()
        # Imported Web credentials may be plaintext. The desktop owns startup
        # and opts into DPAPI before this store is constructed; seal an existing
        # file immediately, without waiting for the first settings edit.
        if self.path.exists() and os.getenv("TTS_DESKTOP_PROTECT_SETTINGS") == "1":
            self._write_locked()

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"version": 1, "providers": {}, "updated_at": ""}

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                raw = decode_settings(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"供应商配置文件无法读取：{self.path.name}") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("providers", {}), dict):
            raise RuntimeError(f"供应商配置文件结构无效：{self.path.name}")

        clean = self._empty()
        clean["updated_at"] = str(raw.get("updated_at") or "")
        for provider, fields in PROVIDER_FIELDS.items():
            value = raw.get("providers", {}).get(provider, {})
            if not isinstance(value, dict):
                continue
            clean["providers"][provider] = {
                key: str(item).strip()
                for key, item in value.items()
                if key in fields and item is not None and str(item).strip()
            }
        # Repair permissive permissions left by a manual file copy.
        try:
            os.chmod(self.path, 0o600)
        except OSError as exc:
            raise RuntimeError(f"无法保护供应商配置文件权限：{self.path.name}") from exc
        return clean

    def _write_locked(self) -> None:
        self._data["updated_at"] = _utc_now()
        payload = json.dumps(self._data, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        payload = encode_settings(payload)
        descriptor = -1
        temporary_path = ""
        try:
            descriptor, temporary_path = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
            )
            # fchmod does not exist on Windows. Its chmod fallback only sets
            # file flags there; credential confidentiality comes from DPAPI,
            # not from pretending POSIX mode bits are Windows ACLs.
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            else:
                os.chmod(temporary_path, 0o600)
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
                # The file itself is already durable; some filesystems do not
                # support fsync on a directory descriptor.
                pass
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_path:
                try:
                    os.unlink(temporary_path)
                except FileNotFoundError:
                    pass

    def get_provider(self, provider: str) -> dict[str, str]:
        if provider not in PROVIDER_FIELDS:
            raise ValueError(f"不支持的供应商：{provider}")
        with self._lock:
            return copy.deepcopy(self._data["providers"].get(provider, {}))

    def update_provider(
        self,
        provider: str,
        values: Mapping[str, Any],
        *,
        preserve_empty_secrets: bool = True,
    ) -> dict[str, str]:
        if provider not in PROVIDER_FIELDS:
            raise ValueError(f"不支持的供应商：{provider}")
        unknown = set(values) - PROVIDER_FIELDS[provider]
        if unknown:
            raise ValueError(f"不支持的配置字段：{', '.join(sorted(unknown))}")
        with self._lock:
            current = dict(self._data["providers"].get(provider, {}))
            for key, raw_value in values.items():
                value = str(raw_value).strip() if raw_value is not None else ""
                if len(value) > 4096:
                    raise ValueError(f"配置字段 {key} 超过 4096 字符限制")
                if not value and preserve_empty_secrets and key in SECRET_FIELDS[provider]:
                    continue
                if value:
                    if key in PRICING_FIELDS:
                        _validate_pricing_field(key, value)
                    current[key] = value
                else:
                    current.pop(key, None)
            self._data["providers"][provider] = current
            self._write_locked()
            return copy.deepcopy(current)

    def clear_provider(self, provider: str) -> None:
        if provider not in PROVIDER_FIELDS:
            raise ValueError(f"不支持的供应商：{provider}")
        with self._lock:
            self._data["providers"].pop(provider, None)
            self._write_locked()

    def public_provider(self, provider: str) -> dict[str, Any]:
        config = self.get_provider(provider)
        result: dict[str, Any] = {
            "id": provider,
            "configured": all(config.get(key) for key in REQUIRED_FIELDS[provider]),
        }
        if provider == "volcengine":
            result["v1_configured"] = all(
                config.get(key) for key in ("app_id", "access_token")
            )
            result["v2_configured"] = bool(config.get("api_key"))
            result["synthesis_configured"] = result["v1_configured"] or result["v2_configured"]
            result["configured"] = result["synthesis_configured"]
            result["catalog_configured"] = all(
                config.get(key) for key in ("access_key_id", "access_key_secret")
            )
        elif provider == "aliyun":
            result["synthesis_configured"] = bool(config.get("api_key"))
            result["custom_voice_query_configured"] = bool(config.get("api_key"))
        elif provider == "xiaomi":
            result["synthesis_configured"] = bool(config.get("api_key"))
            result["catalog_configured"] = True  # Official preset snapshot, not an account permission check.
        for field in sorted(PROVIDER_FIELDS[provider]):
            if field in SECRET_FIELDS[provider]:
                value = config.get(field, "")
                result[field] = {
                    "configured": bool(value),
                    "masked": _mask_secret(value),
                }
            else:
                result[field] = config.get(field, "")
        return result

    def public_all(self) -> list[dict[str, Any]]:
        return [self.public_provider(provider) for provider in PROVIDER_FIELDS]


_default_lock = threading.Lock()
_default_store: ProviderSettingsStore | None = None


def set_default_settings_store(store: ProviderSettingsStore) -> None:
    global _default_store
    with _default_lock:
        _default_store = store


def get_default_settings_store() -> ProviderSettingsStore:
    global _default_store
    with _default_lock:
        if _default_store is None:
            app_dir = Path(__file__).resolve().parent
            source_default = (
                app_dir.parent.parent / "data" / "tts_app"
                if app_dir.parent.name == "tools"
                else app_dir / "data"
            )
            data_root = Path(os.getenv("TTS_APP_DATA_DIR", source_default)).resolve()
            _default_store = ProviderSettingsStore(data_root / "provider_settings.json")
        return _default_store


__all__ = [
    "MAX_PRICING_CURRENCY_LENGTH",
    "MAX_PRICING_RATE_LENGTH",
    "PRICING_FIELDS",
    "PRICING_UNITS",
    "PROVIDER_FIELDS",
    "REQUIRED_FIELDS",
    "SECRET_FIELDS",
    "ProviderSettingsStore",
    "get_default_settings_store",
    "set_default_settings_store",
]
