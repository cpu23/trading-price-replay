import { create } from "zustand";
import { ApiError, api, errorMessage } from "./api";
import type {
  DisplayBar,
  Fill,
  FillHistoryPage,
  IndicatorPoint,
  ReplaySnapshot,
  ReplayUpdate,
  SessionSummary,
  SymbolMetadata,
  Trade,
  TradeHistoryPage,
} from "./types";

export const SESSION_STORAGE_KEY = "price-replay-session-id";

// The backend caps snapshot history windows (most recent closed trades /
// fills); the client mirrors them so the merged live state never grows with
// session length. Older history lives in the paginated `older*` arrays.
export const RECENT_CLOSED_TRADES_LIMIT = 200;
export const RECENT_FILLS_LIMIT = 1000;

/** A bounded historical chart window fetched for focusing an old trade. */
export type ReplayWindow = {
  bars: DisplayBar[];
  indicators: Record<string, IndicatorPoint[]>;
  fills: Fill[];
  trade: Trade;
};

/** How a mutation update relates to the installed revision. */
export type UpdateVerdict = "stale" | "next" | "gap";

/** Classify an update against the installed revision. Never merges a gap. */
export function classifyUpdate(installedRevision: number, update: ReplayUpdate): UpdateVerdict {
  if (update.revision <= installedRevision) return "stale";
  if (update.revision === installedRevision + 1) return "next";
  return "gap";
}

/** Apply a mutation delta to an installed snapshot without re-sending history. */
export function mergeUpdate(replay: ReplaySnapshot, update: ReplayUpdate): ReplaySnapshot {
  // Trades: upsert changed trades by id, drop closed ones from the open set,
  // append newly closed ones to the tail of the closed window.
  const openById = new Map<string, Trade>();
  const closedById = new Map<string, Trade>();
  for (const trade of replay.trades) {
    (trade.status === "open" ? openById : closedById).set(trade.id, trade);
  }
  for (const id of update.trade_removals_from_open) openById.delete(id);
  for (const trade of update.trade_upserts) {
    (trade.status === "open" ? openById : closedById).set(trade.id, trade);
  }
  for (const trade of update.newly_closed_trades) closedById.set(trade.id, trade);
  const closedTrades = [...closedById.values()].slice(-RECENT_CLOSED_TRADES_LIMIT);

  // Fills: append new fills, de-duplicate by id, keep the recent cap. When no
  // delta arrives the original array references are preserved so memoized
  // consumers can skip re-rendering on clock-only steps.
  const fillById = new Map<string, Fill>();
  for (const fill of replay.fills) fillById.set(fill.id, fill);
  for (const fill of update.new_fills) {
    if (!fillById.has(fill.id)) fillById.set(fill.id, fill);
  }
  const fills = update.new_fills.length === 0
    ? replay.fills
    : [...fillById.values()].slice(-RECENT_FILLS_LIMIT);
  const trades = update.trade_upserts.length === 0
    && update.trade_removals_from_open.length === 0
    && update.newly_closed_trades.length === 0
    ? replay.trades
    : [...openById.values(), ...closedTrades];

  return {
    ...replay,
    status: update.status,
    revision: update.revision,
    current_index: update.current_index,
    current_market_time: update.current_market_time,
    current_candle_time: update.current_candle_time,
    current_price: update.current_price,
    remaining_bars: update.remaining_bars,
    visible_timeframe: update.visible_timeframe,
    advance_step_minutes: update.advance_step_minutes,
    enabled_indicators: update.enabled_indicators,
    displayed_bars: update.displayed_bars,
    indicators: update.indicators,
    warnings: update.warnings,
    stats: update.stats,
    trades,
    fills,
    closed_trades_total: update.closed_trades_total,
    fills_total: update.fills_total,
    closed_trades_truncated: update.closed_trades_total > closedTrades.length,
    fills_truncated: update.fills_total > fills.length,
  };
}

