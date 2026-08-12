from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from dukascopy_downloader.catalog import INSTRUMENTS, get_instrument
from dukascopy_downloader.cli import main, parse_args, select_source


UTC = timezone.utc


def test_curated_catalog_is_complete() -> None:
    assert set(INSTRUMENTS) == {"DEUIDXEUR", "EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "BTCUSD", "USATECHIDXUSD"}
    for item in INSTRUMENTS.values():
        assert item.price_scale > 0
        assert item.point_size > 0
        assert item.precision >= 0
        assert item.tick_volume_scale == 1_000_000
        assert item.native_timeframes == ("M1", "H1", "D1")
        assert item.session_model
        assert isinstance(item.sparse_candles_expected, bool)
    assert INSTRUMENTS["BTCUSD"].price_scale == 10


def test_unknown_instrument_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported instrument"):
        get_instrument("NOTREAL")


def test_discovery_commands(capsys) -> None:
    assert main(["--list-instruments"]) == 0
    assert "EURUSD" in capsys.readouterr().out
    assert main(["--describe", "USDJPY"]) == 0
    assert json.loads(capsys.readouterr().out)["precision"] == 3


def test_tick_rejects_price_and_volume() -> None:
    with pytest.raises(SystemExit):
        parse_args(["EURUSD", "--start", "2024-01-01", "--end", "2024-01-01", "--timeframe", "TICK", "--price", "bid"])


@pytest.mark.parametrize(
    ("source", "price", "volume", "expected"),
    [
        ("auto", "bid", "native", "native_candle"),
        ("auto", "ask", "native", "native_candle"),
        ("auto", "mid", "total", "tick"),
        ("auto", "bid", "ticks", "tick"),
        ("tick", "ask", "bid", "tick"),
        ("native", "ask", "native", "native_candle"),
    ],
)
def test_source_selection(source: str, price: str, volume: str, expected: str) -> None:
    assert select_source(source, price, volume) == expected


def test_invalid_explicit_sources() -> None:
    with pytest.raises(ValueError):
        select_source("native", "mid", "native")
    with pytest.raises(ValueError):
        select_source("tick", "bid", "native")


@pytest.mark.parametrize(
    ("friday", "close_hour", "sunday", "open_hour"),
    [
        (datetime(2024, 1, 5, tzinfo=UTC), 22, datetime(2024, 1, 7, tzinfo=UTC), 22),  # winter EST (UTC-5)
        (datetime(2024, 7, 5, tzinfo=UTC), 21, datetime(2024, 7, 7, tzinfo=UTC), 21),  # summer EDT (UTC-4)
    ],
)
def test_fx_session_boundaries_are_dst_aware(friday, close_hour, sunday, open_hour) -> None:
    eurusd = get_instrument("EURUSD")
    # Friday: open through the hour before close, closed from the close hour on.
    assert eurusd.expected_open(friday.replace(hour=close_hour - 1)) is True
    assert eurusd.expected_open(friday.replace(hour=close_hour)) is False
    assert eurusd.expected_open(friday.replace(hour=close_hour + 1)) is False
    # Sunday: closed until the reopen hour, open from it on.
    assert eurusd.expected_open(sunday.replace(hour=open_hour - 1)) is False
    assert eurusd.expected_open(sunday.replace(hour=open_hour)) is True
    assert eurusd.expected_open(sunday.replace(hour=open_hour + 1)) is True


def test_fx_session_is_24h_midweek_and_closed_on_saturday() -> None:
    eurusd = get_instrument("EURUSD")
    wednesday = datetime(2024, 1, 3, 21, tzinfo=UTC)
    assert eurusd.expected_open(wednesday) is True
    assert eurusd.expected_open(wednesday.replace(hour=0)) is True
    assert eurusd.expected_open(wednesday.replace(hour=23)) is True
    saturday = datetime(2024, 1, 6, 12, tzinfo=UTC)
    assert eurusd.expected_open(saturday) is False
    assert eurusd.expected_open(saturday.replace(hour=0)) is False


def test_fx_session_handles_dst_transition_weeks() -> None:
    eurusd = get_instrument("EURUSD")
    # Week before the 2024-03-10 spring-forward: still EST, Friday closes 22:00 UTC.
    assert eurusd.expected_open(datetime(2024, 3, 8, 21, tzinfo=UTC)) is True
    assert eurusd.expected_open(datetime(2024, 3, 8, 22, tzinfo=UTC)) is False
    # Week after: EDT, Friday closes one hour earlier at 21:00 UTC.
    assert eurusd.expected_open(datetime(2024, 3, 15, 20, tzinfo=UTC)) is True
    assert eurusd.expected_open(datetime(2024, 3, 15, 21, tzinfo=UTC)) is False
    # Week before the 2024-11-03 fall-back: still EDT, closes 21:00 UTC.
    assert eurusd.expected_open(datetime(2024, 11, 1, 21, tzinfo=UTC)) is False
    # Week after: EST again, Friday close shifts back to 22:00 UTC.
    assert eurusd.expected_open(datetime(2024, 11, 8, 21, tzinfo=UTC)) is True
    assert eurusd.expected_open(datetime(2024, 11, 8, 22, tzinfo=UTC)) is False
