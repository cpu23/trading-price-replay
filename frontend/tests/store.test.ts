import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../src/api";
import {
  RECENT_CLOSED_TRADES_LIMIT,
  RECENT_FILLS_LIMIT,
  SESSION_STORAGE_KEY,
  classifyUpdate,
  mergeUpdate,
  useReplayStore,
} from "../src/store";
import type { Fill, FillHistoryPage, ReplaySnapshot, ReplayUpdate, Trade, TradeHistoryPage } from "../src/types";

vi.mock("../src/api", () => ({
  ApiError: class ApiError extends Error {
    status: number;

    constructor(message: string, status: number) {
      super(message);
      this.name = "ApiError";
      this.status = status;
    }
  },
  api: {
    getSymbols: vi.fn(),
    getSessions: vi.fn(),
    getSessionState: vi.fn(),
    getTrades: vi.fn(),
    getFills: vi.fn(),
  },
  errorMessage: (error: unknown) => (error instanceof Error ? error.message : String(error)),
}));

type Deferred<T> = {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason: unknown) => void;
};

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function trade(id: string, status: "open" | "closed"): Trade {
  return { id, session_id: "s1", status } as Trade;
}

function fill(id: string): Fill {
  return { id, trade_id: "t1", session_id: "s1" } as Fill;
}

function closedTradeRange(count: number, start = 0): Trade[] {
  return Array.from({ length: count }, (_, index) => trade(`t-${start + index}`, "closed"));
}

function fillRange(count: number, start = 0): Fill[] {
  return Array.from({ length: count }, (_, index) => fill(`f-${start + index}`));
}

function snapshot(overrides: Partial<ReplaySnapshot> = {}): ReplaySnapshot {
  return {
    id: "s1",
    revision: 1,
    status: "active",
    current_index: 0,
    current_market_time: null,
    current_candle_time: null,
    current_price: null,
    remaining_bars: 0,
    visible_timeframe: "1m",
    advance_step_minutes: 1,
    enabled_indicators: [],
    displayed_bars: [],
    indicators: {},
    warnings: [],
    stats: {},
    trades: [],
    fills: [],
    closed_trades_total: 0,
    fills_total: 0,
    closed_trades_truncated: false,
    fills_truncated: false,
    ...overrides,
  } as ReplaySnapshot;
}

function update(revision: number, overrides: Partial<ReplayUpdate> = {}): ReplayUpdate {
  return {
    id: "s1",
    revision,
    status: "active",
    current_index: revision,
    current_market_time: null,
    current_candle_time: null,
    current_price: null,
    remaining_bars: 0,
    visible_timeframe: "1m",
    advance_step_minutes: 1,
    enabled_indicators: [],
    displayed_bars: [],
    indicators: {},
    warnings: [],
    stats: {},
    trade_upserts: [],
    trade_removals_from_open: [],
    new_fills: [],
    newly_closed_trades: [],
    closed_trades_total: 0,
    fills_total: 0,
    ...overrides,
  } as ReplayUpdate;
}

function memoryStorage(): Storage {
  const values = new Map<string, string>();
  return {
    get length() {
      return values.size;
    },
    clear: () => values.clear(),
    getItem: (key) => values.get(key) ?? null,
    key: (index) => Array.from(values.keys())[index] ?? null,
    removeItem: (key) => {
      values.delete(key);
    },
    setItem: (key, value) => {
      values.set(key, value);
    },
  };
}

Object.defineProperty(globalThis, "localStorage", {
  configurable: true,
  value: memoryStorage(),
});

function store() {
  return useReplayStore.getState();
}

beforeEach(() => {
  vi.mocked(api.getSymbols).mockReset();
  vi.mocked(api.getSessions).mockReset();
  vi.mocked(api.getSessionState).mockReset();
  vi.mocked(api.getTrades).mockReset();
  vi.mocked(api.getFills).mockReset();
  store().leave();
  localStorage.clear();
});

