from __future__ import annotations

import json
import math
import re
import shutil
import sqlite3
from collections import OrderedDict
from collections.abc import Iterator, Sequence
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import polars as pl
import pyarrow.parquet as pq

from .config import OHLCV_ROOT, RAW_ROOT, ensure_directories
from .domain import Bar
from .repository import get_symbol, publish_import

SCHEMA_ID = "dukascopy_mt5_1m_v1"
EXPECTED_COLUMNS = ["Date", "Time", "Open", "High", "Low", "Close", "TickVolume", "Volume", "Spread"]
SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
PROFILES = ("utc_aligned", "new_york_close")
OHLC_COLUMNS = ("open", "high", "low", "close")

# Bounded bars cache: keyed by (symbol, version, window_start, window_end, file signature).
# Versions are immutable once published, so a pinned version never reloads; the legacy
# (unnumbered) dataset is retained for pre-version sessions and re-read on any change.
_BARS_CACHE: OrderedDict[tuple, list[Bar]] = OrderedDict()
_BARS_CACHE_MAX = 8

# Bounded signature cache: keyed by (symbol, version), holding the partition paths and
# file signature of one immutable pinned version. Computing a signature is a glob + stat
# pass over the partition directory, and session state responses recompute it on every
# call, so caching the scan for immutable versions removes that repeated filesystem work.
# Only complete (non-empty) pinned versions are cached: a missing version must be
# rescanned so a later publication is never hidden, and the legacy (unnumbered) dataset
# is always rescanned so on-disk changes are detected. Entries are a few small tuples, so
# the cap can exceed the bars caches without meaningful memory cost; the LRU mechanism
# matches them and the cache is restart-safe (in-memory, recomputed on startup).
_SIGNATURE_CACHE: OrderedDict[tuple[str, str], tuple[tuple[Path, ...], tuple[tuple[str, int, int], ...]]] = OrderedDict()
_SIGNATURE_CACHE_MAX = 64

# Lazy replay paging: individual reads are bounded by a page regardless of the
# selected range, and the per-sequence page cache is capped in bars so long
# ranges never accumulate in memory. The budget covers the largest legal chart
# context plus indicator warmup (a 1d window needs ~52k one-minute bars).
_BARS_PAGE_SIZE = 1024
_MAX_CACHED_BARS = 64 * 1024


def _root_for(symbol: str, version: str | None) -> Path:
    """Partitioned store for one version. `None` is the retained legacy dataset."""
    if version is None:
        return OHLCV_ROOT / symbol / "1m"
    return OHLCV_ROOT / symbol / "versions" / version / "1m"


def _scan_partitions(symbol: str, version: str | None) -> tuple[tuple[Path, ...], tuple[tuple[str, int, int], ...]]:
    """One discovery pass over the 1m partitions: their paths plus a file signature.

    The paths are sorted by name, which orders `year=YYYY` partitions chronologically;
    callers that need newest-first iterate in reverse. The signature covers every
    partition file, so any re-import (new file, changed size, rewritten mtime) changes it.
    """
    root = _root_for(symbol, version)
    paths = tuple(sorted(root.glob("year=*/data.parquet")))
    return paths, tuple((str(path), path.stat().st_mtime_ns, path.stat().st_size) for path in paths)


def _cached_scan(symbol: str, version: str | None) -> tuple[tuple[Path, ...], tuple[tuple[str, int, int], ...]]:
    """Partition paths and signature, served from the immutable-version LRU when possible.

    Version directories are immutable once published, so a complete pinned scan is
    cached and repeated calls (session state responses, bars cache keying) skip the
    glob/stat pass entirely. A missing or empty version is never cached — its scan is
    recomputed every time so a later publication is not hidden — and the legacy
    (unnumbered) dataset is always rescanned so on-disk changes are detected.
    """
    if version is None:
        return _scan_partitions(symbol, None)
    key = (symbol, version)
    cached = _SIGNATURE_CACHE.get(key)
    if cached is not None:
        _SIGNATURE_CACHE.move_to_end(key)
        return cached
    paths, signature = _scan_partitions(symbol, version)
    if signature:
        _SIGNATURE_CACHE[key] = (paths, signature)
        _SIGNATURE_CACHE.move_to_end(key)
        while len(_SIGNATURE_CACHE) > _SIGNATURE_CACHE_MAX:
            _SIGNATURE_CACHE.popitem(last=False)
    return paths, signature


