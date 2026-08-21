import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../src/api";
import {
  RECENT_CLOSED_TRADES_LIMIT,
  SESSION_STORAGE_KEY,
  classifyUpdate,
  mergeUpdate,
  useReplayStore,
} from "../src/store";
import type { Fill, ReplaySnapshot, ReplayUpdate, Trade } from "../src/types";

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
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
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
  vi.mocked(api.get).mockReset();
  vi.mocked(api.post).mockReset();
  vi.mocked(api.patch).mockReset();
  vi.mocked(api.put).mockReset();
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
    const merged = mergeUpdate(installed, update(2, { trade_upserts: [changed] }));
    const open = merged.trades.filter((item) => item.status === "open");
    expect(open.map((item) => item.id)).toEqual(["t-open-1", "t-open-2"]);
    expect(open[0]).toMatchObject({ id: "t-open-1", stop_price: 99 });
  });

  it("moves a trade from open to closed without losing it", () => {
    const installed = snapshot({
      trades: [trade("t1", "open"), trade("t0", "closed")],
    });
    const closedTrade = { ...trade("t1", "closed") };
    const merged = mergeUpdate(installed, update(2, {
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
    const merged = mergeUpdate(installed, update(2, {
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
    const merged = mergeUpdate(installed, update(2, {
      trade_upserts: [{ ...trade("t-new", "closed") }],
      trade_removals_from_open: ["t-new"],
      newly_closed_trades: [{ ...trade("t-new", "closed") }],
      closed_trades_total: RECENT_CLOSED_TRADES_LIMIT + 1,
    }));
    expect(merged.trades).toHaveLength(RECENT_CLOSED_TRADES_LIMIT);
    expect(merged.trades[0].id).toBe("t1"); // oldest dropped
    expect(merged.trades.at(-1)?.id).toBe("t-new");
    expect(merged.closed_trades_truncated).toBe(true);
  });

  it("keeps array references stable when the update carries no history delta", () => {
    const installed = snapshot({
      trades: [trade("t1", "open")],
      fills: [fill("f1")],
    });
    const merged = mergeUpdate(installed, update(2));
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
    expect(api.get).not.toHaveBeenCalled();
  });

  it("rejects a duplicate or stale update without touching state", async () => {
    store().installSnapshot(snapshot({ revision: 3, trades: [trade("t1", "open")] }));
    await store().applyUpdate(update(3));
    await store().applyUpdate(update(2));

    expect(store().revision).toBe(3);
    expect(store().replay?.trades[0]).toMatchObject({ id: "t1" });
    expect(api.get).not.toHaveBeenCalled();
  });

  it("fetches a fresh snapshot when the revision jumps ahead", async () => {
    store().installSnapshot(snapshot({ revision: 1 }));
    vi.mocked(api.get).mockResolvedValue(snapshot({ revision: 5, trades: [trade("t5", "open")] }));

    await store().applyUpdate(update(5));

    expect(api.get).toHaveBeenCalledWith("/api/replay/sessions/s1/state");
    expect(store().revision).toBe(5);
    expect(store().replay?.trades.map((item) => item.id)).toEqual(["t5"]);
  });
});

describe("store: 409 reconciliation", () => {
  it("stops playback, surfaces a message, and re-fetches the authoritative snapshot", async () => {
    store().installSnapshot(snapshot({ revision: 1 }));
    store().setPlaying(true);
    vi.mocked(api.get).mockResolvedValue(snapshot({ revision: 4 }));

    await store().action(async () => {
      throw new ApiError("session was modified", 409);
    });

    const state = store();
    expect(state.playing).toBe(false);
    expect(state.busy).toBe(false);
    expect(state.error).toContain("another tab");
    expect(state.revision).toBe(4);
    expect(api.get).toHaveBeenCalledWith("/api/replay/sessions/s1/state");
  });

  it("keeps the session usable after reconciliation: a fresh mutation still applies", async () => {
    store().installSnapshot(snapshot({ revision: 1 }));
    vi.mocked(api.get).mockResolvedValue(snapshot({ revision: 4, trades: [trade("t1", "open")] }));
    await store().action(async () => {
      throw new ApiError("session was modified", 409);
    });

    await store().applyUpdate(update(5, { trade_removals_from_open: ["t1"], newly_closed_trades: [{ ...trade("t1", "closed") }], closed_trades_total: 1 }));
    expect(store().revision).toBe(5);
    expect(store().replay?.trades[0].status).toBe("closed");
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

    vi.mocked(api.get).mockResolvedValue({
      items: [trade("t-in-window", "closed"), trade("t-older-1", "closed")],
      total: 3,
      next_cursor: "t-older-1",
    });
    await store().loadOlderTrades();
    expect(store().olderClosedTrades.map((item) => item.id)).toEqual(["t-older-1"]);
    expect(store().tradesCursor).toBe("t-older-1");

    vi.mocked(api.get).mockResolvedValue({
      items: [trade("t-older-1", "closed"), trade("t-oldest", "closed")],
      total: 3,
      next_cursor: null,
    });
    await store().loadOlderTrades();
    expect(store().olderClosedTrades.map((item) => item.id)).toEqual(["t-older-1", "t-oldest"]);
    expect(store().tradesCursor).toBeNull();

    // Nothing older remains: the call is a no-op and hits the network once total.
    await store().loadOlderTrades();
    expect(api.get).toHaveBeenCalledTimes(2);
  });

  it("merges older fill pages with de-duplication", async () => {
    store().installSnapshot(snapshot({
      fills: [fill("f-in-window")],
      fills_total: 2,
      fills_truncated: true,
    }));
    vi.mocked(api.get).mockResolvedValue({
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
    vi.mocked(api.get).mockResolvedValue({
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
