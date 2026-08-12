from __future__ import annotations

import csv
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Iterable

from .catalog import Instrument
from .models import Candle, OutputPaths, Tick


CANDLE_COLUMNS = ["Date", "Time", "Open", "High", "Low", "Close", "TickVolume", "Volume", "Spread"]
TICK_COLUMNS = ["TimestampUTC", "Bid", "Ask", "BidVolume", "AskVolume"]


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


def write_candles(path: Path, candles: Iterable[Candle], precision: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", newline="", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            writer = csv.writer(handle)
            writer.writerow(CANDLE_COLUMNS)
            for item in candles:
                if not all(math.isfinite(value) for value in (item.open, item.high, item.low, item.close, item.volume)):
                    raise ValueError(f"refusing to write non-finite OHLC or volume for candle at {item.time.isoformat()}")
                if item.volume < 0:
                    raise ValueError(f"refusing to write negative volume for candle at {item.time.isoformat()}")
                writer.writerow([item.time.strftime("%Y.%m.%d"), item.time.strftime("%H:%M:%S"), *[f"{value:.{precision}f}" for value in (item.open, item.high, item.low, item.close)], item.tick_volume, format_volume(item.volume), item.spread])
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    os.replace(temporary, path)


def write_ticks(path: Path, ticks: Iterable[Tick], precision: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", newline="", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            writer = csv.writer(handle)
            writer.writerow(TICK_COLUMNS)
            for item in ticks:
                if not all(math.isfinite(value) for value in (item.bid, item.ask, item.bid_volume, item.ask_volume)):
                    raise ValueError(f"refusing to write non-finite tick value at {item.time.isoformat()}")
                if item.bid_volume < 0 or item.ask_volume < 0:
                    raise ValueError(f"refusing to write negative tick volume at {item.time.isoformat()}")
                writer.writerow([item.time.isoformat(), f"{item.bid:.{precision}f}", f"{item.ask:.{precision}f}", format_volume(item.bid_volume), format_volume(item.ask_volume)])
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    os.replace(temporary, path)


def format_volume(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.6f}".rstrip("0").rstrip(".")


def provenance(source_kind: str, offer_side: str | None, volume: str | None) -> dict[str, object]:
    semantics = {
        "native": "dukascopy_native",
        "bid": "bid_quote_volume",
        "ask": "ask_quote_volume",
        "total": "bid_plus_ask_quote_volume",
        "ticks": "tick_count",
        None: None,
    }[volume]
    return {
        "source_kind": source_kind,
        "offer_side": offer_side,
        "volume_semantics": semantics,
        "volume_is_trade_volume": False,
        "spread_available": source_kind == "tick",
    }


def manifest(
    instrument: Instrument,
    timeframe: str,
    source_kind: str,
    offer_side: str | None,
    volume: str | None,
    start: datetime,
    end: datetime,
    paths: OutputPaths,
    trusted: bool,
    record_count: int,
) -> dict[str, object]:
    return {
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
        "instrument": instrument.symbol,
        "asset_class": instrument.asset_class,
        "timeframe": timeframe,
        "range_start_utc": start.isoformat(),
        "range_end_exclusive_utc": end.isoformat(),
        "timezone": "UTC",
        "trusted": trusted,
        "record_count": record_count,
        "csv_path": str(paths.csv_path),
        "quality_path": str(paths.quality_path),
        **provenance(source_kind, offer_side, volume),
    }
