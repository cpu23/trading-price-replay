import { beforeEach, describe, expect, it } from "vitest";
import { SESSION_STORAGE_KEY, useReplayStore } from "../src/store";
import type { ReplayState } from "../src/types";

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

function replay(id: string): ReplayState {
  return { id, status: "active" } as ReplayState;
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

describe("replay action generations", () => {
  beforeEach(() => {
    useReplayStore.getState().leave();
    useReplayStore.setState({
      replay: null,
      busy: false,
      error: null,
      playing: false,
    });
    localStorage.clear();
  });

  it("stays on setup and keeps storage clear when a left action resolves", async () => {
    const pending = deferred<ReplayState>();
    const current = replay("left-session");
    useReplayStore.setState({ replay: current });
    localStorage.setItem(SESSION_STORAGE_KEY, current.id);

    const completion = useReplayStore.getState().action(() => pending.promise);
    expect(useReplayStore.getState().busy).toBe(true);

    useReplayStore.getState().leave();
    pending.resolve(replay("left-session"));
    await completion;

    expect(useReplayStore.getState().replay).toBeNull();
    expect(useReplayStore.getState().busy).toBe(false);
    expect(useReplayStore.getState().error).toBeNull();
    expect(localStorage.getItem(SESSION_STORAGE_KEY)).toBeNull();
  });

  it("does not let an older completion overwrite a newer resumed session", async () => {
    const older = deferred<ReplayState>();
    const newer = deferred<ReplayState>();
    const olderCompletion = useReplayStore.getState().action(() => older.promise);

    useReplayStore.getState().leave();
    const newerCompletion = useReplayStore.getState().action(() => newer.promise);
    newer.resolve(replay("newer-session"));
    await newerCompletion;

    older.resolve(replay("older-session"));
    await olderCompletion;

    expect(useReplayStore.getState().replay?.id).toBe("newer-session");
    expect(useReplayStore.getState().busy).toBe(false);
    expect(useReplayStore.getState().error).toBeNull();
    expect(localStorage.getItem(SESSION_STORAGE_KEY)).toBe("newer-session");
  });

  it("does not let a stale failure clear the busy state or set the error for a newer action", async () => {
    const older = deferred<ReplayState>();
    const newer = deferred<ReplayState>();
    const olderCompletion = useReplayStore.getState().action(() => older.promise);

    useReplayStore.getState().leave();
    const newerCompletion = useReplayStore.getState().action(() => newer.promise);
    older.reject(new Error("stale failure"));
    await olderCompletion;

    expect(useReplayStore.getState().busy).toBe(true);
    expect(useReplayStore.getState().error).toBeNull();

    newer.reject(new Error("current failure"));
    await newerCompletion;
    expect(useReplayStore.getState().busy).toBe(false);
    expect(useReplayStore.getState().error).toBe("current failure");
  });
});
