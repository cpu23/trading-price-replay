"""Stats-accumulator equivalence.

The session statistics are maintained incrementally (`book_fill` /
`book_trade_close`) so routine reads never scan history. These tests prove the
incremental accumulator is exactly equivalent to the previous full-history
calculation (`calculate_stats_from_history`) and to the one-time backfill
(`build_accumulator_from_history`) on deterministically generated sessions.
"""
import copy
import math
import random
from datetime import datetime, timezone

from app.domain import Bar, ReplayState, bar_reveal_time
from app.execution import close_trade, open_trade, process_bar, update_close_excursions
from app.stats import (
    build_accumulator_from_history,
    calculate_stats,
    calculate_stats_from_history,
)


def ts(minute: int) -> datetime:
    return datetime(2026, 1, 2, 17, minute, tzinfo=timezone.utc)


def make_state() -> ReplayState:
    return ReplayState.create(
        symbol="TEST",
        start=datetime(2026, 1, 2, 17, 0, tzinfo=timezone.utc),
        end=datetime(2026, 1, 3, 0, 0, tzinfo=timezone.utc),
        profile="utc_aligned",
        chart_context_1m_bars=0,
        advance_step_minutes=1,
        initial_balance=10000.0,
        contract_multiplier=1.0,
        price_precision=5,
        pnl_currency="USD",
    )


def _drive_session(seed: int, n_bars: int) -> ReplayState:
    """Run a seeded, pseudo-random session through the real engine."""
    rng = random.Random(seed)
    state = make_state()
    price = 100.0
    for i in range(n_bars):
        price = max(1.0, price + rng.uniform(-0.5, 0.5))
        open_ = price
        close = price + rng.uniform(-0.3, 0.3)
        high = max(open_, close) + rng.uniform(0.0, 0.3)
        low = min(open_, close) - rng.uniform(0.0, 0.3)
        bar_ = Bar(timestamp=ts(i), open=open_, high=high, low=low, close=close, volume=1.0)
        process_bar(state, bar_, 1.0)
        update_close_excursions(state, bar_, 1.0)
        now = bar_reveal_time(bar_)
        # Enter a trade at the revealed close (with or without a stop/target).
        if rng.random() < 0.5:
            direction = "long" if rng.random() < 0.5 else "short"
            entry = float(bar_.close)
            if direction == "long":
                stop = entry - rng.uniform(0.2, 1.0) if rng.random() < 0.8 else None
                target = entry + rng.uniform(0.2, 1.0) if rng.random() < 0.5 else None
            else:
                stop = entry + rng.uniform(0.2, 1.0) if rng.random() < 0.8 else None
                target = entry - rng.uniform(0.2, 1.0) if rng.random() < 0.5 else None
            try:
                open_trade(state, now, entry, direction, rng.uniform(0.5, 2.0), stop, target, 1.0)
            except ValueError:
                pass
        # Manually close a random open trade, fully or partially.
        open_trades = [t for t in state.trades if t.status == "open"]
        if open_trades and rng.random() < 0.4:
            trade = rng.choice(open_trades)
            quantity = trade.remaining_quantity if rng.random() < 0.7 else trade.remaining_quantity / 2.0
            try:
                close_trade(state, trade, now, float(bar_.close), quantity, "manual", 1.0)
            except ValueError:
                pass
    return state


def _assert_stats_close(a: dict, b: dict, context: str) -> None:
    assert set(a) == set(b), f"{context}: key mismatch {set(a) ^ set(b)}"
    for key in a:
        va, vb = a[key], b[key]
        if isinstance(va, float) or isinstance(vb, float):
            assert math.isclose(va, vb, rel_tol=1e-9, abs_tol=1e-6), \
                f"{context}: {key} {va!r} != {vb!r}"
        else:
            assert va == vb, f"{context}: {key} {va!r} != {vb!r}"


