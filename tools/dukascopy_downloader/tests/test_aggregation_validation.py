from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dukascopy_downloader.aggregate import aggregate_ticks, bucket_for
from dukascopy_downloader.catalog import get_instrument
from dukascopy_downloader.models import Candle, FetchResult, SourcePeriod, Tick
from dukascopy_downloader.output import CANDLE_COLUMNS, provenance, write_candles, write_ticks
from dukascopy_downloader.validation import validate_candles


UTC = timezone.utc


@pytest.mark.parametrize("timeframe", ["M1", "H1", "D1"])
def test_utc_bucketing(timeframe: str) -> None:
    value = datetime(2024, 3, 31, 12, 34, 56, tzinfo=UTC)
    expected = {"M1": (12, 34), "H1": (12, 0), "D1": (0, 0)}[timeframe]
    assert (bucket_for(value, timeframe).hour, bucket_for(value, timeframe).minute) == expected


@pytest.mark.parametrize(
    ("side", "volume", "expected_open", "expected_volume"),
    [
        ("bid", "bid", 1.1000, 5),
        ("ask", "ask", 1.1002, 7),
        ("mid", "total", 1.1001, 12),
        ("bid", "ticks", 1.1000, 2),
    ],
)
def test_tick_aggregation_price_and_volume_semantics(side, volume, expected_open, expected_volume) -> None:
    start = datetime(2024, 1, 2, 12, tzinfo=UTC)
    ticks = [
        Tick(start, 1.1000, 1.1002, 2, 3),
        Tick(start + timedelta(seconds=30), 1.1005, 1.1008, 3, 4),
    ]
    candle = aggregate_ticks(ticks, "M1", side, volume, get_instrument("EURUSD"))[0]
    assert candle.open == pytest.approx(expected_open)
    assert candle.volume == expected_volume
    assert candle.tick_volume == 2
    assert candle.spread > 0


def test_native_and_tick_m1_comparison() -> None:
    start = datetime(2024, 1, 2, 12, tzinfo=UTC)
    ticks = [Tick(start, 1.1, 1.1002, 1, 1), Tick(start + timedelta(seconds=30), 1.1005, 1.1007, 1, 1)]
    tick_candle = aggregate_ticks(ticks, "M1", "bid", "ticks", get_instrument("EURUSD"))[0]
    native = Candle(start, 1.1, 1.1005, 1.1, 1.1005, 0, 20, 0)
    assert (tick_candle.open, tick_candle.high, tick_candle.low, tick_candle.close) == (native.open, native.high, native.low, native.close)


def test_session_aware_gaps_and_strict_trust(tmp_path: Path) -> None:
    instrument = get_instrument("BTCUSD")
    friday = datetime(2024, 1, 5, 20, tzinfo=UTC)
    saturday = datetime(2024, 1, 6, 0, tzinfo=UTC)
    period = SourcePeriod("p", friday, "url", tmp_path / "p", True)
    candle = Candle(friday, 1, 1, 1, 1, 1, 1, 0)
    quality = validate_candles([candle], [FetchResult(period, "cached")], instrument, "H1", friday, saturday + timedelta(hours=2))
    assert quality["expected_gap_count"] == 0
    assert quality["unexpected_gap_count"] == 5
    assert quality["trusted"] is False


def test_sparse_candles_do_not_hide_source_failures(tmp_path: Path) -> None:
    instrument = get_instrument("EURUSD")
    start = datetime(2024, 1, 2, 12, tzinfo=UTC)
    period = SourcePeriod("p", start, "url", tmp_path / "p", True)
    candle = Candle(start, 1, 1, 1, 1, 1, 1, 0)
    quality = validate_candles([candle], [FetchResult(period, "missing")], instrument, "M1", start, start + timedelta(minutes=2))
    assert quality["unexpected_gap_count"] == 0
    assert quality["unexpected_source_periods"]
    assert quality["trusted"] is False


