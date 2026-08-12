from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timedelta

from .aggregate import bucket_for
from .catalog import Instrument
from .config import TIMEFRAME_SECONDS
from .models import Candle, FetchResult


BAD_SOURCE_STATUSES = {"missing", "empty", "error", "parse_error"}

# Sparse instruments (e.g. forex M1) legitimately skip quiet open-session
# minutes, but an expected-open window with less than this share of its bars
# present is implausibly under-covered and must not be trusted.
MIN_OPEN_SESSION_COVERAGE_PCT = 50.0


def validate_candles(
    candles: list[Candle],
    results: list[FetchResult],
    instrument: Instrument,
    timeframe: str,
    start: datetime,
    end: datetime,
) -> dict[str, object]:
    source_counts = Counter(result.status for result in results)
    unexpected_source = [
        {"period": result.period.key, "status": result.status, "error": result.error}
        for result in results
        if result.status in BAD_SOURCE_STATUSES and result.period.expected_open
    ]
    expected_source = [
        {"period": result.period.key, "status": result.status}
        for result in results
        if result.status in BAD_SOURCE_STATUSES and not result.period.expected_open
    ]
    times = [candle.time for candle in candles]
    duplicates = len(times) - len(set(times))
    invalid = []
    boundary_errors = []
    for candle in candles:
        if not (
            all(math.isfinite(value) for value in (candle.open, candle.high, candle.low, candle.close, candle.volume))
            and candle.volume >= 0
            and 0 < candle.low <= min(candle.open, candle.close) <= max(candle.open, candle.close) <= candle.high
        ):
            invalid.append(candle.time.isoformat())
        if bucket_for(candle.time, timeframe) != candle.time:
            boundary_errors.append(candle.time.isoformat())
    expected_gaps = []
    unexpected_gaps = []
    sparse_missing = []
    existing = set(times)
    step = timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
    open_expected = 0
    open_present = 0
    current = start
    while current < end:
        is_open = instrument.expected_open(current)
        present = current in existing
        if is_open:
            open_expected += 1
            if present:
                open_present += 1
        if not present:
            if not is_open:
                expected_gaps.append(current.isoformat())
            elif instrument.sparse_candles_expected:
                expected_gaps.append(current.isoformat())
                sparse_missing.append(current.isoformat())
            else:
                unexpected_gaps.append(current.isoformat())
        current += step
    open_session_missing = open_expected - open_present
    open_session_coverage_pct = round(open_present / open_expected * 100, 3) if open_expected else 100.0
    sparse_trust_failed = bool(
        instrument.sparse_candles_expected
        and open_expected
        and open_session_coverage_pct < MIN_OPEN_SESSION_COVERAGE_PCT
    )
    trusted = (
        bool(candles)
        and not unexpected_source
        and not unexpected_gaps
        and not duplicates
        and not invalid
        and not boundary_errors
        and not sparse_trust_failed
    )
    covered = sum(result.status in {"cached", "downloaded"} for result in results)
    return {
        "trusted": trusted,
        "source_status_counts": dict(source_counts),
        "source_period_count": len(results),
        "source_coverage_pct": round(covered / len(results) * 100, 3) if results else 0,
        "unexpected_source_periods": unexpected_source,
        "expected_closed_source_periods": expected_source,
        "expected_gap_count": len(expected_gaps),
        "unexpected_gap_count": len(unexpected_gaps),
        "open_session_expected_bars": open_expected,
        "open_session_present_bars": open_present,
        "open_session_missing_bars": open_session_missing,
        "open_session_coverage_pct": open_session_coverage_pct,
        "min_open_session_coverage_pct": MIN_OPEN_SESSION_COVERAGE_PCT,
        "sparse_candles_expected": instrument.sparse_candles_expected,
        "sparse_trust_failed": sparse_trust_failed,
        "sparse_missing_bars": len(sparse_missing),
        "sparse_missing_sample": sparse_missing[:200],
        "expected_gaps": expected_gaps,
        "unexpected_gaps": unexpected_gaps,
        "expected_gaps_sample": expected_gaps[:200],
        "unexpected_gaps_sample": unexpected_gaps[:200],
        "duplicate_count": duplicates,
        "invalid_ohlc_count": len(invalid),
        "invalid_ohlc": invalid,
        "invalid_ohlc_sample": invalid[:200],
        "boundary_error_count": len(boundary_errors),
        "boundary_errors": boundary_errors,
        "boundary_errors_sample": boundary_errors[:200],
        "bar_count": len(candles),
        "first_bar_utc": candles[0].time.isoformat() if candles else None,
        "last_bar_utc": candles[-1].time.isoformat() if candles else None,
        "price_min": min((item.low for item in candles), default=None),
        "price_max": max((item.high for item in candles), default=None),
    }


def validate_ticks(results: list[FetchResult], tick_count: int) -> dict[str, object]:
    source_counts = Counter(result.status for result in results)
    unexpected = [
        {"period": result.period.key, "status": result.status, "error": result.error}
        for result in results
        if result.status in BAD_SOURCE_STATUSES and result.period.expected_open
    ]
    expected = [
        {"period": result.period.key, "status": result.status}
        for result in results
        if result.status in BAD_SOURCE_STATUSES and not result.period.expected_open
    ]
    covered = sum(result.status in {"cached", "downloaded"} for result in results)
    return {
        "trusted": tick_count > 0 and not unexpected,
        "source_status_counts": dict(source_counts),
        "source_period_count": len(results),
        "source_coverage_pct": round(covered / len(results) * 100, 3) if results else 0,
        "unexpected_source_periods": unexpected,
        "expected_closed_source_periods": expected,
        "expected_gap_count": len(expected),
        "unexpected_gap_count": len(unexpected),
        "tick_count": tick_count,
    }
