from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .models import OutputPaths


DATAFEED_BASE_URL = "https://datafeed.dukascopy.com/datafeed"
TIMEFRAME_SECONDS = {"M1": 60, "H1": 3600, "D1": 86400}
TIMEFRAMES = ("TICK", "M1", "H1", "D1")
PRICES = ("bid", "ask", "mid")
VOLUMES = ("native", "bid", "ask", "total", "ticks")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def default_output_dir() -> Path:
    return repo_root() / "data" / "downloads" / "dukascopy"


def inclusive_date_range(start: date, end: date) -> tuple[datetime, datetime]:
    if end < start:
        raise ValueError("end date must be on or after start date")
    return (
        datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc),
        datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc),
    )


def output_paths(output_dir: Path, stem: str) -> OutputPaths:
    return OutputPaths(
        csv_path=output_dir / f"{stem}.csv",
        manifest_path=output_dir / f"{stem}.manifest.json",
        quality_path=output_dir / f"{stem}.quality.json",
        status_path=output_dir / f"{stem}.source_status.jsonl",
    )