def _assert_accumulator_close(a, b, context: str) -> None:
    for field in a.__dataclass_fields__:
        va, vb = getattr(a, field), getattr(b, field)
        if isinstance(va, list):
            assert len(va) == len(vb), f"{context}: {field} length {len(va)} != {len(vb)}"
            for x, y in zip(va, vb):
                assert math.isclose(x, y, rel_tol=1e-9, abs_tol=1e-9), f"{context}: {field} {x!r} != {y!r}"
        elif isinstance(va, float) or isinstance(vb, float):
            assert math.isclose(va, vb, rel_tol=1e-9, abs_tol=1e-9), f"{context}: {field} {va!r} != {vb!r}"
        else:
            assert va == vb, f"{context}: {field} {va!r} != {vb!r}"


def _assert_rich_history(state: ReplayState, context: str) -> None:
    closed = [t for t in state.trades if t.status == "closed"]
    with_risk = [t for t in closed if t.initial_risk]
    assert len(state.trades) > 10, f"{context}: expected a busy session"
    assert closed, f"{context}: expected closed trades"
    assert with_risk, f"{context}: expected R-bearing trades"
    assert any(t.mfe_gross_pnl is not None for t in state.trades), f"{context}: expected excursions"


def test_incremental_stats_match_full_history_reference():
    for seed in (1, 7, 42, 99, 1234):
        state = _drive_session(seed, 60)
        _assert_rich_history(state, f"seed={seed}")
        last_close = state.fills[-1].market_price if state.fills else 100.0
        inc = calculate_stats(state, last_close, 1.0)
        hist = calculate_stats_from_history(state, last_close, 1.0)
        _assert_stats_close(inc, hist, f"seed={seed} stats")


def test_incremental_stats_match_without_live_price():
    for seed in (3, 21):
        state = _drive_session(seed, 40)
        inc = calculate_stats(state, None, 1.0)
        hist = calculate_stats_from_history(state, None, 1.0)
        _assert_stats_close(inc, hist, f"seed={seed} no-price stats")
        # Equity without a live price is pure realized balance.
        assert inc["equity"] == inc["balance"]


def test_backfill_reconstruction_matches_incremental_accumulator():
    for seed in (5, 60, 2026):
        state = _drive_session(seed, 50)
        incremental = copy.deepcopy(state.accumulator)
        rebuilt_state = copy.deepcopy(state)
        rebuilt = build_accumulator_from_history(rebuilt_state, list(rebuilt_state.trades), list(rebuilt_state.fills))
        _assert_accumulator_close(incremental, rebuilt, f"seed={seed} backfill")
        # The rebuilt state must report identical stats to the incremental one.
        last_close = state.fills[-1].market_price if state.fills else 100.0
        _assert_stats_close(
            calculate_stats(state, last_close, 1.0),
            calculate_stats(rebuilt_state, last_close, 1.0),
            f"seed={seed} rebuilt stats",
        )


def test_empty_session_stats_are_zero_and_equivalent():
    state = make_state()
    inc = calculate_stats(state, 100.0, 1.0)
    hist = calculate_stats_from_history(state, 100.0, 1.0)
    _assert_stats_close(inc, hist, "empty session")
    assert inc["trades_opened"] == 0
    assert inc["balance"] == state.initial_balance
    assert inc["max_drawdown"] == 0.0


def test_partial_close_is_not_double_counted():
    state = make_state()
    b = Bar(timestamp=ts(0), open=100.0, high=101.0, low=99.0, close=100.0, volume=1.0)
    process_bar(state, b, 1.0)
    update_close_excursions(state, b, 1.0)
    now = bar_reveal_time(b)
    trade = open_trade(state, now, 100.0, "long", 2.0, 99.0, 102.0, 1.0)
    # Two partial closes of 1.0 each, then the trade is fully closed.
    close_trade(state, trade, now, 100.5, 1.0, "manual", 1.0)
    assert trade.status == "open"
    close_trade(state, trade, now, 100.6, 1.0, "manual", 1.0)
    assert trade.status == "closed"
    inc = calculate_stats(state, None, 1.0)
    hist = calculate_stats_from_history(state, None, 1.0)
    _assert_stats_close(inc, hist, "partial close")
    # One trade, closed once: no double counting of the completion.
    assert inc["trades_opened"] == 1
    assert inc["trades_completed"] == 1