type ReplayStore = {
  symbols: SymbolMetadata[];
  sessions: SessionSummary[];
  replay: ReplaySnapshot | null;
  /** Revision of the installed snapshot; null until one is installed. */
  revision: number | null;
  /** Older closed-trade history loaded through the paginated endpoint,
   * newest first, in the order pages were loaded. */
  olderClosedTrades: Trade[];
  /** Older fill history loaded through the paginated endpoint, newest first. */
  olderFills: Fill[];
  /** Cursor for the next older-trade page; null when nothing older is left. */
  tradesCursor: string | null;
  /** Cursor for the next older-fill page; null when nothing older is left. */
  fillsCursor: string | null;
  historyLoading: boolean;
  busy: boolean;
  restoring: boolean;
  symbolsLoading: boolean;
  sessionsLoading: boolean;
  error: string | null;
  playing: boolean;

  loadSymbols: () => Promise<void>;
  loadSessions: () => Promise<void>;
  restoreSavedSession: () => Promise<void>;
  /** Install an authoritative snapshot (creation, resume, reconciliation). */
  installSnapshot: (snapshot: ReplaySnapshot) => void;
  /** Apply a mutation update under the revision rules; refreshes on gaps. */
  applyUpdate: (update: ReplayUpdate) => Promise<void>;
  /** Fetch a fresh snapshot from the server and install it. */
  refreshReplay: () => Promise<void>;
  loadOlderTrades: () => Promise<void>;
  loadOlderFills: () => Promise<void>;
  action: (call: () => Promise<ReplaySnapshot | ReplayUpdate>) => Promise<void>;
  leave: () => void;
  setPlaying: (value: boolean) => void;
  clearError: () => void;
};

let actionGeneration = 0;

