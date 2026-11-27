from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    data_dir: Path
    inbox_dir: Path
    watch_inbox: bool = True
    watch_interval: float = 15.0
    radar_auto: bool = True
    host: str = "127.0.0.1"
    port: int = 8000

    @property
    def db_path(self) -> Path:
        return self.data_dir / "radar.db"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def downloads_dir(self) -> Path:
        return self.data_dir / "downloads"


def load_settings() -> Settings:
    return Settings(
        data_dir=Path(os.environ.get("RADAR_DATA_DIR", BASE_DIR / "data")),
        inbox_dir=Path(os.environ.get("RADAR_INBOX_DIR", BASE_DIR / "inbox_editais")),
        watch_inbox=os.environ.get("RADAR_WATCH_INBOX", "1") not in ("0", "false", "False"),
        watch_interval=float(os.environ.get("RADAR_WATCH_INTERVAL", "15")),
        radar_auto=os.environ.get("RADAR_AUTO_SCAN", "1") not in ("0", "false", "False"),
        host=os.environ.get("RADAR_HOST", "127.0.0.1"),
        port=int(os.environ.get("RADAR_PORT", "8000")),
    )
