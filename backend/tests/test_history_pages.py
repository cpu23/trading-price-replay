"""Paginated history endpoints, cursor contract, and chart-focus semantics.

Covers the public history APIs (multiple pages, no overlap, stable ordering
under concurrent appends, status filtering, invalid/foreign cursors, final
page, bounded reads) and the execution-anchor fields the chart relies on:
markers must key to the source candle's open (the chart's time axis), never
to the execution timestamp.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, market_data, repository
from app.main import app

FIXTURES = Path(__file__).parent / "fixtures"


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    for module in (config, market_data, repository):
        for name, relative in (("RAW_ROOT", "raw"), ("OHLCV_ROOT", "ohlcv"), ("DB_PATH", "sessions/db.sqlite3")):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, tmp_path / relative)
    market_data.invalidate_bars()
    with TestClient(app) as test_client:
        imported = test_client.post("/api/imports", json={
            "path": str(FIXTURES / "dukascopy_1m.csv"), "symbol": "EURUSD",
            "asset_class": "forex", "pnl_currency": "USD", "price_precision": 5,
            "contract_multiplier": 1, "default_profile": "utc_aligned",
        })
        assert imported.status_code == 200, imported.text
        yield test_client


def create_session(client, **overrides):
    body = {
        "symbol": "EURUSD", "start": "2026-01-02T17:00:00Z", "end": "2026-01-02T17:06:00Z",
        "chart_context_1m_bars": 500, "advance_step_minutes": 1,
    }
    body.update(overrides)
    response = client.post("/api/replay/sessions", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def seed_closed_trades(client, session_id: str, count: int) -> list[str]:
    """Count closed round-trip trades at the first revealed price, in creation order."""
    assert client.post(f"/api/replay/sessions/{session_id}/step").status_code == 200
    trade_ids: list[str] = []
    for _ in range(count):
        opened = client.post(f"/api/replay/sessions/{session_id}/orders/market",
                             json={"direction": "long", "quantity": 1})
        assert opened.status_code == 200, opened.text
        assert opened.json()["trade_upserts"], "market order must upsert the new trade"
        closed = client.post(f"/api/replay/sessions/{session_id}/close-all")
        assert closed.status_code == 200, closed.text
        trade_ids.append(closed.json()["newly_closed_trades"][0]["id"])
    return trade_ids


def get_page(client, session_id: str, path: str, **params):
    response = client.get(f"/api/replay/sessions/{session_id}/{path}", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def collect_all(client, session_id: str, path: str, **params) -> list[str]:
    items: list[str] = []
    cursor = None
    while True:
        page = get_page(client, session_id, path, **params, **({"cursor": cursor} if cursor else {}))
        items.extend(item["id"] for item in page["items"])
        if page["next_cursor"] is None:
            return items
        cursor = page["next_cursor"]


# ---------------------------------------------------------------------------
# Trade history pagination
# ---------------------------------------------------------------------------

def test_trade_pages_cover_full_history_without_overlap(client):
    session_id = create_session(client)["id"]
    created = seed_closed_trades(client, session_id, 13)

    page1 = get_page(client, session_id, "trades", status="closed", limit=5)
    assert page1["total"] == 13
    assert len(page1["items"]) == 5
    # Newest first: the first page is the five most recently created trades.
    assert [item["id"] for item in page1["items"]] == list(reversed(created[-5:]))
    assert page1["next_cursor"] == page1["items"][-1]["id"]

    all_ids = collect_all(client, session_id, "trades", status="closed", limit=5)
    assert len(all_ids) == 13
    assert len(set(all_ids)) == 13, "pages must not overlap"
    # Stable newest-first ordering across page boundaries.
    assert all_ids == list(reversed(created))


def test_trade_status_filtering(client):
    session_id = create_session(client)["id"]
    closed = seed_closed_trades(client, session_id, 10)
    opened = client.post(f"/api/replay/sessions/{session_id}/orders/market",
                         json={"direction": "long", "quantity": 1})
    assert opened.status_code == 200, opened.text
    open_id = opened.json()["trade_upserts"][0]["id"]

    open_page = get_page(client, session_id, "trades", status="open", limit=50)
    assert open_page["total"] == 1
    assert [item["id"] for item in open_page["items"]] == [open_id]

    closed_page = get_page(client, session_id, "trades", status="closed", limit=50)
    assert closed_page["total"] == 10
    assert [item["id"] for item in closed_page["items"]] == list(reversed(closed))
    assert closed_page["next_cursor"] is None, "a single full page is the final page"


def test_trade_page_reports_review_fields_on_closed_trades(client):
    session_id = create_session(client)["id"]
    [trade_id] = seed_closed_trades(client, session_id, 1)
    reviewed = client.patch(f"/api/trades/{trade_id}/review",
                            json={"session_id": session_id, "review_note": "note", "review_tags": ["a"]})
    assert reviewed.status_code == 200, reviewed.text

    page = get_page(client, session_id, "trades", status="closed", limit=50)
    assert page["items"][0]["review_note"] == "note"
    assert page["items"][0]["review_tags"] == ["a"]


def test_invalid_and_foreign_session_cursors_are_rejected(client):
    session_a = create_session(client)["id"]
    session_b = create_session(client)["id"]
    [trade_a] = seed_closed_trades(client, session_a, 1)
    [trade_b] = seed_closed_trades(client, session_b, 1)

    unknown = client.get(f"/api/replay/sessions/{session_a}/trades",
                         params={"cursor": "no-such-id"})
    assert unknown.status_code == 400
    foreign = client.get(f"/api/replay/sessions/{session_a}/trades",
                         params={"cursor": trade_b})
    assert foreign.status_code == 400
    # The session's own cursor still works.
    own = client.get(f"/api/replay/sessions/{session_a}/trades", params={"cursor": trade_a})
    assert own.status_code == 200
    assert own.json()["items"] == []

    fills_foreign = client.get(f"/api/replay/sessions/{session_a}/fills",
                               params={"cursor": "no-such-fill"})
    assert fills_foreign.status_code == 400


def test_concurrent_appends_do_not_shift_open_pages(client):
    session_id = create_session(client)["id"]
    seed_closed_trades(client, session_id, 8)

    page1 = get_page(client, session_id, "trades", status="closed", limit=3)
    cursor = page1["next_cursor"]
    page2_before = get_page(client, session_id, "trades", status="closed", limit=3, cursor=cursor)

    seed_closed_trades(client, session_id, 2)  # appends two newer rows

    page2_after = get_page(client, session_id, "trades", status="closed", limit=3, cursor=cursor)
    assert [item["id"] for item in page2_after["items"]] \
        == [item["id"] for item in page2_before["items"]], \
        "rows appended after a page was read must not shift that page"

    fresh = get_page(client, session_id, "trades", status="closed", limit=3)
    assert len(fresh["items"]) == 3
    assert fresh["total"] == 10
    all_ids = collect_all(client, session_id, "trades", status="closed", limit=3)
    assert len(all_ids) == 10 and len(set(all_ids)) == 10


def test_limit_bounds_are_enforced(client):
    session_id = create_session(client)["id"]
    assert client.get(f"/api/replay/sessions/{session_id}/trades", params={"limit": 0}).status_code == 422
    assert client.get(f"/api/replay/sessions/{session_id}/trades", params={"limit": 201}).status_code == 422
    assert client.get(f"/api/replay/sessions/{session_id}/fills", params={"limit": 0}).status_code == 422
    assert client.get(f"/api/replay/sessions/{session_id}/fills", params={"limit": 501}).status_code == 422
    assert client.get(f"/api/replay/sessions/{session_id}/trades", params={"limit": 1}).status_code == 200


# ---------------------------------------------------------------------------
# Fill history pagination
# ---------------------------------------------------------------------------

def test_fill_pages_cover_full_history_without_overlap(client):
    session_id = create_session(client)["id"]
    created = seed_closed_trades(client, session_id, 13)  # 26 fills: entry + exit per trade

    page1 = get_page(client, session_id, "fills", limit=10)
    assert page1["total"] == 26
    assert len(page1["items"]) == 10
    assert page1["next_cursor"] == page1["items"][-1]["id"]

    all_ids = collect_all(client, session_id, "fills", limit=10)
    assert len(all_ids) == 26
    assert len(set(all_ids)) == 26, "fill pages must not overlap"
    # Newest first: the newest fill belongs to the last created trade.
    assert page1["items"][0]["trade_id"] == created[-1]


def test_fill_page_is_bounded_by_limit(client):
    session_id = create_session(client)["id"]
    seed_closed_trades(client, session_id, 60)  # 120 fills

    page = get_page(client, session_id, "fills", limit=50)
    assert len(page["items"]) == 50
    assert page["total"] == 120
    # Walking with the smallest allowed limit still terminates at the final page.
    cursor = page["next_cursor"]
    hops = 1
    while cursor is not None:
        page = get_page(client, session_id, "fills", limit=1, cursor=cursor)
        assert len(page["items"]) == 1
        cursor = page["next_cursor"]
        hops += 1
    assert hops <= 120


# ---------------------------------------------------------------------------
# Execution anchors: the chart must key markers to the source candle's open
# ---------------------------------------------------------------------------

def test_market_entry_and_manual_close_anchor_to_the_source_candle(client):
    session_id = create_session(client)["id"]
    [trade_id] = seed_closed_trades(client, session_id, 1)

    page = get_page(client, session_id, "trades", status="closed", limit=50)
    trade = page["items"][0]
    assert trade["id"] == trade_id
    # The 17:00 candle opens at 17:00 and its close becomes causal at 17:01:
    # the entry executes exactly at 17:01, but the chart anchor is the candle.
    assert parse(trade["entry_time"]) == datetime(2026, 1, 2, 17, 1, tzinfo=timezone.utc)
    assert parse(trade["entry_source_candle_time"]) == datetime(2026, 1, 2, 17, 0, tzinfo=timezone.utc)
    assert trade["exit_time"] is not None
    assert parse(trade["exit_time"]) == datetime(2026, 1, 2, 17, 1, tzinfo=timezone.utc)

    fills = get_page(client, session_id, "fills", limit=50)
    by_reason = {item["reason"]: item for item in fills["items"]}
    entry_fill, exit_fill = by_reason["entry"], by_reason["manual"]
    assert parse(entry_fill["timestamp"]) == datetime(2026, 1, 2, 17, 1, tzinfo=timezone.utc)
    assert parse(entry_fill["source_candle_time"]) == datetime(2026, 1, 2, 17, 0, tzinfo=timezone.utc)
    assert parse(exit_fill["timestamp"]) == datetime(2026, 1, 2, 17, 1, tzinfo=timezone.utc)
    assert parse(exit_fill["source_candle_time"]) == datetime(2026, 1, 2, 17, 0, tzinfo=timezone.utc)


def test_intrabar_stop_anchors_to_the_crossing_candle(client):
    session_id = create_session(client)["id"]
    [trade_id] = seed_closed_trades(client, session_id, 1)
    # Reopen at the same revealed price and set a stop the next candle's low
    # (1.1010) crosses without a gap at its open (1.1013).
    opened = client.post(f"/api/replay/sessions/{session_id}/orders/market",
                         json={"direction": "long", "quantity": 1})
    trade_id = opened.json()["trade_upserts"][0]["id"]
    stopped = client.put(f"/api/trades/{trade_id}/stop",
                         json={"session_id": session_id, "price": 1.1011})
    assert stopped.status_code == 200, stopped.text
    stepped = client.post(f"/api/replay/sessions/{session_id}/step")
    assert stepped.status_code == 200, stepped.text
    closed_trade = stepped.json()["newly_closed_trades"][0]

    assert closed_trade["exit_time_precision"] == "bar_interval"
    # The ordering timestamp is the crossing candle's open; the chart anchor
    # is that same candle.
    assert parse(closed_trade["exit_time"]) == datetime(2026, 1, 2, 17, 1, tzinfo=timezone.utc)
    assert parse(closed_trade["entry_source_candle_time"]) == datetime(2026, 1, 2, 17, 0, tzinfo=timezone.utc)

    fills = get_page(client, session_id, "fills", limit=50)
    stop_fill = next(item for item in fills["items"] if item["reason"] == "stop")
    assert stop_fill["time_precision"] == "bar_interval"
    assert parse(stop_fill["source_candle_time"]) == datetime(2026, 1, 2, 17, 1, tzinfo=timezone.utc)
    assert stop_fill["price"] == 1.1011


def test_opening_gap_stop_executes_exactly_at_the_candle_open(client):
    gapped = client.post("/api/imports", json={
        "path": str(FIXTURES / "gapped_1m.csv"), "symbol": "GAP",
        "asset_class": "forex", "pnl_currency": "USD", "price_precision": 5,
        "contract_multiplier": 1, "default_profile": "utc_aligned",
    })
    assert gapped.status_code == 200, gapped.text
    session = client.post("/api/replay/sessions", json={
        "symbol": "GAP", "start": "2026-01-02T18:00:00Z", "end": "2026-01-02T18:06:00Z",
        "chart_context_1m_bars": 500, "advance_step_minutes": 1,
    })
    assert session.status_code == 200, session.text
    session_id = session.json()["id"]

    assert client.post(f"/api/replay/sessions/{session_id}/step").status_code == 200
    opened = client.post(f"/api/replay/sessions/{session_id}/orders/market",
                         json={"direction": "long", "quantity": 1})
    trade_id = opened.json()["trade_upserts"][0]["id"]
    # Stop between the entry price (1.1013) and the next candle's open
    # (1.1009): the 18:01 candle gaps down through it at the open.
    stopped = client.put(f"/api/trades/{trade_id}/stop",
                         json={"session_id": session_id, "price": 1.1011})
    assert stopped.status_code == 200, stopped.text
    stepped = client.post(f"/api/replay/sessions/{session_id}/step")
    assert stepped.status_code == 200, stepped.text
    closed_trade = stepped.json()["newly_closed_trades"][0]

    assert closed_trade["exit_time_precision"] == "exact"
    assert parse(closed_trade["exit_time"]) == datetime(2026, 1, 2, 18, 1, tzinfo=timezone.utc)

    fills = get_page(client, session_id, "fills", limit=50)
    stop_fill = next(item for item in fills["items"] if item["reason"] == "stop")
    assert stop_fill["time_precision"] == "exact"
    # Gap fills execute at the candle's open price, anchored to that candle.
    assert stop_fill["price"] == 1.1009
    assert parse(stop_fill["source_candle_time"]) == datetime(2026, 1, 2, 18, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Bounded historical chart window for old trades
# ---------------------------------------------------------------------------

def test_chart_history_returns_a_bounded_window_around_an_old_trade(client):
    long_fixture = client.post("/api/imports", json={
        "path": str(FIXTURES / "dukascopy_1m_700.csv"), "symbol": "LONG",
        "asset_class": "forex", "pnl_currency": "USD", "price_precision": 5,
        "contract_multiplier": 1, "default_profile": "utc_aligned",
    })
    assert long_fixture.status_code == 200, long_fixture.text
    session = client.post("/api/replay/sessions", json={
        "symbol": "LONG", "start": "2026-01-02T08:00:00Z", "end": "2026-01-02T20:00:00Z",
        "chart_context_1m_bars": 500, "advance_step_minutes": 5,
    })
    assert session.status_code == 200, session.text
    session_id = session.json()["id"]

    for _ in range(20):
        assert client.post(f"/api/replay/sessions/{session_id}/step").status_code == 200
    opened = client.post(f"/api/replay/sessions/{session_id}/orders/market",
                         json={"direction": "long", "quantity": 1})
    trade_id = opened.json()["trade_upserts"][0]["id"]
    assert client.post(f"/api/replay/sessions/{session_id}/close-all").status_code == 200
    # 525 bars past the trade: beyond the 500-bar live chart context.
    for _ in range(105):
        assert client.post(f"/api/replay/sessions/{session_id}/step").status_code == 200

    response = client.get(f"/api/replay/sessions/{session_id}/trades/{trade_id}/chart-history",
                          params={"context_bars": 50})
    assert response.status_code == 200, response.text
    window = response.json()

    assert window["trade"]["id"] == trade_id
    assert len(window["fills"]) == 2, "the window carries the trade's own ledger"
    bars = window["displayed_bars"]
    assert bars, "the window must contain candles"
    first, last = parse(bars[0]["timestamp"]), parse(bars[-1]["timestamp"])
    entry_anchor = parse(window["trade"]["entry_source_candle_time"])
    # The trade's source candle sits inside the returned window.
    assert first <= entry_anchor <= last
    # Bounded: the window is the trade's span plus 50 minutes of context on
    # each side, never the whole session.
    span = parse(window["trade"]["exit_time"]) - parse(window["trade"]["entry_time"])
    allowed = span + timedelta(minutes=100) + timedelta(minutes=2)
    assert last - first <= allowed
    # No future leakage: the window ends at exit + context, far before the
    # current replay position (bar 625, revealed 18:26).
    assert last <= parse(window["trade"]["exit_time"]) + timedelta(minutes=50) + timedelta(minutes=1)

    missing = client.get(f"/api/replay/sessions/{session_id}/trades/no-such-trade/chart-history")
    assert missing.status_code == 404


# ---------------------------------------------------------------------------
# Causal clamp: a focused window cannot leak unrevealed candles
# ---------------------------------------------------------------------------

def test_chart_history_focus_is_clamped_to_the_revealed_candle(client):
    """While the replay is active, a focused window must not end past the open
    time of the most recently revealed candle: the trade's exit timestamp is
    the source candle's reveal time (one candle ahead of its chart key), and
    candles after the reveal are not causal yet."""
    session_id = create_session(client)["id"]  # 7 bars, 17:00-17:06
    assert client.post(f"/api/replay/sessions/{session_id}/step").status_code == 200
    opened = client.post(f"/api/replay/sessions/{session_id}/orders/market",
                         json={"direction": "long", "quantity": 1})
    assert opened.status_code == 200, opened.text
    trade_id = opened.json()["trade_upserts"][0]["id"]
    assert client.post(f"/api/replay/sessions/{session_id}/step").status_code == 200
    # Close at the latest candle's reveal: the exit timestamp (17:02) is one
    # minute ahead of that candle's chart key (17:01).
    closed = client.post(f"/api/replay/sessions/{session_id}/close-all")
    assert closed.status_code == 200, closed.text
    state = client.get(f"/api/replay/sessions/{session_id}/state").json()
    assert closed.json()["newly_closed_trades"][0]["exit_time"] == state["current_market_time"]
    assert state["current_market_time"] == "2026-01-02T17:02:00+00:00"

    response = client.get(f"/api/replay/sessions/{session_id}/trades/{trade_id}/chart-history",
                          params={"context_bars": 5})
    assert response.status_code == 200, response.text
    window = response.json()
    bars = window["displayed_bars"]
    first, last = parse(bars[0]["timestamp"]), parse(bars[-1]["timestamp"])
    # The window ends at the revealed candle's open, not the exit timestamp,
    # and no unrevealed candle (17:02 or later) is returned.
    assert last <= parse(state["current_candle_time"])
    # Both of the trade's own fill anchors sit inside the returned window.
    assert len(window["fills"]) == 2
    for fill in window["fills"]:
        assert first <= parse(fill["source_candle_time"]) <= last


def test_chart_history_does_not_materialize_the_full_range(client, monkeypatch):
    """The focus endpoint reads through the paged RangeBars view; the
    materialized full-range read (load_bars_range) is no longer on the path."""
    def boom(*args, **kwargs):
        raise AssertionError("chart history must not materialize the full range")

    monkeypatch.setattr("app.service.load_bars_range", boom)
    session_id = create_session(client)["id"]
    assert client.post(f"/api/replay/sessions/{session_id}/step").status_code == 200
    opened = client.post(f"/api/replay/sessions/{session_id}/orders/market",
                         json={"direction": "long", "quantity": 1})
    assert opened.status_code == 200, opened.text
    trade_id = opened.json()["trade_upserts"][0]["id"]
    assert client.post(f"/api/replay/sessions/{session_id}/close-all").status_code == 200
    response = client.get(f"/api/replay/sessions/{session_id}/trades/{trade_id}/chart-history")
    assert response.status_code == 200, response.text
    assert response.json()["displayed_bars"]


def test_very_long_trade_focus_window_is_truncated(client, tmp_path):
    """A trade whose span exceeds CHART_FOCUS_MAX_BARS gets the first
    CHART_FOCUS_MAX_BARS bars from its entry, flagged truncated: the rest of
    the trade lies beyond the window's right edge."""
    from app import service

    start = datetime(2026, 3, 2, 0, 0, tzinfo=timezone.utc)
    lines = ["Date,Time,Open,High,Low,Close,TickVolume,Volume,Spread"]
    for i in range(2600):
        ts = start + timedelta(minutes=i)
        price = 1.1 + (i % 97) * 0.0001
        lines.append(f"{ts.year:04d}.{ts.month:02d}.{ts.day:02d},{ts.hour:02d}:{ts.minute:02d}:00,"
                     f"{price:.4f},{price + 0.0004:.4f},{price - 0.0004:.4f},{price + 0.0001:.4f},10,0,0")
    csv_path = tmp_path / "long.csv"
    csv_path.write_text("\n".join(lines) + "\n")
    imported = client.post("/api/imports", json={
        "path": str(csv_path), "symbol": "LONGT", "asset_class": "forex",
        "pnl_currency": "USD", "price_precision": 4, "contract_multiplier": 1,
        "default_profile": "utc_aligned",
    })
    assert imported.status_code == 200, imported.text
    session = client.post("/api/replay/sessions", json={
        "symbol": "LONGT", "start": "2026-03-02T00:00:00Z", "end": "2026-03-03T19:20:00Z",
        "chart_context_1m_bars": 500, "advance_step_minutes": 50,
    })
    assert session.status_code == 200, session.text
    session_id = session.json()["id"]
    assert client.post(f"/api/replay/sessions/{session_id}/step").status_code == 200
    opened = client.post(f"/api/replay/sessions/{session_id}/orders/market",
                         json={"direction": "long", "quantity": 1})
    assert opened.status_code == 200, opened.text
    trade_id = opened.json()["trade_upserts"][0]["id"]
    for _ in range(42):
        assert client.post(f"/api/replay/sessions/{session_id}/step").status_code == 200
    closed = client.post(f"/api/replay/sessions/{session_id}/close-all")
    assert closed.status_code == 200, closed.text

    response = client.get(f"/api/replay/sessions/{session_id}/trades/{trade_id}/chart-history",
                          params={"context_bars": 500})
    assert response.status_code == 200, response.text
    window = response.json()
    bars = window["displayed_bars"]
    assert window["truncated"] is True
    assert len(bars) == service.CHART_FOCUS_MAX_BARS
    entry_anchor = parse(window["trade"]["entry_source_candle_time"])
    first, last = parse(bars[0]["timestamp"]), parse(bars[-1]["timestamp"])
    assert first == entry_anchor
    assert last == entry_anchor + timedelta(minutes=service.CHART_FOCUS_MAX_BARS - 1)
    # The trade's exit lies beyond the window's right edge.
    assert parse(window["trade"]["exit_time"]) > last


