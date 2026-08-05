"""SQLite persistence for the TTS dataset application."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    """Small thread-safe SQLite wrapper with explicit, auditable SQL."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_lock = threading.Lock()
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._init_lock, self.transaction() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS voices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT NOT NULL,
                    voice_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT '',
                    gender TEXT NOT NULL DEFAULT 'unknown',
                    age_group TEXT NOT NULL DEFAULT 'unknown',
                    languages TEXT NOT NULL DEFAULT '["zh"]',
                    source TEXT NOT NULL DEFAULT 'builtin',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(provider, voice_id, model)
                );

                CREATE TABLE IF NOT EXISTS samples (
                    id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    voice_id TEXT NOT NULL,
                    voice_name TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    text TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '你好芯宝',
                    keyword_id TEXT NOT NULL DEFAULT 'ni_hao_xin_bao',
                    speed REAL NOT NULL DEFAULT 1.0,
                    pitch REAL NOT NULL DEFAULT 1.0,
                    volume INTEGER NOT NULL DEFAULT 50,
                    seed INTEGER NOT NULL DEFAULT 0,
                    file_path TEXT NOT NULL,
                    duration REAL NOT NULL DEFAULT 0,
                    sample_rate INTEGER NOT NULL DEFAULT 16000,
                    channels INTEGER NOT NULL DEFAULT 1,
                    sha256 TEXT NOT NULL,
                    parent_id TEXT,
                    noise_id TEXT,
                    snr_db REAL,
                    split TEXT NOT NULL DEFAULT 'unassigned',
                    quality_status TEXT NOT NULL DEFAULT 'unchecked',
                    notes TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    deleted_at TEXT,
                    FOREIGN KEY(parent_id) REFERENCES samples(id)
                );
                CREATE INDEX IF NOT EXISTS idx_samples_active ON samples(deleted_at, created_at);
                CREATE INDEX IF NOT EXISTS idx_samples_provider ON samples(provider, voice_id);
                CREATE INDEX IF NOT EXISTS idx_samples_sha ON samples(sha256);

                CREATE TABLE IF NOT EXISTS noises (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'other',
                    file_path TEXT NOT NULL,
                    duration REAL NOT NULL DEFAULT 0,
                    sample_rate INTEGER NOT NULL DEFAULT 16000,
                    weight REAL NOT NULL DEFAULT 1.0,
                    min_snr REAL NOT NULL DEFAULT 0,
                    max_snr REAL NOT NULL DEFAULT 20,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    sha256 TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    deleted_at TEXT
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    total INTEGER NOT NULL DEFAULT 0,
                    completed INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS exports (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    sample_count INTEGER NOT NULL DEFAULT 0,
                    format TEXT NOT NULL DEFAULT 'generic',
                    created_at TEXT NOT NULL,
                    deleted_at TEXT
                );
                """
            )
            # Keep existing development databases forward-compatible without a
            # separate migration dependency.
            noise_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(noises)").fetchall()
            }
            if "metadata_json" not in noise_columns:
                db.execute(
                    "ALTER TABLE noises ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'"
                )
            if "deleted_at" not in noise_columns:
                db.execute("ALTER TABLE noises ADD COLUMN deleted_at TEXT")
            export_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(exports)").fetchall()
            }
            if "deleted_at" not in export_columns:
                db.execute("ALTER TABLE exports ADD COLUMN deleted_at TEXT")
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_exports_active "
                "ON exports(deleted_at, created_at)"
            )

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self.transaction() as db:
            cursor = db.execute(sql, params)
            return cursor.lastrowid

    def execute_many(self, sql: str, params: list[tuple[Any, ...]]) -> None:
        with self.transaction() as db:
            db.executemany(sql, params)

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(sql, params).fetchone()
            return dict(row) if row else None

    def all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql, params).fetchall()]

    def upsert_voice(self, voice: dict[str, Any]) -> int:
        now = utc_now()
        languages = voice.get("languages", ["zh"])
        metadata = voice.get("metadata", {})
        with self.transaction() as db:
            db.execute(
                """
                INSERT INTO voices(provider, voice_id, name, model, gender, age_group,
                                   languages, source, enabled, metadata_json, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, voice_id, model) DO UPDATE SET
                    name=excluded.name, gender=excluded.gender, age_group=excluded.age_group,
                    languages=excluded.languages, source=excluded.source,
                    enabled=excluded.enabled, metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at
                """,
                (
                    voice["provider"], voice["voice_id"], voice["name"], voice.get("model", ""),
                    voice.get("gender", "unknown"), voice.get("age_group", "unknown"),
                    json.dumps(languages, ensure_ascii=False), voice.get("source", "builtin"),
                    int(voice.get("enabled", True)), json.dumps(metadata, ensure_ascii=False), now, now,
                ),
            )
            row = db.execute(
                "SELECT id FROM voices WHERE provider=? AND voice_id=? AND model=?",
                (voice["provider"], voice["voice_id"], voice.get("model", "")),
            ).fetchone()
            return int(row["id"])

    def disable_missing_voices(
        self,
        provider: str,
        source: str,
        active_keys: set[tuple[str, str]],
    ) -> int:
        """Disable stale catalog rows while preserving history and foreign keys."""

        now = utc_now()
        with self.transaction() as db:
            rows = db.execute(
                "SELECT id,voice_id,model FROM voices WHERE provider=? AND source=? AND enabled=1",
                (provider, source),
            ).fetchall()
            stale_ids = [
                int(row["id"])
                for row in rows
                if (str(row["voice_id"]), str(row["model"])) not in active_keys
            ]
            if stale_ids:
                db.executemany(
                    "UPDATE voices SET enabled=0,updated_at=? WHERE id=?",
                    [(now, voice_id) for voice_id in stale_ids],
                )
            return len(stale_ids)

    @staticmethod
    def decode_json_fields(row: dict[str, Any]) -> dict[str, Any]:
        for key in ("languages", "metadata_json", "payload_json", "result_json"):
            if key in row and isinstance(row[key], str):
                try:
                    row[key.removesuffix("_json")] = json.loads(row[key])
                except json.JSONDecodeError:
                    row[key.removesuffix("_json")] = {} if key.endswith("_json") else []
        return row
