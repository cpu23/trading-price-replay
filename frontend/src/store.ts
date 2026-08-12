import { create } from "zustand";
import { api, errorMessage } from "./api";
import type { ReplayState, SessionSummary, SymbolMetadata } from "./types";

export const SESSION_STORAGE_KEY = "price-replay-session-id";

type ReplayStore = {
  symbols: SymbolMetadata[];
  sessions: SessionSummary[];
  replay: ReplayState | null;
  busy: boolean;
  restoring: boolean;
  symbolsLoading: boolean;
  sessionsLoading: boolean;
  error: string | null;
  playing: boolean;
  loadSymbols: () => Promise<void>;
  loadSessions: () => Promise<void>;
  restoreSavedSession: () => Promise<void>;
  action: (call: () => Promise<ReplayState>) => Promise<void>;
  leave: () => void;
  setPlaying: (value: boolean) => void;
  clearError: () => void;
};

let actionGeneration = 0;

export const useReplayStore = create<ReplayStore>((set, get) => ({
  symbols: [],
  sessions: [],
  replay: null,
  busy: false,
  restoring: true,
  symbolsLoading: false,
  sessionsLoading: false,
  error: null,
  playing: false,

  loadSymbols: async () => {
    if (get().symbolsLoading) return;
    set({ symbolsLoading: true });
    try {
      const symbols = await api.get<SymbolMetadata[]>("/api/symbols");
      set({ symbols });
    } catch (error) {
      set({ error: `Could not load instruments: ${errorMessage(error)}` });
    } finally {
      set({ symbolsLoading: false });
    }
  },

  loadSessions: async () => {
    if (get().sessionsLoading) return;
    set({ sessionsLoading: true });
    try {
      const sessions = await api.get<SessionSummary[]>("/api/replay/sessions");
      set({ sessions });
    } catch (error) {
      set({ error: `Could not load replay history: ${errorMessage(error)}` });
    } finally {
      set({ sessionsLoading: false });
    }
  },

  restoreSavedSession: async () => {
    const sessionId = localStorage.getItem(SESSION_STORAGE_KEY);
    if (!sessionId) {
      set({ restoring: false });
      return;
    }
    try {
      const replay = await api.get<ReplayState>(`/api/replay/sessions/${sessionId}/state`);
      set({ replay, restoring: false, playing: false, error: null });
    } catch (error) {
      localStorage.removeItem(SESSION_STORAGE_KEY);
      set({
        restoring: false,
        error: `The saved replay could not be resumed: ${errorMessage(error)}`,
      });
    }
  },


  action: async (call) => {
    if (get().busy) return;
    const generation = ++actionGeneration;
    set({ busy: true, error: null });
    try {
      const replay = await call();
      if (generation !== actionGeneration) return;
      localStorage.setItem(SESSION_STORAGE_KEY, replay.id);
      set({ replay, playing: replay.status === "completed" ? false : get().playing });
    } catch (error) {
      if (generation !== actionGeneration) return;
      set({ error: errorMessage(error), playing: false });
    } finally {
      if (generation === actionGeneration) set({ busy: false });
    }
  },

  leave: () => {
    actionGeneration += 1;
    localStorage.removeItem(SESSION_STORAGE_KEY);
    set({ replay: null, playing: false, busy: false, error: null });
  },

  setPlaying: (playing) => {
    const replay = get().replay;
    set({ playing: Boolean(playing && replay && replay.status !== "completed") });
  },

  clearError: () => set({ error: null }),
}));
