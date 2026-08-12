from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class Tick:
    time: datetime
    bid: float
    ask: float
    bid_volume: float
    ask_volume: float


@dataclass(frozen=True)
class Candle:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    tick_volume: int
    volume: float
    spread: int


@dataclass(frozen=True)
class SourcePeriod:
    key: str
    base_time: datetime
    url: str
    cache_path: Path
    expected_open: bool


@dataclass(frozen=True)
class FetchResult:
    period: SourcePeriod
    status: str
    byte_count: int = 0
    record_count: int = 0
    error: str | None = None


@dataclass(frozen=True)
class OutputPaths:
    csv_path: Path
    manifest_path: Path
    quality_path: Path
    status_path: Path
