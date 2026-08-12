import json
import itertools
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app import config, market_data, repository
from app.domain import Bar, Fill, ReplayState, Trade
from app.market_data import RangeBars, _BARS_PAGE_SIZE
from app.service import state_response, step


def ts(minute: int = 0) -> datetime:
    return datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc) + timedelta(minutes=minute)


# ---------------------------------------------------------------------------
# Lazy replay sequence: step/state must touch only bounded pages of a very
# large range, never load it fully.
# ---------------------------------------------------------------------------

def _mock_partition_reads(monkeypatch, years: list[int], bars_per_year: int) -> list[tuple[int, int, int]]:
    """Point RangeBars at fake partitions and count every page read.

    Returns the recorded (year, file_offset, length) page reads.
    """
    page_reads: list[tuple[int, int, int]] = []

    def fake_partitions(symbol, version, start, end):
        return [(year, Path(f"/fake/{year}.parquet")) for year in years]

    def fake_count(path, year, start, end):
        return bars_per_year

    def fake_count_before(path, year, before):
        return 0

    def fake_page(path, year, file_offset, length):
        page_reads.append((year, file_offset, length))
        base = datetime(year, 1, 1, tzinfo=timezone.utc)
        return [
            Bar(base + timedelta(minutes=file_offset + index), 1.0, 1.1, 0.9, 1.0, 10.0)
            for index in range(length)
        ]

    monkeypatch.setattr(market_data, "partitions_for_range", fake_partitions)
    monkeypatch.setattr(market_data, "count_bars_partition", fake_count)
    monkeypatch.setattr(market_data, "count_bars_before_partition", fake_count_before)
    monkeypatch.setattr(market_data, "load_bars_page", fake_page)
    return page_reads


def test_range_bars_len_and_indexing_never_touch_the_full_range(monkeypatch):
    # Non-leap years only, so 525_600 bars per year matches the synthetic count.
    start = datetime(2021, 1, 1, tzinfo=timezone.utc)
    end = datetime(2023, 12, 31, 23, 59, tzinfo=timezone.utc)
    page_reads = _mock_partition_reads(monkeypatch, [2021, 2022, 2023], 525_600)

    replay = RangeBars("HUGE", start, end, "v1")
    # len() is count-only: ~1.5M bars computed without a single page read.
    assert len(replay) == 3 * 525_600
    assert page_reads == []

    # Integer access reads exactly one bounded page.
    assert replay[0].timestamp == datetime(2021, 1, 1, tzinfo=timezone.utc)
    assert len(page_reads) == 1
    assert page_reads[0][2] <= _BARS_PAGE_SIZE

    # Negative indices and cross-partition slices stay bounded and correct.
    tail = replay[-5:]
    assert len(tail) == 5
    assert tail[0].timestamp == datetime(2023, 12, 31, 23, 55, tzinfo=timezone.utc)
    middle = replay[525_600 + 100: 525_600 + 103]
    assert [bar.timestamp for bar in middle] == [
        datetime(2022, 1, 1, 1, 40, tzinfo=timezone.utc),
        datetime(2022, 1, 1, 1, 41, tzinfo=timezone.utc),
        datetime(2022, 1, 1, 1, 42, tzinfo=timezone.utc),
    ]
    assert all(length <= _BARS_PAGE_SIZE for _, _, length in page_reads)

    # Sequential access reuses the cached page: one new page at most.
    reads_before = len(page_reads)
    for index in range(1_200_000, 1_200_010):
        replay[index]
    assert len(page_reads) - reads_before <= 1
    assert all(length <= _BARS_PAGE_SIZE for _, _, length in page_reads)


