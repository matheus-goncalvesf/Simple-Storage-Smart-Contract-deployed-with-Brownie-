"""Pasta monitorada ./inbox_editais: cada arquivo ou subpasta de primeiro nível é um caso."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from pathlib import Path
from typing import Callable

from .auditor import audit_paths
from .extractor import SUPPORTED_EXTENSIONS, UNSUPPORTED_HINT

log = logging.getLogger(__name__)

SETTLE_SECONDS = 3.0  # ignora o que ainda está sendo copiado


def _entry_files(entry: Path) -> list[Path]:
    ok = SUPPORTED_EXTENSIONS | set(UNSUPPORTED_HINT)
    if entry.is_file():
        return [entry] if entry.suffix.lower() in ok else []
    if entry.is_dir():
        return sorted(
            f for f in entry.rglob("*")
            if f.is_file() and f.suffix.lower() in ok and not any(p.startswith(".") for p in f.relative_to(entry).parts)
        )
    return []


def list_inbox_entries(inbox: Path, settle: float = SETTLE_SECONDS) -> list[tuple[Path, str]]:
    if not inbox.exists():
        return []
    now = time.time()
    out = []
    for entry in sorted(inbox.iterdir()):
        if entry.name.startswith("."):
            continue
        files = _entry_files(entry)
        if not files:
            continue
        stats = [(f, f.stat()) for f in files]
        if settle and max(s.st_mtime for _, s in stats) > now - settle:
            continue
        h = hashlib.sha1()
        for f, s in stats:
            h.update(f"{f.relative_to(inbox)}|{s.st_size}|{s.st_mtime_ns}\n".encode())
        out.append((entry, h.hexdigest()))
    return out


def audit_inbox_entry(entry: Path) -> dict:
    return audit_paths(_entry_files(entry), title=entry.name, source="inbox", ref=str(entry.resolve()))


class InboxWatcher:
    """Verifica a pasta a cada `interval` segundos e dispara uma varredura quando há algo novo."""

    def __init__(self, has_pending: Callable[[], bool], trigger: Callable[[], None], interval: float = 15.0):
        self.has_pending = has_pending
        self.trigger = trigger
        self.interval = interval
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                if await asyncio.to_thread(self.has_pending):
                    self.trigger()
            except Exception:  # noqa: BLE001
                log.exception("falha ao verificar a pasta monitorada")
            await asyncio.sleep(self.interval)