def _signature(symbol: str, version: str | None = None) -> tuple[tuple[str, int, int], ...]:
    return _cached_scan(symbol, version)[1]


def bars_signature(symbol: str, version: str | None = None) -> tuple[tuple[str, int, int], ...]:
    """Signature of the published 1m files for a symbol/version; changes on any re-import.

    Pinned versions are immutable, so their signature is served from a bounded LRU
    (see `_SIGNATURE_CACHE`); the legacy dataset and missing versions are always rescanned.
    """
    return _signature(symbol, version)


def invalidate_bars(symbol: str | None = None) -> None:
    """Drop cached bars and pinned-version signatures; call after publishing a replacement.

    `None` clears every cache entry; a symbol clears only that symbol's entries. Bars are
    keyed by file signature, so a changed dataset re-reads even without explicit
    invalidation; the signature cache is dropped so the next scan observes on-disk state.
    """
    if symbol is None:
        _BARS_CACHE.clear()
        _SIGNATURE_CACHE.clear()
        return
    for key in [key for key in _BARS_CACHE if key[0] == symbol]:
        del _BARS_CACHE[key]
    for key in [key for key in _SIGNATURE_CACHE if key[0] == symbol]:
        del _SIGNATURE_CACHE[key]


def _current_version(symbol: str) -> str | None:
    """The symbol's currently published version, or None when only the legacy dataset exists."""
    try:
        metadata = get_symbol(symbol)
    except sqlite3.OperationalError:
        return None
    if not metadata:
        return None
    return metadata.get("data_version")


def _normalize_symbol(symbol: str) -> str:
    if not isinstance(symbol, str):
        raise ValueError("symbol must be a string")
    normalized = symbol.strip().upper()
    if not normalized:
        raise ValueError("symbol must not be empty")
    if not SYMBOL_PATTERN.match(normalized):
        raise ValueError("symbol may only contain letters, digits, '.', '_' and '-'")
    return normalized


def inspect_path(path: str) -> dict[str, object]:
    source = Path(path).expanduser().resolve()
    if not source.exists():
        raise ValueError("path does not exist")
    if source.is_file():
        return {"kind": "file", "files": [str(source)] if source.suffix.lower() == ".csv" else []}
    return {"kind": "folder", "files": [str(item) for item in sorted(source.glob("*.csv"))]}