# ---------------------------------------------------------------------------
# Mutation deltas at the response caps
# ---------------------------------------------------------------------------

def test_entry_fill_at_the_response_cap_still_reaches_the_client(client):
    """A fill appended by a mutation while the session is exactly at the fill
    response cap must still reach the client: save_session prunes the
    in-memory working set to the cap in the same commit that persists the
    fill, so a delta derived from the post-save state sees an empty tail."""
    from app import service

    session_id = create_session(client)["id"]
    seed_closed_trades(client, session_id, service.MAX_RESPONSE_FILLS // 2)
    state = client.get(f"/api/replay/sessions/{session_id}/state").json()
    assert state["fills_total"] == service.MAX_RESPONSE_FILLS
    opened = client.post(f"/api/replay/sessions/{session_id}/orders/market",
                         json={"direction": "long", "quantity": 1})
    assert opened.status_code == 200, opened.text
    body = opened.json()
    assert body["fills_total"] == service.MAX_RESPONSE_FILLS + 1
    assert body["new_fills"], "the cap-boundary fill must be delivered in the delta"
    assert body["new_fills"][0]["trade_id"] == body["trade_upserts"][0]["id"]


def test_close_all_past_the_closed_cap_delivers_every_new_close(client):
    """A close-all that pushes the closed-trade window past its cap must
    report every trade that just closed: at the cap, the prune drops the
    oldest closed row in the same commit, so a delta derived from the
    post-save state loses exactly the rows that just closed."""
    from app import service

    session_id = create_session(client)["id"]
    assert client.post(f"/api/replay/sessions/{session_id}/step").status_code == 200
    for _ in range(service.MAX_RESPONSE_CLOSED_TRADES + 1):
        opened = client.post(f"/api/replay/sessions/{session_id}/orders/market",
                             json={"direction": "long", "quantity": 1})
        assert opened.status_code == 200, opened.text
    closed = client.post(f"/api/replay/sessions/{session_id}/close-all")
    assert closed.status_code == 200, closed.text
    body = closed.json()
    assert body["closed_trades_total"] == service.MAX_RESPONSE_CLOSED_TRADES + 1
    assert len(body["newly_closed_trades"]) == service.MAX_RESPONSE_CLOSED_TRADES + 1
    assert len(body["trade_removals_from_open"]) == service.MAX_RESPONSE_CLOSED_TRADES + 1
    assert len({trade["id"] for trade in body["newly_closed_trades"]}) == service.MAX_RESPONSE_CLOSED_TRADES + 1


def test_trade_fills_are_read_by_chart_anchor_window(client):
    """get_trade_fills bounds its read by the indexed chart anchor, so a trade
    with partial exits spread over a long span returns only the fills the
    requested chart window can show (the unbounded-ledger case)."""
    base = datetime(2026, 1, 2, 17, 0, tzinfo=timezone.utc)

    session_id = create_session(client)["id"]
    assert client.post(f"/api/replay/sessions/{session_id}/step").status_code == 200
    opened = client.post(f"/api/replay/sessions/{session_id}/orders/market",
                         json={"direction": "long", "quantity": 1})
    assert opened.status_code == 200, opened.text
    trade_id = opened.json()["trade_upserts"][0]["id"]
    for _ in range(3):
        assert client.post(f"/api/replay/sessions/{session_id}/step").status_code == 200
    assert client.post(f"/api/trades/{trade_id}/close",
                       json={"session_id": session_id, "quantity": 0.5}).status_code == 200
    for _ in range(3):
        assert client.post(f"/api/replay/sessions/{session_id}/step").status_code == 200
    assert client.post(f"/api/replay/sessions/{session_id}/close-all").status_code == 200

    all_fills = repository.get_trade_fills(session_id, trade_id)
    assert len(all_fills) == 3
    anchors = [fill.source_candle_time for fill in all_fills]
    assert anchors == [base, base + timedelta(minutes=3), base + timedelta(minutes=6)]

    # A window covering only the entry anchor.
    early = repository.get_trade_fills(session_id, trade_id, base, base + timedelta(minutes=2))
    assert [fill.id for fill in early] == [all_fills[0].id]
    # A window covering only the two exit anchors' neighbourhood.
    late = repository.get_trade_fills(session_id, trade_id, base + timedelta(minutes=2), base + timedelta(minutes=7))
    assert [fill.id for fill in late] == [all_fills[1].id, all_fills[2].id]
