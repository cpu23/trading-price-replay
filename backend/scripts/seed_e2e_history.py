"""Test-only CLI: deterministically seed a large closed-trade history.

The frontend E2E suite needs a session whose history exceeds the response
caps (200 closed trades / 1000 fills) to exercise pagination and truncation.
Seeding it over HTTP means one mutation per trade, which is slow and heavy
in CI. This CLI replaces that loop: it loads an *already-revealed* session
from the database under ``PRICE_REPLAY_DATA_ROOT``, executes the real domain
open/close engine for every round-trip (the same fills, per-trade costs,
statistics accumulator, history totals, normalized rows, and order audit
records as the HTTP setup), then persists them through one transactional
``repository.save_session``. The deliberately test-only shortcut is one
event and revision rather than one per HTTP mutation. The browser still uses
the real API and frontend afterwards; no production endpoint is added.

Usage (from backend/, with the same data root the API server uses)::

    PRICE_REPLAY_DATA_ROOT=/tmp/root uv run python -m scripts.seed_e2e_history \
        --session-id <id>

The session must exist and be active with at least one revealed bar; unknown,
unrevealed and completed sessions are rejected with a non-zero exit code.
"""

from __future__ import annotations

import argparse
import json
import sys

from app import config, repository
from app.domain import Bar, bar_reveal_time
from app.execution import close_trade, open_trade
from app.market_data import RangeBars

# Past the 200-closed-trade and 1000-fill response caps: 501 round-trips book
# exactly 1002 fills, so every first-page cap is exceeded by the seeded data.
DEFAULT_CLOSED_TRADES = 501


class SeedError(Exception):
    """The session cannot be seeded (unknown, unrevealed, or completed)."""


def _current_bar(state) -> Bar:
    """The latest causally revealed bar of an active, revealed session."""
    if state.current_index < 0:
        raise SeedError(
            "session has no revealed causal price; step the replay once before seeding"
        )
    if state.status != "active":
        raise SeedError("session is completed; seeding requires an active replay")
    replay = RangeBars(state.symbol, state.start, state.end, state.data_version)
    if state.current_index >= len(replay):
        raise SeedError("session cursor is beyond its replay range")
    return replay[state.current_index]


def seed_history(session_id: str, closed_trades: int = DEFAULT_CLOSED_TRADES) -> dict[str, object]:
    """Append ``closed_trades`` full round-trips at the current causal price.

    Runs the real migrations first (a fresh data root must be initialized the
    same way the API server initializes it), then uses the same domain
    execution calls the service uses for market orders and manual closes:
    ``open_trade`` and ``close_trade`` at the latest revealed close, with the
    identical order audit rows. Every trade, fill, accumulator booking,
    history total and order row commits in the single
    ``repository.save_session`` call.
    """
    if closed_trades <= 0:
        raise SeedError("closed_trades must be positive")
    repository.initialize()
    state = repository.load_session(session_id)
    if state is None:
        raise SeedError(f"unknown session {session_id}")
    current_bar = _current_bar(state)
    now = bar_reveal_time(current_bar)
    price = float(current_bar.close)
    multiplier = (
        state.contract_multiplier
        if state.contract_multiplier is not None
        else float(repository.get_symbol(state.symbol)["contract_multiplier"])
    )
    orders: list[dict[str, object]] = []
    trades = []
    # Match the browser fixture's former semantics: open every position first,
    # then execute one close-all pass at the same revealed price.
    for _ in range(closed_trades):
        trade = open_trade(state, now, price, "long", 1.0, None, None, multiplier,
                           source_candle_time=current_bar.timestamp)
        trades.append(trade)
        orders.append({"trade_id": trade.id, "order_type": "market_entry",
                       "payload": {"direction": "long", "quantity": 1.0}})
    for trade in trades:
        close_trade(state, trade, now, price, trade.remaining_quantity, "manual", multiplier,
                    source_candle_time=current_bar.timestamp)
        orders.append({"trade_id": trade.id, "order_type": "close_all",
                       "payload": {"quantity": 1.0, "reason": "manual"}})
    repository.save_session(state, "e2e_history_seeded",
                            {"closed_trades": closed_trades}, orders=orders)
    return {
        "session_id": state.id,
        "closed_trades": closed_trades,
        "fills": closed_trades * 2,
        "closed_trades_total": state.closed_trades_total,
        "fills_total": state.fills_total,
        "revision": state.revision,
        "data_root": str(config.DATA_ROOT),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts.seed_e2e_history",
        description=(
            "Test-only: seed a large closed-trade history into an existing, "
            "already-revealed replay session (one repository save)."
        ),
    )
    parser.add_argument("--session-id", required=True, help="session to seed")
    parser.add_argument(
        "--closed-trades", type=int, default=DEFAULT_CLOSED_TRADES,
        help=f"full round-trips to book (default: {DEFAULT_CLOSED_TRADES})",
    )
    args = parser.parse_args(argv)
    try:
        result = seed_history(args.session_id, args.closed_trades)
    except SeedError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