def import_file(path: str, symbol: str, asset_class: str, pnl_currency: str, price_precision: int,
                contract_multiplier: float, default_profile: str) -> dict[str, object]:
    ensure_directories()
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise ValueError("source must be a file")
    symbol = _normalize_symbol(symbol)
    if not isinstance(asset_class, str) or not asset_class.strip():
        raise ValueError("asset class must be a non-empty string")
    if not isinstance(pnl_currency, str) or not pnl_currency.strip():
        raise ValueError("PNL currency must be a non-empty string")
    if isinstance(price_precision, bool) or not isinstance(price_precision, int) or price_precision < 0:
        raise ValueError("price precision must be a non-negative integer")
    if (not isinstance(contract_multiplier, (int, float)) or not math.isfinite(contract_multiplier)
            or contract_multiplier <= 0):
        raise ValueError("contract multiplier must be a finite positive number")
    if default_profile not in PROFILES:
        raise ValueError("unsupported default profile")
    if source.stat().st_size == 0:
        raise ValueError("source file is empty")
    try:
        frame = pl.read_csv(source)
    except Exception as error:  # polars raises various errors for unreadable input
        raise ValueError(f"failed to read CSV: {error}") from error
    if frame.height == 0:
        raise ValueError("source contains no data rows")
    if frame.columns != EXPECTED_COLUMNS:
        raise ValueError(f"unsupported schema; expected columns: {','.join(EXPECTED_COLUMNS)}")
    try:
        parsed = frame.with_columns(
            pl.concat_str([pl.col("Date"), pl.lit(" "), pl.col("Time")])
            .str.strptime(pl.Datetime, "%Y.%m.%d %H:%M:%S", strict=True)
            .dt.replace_time_zone("UTC")
            .alias("timestamp_utc"),
            pl.col("Volume").cast(pl.Float64).alias("source_volume"),
            pl.col("TickVolume").cast(pl.Float64).alias("tick_volume"),
        ).with_columns(
            pl.when(pl.col("source_volume") > 0).then(pl.col("source_volume"))
            .otherwise(pl.when(pl.col("tick_volume") > 0).then(pl.col("tick_volume")).otherwise(0.0))
            .alias("normalized_volume"),
        )
        frame = parsed.select(
            pl.col("timestamp_utc"),
            pl.col("Open").cast(pl.Float64).alias("open"),
            pl.col("High").cast(pl.Float64).alias("high"),
            pl.col("Low").cast(pl.Float64).alias("low"),
            pl.col("Close").cast(pl.Float64).alias("close"),
            pl.col("normalized_volume").alias("volume"),
        )
    except Exception as error:
        raise ValueError(f"invalid numeric or timestamp data: {error}") from error

    # Exactly one UTC bar per minute: reject sub-minute timestamps and minute collisions.
    duplicates = frame.select(pl.col("timestamp_utc").dt.truncate("1m").is_duplicated().sum()).item()
    subminute = frame.filter(
        (pl.col("timestamp_utc").dt.second() != 0) | (pl.col("timestamp_utc").dt.microsecond() != 0)
    ).height
    invalid_ohlc = frame.filter(
        (pl.col("high") < pl.col("low"))
        | ~pl.col("open").is_between(pl.col("low"), pl.col("high"), closed="both")
        | ~pl.col("close").is_between(pl.col("low"), pl.col("high"), closed="both")
    ).height
    invalid_numeric = frame.select(
        pl.sum_horizontal([pl.col(column).is_null() | ~pl.col(column).is_finite() for column in OHLC_COLUMNS]).sum()
    ).item()
    # Negative raw volumes must be rejected, not silently zeroed by the fallback
    # normalization (which only ever yields positive values or 0.0). The normalized
    # value can therefore never be negative; flag the raw source fields instead.
    invalid_volume = parsed.select(
        pl.col("normalized_volume").is_null() | ~pl.col("normalized_volume").is_finite()
        | (pl.col("source_volume") < 0).fill_null(False)
        | (pl.col("tick_volume") < 0).fill_null(False)
        | pl.col("source_volume").is_null() | ~pl.col("source_volume").is_finite()
        | pl.col("tick_volume").is_null() | ~pl.col("tick_volume").is_finite()
    ).sum().item()
    invalid_timestamps = frame.select(pl.col("timestamp_utc").is_null().sum()).item()
    if duplicates or subminute or invalid_ohlc or invalid_numeric or invalid_volume or invalid_timestamps:
        raise ValueError(
            "validation failed: "
            f"duplicates={duplicates}, subminute={subminute}, invalid_ohlc={invalid_ohlc}, "
            f"invalid_numeric={invalid_numeric}, invalid_volume={invalid_volume}, "
            f"invalid_timestamps={invalid_timestamps}"
        )
    ordered = frame.sort("timestamp_utc")
    gaps = ordered.select(
        (pl.col("timestamp_utc").diff().dt.total_minutes() > 1).sum()
    ).item() or 0
    non_monotonic = not frame["timestamp_utc"].is_sorted()

    # Publish under an immutable version directory keyed by the import batch id. The
    # dataset is fully staged before the pointer switches, and the switch itself is
    # the symbols row update inside the same SQLite transaction that records the
    # batch, so readers always see either the old complete version or the new one.
    batch_id = str(uuid4())
    version_dir = OHLCV_ROOT / symbol / "versions" / batch_id
    raw_dir = RAW_ROOT / symbol / batch_id
    try:
        version_dir.mkdir(parents=True)
        for year, yearly in ordered.with_columns(pl.col("timestamp_utc").dt.year().alias("year")).partition_by("year", as_dict=True).items():
            year_value = year[0] if isinstance(year, tuple) else year
            target = version_dir / "1m" / f"year={year_value}"
            target.mkdir(parents=True, exist_ok=True)
            yearly.drop("year").write_parquet(target / "data.parquet")
        raw_dir.mkdir(parents=True)
        shutil.copy2(source, raw_dir / source.name)
        first = ordered["timestamp_utc"][0].isoformat()
        last = ordered["timestamp_utc"][-1].isoformat()
        validation = {
            "duplicates": duplicates, "subminute": subminute, "invalid_ohlc": invalid_ohlc,
            "invalid_numeric": invalid_numeric, "invalid_volume": invalid_volume,
            "invalid_timestamps": invalid_timestamps, "gap_count": gaps, "source_non_monotonic": non_monotonic,
        }
        batch = {
            "id": batch_id, "symbol": symbol, "source_path": str(source), "schema_id": SCHEMA_ID,
            "status": "complete", "rows_imported": ordered.height, "validation_json": json.dumps(validation),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        publish_import(batch, {
            "symbol": symbol, "asset_class": asset_class, "pnl_currency": pnl_currency,
            "price_precision": price_precision, "contract_multiplier": contract_multiplier,
            "default_profile": default_profile, "first_timestamp": first, "last_timestamp": last,
            "data_version": batch_id,
        })
    except Exception:
        # Failed publish must leave the previous pointer and every session intact:
        # drop the unreferenced staged version and raw copy before surfacing the error.
        shutil.rmtree(version_dir, ignore_errors=True)
        shutil.rmtree(raw_dir, ignore_errors=True)
        raise
    invalidate_bars(symbol)
    return {**batch, "validation": validation}


def _frame_to_bars(frame: pl.DataFrame) -> list[Bar]:
    return [
        Bar(row["timestamp_utc"], row["open"], row["high"], row["low"], row["close"], row["volume"])
        for row in frame.iter_rows(named=True)
    ]


def load_bars(symbol: str, version: str | None = None) -> list[Bar]:
    """Load every published 1m bar for a symbol (full history).

    `version=None` resolves the currently published version through DB metadata
    (falling back to the retained legacy dataset for pre-version symbols).
    """
    if version is None:
        version = _current_version(symbol)
    paths = sorted(_root_for(symbol, version).glob("year=*/data.parquet"))
    if not paths:
        return []
    frame = pl.concat([pl.read_parquet(path) for path in paths]).sort("timestamp_utc")
    return _frame_to_bars(frame)


def load_bars_before(symbol: str, before: datetime, limit: int, version: str | None = None) -> list[Bar]:
    """Load up to `limit` 1m bars strictly before `before`, newest partitions first.

    Scans year partitions newest-to-oldest and stops as soon as `limit` bars are
    collected, so partitions older than the requested window are never read; the
    returned bars are the last `limit` bars in chronological order. This keeps
    weekends and accepted gaps from erasing available history: the window is a
    bar count, not a wall-clock span. Within each partition the lazy query
    returns only the still-needed tail, so no partition ever materializes more
    than `limit` rows for conversion.

    `version` is the session's pinned dataset version; `None` reads the retained
    legacy dataset explicitly. Results are cached per (symbol, version, before,
    limit, file signature); an immutable pinned version never reloads, while the
    legacy dataset transparently reloads if its files ever change.
    """
    before = before.astimezone(timezone.utc)
    if limit <= 0:
        return []
    paths, signature = _cached_scan(symbol, version)
    key = (symbol, version, before, limit, signature)
    cached = _BARS_CACHE.get(key)
    if cached is not None:
        _BARS_CACHE.move_to_end(key)
        return cached
    collected: list[Bar] = []
    remaining = limit
    for path in reversed(paths):  # newest partitions first, so older ones may never be read
        if remaining <= 0:
            break
        # Partitions are published chronologically sorted, so the tail of the
        # filtered rows is exactly the newest `remaining` bars of this partition;
        # the lazy query is bounded, so conversion never sees more than the rows
        # still needed. Cross-year ordering/gaps are preserved because older
        # partitions only contribute their own tail when the newer ones run short.
        frame = pl.scan_parquet(path).filter(pl.col("timestamp_utc") < before).tail(remaining).collect()
        if frame.height == 0:
            continue
        bars = _frame_to_bars(frame)
        collected.extend(bars)
        remaining -= len(bars)
    bars = sorted(collected, key=lambda bar: bar.timestamp)
    _BARS_CACHE[key] = bars
    _BARS_CACHE.move_to_end(key)
    while len(_BARS_CACHE) > _BARS_CACHE_MAX:
        _BARS_CACHE.popitem(last=False)
    return bars


def load_bars_range(symbol: str, start: datetime, end: datetime, version: str | None = None) -> list[Bar]:
    """Load 1m bars within [start, end], reading only overlapping year partitions.

    `version` is the session's pinned dataset version; `None` reads the retained
    legacy dataset explicitly. Results are cached per (symbol, version, window,
    file signature); an immutable pinned version never reloads, while the legacy
    dataset transparently reloads if its files ever change.
    """
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    if end < start:
        return []
    paths, signature = _cached_scan(symbol, version)
    key = (symbol, version, start, end, signature)
    cached = _BARS_CACHE.get(key)
    if cached is not None:
        _BARS_CACHE.move_to_end(key)
        return cached
    frames = []
    for path in paths:
        year = int(path.parent.name.split("=")[1])
        if year < start.year or year > end.year:
            continue
        frames.append(pl.read_parquet(path))
    bars: list[Bar] = []
    if frames:
        frame = pl.concat(frames).filter(
            (pl.col("timestamp_utc") >= start) & (pl.col("timestamp_utc") <= end)
        )
        bars = _frame_to_bars(frame)
    _BARS_CACHE[key] = bars
    _BARS_CACHE.move_to_end(key)
    while len(_BARS_CACHE) > _BARS_CACHE_MAX:
        _BARS_CACHE.popitem(last=False)
    return bars


def partitions_for_range(symbol: str, version: str | None, start: datetime, end: datetime) -> list[tuple[int, Path]]:
    """Year partitions of a pinned version overlapping [start, end], in year order."""
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    paths, _ = _cached_scan(symbol, version)
    result: list[tuple[int, Path]] = []
    for path in paths:
        year = int(path.parent.name.split("=")[1])
        if start.year <= year <= end.year:
            result.append((year, path))
    return result


def count_bars_partition(path: Path, year: int, start: datetime, end: datetime) -> int:
    """Exact count of bars within [start, end] inside one year partition.

    Partitions fully inside the range use the parquet metadata row count without
    reading any data; the (at most two) edge partitions run a predicate count that
    parquet row-group pruning bounds to the matching groups only.
    """
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    if start.year < year < end.year:
        return pq.ParquetFile(path).metadata.num_rows
    return pl.scan_parquet(path).filter(
        (pl.col("timestamp_utc") >= start) & (pl.col("timestamp_utc") <= end)
    ).select(pl.len()).collect().item()


def count_bars_before_partition(path: Path, year: int, before: datetime) -> int:
    """Rows in one partition strictly before `before` (only needed on the first
    in-range partition, to translate global indices into file offsets)."""
    before = before.astimezone(timezone.utc)
    return pl.scan_parquet(path).filter(pl.col("timestamp_utc") < before).select(pl.len()).collect().item()


def load_bars_page(path: Path, year: int, file_offset: int, length: int) -> list[Bar]:
    """Bounded slice of one partition starting at `file_offset`, at most `length` bars.

    The slice is pushed down to the parquet reader, so only the requested rows are
    decoded; `length` caps the read regardless of partition size.
    """
    if length <= 0:
        return []
    frame = pl.scan_parquet(path).slice(file_offset, length).collect()
    return _frame_to_bars(frame)


class RangeBars(Sequence[Bar]):
    """Lazy, paged, partition-aware view of the 1m bars within [start, end] of a
    pinned dataset version.

    Implements the full Sequence protocol — integer and slice indexing, `len`, and
    iteration — without ever materializing the whole range. Partition row counts
    come from parquet metadata (or a bounded predicate count on the edge
    partitions) and pages are sliced scans that polars pushes down to the parquet
    reader, so every single read is bounded by a page no matter how long the
    range is. A bounded LRU keeps recently touched pages in memory.
    """

    __slots__ = ("_start", "_end", "_partitions", "_counts", "_rows_before_cache",
                 "_pages", "_cached_bars", "_total")

    def __init__(self, symbol: str, start: datetime, end: datetime, version: str | None = None) -> None:
        self._start = start.astimezone(timezone.utc)
        self._end = end.astimezone(timezone.utc)
        self._partitions = (
            partitions_for_range(symbol, version, self._start, self._end)
            if self._end >= self._start
            else []
        )
        self._counts: dict[int, int] = {}
        self._rows_before_cache: dict[int, int] = {}
        self._pages: OrderedDict[tuple[int, int], list[Bar]] = OrderedDict()
        self._cached_bars = 0
        self._total: int | None = None

    def _count(self, year: int, path: Path) -> int:
        cached = self._counts.get(year)
        if cached is not None:
            return cached
        count = count_bars_partition(path, year, self._start, self._end)
        self._counts[year] = count
        return count

    def _rows_before(self, year: int, path: Path) -> int:
        """File rows of the partition preceding the range start (0 past the first partition)."""
        if year != self._start.year:
            return 0
        cached = self._rows_before_cache.get(year)
        if cached is None:
            cached = count_bars_before_partition(path, year, self._start)
            self._rows_before_cache[year] = cached
        return cached

    def __len__(self) -> int:
        if self._total is None:
            self._total = sum(self._count(year, path) for year, path in self._partitions)
        return self._total

    def _locate(self, index: int) -> tuple[int, Path, int, int, int]:
        """Map a global in-range index to (year, path, rows_before, cumulative_before, count)."""
        cumulative = 0
        for year, path in self._partitions:
            count = self._count(year, path)
            if index < cumulative + count:
                return year, path, self._rows_before(year, path), cumulative, count
            cumulative += count
        raise IndexError("replay index out of range")

    def _page(self, year: int, path: Path, file_offset: int, length: int) -> list[Bar]:
        key = (year, file_offset)
        cached = self._pages.get(key)
        if cached is not None:
            self._pages.move_to_end(key)
            return cached
        bars = load_bars_page(path, year, file_offset, length)
        self._pages[key] = bars
        self._pages.move_to_end(key)
        self._cached_bars += len(bars)
        while self._cached_bars > _MAX_CACHED_BARS and self._pages:
            _, evicted = self._pages.popitem(last=False)
            self._cached_bars -= len(evicted)
        return bars

    def _read_range(self, global_start: int, length: int) -> list[Bar]:
        """Bars in global in-range order covering [global_start, global_start + length)."""
        if length <= 0:
            return []
        total = len(self)
        if global_start < 0:
            global_start = max(0, total + global_start)
        elif global_start > total:
            global_start = total
        length = min(length, total - global_start)
        result: list[Bar] = []
        index = global_start
        remaining = length
        while remaining > 0:
            year, path, rows_before, cumulative, count = self._locate(index)
            file_offset = index - cumulative + rows_before
            page_file_offset = (file_offset // _BARS_PAGE_SIZE) * _BARS_PAGE_SIZE
            page_global_start = cumulative + page_file_offset - rows_before
            page_available = rows_before + count - page_file_offset
            take = min(remaining, cumulative + count - index)
            read_len = min(max(take, _BARS_PAGE_SIZE), page_available)
            bars = self._page(year, path, page_file_offset, read_len)
            if not bars:
                break  # count/loader inconsistency: never loop forever
            skip = index - page_global_start
            got = bars[skip: skip + remaining]
            if not got:
                break
            result.extend(got)
            index += len(got)
            remaining -= len(got)
        return result

    def __getitem__(self, key: int | slice) -> Bar | list[Bar]:
        if isinstance(key, slice):
            start, stop, step = key.indices(len(self))
            if step != 1:
                return [self[index] for index in range(start, stop, step)]
            if start >= stop:
                return []
            return self._read_range(start, stop - start)
        if isinstance(key, int):
            if key < 0:
                key += len(self)
            if key < 0 or key >= len(self):
                raise IndexError("replay index out of range")
            return self._read_range(key, 1)[0]
        raise TypeError(f"replay indices must be integers or slices, not {type(key).__name__}")

    def __iter__(self) -> Iterator[Bar]:
        index = 0
        total = len(self)
        while index < total:
            year, path, rows_before, cumulative, count = self._locate(index)
            file_offset = index - cumulative + rows_before
            page_file_offset = (file_offset // _BARS_PAGE_SIZE) * _BARS_PAGE_SIZE
            page_global_start = cumulative + page_file_offset - rows_before
            page_available = rows_before + count - page_file_offset
            read_len = min(_BARS_PAGE_SIZE, page_available)
            bars = self._page(year, path, page_file_offset, read_len)
            if not bars or index - page_global_start >= len(bars):
                break
            yield from bars[index - page_global_start:]
            index = page_global_start + len(bars)