def test_sparse_trust_rejects_implausible_open_session_coverage(tmp_path: Path) -> None:
    instrument = get_instrument("EURUSD")
    start = datetime(2024, 1, 2, 12, tzinfo=UTC)
    end = start + timedelta(hours=1)
    period = SourcePeriod("p", start, "url", tmp_path / "p", True)
    candle = Candle(start, 1, 1, 1, 1, 1, 1, 0)
    quality = validate_candles([candle], [FetchResult(period, "cached")], instrument, "M1", start, end)
    assert quality["unexpected_source_periods"] == []
    assert quality["unexpected_gap_count"] == 0
    assert quality["open_session_expected_bars"] == 60
    assert quality["open_session_present_bars"] == 1
    assert quality["open_session_missing_bars"] == 59
    assert quality["open_session_coverage_pct"] == pytest.approx(1.667)
    assert quality["sparse_trust_failed"] is True
    assert quality["trusted"] is False


def test_sparse_trust_allows_legitimate_sparse_minutes(tmp_path: Path) -> None:
    instrument = get_instrument("EURUSD")
    start = datetime(2024, 1, 2, 12, tzinfo=UTC)
    end = start + timedelta(hours=1)
    period = SourcePeriod("p", start, "url", tmp_path / "p", True)
    quiet_minutes = {3, 17, 29, 41, 53}
    candles = [
        Candle(start + timedelta(minutes=minute), 1, 1, 1, 1, 1, 1, 0)
        for minute in range(60)
        if minute not in quiet_minutes
    ]
    quality = validate_candles(candles, [FetchResult(period, "cached")], instrument, "M1", start, end)
    assert quality["open_session_expected_bars"] == 60
    assert quality["open_session_missing_bars"] == 5
    assert quality["open_session_coverage_pct"] == pytest.approx(91.667)
    assert quality["sparse_trust_failed"] is False
    assert quality["trusted"] is True


@pytest.mark.parametrize(
    ("friday", "close_hour"),
    [
        (datetime(2024, 1, 5, tzinfo=UTC), 22),  # winter: Friday 17:00 EST == 22:00 UTC
        (datetime(2024, 7, 5, tzinfo=UTC), 21),  # summer: Friday 17:00 EDT == 21:00 UTC
    ],
)
def test_friday_post_close_source_failure_is_expected(tmp_path: Path, friday, close_hour) -> None:
    instrument = get_instrument("EURUSD")
    start = friday.replace(hour=close_hour - 1)
    end = friday.replace(hour=close_hour + 1)
    open_period = SourcePeriod("open", start, "url", tmp_path / "open", True)
    closed_period = SourcePeriod("closed", friday.replace(hour=close_hour), "url", tmp_path / "closed", False)
    candle = Candle(start, 1, 1, 1, 1, 1, 1, 0)
    quality = validate_candles(
        [candle],
        [FetchResult(open_period, "cached"), FetchResult(closed_period, "missing", error="HTTP 404")],
        instrument,
        "H1",
        start,
        end,
    )
    assert quality["unexpected_source_periods"] == []
    assert [item["period"] for item in quality["expected_closed_source_periods"]] == ["closed"]
    assert quality["expected_gap_count"] == 1
    assert quality["unexpected_gap_count"] == 0
    assert quality["trusted"] is True


def test_output_contract_and_manifest(tmp_path: Path) -> None:
    candle = Candle(datetime(2024, 1, 2, tzinfo=UTC), 1, 2, 0.5, 1.5, 0, 12.5, 0)
    path = tmp_path / "out.csv"
    write_candles(path, [candle], 5)
    assert path.read_text().splitlines()[0].split(",") == CANDLE_COLUMNS
    native = provenance("native_candle", "bid", "native")
    assert native == {
        "source_kind": "native_candle",
        "offer_side": "bid",
        "volume_semantics": "dukascopy_native",
        "volume_is_trade_volume": False,
        "spread_available": False,
    }


