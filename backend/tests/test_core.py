from datetime import datetime, timezone

import pytest

from app.domain import Bar, ReplayState
from app.execution import open_trade, process_bar
from app.indicators import sma
from app.stats import calculate_stats
from app.timeframes import bucket_for, resample


def bar(minute: int, open_: float = 10, high: float = 11, low: float = 9, close: float = 10) -> Bar:
    return Bar(datetime(2026, 1, 2, 12, minute, tzinfo=timezone.utc), open_, high, low, close, 1)


def test_utc_resample_and_partial_candle():
    result = resample([bar(0), bar(1), bar(5)], "5m", "utc_aligned")
    assert len(result) == 2
    assert result[0].is_partial is False
    assert result[1].is_partial is True


def test_new_york_daily_anchor_is_dst_aware():
    winter = bucket_for(datetime(2026, 1, 2, 23, tzinfo=timezone.utc), "1d", "new_york_close")
    summer = bucket_for(datetime(2026, 7, 2, 22, tzinfo=timezone.utc), "1d", "new_york_close")
    assert winter.hour == 22
    assert summer.hour == 21


def test_stop_wins_when_stop_and_target_touch():
    state = ReplayState.create(symbol="TEST", start=bar(0).timestamp, end=bar(5).timestamp, profile="utc_aligned")
    trade = open_trade(state, bar(0).timestamp, 10, "long", 1, 9, 11, 1)
    process_bar(state, bar(1, high=12, low=8), 1)
    assert trade.status == "closed"
    assert [fill.reason for fill in state.fills] == ["entry", "stop"]
    assert calculate_stats(state)["net_pnl"] == -1


def test_target_gap_beats_later_same_candle_stop():
    state = ReplayState.create(symbol="TEST", start=bar(0).timestamp, end=bar(5).timestamp, profile="utc_aligned")
    trade = open_trade(state, bar(0).timestamp, 10, "long", 1, 9, 11, 1)
    # The open gaps through the target; the stop is only touched later intrabar.
    process_bar(state, bar(1, open_=12, high=12, low=8), 1)
    assert trade.status == "closed"
    assert [fill.reason for fill in state.fills] == ["entry", "target"]
    assert state.fills[-1].market_price == 12  # contracted gap price at the open
    assert state.fills[-1].pnl == pytest.approx(2.0)


def test_short_target_gap_beats_later_same_candle_stop():
    state = ReplayState.create(symbol="TEST", start=bar(0).timestamp, end=bar(5).timestamp, profile="utc_aligned")
    trade = open_trade(state, bar(0).timestamp, 10, "short", 1, 11, 9, 1)
    # Mirrored: the open gaps through the short target; the stop is touched later.
    process_bar(state, bar(1, open_=8, high=12, low=8), 1)
    assert trade.status == "closed"
    assert [fill.reason for fill in state.fills] == ["entry", "target"]
    assert state.fills[-1].market_price == 8
    assert state.fills[-1].pnl == pytest.approx(2.0)


def test_opening_gap_stop_beats_later_target_touch():
    state = ReplayState.create(symbol="TEST", start=bar(0).timestamp, end=bar(5).timestamp, profile="utc_aligned")
    trade = open_trade(state, bar(0).timestamp, 10, "long", 1, 9, 11, 1)
    # The open gaps through the stop; the target is only touched later intrabar.
    process_bar(state, bar(1, open_=8.5, high=13, low=8), 1)
    assert trade.status == "closed"
    assert [fill.reason for fill in state.fills] == ["entry", "stop"]
    assert state.fills[-1].market_price == 8.5  # contracted gap price at the open


def test_sma_is_causal():
    values = sma(resample([bar(i, close=float(i)) for i in range(35)], "1m", "utc_aligned"))
    assert len(values) == 1
    assert values[0]["value"] == 17
