from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = Path(os.environ.get("PRICE_REPLAY_DATA_ROOT", PROJECT_ROOT / "data"))
RAW_ROOT = DATA_ROOT / "raw"
OHLCV_ROOT = DATA_ROOT / "ohlcv"
DB_PATH = DATA_ROOT / "sessions" / "price_replay.sqlite3"


def ensure_directories() -> None:
    for path in (RAW_ROOT, OHLCV_ROOT, DB_PATH.parent):
        path.mkdir(parents=True, exist_ok=True)

