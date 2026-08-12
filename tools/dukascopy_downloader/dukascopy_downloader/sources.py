from __future__ import annotations

import json
import lzma
import math
import random
import shutil
import struct
import subprocess
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .catalog import Instrument
from .config import DATAFEED_BASE_URL
from .models import Candle, FetchResult, SourcePeriod, Tick


TICK_RECORD = struct.Struct(">IIIff")
NATIVE_CANDLE_RECORD = struct.Struct(">IIIIIf")


def iter_hours(start: datetime, end_exclusive: datetime):
    current = require_utc_hour(start)
    end = require_utc_hour(end_exclusive)
    while current < end:
        yield current
        current += timedelta(hours=1)


def require_utc_hour(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    value = value.astimezone(timezone.utc)
    if value.minute or value.second or value.microsecond:
        raise ValueError("datetime must be hour-aligned")
    return value


def tick_url(symbol: str, hour: datetime) -> str:
    hour = require_utc_hour(hour)
    return (
        f"{DATAFEED_BASE_URL}/{symbol}/{hour.year:04d}/{hour.month - 1:02d}/"
        f"{hour.day:02d}/{hour.hour:02d}h_ticks.bi5"
    )


def native_candle_url(symbol: str, timeframe: str, offer_side: str, base_time: datetime) -> str:
    side = offer_side.upper()
    if timeframe == "M1":
        return (
            f"{DATAFEED_BASE_URL}/{symbol}/{base_time.year:04d}/{base_time.month - 1:02d}/"
            f"{base_time.day:02d}/{side}_candles_min_1.bi5"
        )
    if timeframe == "H1":
        return (
            f"{DATAFEED_BASE_URL}/{symbol}/{base_time.year:04d}/{base_time.month - 1:02d}/"
            f"{side}_candles_hour_1.bi5"
        )
    if timeframe == "D1":
        return f"{DATAFEED_BASE_URL}/{symbol}/{base_time.year:04d}/{side}_candles_day_1.bi5"
    raise ValueError(f"native candles are not available for {timeframe}")


def tick_periods(instrument: Instrument, start: datetime, end: datetime, cache_root: Path) -> list[SourcePeriod]:
    periods = []
    for hour in iter_hours(start, end):
        cache = cache_root / instrument.symbol / "tick" / f"{hour.year:04d}" / f"{hour.month - 1:02d}" / f"{hour.day:02d}" / f"{hour.hour:02d}h_ticks.bi5"
        periods.append(SourcePeriod(hour.isoformat(), hour, tick_url(instrument.symbol, hour), cache, instrument.expected_open(hour)))
    return periods


def native_periods(
    instrument: Instrument,
    timeframe: str,
    offer_side: str,
    start: datetime,
    end: datetime,
    cache_root: Path,
) -> list[SourcePeriod]:
    bases: list[datetime] = []
    current = start
    if timeframe == "M1":
        while current < end:
            bases.append(current)
            current += timedelta(days=1)
    elif timeframe == "H1":
        current = current.replace(day=1)
        while current < end:
            bases.append(current)
            current = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
    elif timeframe == "D1":
        current = current.replace(month=1, day=1)
        while current < end:
            bases.append(current)
            current = current.replace(year=current.year + 1)
    else:
        raise ValueError(f"native candles are not available for {timeframe}")
    result = []
    for base in bases:
        if timeframe == "M1":
            relative = Path(f"{base.year:04d}") / f"{base.month - 1:02d}" / f"{base.day:02d}" / f"{offer_side.upper()}_candles_min_1.bi5"
            period_end = base + timedelta(days=1)
        elif timeframe == "H1":
            relative = Path(f"{base.year:04d}") / f"{base.month - 1:02d}" / f"{offer_side.upper()}_candles_hour_1.bi5"
            period_end = (base.replace(day=28) + timedelta(days=4)).replace(day=1)
        else:
            relative = Path(f"{base.year:04d}") / f"{offer_side.upper()}_candles_day_1.bi5"
            period_end = base.replace(year=base.year + 1)
        expected = any(instrument.expected_open(item) for item in iter_hours(max(base, start), min(period_end, end)))
        result.append(SourcePeriod(base.isoformat(), base, native_candle_url(instrument.symbol, timeframe, offer_side, base), cache_root / instrument.symbol / "native" / relative, expected))
    return result


def decompress_bi5(payload: bytes) -> bytes:
    remaining = payload
    output = []
    while remaining:
        decompressor = lzma.LZMADecompressor()
        output.append(decompressor.decompress(remaining))
        if not decompressor.eof:
            raise lzma.LZMAError("compressed data ended before end-of-stream marker")
        remaining = decompressor.unused_data
    return b"".join(output)


def parse_tick_payload(payload: bytes, hour: datetime, instrument: Instrument) -> list[Tick]:
    return parse_tick_records(decompress_bi5(payload), hour, instrument)


def parse_tick_records(raw: bytes, hour: datetime, instrument: Instrument) -> list[Tick]:
    hour = require_utc_hour(hour)
    if len(raw) % TICK_RECORD.size:
        raise ValueError(f"tick payload length {len(raw)} is not divisible by {TICK_RECORD.size}")
    ticks = []
    for offset in range(0, len(raw), TICK_RECORD.size):
        ms, ask_raw, bid_raw, ask_volume, bid_volume = TICK_RECORD.unpack_from(raw, offset)
        if ms >= 3_600_000:
            raise ValueError(f"tick millisecond offset outside hour: {ms}")
        if ask_raw <= 0 or bid_raw <= 0 or ask_raw < bid_raw:
            raise ValueError(f"invalid tick prices: ask={ask_raw}, bid={bid_raw}")
        bid_scaled = bid_volume * instrument.tick_volume_scale
        ask_scaled = ask_volume * instrument.tick_volume_scale
        if not (math.isfinite(bid_scaled) and math.isfinite(ask_scaled)) or bid_scaled < 0 or ask_scaled < 0:
            raise ValueError(f"invalid tick volumes: bid={bid_volume}, ask={ask_volume}")
        ticks.append(
            Tick(
                hour + timedelta(milliseconds=ms),
                bid_raw / instrument.price_scale,
                ask_raw / instrument.price_scale,
                bid_scaled,
                ask_scaled,
            )
        )
    return ticks


def parse_native_payload(payload: bytes, base_time: datetime, instrument: Instrument) -> list[Candle]:
    return parse_native_records(decompress_bi5(payload), base_time, instrument)


def parse_native_records(raw: bytes, base_time: datetime, instrument: Instrument) -> list[Candle]:
    if len(raw) % NATIVE_CANDLE_RECORD.size:
        raise ValueError(f"native candle payload length {len(raw)} is not divisible by {NATIVE_CANDLE_RECORD.size}")
    candles = []
    for offset in range(0, len(raw), NATIVE_CANDLE_RECORD.size):
        seconds, open_raw, close_raw, low_raw, high_raw, volume = NATIVE_CANDLE_RECORD.unpack_from(raw, offset)
        if not all((open_raw, close_raw, low_raw, high_raw)):
            continue
        if not low_raw <= min(open_raw, close_raw) <= max(open_raw, close_raw) <= high_raw:
            raise ValueError("native candle has invalid OHLC ordering")
        # Dukascopy inserts flat zero-volume candles during closures.
        if volume == 0 and open_raw == close_raw == low_raw == high_raw:
            continue
        if not math.isfinite(volume) or volume < 0:
            raise ValueError(f"native candle has invalid volume: {volume}")
        candles.append(Candle(base_time + timedelta(seconds=seconds), open_raw / instrument.price_scale, high_raw / instrument.price_scale, low_raw / instrument.price_scale, close_raw / instrument.price_scale, 0, float(volume), 0))
    return candles


def fetch_to_cache(period: SourcePeriod, retries: int, timeout: float, refresh: bool) -> FetchResult:
    if period.cache_path.exists() and not refresh:
        return FetchResult(period, "cached", period.cache_path.stat().st_size)
    request = Request(period.url, headers={"User-Agent": "price-replay-dukascopy-downloader/0.2"})
    last_error = None
    for attempt in range(retries):
        try:
            payload = fetch_url(period.url, request, timeout)
            if not payload:
                return FetchResult(period, "empty")
            period.cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = period.cache_path.with_suffix(period.cache_path.suffix + ".tmp")
            temporary.write_bytes(payload)
            temporary.replace(period.cache_path)
            return FetchResult(period, "downloaded", len(payload))
        except HTTPError as exc:
            if exc.code in {404, 410}:
                return FetchResult(period, "missing", error=f"HTTP {exc.code}")
            last_error = f"HTTP {exc.code}: {exc.reason}"
        except (TimeoutError, URLError, OSError) as exc:
            last_error = str(exc)
        if attempt + 1 < retries:
            time.sleep(min(2 ** attempt + random.random(), 15))
    return FetchResult(period, "error", error=last_error)


def fetch_url(url: str, request: Request, timeout: float) -> bytes:
    if not shutil.which("curl"):
        with urlopen(request, timeout=timeout) as response:
            return response.read()
    process = subprocess.run(
        [
            "curl",
            "-sS",
            "-L",
            "--connect-timeout",
            str(max(2, min(10, timeout / 2))),
            "--max-time",
            str(timeout),
            "-w",
            "\n%{http_code}",
            url,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    body, separator, code = process.stdout.rpartition(b"\n")
    status = code.decode("ascii", errors="replace") if separator else "000"
    if process.returncode == 0 and status == "200":
        return body
    if status in {"404", "410"}:
        raise HTTPError(url, int(status), "missing", {}, None)
    error = process.stderr.decode("utf-8", errors="replace").strip()
    raise URLError(f"curl exit {process.returncode}, HTTP {status}: {error}")


def append_status(path: Path, result: FetchResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(result)
    payload["period"]["base_time"] = result.period.base_time.isoformat()
    payload["period"]["cache_path"] = str(result.period.cache_path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