describe("classifyUpdate", () => {
  it("rejects an update at or below the installed revision", () => {
    expect(classifyUpdate(5, update(5))).toBe("stale");
    expect(classifyUpdate(5, update(3))).toBe("stale");
  });

  it("accepts exactly the next revision", () => {
    expect(classifyUpdate(5, update(6))).toBe("next");
  });

  it("flags anything further ahead as a gap to reconcile", () => {
    expect(classifyUpdate(5, update(7))).toBe("gap");
  });
});

describe("mergeUpdate", () => {
  it("upserts a changed open trade by id without reordering the rest", () => {
    const installed = snapshot({
      trades: [trade("t-open-1", "open"), trade("t-open-2", "open")],
    });
    const changed = { ...trade("t-open-1", "open"), stop_price: 99 };
    const { replay: merged } = mergeUpdate(installed, update(2, { trade_upserts: [changed] }));
    const open = merged.trades.filter((item) => item.status === "open");
    expect(open.map((item) => item.id)).toEqual(["t-open-1", "t-open-2"]);
    expect(open[0]).toMatchObject({ id: "t-open-1", stop_price: 99 });
  });

  it("moves a trade from open to closed without losing it", () => {
    const installed = snapshot({
      trades: [trade("t1", "open"), trade("t0", "closed")],
    });
    const closedTrade = { ...trade("t1", "closed") };
    const { replay: merged } = mergeUpdate(installed, update(2, {
      trade_upserts: [closedTrade],
      trade_removals_from_open: ["t1"],
      newly_closed_trades: [closedTrade],
      closed_trades_total: 2,
    }));
    expect(merged.trades.map((item) => item.id)).toEqual(["t0", "t1"]);
    expect(merged.trades.every((item) => item.status === "closed")).toBe(true);
    expect(merged.closed_trades_total).toBe(2);
  });

  it("appends new fills and de-duplicates by id", () => {
    const installed = snapshot({ fills: [fill("f1")] });
    const { replay: merged } = mergeUpdate(installed, update(2, {
      new_fills: [fill("f1"), fill("f2")],
      fills_total: 2,
    }));
    expect(merged.fills.map((item) => item.id)).toEqual(["f1", "f2"]);
  });

  it("caps the closed window and flags truncation", () => {
    const closed = Array.from(
      { length: RECENT_CLOSED_TRADES_LIMIT },
      (_, index) => trade(`t${index}`, "closed"),
    );
    const installed = snapshot({
      trades: closed,
      closed_trades_total: RECENT_CLOSED_TRADES_LIMIT,
    });
    const { replay: merged, evictedClosedTrades } = mergeUpdate(installed, update(2, {
      trade_upserts: [{ ...trade("t-new", "closed") }],
      trade_removals_from_open: ["t-new"],
      newly_closed_trades: [{ ...trade("t-new", "closed") }],
      closed_trades_total: RECENT_CLOSED_TRADES_LIMIT + 1,
    }));
    expect(merged.trades).toHaveLength(RECENT_CLOSED_TRADES_LIMIT);
    expect(merged.trades[0].id).toBe("t1"); // oldest dropped
    expect(merged.trades.at(-1)?.id).toBe("t-new");
    expect(merged.closed_trades_truncated).toBe(true);
    expect(evictedClosedTrades.map((item) => item.id)).toEqual(["t0"]);
  });

  it("keeps array references stable when the update carries no history delta", () => {
    const installed = snapshot({
      trades: [trade("t1", "open")],
      fills: [fill("f1")],
    });
    const { replay: merged } = mergeUpdate(installed, update(2));
    expect(merged.trades).toBe(installed.trades);
    expect(merged.fills).toBe(installed.fills);
  });
});