def test_validation_rejects_non_finite_candles(tmp_path: Path) -> None:
    instrument = get_instrument("EURUSD")
    start = datetime(2024, 1, 2, tzinfo=UTC)
    period = SourcePeriod("p", start, "url", tmp_path / "p", True)
    # close=high=inf passes the ordering chain (low <= open <= close <= high) but is not finite.
    candle = Candle(start, 1.1, float("inf"), 1.0, float("inf"), 10, 0, 0)
    quality = validate_candles([candle], [FetchResult(period, "cached")], instrument, "M1", start, start + timedelta(minutes=1))
    assert quality["invalid_ohlc_count"] == 1
    assert quality["trusted"] is False
    nan_candle = Candle(start, 1.1, 1.2, 1.0, float("nan"), 10, 0, 0)
    nan_quality = validate_candles([nan_candle], [FetchResult(period, "cached")], instrument, "M1", start, start + timedelta(minutes=1))
    assert nan_quality["invalid_ohlc_count"] == 1
    assert nan_quality["trusted"] is False


def test_validation_rejects_negative_candle_volume(tmp_path: Path) -> None:
    instrument = get_instrument("EURUSD")
    start = datetime(2024, 1, 2, 12, tzinfo=UTC)
    period = SourcePeriod("p", start, "url", tmp_path / "p", True)
    candle = Candle(start, 1.1, 1.2, 1.0, 1.15, 10, -1.0, 0)
    quality = validate_candles([candle], [FetchResult(period, "cached")], instrument, "M1", start, start + timedelta(minutes=1))
    assert quality["invalid_ohlc_count"] == 1
    assert quality["trusted"] is False


def test_write_candles_rejects_non_finite_values(tmp_path: Path) -> None:
    start = datetime(2024, 1, 2, tzinfo=UTC)
    good = Candle(start, 1.1, 1.2, 1.0, 1.15, 10, 12.5, 0)
    inf_candle = Candle(start + timedelta(minutes=1), 1.1, float("inf"), 1.0, float("inf"), 10, 0, 0)
    path = tmp_path / "out.csv"
    with pytest.raises(ValueError, match="non-finite"):
        write_candles(path, [good, inf_candle], 5)
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(ValueError, match="non-finite"):
        write_candles(path, [Candle(start, 1.1, 1.2, 1.0, 1.15, 10, float("nan"), 0)], 5)
    assert not path.exists()


def test_write_candles_preserves_candle_order(tmp_path: Path) -> None:
    times = [datetime(2024, 1, 2, 9, minute, tzinfo=UTC) for minute in (1, 3, 2)]
    candles = [Candle(time, 1, 1, 1, 1, 1, 1, 0) for time in times]
    path = tmp_path / "out.csv"
    write_candles(path, candles, 5)
    rows = path.read_text().splitlines()[1:]
    assert [row.split(",")[0] + "," + row.split(",")[1] for row in rows] == ["2024.01.02,09:01:00", "2024.01.02,09:03:00", "2024.01.02,09:02:00"]


def test_write_candles_rejects_negative_volume(tmp_path: Path) -> None:
    start = datetime(2024, 1, 2, tzinfo=UTC)
    path = tmp_path / "out.csv"
    with pytest.raises(ValueError, match="negative volume"):
        write_candles(path, [Candle(start, 1.1, 1.2, 1.0, 1.15, 10, -2.5, 0)], 5)
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_write_ticks_rejects_non_finite_and_negative_volumes(tmp_path: Path) -> None:
    start = datetime(2024, 1, 2, 12, tzinfo=UTC)
    good = Tick(start, 1.1, 1.1002, 1.0, 2.0)
    path = tmp_path / "out.csv"
    with pytest.raises(ValueError, match="non-finite"):
        write_ticks(path, [good, Tick(start + timedelta(seconds=1), 1.1, 1.1002, float("nan"), 2.0)], 5)
    assert not path.exists()
    with pytest.raises(ValueError, match="negative"):
        write_ticks(path, [Tick(start, 1.1, 1.1002, -1.0, 2.0)], 5)
    assert not path.exists()
    with pytest.raises(ValueError, match="non-finite"):
        write_ticks(path, [Tick(start, 1.1, float("inf"), 1.0, 2.0)], 5)
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []
