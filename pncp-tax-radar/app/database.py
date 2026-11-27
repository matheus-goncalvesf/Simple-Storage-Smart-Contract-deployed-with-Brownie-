"""Persistência em SQLite: auditorias, cache de respostas do PNCP e controle da pasta monitorada."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import SEVERITY_RANK

SCHEMA = """
CREATE TABLE IF NOT EXISTS audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    ref TEXT,
    title TEXT,
    pncp_url TEXT,
    max_severity TEXT,
    max_verdict TEXT,
    max_impact REAL,
    n_alerts INTEGER NOT NULL DEFAULT 0,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audits_source_ref ON audits(source, ref);
CREATE TABLE IF NOT EXISTS pncp_cache (
    key TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inbox_seen (
    path TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    audit_id INTEGER,
    processed_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as con:
            con.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()

    # ------------------------------------------------------------ auditorias

    def save_audit(self, result: dict, *, replace_ref: bool = True) -> int:
        with self._lock, self._connect() as con:
            if replace_ref and result.get("ref") and result.get("source") in ("pncp", "inbox"):
                con.execute("DELETE FROM audits WHERE source = ? AND ref = ?", (result["source"], result["ref"]))
            cur = con.execute(
                "INSERT INTO audits (source, ref, title, pncp_url, max_severity, max_verdict, max_impact, n_alerts, payload, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    result.get("source", "upload"),
                    result.get("ref"),
                    result.get("title"),
                    result.get("pncp_url"),
                    result.get("max_severity"),
                    result.get("max_verdict"),
                    result.get("max_impact"),
                    result.get("n_alerts", 0),
                    json.dumps(result, ensure_ascii=False),
                    result.get("created_at") or _now(),
                ),
            )
            return int(cur.lastrowid)

    def get_audit(self, audit_id: int) -> dict | None:
        with self._connect() as con:
            row = con.execute("SELECT id, payload FROM audits WHERE id = ?", (audit_id,)).fetchone()
        if not row:
            return None
        data = json.loads(row["payload"])
        data["id"] = row["id"]
        return data

    def list_audits(self, *, min_severity: str | None = None, source: str | None = None, limit: int = 500) -> list[dict]:
        sql = "SELECT id, payload FROM audits"
        args: list[Any] = []
        if source:
            sql += " WHERE source = ?"
            args.append(source)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._connect() as con:
            rows = con.execute(sql, args).fetchall()
        out = []
        threshold = SEVERITY_RANK.get(min_severity or "", 0)
        for row in rows:
            data = json.loads(row["payload"])
            data["id"] = row["id"]
            if threshold and SEVERITY_RANK.get(data.get("max_severity") or "", 0) < threshold:
                continue
            out.append(data)
        return out

    def delete_audit(self, audit_id: int) -> bool:
        with self._lock, self._connect() as con:
            cur = con.execute("DELETE FROM audits WHERE id = ?", (audit_id,))
            con.execute("UPDATE inbox_seen SET audit_id = NULL WHERE audit_id = ?", (audit_id,))
            return cur.rowcount > 0

    # ------------------------------------------------------------ cache PNCP

    def cache_get(self, key: str, max_age: timedelta) -> Any | None:
        with self._connect() as con:
            row = con.execute("SELECT payload, fetched_at FROM pncp_cache WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        fetched = datetime.fromisoformat(row["fetched_at"])
        if datetime.now(timezone.utc) - fetched > max_age:
            return None
        return json.loads(row["payload"])

    def cache_put(self, key: str, payload: Any) -> None:
        with self._lock, self._connect() as con:
            con.execute(
                "INSERT INTO pncp_cache (key, payload, fetched_at) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET payload = excluded.payload, fetched_at = excluded.fetched_at",
                (key, json.dumps(payload, ensure_ascii=False), _now()),
            )

    # ------------------------------------------------------------ pasta monitorada

    def inbox_fingerprint(self, path: str) -> str | None:
        with self._connect() as con:
            row = con.execute("SELECT fingerprint FROM inbox_seen WHERE path = ?", (path,)).fetchone()
        return row["fingerprint"] if row else None

    def inbox_mark(self, path: str, fingerprint: str, audit_id: int | None) -> None:
        with self._lock, self._connect() as con:
            con.execute(
                "INSERT INTO inbox_seen (path, fingerprint, audit_id, processed_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(path) DO UPDATE SET fingerprint = excluded.fingerprint, audit_id = excluded.audit_id,"
                " processed_at = excluded.processed_at",
                (path, fingerprint, audit_id, _now()),
            )

    def inbox_entries(self) -> list[dict]:
        with self._connect() as con:
            rows = con.execute("SELECT path, fingerprint, audit_id, processed_at FROM inbox_seen ORDER BY processed_at DESC").fetchall()
        return [dict(r) for r in rows]