describe("store: snapshot installation", () => {
  it("installs a full snapshot, stores the id, and derives older-history cursors", () => {
    const closed = [trade("t-old", "closed"), trade("t-newer", "closed")];
    store().installSnapshot(snapshot({
      trades: closed,
      closed_trades_total: 5,
      closed_trades_truncated: true,
      fills: [fill("f-old"), fill("f-new")],
      fills_total: 9,
      fills_truncated: true,
    }));

    const state = store();
    expect(state.revision).toBe(1);
    expect(state.replay?.id).toBe("s1");
    expect(localStorage.getItem(SESSION_STORAGE_KEY)).toBe("s1");
    // The first older page starts just past the window's oldest row.
    expect(state.tradesCursor).toBe("t-old");
    expect(state.fillsCursor).toBe("f-old");
    expect(state.olderClosedTrades).toEqual([]);
    expect(state.olderFills).toEqual([]);
  });

  it("leaves the older-history cursors empty when nothing is truncated", () => {
    store().installSnapshot(snapshot({ trades: [trade("t1", "closed")] }));
    expect(store().tradesCursor).toBeNull();
    expect(store().fillsCursor).toBeNull();
  });
});

describe("store: revision-aware updates", () => {
  it("applies a sequential update normally", async () => {
    store().installSnapshot(snapshot({ trades: [trade("t1", "open")] }));
    await store().applyUpdate(update(2, { trade_upserts: [{ ...trade("t1", "open"), stop_price: 5 }] }));

    expect(store().revision).toBe(2);
    expect(store().replay?.trades[0]).toMatchObject({ id: "t1", stop_price: 5 });
    expect(api.getSessionState).not.toHaveBeenCalled();
  });

  it("rejects a duplicate or stale update without touching state", async () => {
    store().installSnapshot(snapshot({ revision: 3, trades: [trade("t1", "open")] }));
    await store().applyUpdate(update(3));
    await store().applyUpdate(update(2));

    expect(store().revision).toBe(3);
    expect(store().replay?.trades[0]).toMatchObject({ id: "t1" });
    expect(api.getSessionState).not.toHaveBeenCalled();
  });

  it("fetches a fresh snapshot when the revision jumps ahead", async () => {
    store().installSnapshot(snapshot({ revision: 1 }));
    vi.mocked(api.getSessionState).mockResolvedValue(snapshot({ revision: 5, trades: [trade("t5", "open")] }));

    await store().applyUpdate(update(5));

    expect(api.getSessionState).toHaveBeenCalledWith("s1");
    expect(store().revision).toBe(5);
    expect(store().replay?.trades.map((item) => item.id)).toEqual(["t5"]);
  });
});

describe("store: 409 reconciliation", () => {
  it("stops playback, surfaces a message, and re-fetches the authoritative snapshot", async () => {
    store().installSnapshot(snapshot({ revision: 1 }));
    store().setPlaying(true);
    vi.mocked(api.getSessionState).mockResolvedValue(snapshot({ revision: 4 }));

    await store().action(async () => {
      throw new ApiError("session was modified", 409);
    });

    const state = store();
    expect(state.playing).toBe(false);
    expect(state.busy).toBe(false);
    expect(state.error).toContain("another tab");
    expect(state.revision).toBe(4);
    expect(api.getSessionState).toHaveBeenCalledWith("s1");
  });

  it("keeps the session usable after reconciliation: a fresh mutation still applies", async () => {
    store().installSnapshot(snapshot({ revision: 1 }));
    vi.mocked(api.getSessionState).mockResolvedValue(snapshot({ revision: 4, trades: [trade("t1", "open")] }));
    await store().action(async () => {
      throw new ApiError("session was modified", 409);
    });

    await store().applyUpdate(update(5, { trade_removals_from_open: ["t1"], newly_closed_trades: [{ ...trade("t1", "closed") }], closed_trades_total: 1 }));
    expect(store().revision).toBe(5);
    expect(store().replay?.trades[0].status).toBe("closed");
  });
});

describe("store: live recent-history rollover", () => {
  it("moves one evicted closed trade behind the recent window without older pages", async () => {
    const recent = closedTradeRange(RECENT_CLOSED_TRADES_LIMIT);
    store().installSnapshot(snapshot({
      trades: recent,
      closed_trades_total: RECENT_CLOSED_TRADES_LIMIT,
    }));
    const newest = trade(`t-${RECENT_CLOSED_TRADES_LIMIT}`, "closed");

    await store().applyUpdate(update(2, {
      trade_upserts: [newest],
      newly_closed_trades: [newest],
      closed_trades_total: RECENT_CLOSED_TRADES_LIMIT + 1,
    }));

    const state = store();
    const recentIds = state.replay?.trades.map((item) => item.id) ?? [];
    const logicalIds = [...recentIds].reverse().concat(
      state.olderClosedTrades.map((item) => item.id),
    );
    expect(recentIds).toEqual(closedTradeRange(RECENT_CLOSED_TRADES_LIMIT, 1).map((item) => item.id));
    expect(state.olderClosedTrades.map((item) => item.id)).toEqual(["t-0"]);
    expect(state.tradesCursor).toBeNull();
    expect(logicalIds).toHaveLength(RECENT_CLOSED_TRADES_LIMIT + 1);
    expect(new Set(logicalIds).size).toBe(logicalIds.length);
    expect(logicalIds).toEqual(closedTradeRange(RECENT_CLOSED_TRADES_LIMIT + 1).map((item) => item.id).reverse());
  });

  it("moves one evicted fill behind the recent window without older pages", async () => {
    const recent = fillRange(RECENT_FILLS_LIMIT);
    store().installSnapshot(snapshot({
      fills: recent,
      fills_total: RECENT_FILLS_LIMIT,
    }));
    const newest = fill(`f-${RECENT_FILLS_LIMIT}`);

    await store().applyUpdate(update(2, {
      new_fills: [newest],
      fills_total: RECENT_FILLS_LIMIT + 1,
    }));

    const state = store();
    const recentIds = state.replay?.fills.map((item) => item.id) ?? [];
    const logicalIds = [...recentIds].reverse().concat(state.olderFills.map((item) => item.id));
    expect(recentIds).toEqual(fillRange(RECENT_FILLS_LIMIT, 1).map((item) => item.id));
    expect(state.olderFills.map((item) => item.id)).toEqual(["f-0"]);
    expect(state.fillsCursor).toBeNull();
    expect(logicalIds).toHaveLength(RECENT_FILLS_LIMIT + 1);
    expect(new Set(logicalIds).size).toBe(logicalIds.length);
    expect(logicalIds).toEqual(fillRange(RECENT_FILLS_LIMIT + 1).map((item) => item.id).reverse());
  });

  it("inserts evicted closed trades before loaded older pages and preserves the cursor", async () => {
    store().installSnapshot(snapshot({
      trades: closedTradeRange(RECENT_CLOSED_TRADES_LIMIT),
      closed_trades_total: RECENT_CLOSED_TRADES_LIMIT + 2,
      closed_trades_truncated: true,
    }));
    useReplayStore.setState({
      olderClosedTrades: [trade("t-older-1", "closed"), trade("t-older-2", "closed")],
      tradesCursor: "t-older-2",
    });
    const newest = trade(`t-${RECENT_CLOSED_TRADES_LIMIT}`, "closed");

    await store().applyUpdate(update(2, {
      trade_upserts: [newest],
      newly_closed_trades: [newest],
      closed_trades_total: RECENT_CLOSED_TRADES_LIMIT + 3,
    }));

    const state = store();
    expect(state.olderClosedTrades.map((item) => item.id)).toEqual([
      "t-0",
      "t-older-1",
      "t-older-2",
    ]);
    expect(state.tradesCursor).toBe("t-older-2");
    const ids = [
      ...state.replay?.trades ?? [],
      ...state.olderClosedTrades,
    ].map((item) => item.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("inserts evicted fills before loaded older pages and preserves the cursor", async () => {
    store().installSnapshot(snapshot({
      fills: fillRange(RECENT_FILLS_LIMIT),
      fills_total: RECENT_FILLS_LIMIT + 2,
      fills_truncated: true,
    }));
    useReplayStore.setState({
      olderFills: [fill("f-older-1"), fill("f-older-2")],
      fillsCursor: "f-older-2",
    });

    await store().applyUpdate(update(2, {
      new_fills: [fill(`f-${RECENT_FILLS_LIMIT}`)],
      fills_total: RECENT_FILLS_LIMIT + 3,
    }));

    const state = store();
    expect(state.olderFills.map((item) => item.id)).toEqual([
      "f-0",
      "f-older-1",
      "f-older-2",
    ]);
    expect(state.fillsCursor).toBe("f-older-2");
    const ids = [...state.replay?.fills ?? [], ...state.olderFills].map((item) => item.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("preserves the order of multiple records evicted by one mutation", async () => {
    store().installSnapshot(snapshot({
      trades: closedTradeRange(RECENT_CLOSED_TRADES_LIMIT),
      fills: fillRange(RECENT_FILLS_LIMIT),
      closed_trades_total: RECENT_CLOSED_TRADES_LIMIT,
      fills_total: RECENT_FILLS_LIMIT,
    }));
    const newTrades = closedTradeRange(5, RECENT_CLOSED_TRADES_LIMIT);
    const newFills = fillRange(4, RECENT_FILLS_LIMIT);

    await store().applyUpdate(update(2, {
      trade_upserts: newTrades,
      newly_closed_trades: newTrades,
      new_fills: newFills,
      closed_trades_total: RECENT_CLOSED_TRADES_LIMIT + newTrades.length,
      fills_total: RECENT_FILLS_LIMIT + newFills.length,
    }));

    const state = store();
    expect(state.olderClosedTrades.map((item) => item.id)).toEqual([
      "t-4",
      "t-3",
      "t-2",
      "t-1",
      "t-0",
    ]);
    expect(state.olderFills.map((item) => item.id)).toEqual([
      "f-3",
      "f-2",
      "f-1",
      "f-0",
    ]);
    expect(state.replay?.trades.filter((item) => item.status === "closed")).toHaveLength(
      RECENT_CLOSED_TRADES_LIMIT,
    );
    expect(state.replay?.fills).toHaveLength(RECENT_FILLS_LIMIT);
  });

  it("continues trade and fill pagination from the oldest loaded boundary", async () => {
    store().installSnapshot(snapshot({
      trades: closedTradeRange(RECENT_CLOSED_TRADES_LIMIT),
      fills: fillRange(RECENT_FILLS_LIMIT),
      closed_trades_total: RECENT_CLOSED_TRADES_LIMIT + 2,
      fills_total: RECENT_FILLS_LIMIT + 2,
      closed_trades_truncated: true,
      fills_truncated: true,
    }));
    const newestTrade = trade(`t-${RECENT_CLOSED_TRADES_LIMIT}`, "closed");
    await store().applyUpdate(update(2, {
      trade_upserts: [newestTrade],
      newly_closed_trades: [newestTrade],
      new_fills: [fill(`f-${RECENT_FILLS_LIMIT}`)],
      closed_trades_total: RECENT_CLOSED_TRADES_LIMIT + 3,
      fills_total: RECENT_FILLS_LIMIT + 3,
    }));
    expect(store().tradesCursor).toBe("t-0");
    expect(store().fillsCursor).toBe("f-0");

    vi.mocked(api.getTrades).mockResolvedValue({
      items: [trade("t-0", "closed"), trade("t-older-1", "closed"), trade("t-older-2", "closed")],
      total: RECENT_CLOSED_TRADES_LIMIT + 3,
      next_cursor: null,
    });
    vi.mocked(api.getFills).mockResolvedValue({
      items: [fill("f-0"), fill("f-older-1"), fill("f-older-2")],
      total: RECENT_FILLS_LIMIT + 3,
      next_cursor: null,
    });

    await store().loadOlderTrades();
    await store().loadOlderFills();

    expect(api.getTrades).toHaveBeenCalledWith("s1", {
      status: "closed",
      limit: RECENT_CLOSED_TRADES_LIMIT,
      cursor: "t-0",
    });
    expect(api.getFills).toHaveBeenCalledWith("s1", {
      limit: 500,
      cursor: "f-0",
    });
    expect(store().olderClosedTrades.map((item) => item.id)).toEqual([
      "t-0",
      "t-older-1",
      "t-older-2",
    ]);
    expect(store().olderFills.map((item) => item.id)).toEqual([
      "f-0",
      "f-older-1",
      "f-older-2",
    ]);
    const tradeIds = [
      ...store().replay?.trades ?? [],
      ...store().olderClosedTrades,
    ].map((item) => item.id);
    const fillIds = [...store().replay?.fills ?? [], ...store().olderFills].map((item) => item.id);
    expect(tradeIds).toHaveLength(RECENT_CLOSED_TRADES_LIMIT + 3);
    expect(fillIds).toHaveLength(RECENT_FILLS_LIMIT + 3);
    expect(new Set(tradeIds).size).toBe(tradeIds.length);
    expect(new Set(fillIds).size).toBe(fillIds.length);
    expect(store().tradesCursor).toBeNull();
    expect(store().fillsCursor).toBeNull();
  });
});

describe("store: paginated older history", () => {
  it("merges older trade pages without duplicates and stops on the final cursor", async () => {
    store().installSnapshot(snapshot({
      trades: [trade("t-in-window", "closed")],
      closed_trades_total: 3,
      closed_trades_truncated: true,
    }));
    expect(store().tradesCursor).toBe("t-in-window");

    vi.mocked(api.getTrades).mockResolvedValue({
      items: [trade("t-in-window", "closed"), trade("t-older-1", "closed")],
      total: 3,
      next_cursor: "t-older-1",
    });
    await store().loadOlderTrades();
    expect(store().olderClosedTrades.map((item) => item.id)).toEqual(["t-older-1"]);
    expect(store().tradesCursor).toBe("t-older-1");

    vi.mocked(api.getTrades).mockResolvedValue({
      items: [trade("t-older-1", "closed"), trade("t-oldest", "closed")],
      total: 3,
      next_cursor: null,
    });
    await store().loadOlderTrades();
    expect(store().olderClosedTrades.map((item) => item.id)).toEqual(["t-older-1", "t-oldest"]);
    expect(store().tradesCursor).toBeNull();

    // Nothing older remains: the call is a no-op and hits the network once total.
    await store().loadOlderTrades();
    expect(api.getTrades).toHaveBeenCalledTimes(2);
  });

  it("merges older fill pages with de-duplication", async () => {
    store().installSnapshot(snapshot({
      fills: [fill("f-in-window")],
      fills_total: 2,
      fills_truncated: true,
    }));
    vi.mocked(api.getFills).mockResolvedValue({
      items: [fill("f-in-window"), fill("f-older")],
      total: 2,
      next_cursor: null,
    });
    await store().loadOlderFills();
    expect(store().olderFills.map((item) => item.id)).toEqual(["f-older"]);
    expect(store().fillsCursor).toBeNull();
  });

  it("resets older history when a new snapshot is installed", async () => {
    store().installSnapshot(snapshot({
      trades: [trade("t-in-window", "closed")],
      closed_trades_total: 3,
      closed_trades_truncated: true,
    }));
    vi.mocked(api.getTrades).mockResolvedValue({
      items: [trade("t-older", "closed")],
      total: 3,
      next_cursor: null,
    });
    await store().loadOlderTrades();
    expect(store().olderClosedTrades).toHaveLength(1);

    store().installSnapshot(snapshot({ revision: 2 }));
    expect(store().olderClosedTrades).toEqual([]);
    expect(store().tradesCursor).toBeNull();
  });

  it("does not merge an older session's trade page into a newer session", async () => {
    const pending = deferred<TradeHistoryPage>();
    store().installSnapshot(snapshot({
      id: "session-a",
      trades: [trade("a-in-window", "closed")],
      closed_trades_total: 2,
      closed_trades_truncated: true,
    }));
    vi.mocked(api.getTrades).mockReturnValue(pending.promise);

    const completion = store().loadOlderTrades();
    expect(store().historyLoading).toBe(true);
    store().installSnapshot(snapshot({
      id: "session-b",
      trades: [{ ...trade("b-current", "closed"), session_id: "session-b" }],
    }));
    pending.resolve({
      items: [{ ...trade("a-older", "closed"), session_id: "session-a" }],
      total: 2,
      next_cursor: null,
    });
    await completion;

    expect(store().replay?.id).toBe("session-b");
    expect(store().olderClosedTrades).toEqual([]);
    expect(store().historyLoading).toBe(false);
    expect(store().error).toBeNull();
  });

  it("does not surface an older session's fill-page failure in a newer session", async () => {
    const pending = deferred<FillHistoryPage>();
    store().installSnapshot(snapshot({
      id: "session-a",
      fills: [fill("a-in-window")],
      fills_total: 2,
      fills_truncated: true,
    }));
    vi.mocked(api.getFills).mockReturnValue(pending.promise);

    const completion = store().loadOlderFills();
    store().installSnapshot(snapshot({ id: "session-b" }));
    pending.reject(new Error("session A failed"));
    await completion;

    expect(store().replay?.id).toBe("session-b");
    expect(store().olderFills).toEqual([]);
    expect(store().historyLoading).toBe(false);
    expect(store().error).toBeNull();
  });
});

describe("store: action generations", () => {
  it("stays on setup and keeps storage clear when a left action resolves", async () => {
    const pending = deferred<ReplaySnapshot>();
    const current = snapshot();
    useReplayStore.setState({ replay: current });
    localStorage.setItem(SESSION_STORAGE_KEY, current.id);

    const completion = store().action(() => pending.promise);
    expect(store().busy).toBe(true);

    store().leave();
    pending.resolve(snapshot());
    await completion;

    expect(store().replay).toBeNull();
    expect(store().busy).toBe(false);
    expect(store().error).toBeNull();
    expect(localStorage.getItem(SESSION_STORAGE_KEY)).toBeNull();
  });

  it("does not let an older completion overwrite a newer resumed session", async () => {
    const older = deferred<ReplaySnapshot>();
    const newer = deferred<ReplaySnapshot>();
    const olderCompletion = store().action(() => older.promise);

    store().leave();
    const newerCompletion = store().action(() => newer.promise);
    newer.resolve(snapshot({ id: "newer-session" }));
    await newerCompletion;

    older.resolve(snapshot({ id: "older-session" }));
    await olderCompletion;

    expect(store().replay?.id).toBe("newer-session");
    expect(store().busy).toBe(false);
    expect(store().error).toBeNull();
    expect(localStorage.getItem(SESSION_STORAGE_KEY)).toBe("newer-session");
  });

  it("does not let a stale failure clear the busy state or set the error for a newer action", async () => {
    const older = deferred<ReplaySnapshot>();
    const newer = deferred<ReplaySnapshot>();
    const olderCompletion = store().action(() => older.promise);

    store().leave();
    const newerCompletion = store().action(() => newer.promise);
    older.reject(new Error("stale failure"));
    await olderCompletion;

    expect(store().busy).toBe(true);
    expect(store().error).toBeNull();

    newer.reject(new Error("current failure"));
    await newerCompletion;
    expect(store().busy).toBe(false);
    expect(store().error).toBe("current failure");
  });
});