def test_range_bars_iteration_preserves_global_order_across_partitions(monkeypatch):
    # Non-leap years only, so 525_600 bars per year matches the synthetic count.
    start = datetime(2019, 1, 1, tzinfo=timezone.utc)
    end = datetime(2021, 12, 31, 23, 59, tzinfo=timezone.utc)
    page_reads = _mock_partition_reads(monkeypatch, [2019, 2021], 525_600)

    replay = RangeBars("HUGE", start, end, "v1")
    assert len(replay) == 2 * 525_600
    # Spot-check around the year boundary: 2019-12-31 23:5x then 2021-01-01 00:0x.
    around = list(itertools.islice(replay, 525_596, 525_604))
    assert around[0].timestamp == datetime(2019, 12, 31, 23, 56, tzinfo=timezone.utc)
    assert around[3].timestamp == datetime(2019, 12, 31, 23, 59, tzinfo=timezone.utc)
    assert around[4].timestamp == datetime(2021, 1, 1, 0, 0, tzinfo=timezone.utc)
    assert around[-1].timestamp == datetime(2021, 1, 1, 0, 3, tzinfo=timezone.utc)
    assert all(length <= _BARS_PAGE_SIZE for _, _, length in page_reads)


@pytest.fixture()
def db_paths(tmp_path, monkeypatch):
    for module in (config, market_data, repository):
        for name, relative in (("RAW_ROOT", "raw"), ("OHLCV_ROOT", "ohlcv"), ("DB_PATH", "sessions/db.sqlite3")):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, tmp_path / relative)
    market_data.invalidate_bars()
    repository.initialize()
    return tmp_path


def _huge_state(closed_count: int = 3000, fills_per_closed: int = 2, open_count: int = 5) -> ReplayState:
    """A session with thousands of trades and fills exceeding every response cap."""
    state = ReplayState.create(
        symbol="BIG", start=ts(0), end=ts(closed_count + open_count), profile="utc_aligned",
        contract_multiplier=1.0, price_precision=5, pnl_currency="USD",
    )
    for index in range(closed_count + open_count):
        trade_id = str(uuid4())
        open_ = index >= closed_count
        state.trades.append(Trade(
            id=trade_id, session_id=state.id, direction="long", initial_quantity=1.0,
            remaining_quantity=0.0 if not open_ else 1.0, entry_time=ts(index),
            entry_price=10.0, entry_market_price=10.0, realized_pnl=0.0,
            status="open" if open_ else "closed",
        ))
        state.fills.append(Fill(
            id=str(uuid4()), trade_id=trade_id, session_id=state.id, timestamp=ts(index),
            price=10.0, quantity=1.0, reason="entry", pnl=-0.5, market_price=10.0,
            gross_pnl=0.0, commission=0.0, spread_cost=0.0, slippage_cost=0.0,
        ))
        if not open_:
            for fill_index in range(fills_per_closed - 1):
                state.fills.append(Fill(
                    id=str(uuid4()), trade_id=trade_id, session_id=state.id,
                    timestamp=ts(index + fill_index + 1), price=10.0, quantity=0.2,
                    reason="manual", pnl=1.0, market_price=10.0, gross_pnl=1.0,
                    commission=0.0, spread_cost=0.0, slippage_cost=0.0,
                ))
    return state


def test_state_json_stays_bounded_while_tables_keep_full_history(db_paths):
    state = _huge_state()  # 3005 trades, ~6000 fills
    repository.save_session(state, "first_save")
    with repository.connect() as db:
        blob = db.execute("SELECT state_json FROM replay_sessions WHERE id=?", (state.id,)).fetchone()[0]
    assert len(blob) < 4096  # no trade/fill history embedded
    value = json.loads(blob)
    assert "trades" not in value
    assert "fills" not in value

    # A repeated save stays bounded (only the revision field changes between
    # saves; the trade/fill history is never embedded or reserialized).
    repository.save_session(state, "second_save")
    with repository.connect() as db:
        blob2 = db.execute("SELECT state_json FROM replay_sessions WHERE id=?", (state.id,)).fetchone()[0]
    assert len(blob2) < 4096
    value2 = json.loads(blob2)
    assert "trades" not in value2
    assert "fills" not in value2
    assert value2["revision"] == 1

    # The normalized tables keep the complete history, in insertion order.
    loaded = repository.load_session(state.id)
    assert len(loaded.trades) == 3005
    assert len(loaded.fills) == 3005 + 3000
    assert [trade.id for trade in loaded.trades] == [trade.id for trade in state.trades]
    assert [fill.id for fill in loaded.fills] == [fill.id for fill in state.fills]
    # Statistics inputs reconstruct in full from the tables.
    assert sum(fill.pnl for fill in loaded.fills) == sum(fill.pnl for fill in state.fills)


