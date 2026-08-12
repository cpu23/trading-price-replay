from __future__ import annotations

import lzma
import struct
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from dukascopy_downloader.catalog import get_instrument
from dukascopy_downloader.config import inclusive_date_range
from dukascopy_downloader.sources import (
    native_candle_url,
    native_periods,
    parse_native_payload,
    parse_tick_payload,
    tick_periods,
    tick_url,
)


UTC = timezone.utc


def test_dukascopy_urls_use_zero_based_months_and_utc_boundaries(tmp_path: Path) -> None:
    hour = datetime(2024, 1, 31, 23, tzinfo=UTC)
    assert tick_url("EURUSD", hour).endswith("/EURUSD/2024/00/31/23h_ticks.bi5")
    assert native_candle_url("EURUSD", "M1", "bid", hour).endswith("/EURUSD/2024/00/31/BID_candles_min_1.bi5")
    assert native_candle_url("EURUSD", "H1", "ask", datetime(2024, 12, 1, tzinfo=UTC)).endswith("/2024/11/ASK_candles_hour_1.bi5")
    assert native_candle_url("EURUSD", "D1", "bid", hour).endswith("/EURUSD/2024/BID_candles_day_1.bi5")
    start, end = inclusive_date_range(date(2024, 1, 31), date(2024, 2, 1))
    assert start == datetime(2024, 1, 31, tzinfo=UTC)
    assert end == datetime(2024, 2, 2, tzinfo=UTC)
    periods = native_periods(get_instrument("EURUSD"), "M1", "bid", start, end, tmp_path)
    assert len(periods) == 2


@pytest.mark.parametrize(
    ("symbol", "raw_ask", "raw_bid", "ask", "bid"),
    [
        ("EURUSD", 109_693, 109_691, 1.09693, 1.09691),
        ("USDJPY", 150_123, 150_120, 150.123, 150.120),
        ("DEUIDXEUR", 18_001_500, 18_000_500, 18001.5, 18000.5),
        ("XAUUSD", 2_300_500, 2_300_000, 2300.5, 2300.0),
        ("BTCUSD", 700_010, 700_000, 70001.0, 70000.0),
    ],
)
def test_tick_parser_uses_verified_ask_bid_layout(symbol, raw_ask, raw_bid, ask, bid) -> None:
    raw = struct.pack(">IIIff", 37, raw_ask, raw_bid, 1.25, 2.5)
    ticks = parse_tick_payload(lzma.compress(raw), datetime(2024, 1, 2, 12, tzinfo=UTC), get_instrument(symbol))
    assert ticks[0].ask == ask
    assert ticks[0].bid == bid
    assert ticks[0].ask_volume == 1_250_000
    assert ticks[0].bid_volume == 2_500_000


def test_tick_parser_rejects_malformed_and_crossed_records() -> None:
    instrument = get_instrument("EURUSD")
    with pytest.raises(ValueError, match="not divisible"):
        parse_tick_payload(lzma.compress(b"bad"), datetime(2024, 1, 2, 12, tzinfo=UTC), instrument)
    crossed = struct.pack(">IIIff", 0, 100_000, 100_001, 1.0, 1.0)
    with pytest.raises(ValueError, match="invalid tick"):
        parse_tick_payload(lzma.compress(crossed), datetime(2024, 1, 2, 12, tzinfo=UTC), instrument)


def test_tick_parser_rejects_non_finite_and_negative_volumes() -> None:
    instrument = get_instrument("EURUSD")
    hour = datetime(2024, 1, 2, 12, tzinfo=UTC)
    nan_volume = struct.pack(">IIIff", 0, 100_000, 99_990, float("nan"), 1.0)
    with pytest.raises(ValueError, match="invalid tick volumes"):
        parse_tick_payload(lzma.compress(nan_volume), hour, instrument)
    negative_volume = struct.pack(">IIIff", 0, 100_000, 99_990, 1.0, -0.5)
    with pytest.raises(ValueError, match="invalid tick volumes"):
        parse_tick_payload(lzma.compress(negative_volume), hour, instrument)


def test_native_parser_layout_volume_and_empty_periods() -> None:
    instrument = get_instrument("EURUSD")
    raw = b"".join(
        [
            struct.pack(">IIIIIf", 60, 110_000, 110_010, 109_990, 110_020, 12.5),
            struct.pack(">IIIIIf", 120, 110_010, 110_010, 110_010, 110_010, 0.0),
        ]
    )
    candles = parse_native_payload(lzma.compress(raw), datetime(2024, 1, 2, tzinfo=UTC), instrument)
    assert len(candles) == 1
    assert candles[0].time == datetime(2024, 1, 2, 0, 1, tzinfo=UTC)
    assert candles[0].open == 1.1
    assert candles[0].high == 1.1002
    assert candles[0].close == 1.1001
    assert candles[0].volume == 12.5
    assert candles[0].tick_volume == candles[0].spread == 0


def test_native_parser_rejects_malformed_file() -> None:
    with pytest.raises(ValueError, match="not divisible"):
        parse_native_payload(lzma.compress(b"bad"), datetime(2024, 1, 2, tzinfo=UTC), get_instrument("EURUSD"))


def test_native_parser_rejects_non_finite_and_negative_volume() -> None:
    instrument = get_instrument("EURUSD")
    base = datetime(2024, 1, 2, tzinfo=UTC)
    nan_volume = struct.pack(">IIIIIf", 60, 110_000, 110_010, 109_990, 110_020, float("nan"))
    with pytest.raises(ValueError, match="invalid volume"):
        parse_native_payload(lzma.compress(nan_volume), base, instrument)
    negative_volume = struct.pack(">IIIIIf", 60, 110_000, 110_010, 109_990, 110_020, -12.5)
    with pytest.raises(ValueError, match="invalid volume"):
        parse_native_payload(lzma.compress(negative_volume), base, instrument)


def test_tick_periods_use_dst_aware_fx_session(tmp_path: Path) -> None:
    instrument = get_instrument("EURUSD")
    # Winter (EST, UTC-5): Friday 22:00 UTC is post-close; Sunday 22:00 UTC is the reopen.
    start = datetime(2024, 1, 5, 22, tzinfo=UTC)
    end = datetime(2024, 1, 7, 23, tzinfo=UTC)
    periods = tick_periods(instrument, start, end, tmp_path)
    by_hour = {period.base_time: period for period in periods}
    assert by_hour[datetime(2024, 1, 5, 22, tzinfo=UTC)].expected_open is False
    assert by_hour[datetime(2024, 1, 6, 12, tzinfo=UTC)].expected_open is False
    assert by_hour[datetime(2024, 1, 7, 22, tzinfo=UTC)].expected_open is True


def test_native_parser_accepts_empty_period() -> None:
    assert parse_native_payload(lzma.compress(b""), datetime(2024, 1, 2, tzinfo=UTC), get_instrument("EURUSD")) == []