export const useReplayStore = create<ReplayStore>((set, get) => ({
  symbols: [],
  sessions: [],
  replay: null,
  revision: null,
  olderClosedTrades: [],
  olderFills: [],
  tradesCursor: null,
  fillsCursor: null,
  historyLoading: false,
  busy: false,
  restoring: false,
  symbolsLoading: false,
  sessionsLoading: false,
  error: null,
  playing: false,

  loadSymbols: async () => {
    set({ symbolsLoading: true });
    try {
      const symbols = await api.get<SymbolMetadata[]>("/api/symbols");
      set({ symbols, symbolsLoading: false });
    } catch (error) {
      set({ symbolsLoading: false, error: `Could not load symbols: ${errorMessage(error)}` });
    }
  },

  loadSessions: async () => {
    set({ sessionsLoading: true });
    try {
      const sessions = await api.get<SessionSummary[]>("/api/replay/sessions");
      set({ sessions, sessionsLoading: false });
    } catch (error) {
      set({ sessionsLoading: false, error: `Could not load sessions: ${errorMessage(error)}` });
    }
  },

  restoreSavedSession: async () => {
    const saved = localStorage.getItem(SESSION_STORAGE_KEY);
    if (!saved) return;
    set({ restoring: true, error: null });
    try {
      const snapshot = await api.get<ReplaySnapshot>(`/api/replay/sessions/${saved}/state`);
      get().installSnapshot(snapshot);
    } catch (error) {
      if (error instanceof ApiError && (error.status === 404 || error.status === 410)) {
        localStorage.removeItem(SESSION_STORAGE_KEY);
      } else {
        set({ error: `Could not restore replay session: ${errorMessage(error)}` });
      }
    } finally {
      set({ restoring: false });
    }
  },

  installSnapshot: (snapshot) => {
    localStorage.setItem(SESSION_STORAGE_KEY, snapshot.id);
    // The snapshot window holds the most recent closed trades / fills; when
    // it is truncated, the first older page starts just past its oldest row.
    const closedInWindow = snapshot.trades.filter((trade) => trade.status === "closed");
    const tradesCursor =
      snapshot.closed_trades_truncated && closedInWindow.length > 0
        ? closedInWindow[0].id
        : null;
    const fillsCursor =
      snapshot.fills_truncated && snapshot.fills.length > 0
        ? snapshot.fills[0].id
        : null;
    set({
      replay: snapshot,
      revision: snapshot.revision,
      olderClosedTrades: [],
      olderFills: [],
      tradesCursor,
      fillsCursor,
      historyLoading: false,
      playing: snapshot.status === "completed" ? false : get().playing,
    });
  },

  applyUpdate: async (update) => {
    const installed = get().revision;
    if (installed === null) {
      // No snapshot installed; reconcile from the server instead of guessing.
      await get().refreshReplay();
      return;
    }
    const verdict = classifyUpdate(installed, update);
    if (verdict === "stale") return;
    if (verdict === "next") {
      const replay = get().replay;
      if (!replay || replay.id !== update.id) {
        await get().refreshReplay();
        return;
      }
      set({ replay: mergeUpdate(replay, update), revision: update.revision });
      return;
    }
    // Revision gap: fetch a fresh snapshot and reconcile from the server.
    await get().refreshReplay();
  },

  refreshReplay: async () => {
    const replay = get().replay;
    if (!replay) return;
    try {
      const snapshot = await api.get<ReplaySnapshot>(`/api/replay/sessions/${replay.id}/state`);
      get().installSnapshot(snapshot);
    } catch (error) {
      set({ error: `Replay reconciliation failed: ${errorMessage(error)}`, playing: false });
    }
  },

  loadOlderTrades: async () => {
    const { replay, tradesCursor, historyLoading } = get();
    if (!replay || !tradesCursor || historyLoading) return;
    set({ historyLoading: true, error: null });
    try {
      const page = await api.get<TradeHistoryPage>(
        `/api/replay/sessions/${replay.id}/trades?status=closed&limit=200&cursor=${encodeURIComponent(tradesCursor)}`,
      );
      const known = new Set([
        ...get().replay?.trades ?? [],
        ...get().olderClosedTrades,
      ].map((trade) => trade.id));
      const fresh = page.items.filter((trade) => !known.has(trade.id));
      set({
        olderClosedTrades: [...get().olderClosedTrades, ...fresh],
        tradesCursor: page.next_cursor,
      });
    } catch (error) {
      set({ error: `Could not load older trade history: ${errorMessage(error)}` });
    } finally {
      set({ historyLoading: false });
    }
  },

  loadOlderFills: async () => {
    const { replay, fillsCursor, historyLoading } = get();
    if (!replay || !fillsCursor || historyLoading) return;
    set({ historyLoading: true, error: null });
    try {
      const page = await api.get<FillHistoryPage>(
        `/api/replay/sessions/${replay.id}/fills?limit=500&cursor=${encodeURIComponent(fillsCursor)}`,
      );
      const known = new Set([
        ...get().replay?.fills ?? [],
        ...get().olderFills,
      ].map((fill) => fill.id));
      const fresh = page.items.filter((fill) => !known.has(fill.id));
      set({
        olderFills: [...get().olderFills, ...fresh],
        fillsCursor: page.next_cursor,
      });
    } catch (error) {
      set({ error: `Could not load older fill history: ${errorMessage(error)}` });
    } finally {
      set({ historyLoading: false });
    }
  },

  action: async (call) => {
    if (get().busy) return;
    const generation = ++actionGeneration;
    set({ busy: true, error: null });
    try {
      const value = await call();
      if (generation !== actionGeneration) return;
      if ("trade_upserts" in value) {
        await get().applyUpdate(value);
      } else {
        // Session creation returns the initial snapshot.
        get().installSnapshot(value);
      }
    } catch (error) {
      if (generation !== actionGeneration) return;
      if (error instanceof ApiError && error.status === 409) {
        set({ busy: false, playing: false, error: "The session changed in another tab; refreshed." });
        await get().refreshReplay();
        return;
      }
      set({ busy: false, playing: false, error: errorMessage(error) });
      return;
    }
    if (generation === actionGeneration) set({ busy: false });
  },

  leave: () => {
    actionGeneration += 1;
    localStorage.removeItem(SESSION_STORAGE_KEY);
    set({
      replay: null,
      revision: null,
      olderClosedTrades: [],
      olderFills: [],
      tradesCursor: null,
      fillsCursor: null,
      historyLoading: false,
      busy: false,
      error: null,
      playing: false,
    });
  },

  setPlaying: (value) => set({ playing: value }),
  clearError: () => set({ error: null }),
}));