def test_step_and_state_touch_only_bounded_pages_of_a_huge_range(db_paths, monkeypatch):
    start = datetime(2018, 1, 1, tzinfo=timezone.utc)
    end = datetime(2025, 12, 31, 23, 59, tzinfo=timezone.utc)
    page_reads = _mock_partition_reads(monkeypatch, list(range(2018, 2026)), 525_600)

    state = ReplayState.create(
        symbol="HUGE", start=start, end=end, profile="utc_aligned",
        chart_context_1m_bars=500, advance_step_minutes=1, current_index=2_000_000,
        contract_multiplier=1.0, price_precision=5, pnl_currency="USD",
    )
    response = state_response(state)
    assert response["current_price"] is not None
    step(state)
    step(state)
    assert state.status == "active"

    # len()/remaining_bars used partition counts, never a page read; every page
    # read is bounded and lies within a page-sized window around the cursor.
    assert len(page_reads) <= 6
    assert all(length <= _BARS_PAGE_SIZE for _, _, length in page_reads)
    assert {year for year, _, _ in page_reads} <= {2021}  # cursor year only
    # The full range was never materialized: no read approaches the range length.
    assert sum(length for _, _, length in page_reads) <= 6 * _BARS_PAGE_SIZE


def test_legacy_json_snapshot_reconstructs_when_normalized_tables_are_empty(db_paths):
    state = _huge_state(closed_count=3, fills_per_closed=2, open_count=1)
    payload = repository.serializable(state)  # legacy format: full history embedded
    with repository.connect() as db:
        db.execute(
            "INSERT INTO replay_sessions(id,state_json,updated_at,revision) VALUES(?,?,?,1)",
            (state.id, json.dumps(payload), "2026-01-01T00:00:00+00:00"),
        )
    loaded = repository.load_session(state.id)
    assert loaded is not None
    assert len(loaded.trades) == 4
    assert len(loaded.fills) == 7
    assert [trade.id for trade in loaded.trades] == [trade.id for trade in state.trades]
    assert [fill.id for fill in loaded.fills] == [fill.id for fill in state.fills]
    assert loaded.revision == 1


def test_post_cas_failure_restores_in_memory_revision(db_paths):
    state = ReplayState.create(
        symbol="TEST", start=ts(0), end=ts(59), profile="utc_aligned", contract_multiplier=1.0,
    )
    repository.save_session(state, "session_started")
    assert state.revision == 1

    state.current_index = 3
    # The CAS UPDATE succeeds, then a later statement (a non-serializable order
    # payload) fails; the whole transaction must roll back and the in-memory
    # revision must be restored so the caller can retry the save.
    with pytest.raises(ValueError):
        repository.save_session(state, "bad_event", orders=[
            {"trade_id": "t1", "order_type": "market_entry", "payload": {"v": float("nan")}},
        ])
    assert state.revision == 1  # restored, not left at the phantom 2
    reloaded = repository.load_session(state.id)
    assert reloaded.revision == 1
    assert reloaded.current_index == -1  # nothing from the failed save leaked
    with repository.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM orders WHERE session_id=?", (state.id,)).fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM replay_events WHERE session_id=?", (state.id,)
        ).fetchone()[0] == 1

    # The restored state saves normally (CAS against revision 1 succeeds).
    state.current_index = 5
    repository.save_session(state, "replay_stepped")
    assert state.revision == 2
    assert repository.load_session(state.id).current_index == 5


def test_first_save_failure_leaves_revision_none(db_paths):
    state = ReplayState.create(
        symbol="TEST", start=ts(0), end=ts(59), profile="utc_aligned", contract_multiplier=1.0,
    )
    with pytest.raises(ValueError):
        repository.save_session(state, "bad_event", orders=[
            {"trade_id": "t1", "order_type": "market_entry", "payload": {"v": float("nan")}},
        ])
    assert state.revision is None
    assert repository.load_session(state.id) is None
    # A fresh insert still works after the rolled-back attempt.
    repository.save_session(state, "session_started")
    assert state.revision == 1
