import { describe, expect, it } from "vitest";
import { apiErrorDetail } from "../src/api";
import {
  REPLAY_STEP_SIZES,
  canActShortcut,
  clampFocusViewport,
  classifyChartSeriesUpdate,
  closeQuantityExceedsRemainder,
  focusWithinLiveBounds,
  formatAdaptiveNumber,
  formatDuration,
  formatExecutionTime,
  formatMetricLabel,
  formatStatistic,
  fromDateTimeLocalValue,
  historyCountLabel,
  isEditableKeyboardTarget,
  isTimestampWithinRange,
  parsePositiveQuantity,
  parseTags,
  quantityDraft,
  replayProgress,
  stepSizeOptions,
  toDateTimeLocalValue,
  utcDateTime,
  validateOrderTicket,
  validateReplayRange,
} from "../src/helpers";

describe("UTC datetime-local conversion", () => {
  it("round-trips a UTC minute without applying the browser timezone", () => {
    const iso = "2025-02-03T14:05:00.000Z";
    expect(toDateTimeLocalValue(iso)).toBe("2025-02-03T14:05");
    expect(fromDateTimeLocalValue("2025-02-03T14:05")).toBe(iso);
  });

  it("rejects malformed values and reversed or out-of-range replay windows", () => {
    expect(fromDateTimeLocalValue("02/03/2025 14:05")).toBeNull();
    expect(validateReplayRange(
      "2025-02-03T15:00",
      "2025-02-03T14:00",
      "2025-02-03T10:00:00Z",
      "2025-02-03T20:00:00Z",
    )).toBe("End time must be later than start time.");
    expect(validateReplayRange(
      "2025-02-03T09:59",
      "2025-02-03T14:00",
      "2025-02-03T10:00:00Z",
      "2025-02-03T20:00:00Z",
    )).toBe("Replay range must stay within the imported data range.");
  });
});

describe("order ticket validation", () => {
  const baseTicket = {
    quantity: "1",
    stop: "",
    target: "",
    currentPrice: 100,
    canEnter: true,
  };

  it("enforces direction-aware stop and target placement", () => {
    expect(validateOrderTicket({ ...baseTicket, direction: "long", stop: "101" }))
      .toBe("A long stop must be below the market price.");
    expect(validateOrderTicket({ ...baseTicket, direction: "long", stop: "99", target: "101" }))
      .toBeNull();
    expect(validateOrderTicket({ ...baseTicket, direction: "short", stop: "99" }))
      .toBe("A short stop must be above the market price.");
    expect(validateOrderTicket({ ...baseTicket, direction: "short", stop: "101", target: "99" }))
      .toBeNull();
  });

  it("allows finite negative prices for instruments that trade below zero", () => {
    expect(validateOrderTicket({
      ...baseTicket,
      direction: "long",
      currentPrice: -5,
      stop: "-6",
      target: "-4",
    })).toBeNull();
    expect(validateOrderTicket({
      ...baseTicket,
      direction: "short",
      currentPrice: -5,
      stop: "-4",
      target: "-6",
    })).toBeNull();
  });

  it("blocks repeated entry paths before a causal price is available", () => {
    expect(validateOrderTicket({ ...baseTicket, direction: "long", canEnter: false }))
      .toBe("A causal market price is required before placing an order.");
  });

  it("rejects empty, non-finite, and non-positive quantity drafts with actionable messages", () => {
    expect(validateOrderTicket({ ...baseTicket, direction: "long", quantity: "" }))
      .toBe("Enter a quantity greater than zero.");
    expect(validateOrderTicket({ ...baseTicket, direction: "long", quantity: "   " }))
      .toBe("Enter a quantity greater than zero.");
    expect(validateOrderTicket({ ...baseTicket, direction: "long", quantity: "." }))
      .toBe("Quantity must be a finite decimal number greater than zero.");
    expect(validateOrderTicket({ ...baseTicket, direction: "long", quantity: "abc" }))
      .toBe("Quantity must be a finite decimal number greater than zero.");
    expect(validateOrderTicket({ ...baseTicket, direction: "long", quantity: "1e999" }))
      .toBe("Quantity must be a finite decimal number greater than zero.");
    expect(validateOrderTicket({ ...baseTicket, direction: "long", quantity: "0" }))
      .toBe("Quantity must be greater than zero.");
    expect(validateOrderTicket({ ...baseTicket, direction: "long", quantity: "-2" }))
      .toBe("Quantity must be greater than zero.");
  });

  it("accepts incomplete-but-valid decimal drafts and legitimate tiny quantities", () => {
    expect(validateOrderTicket({ ...baseTicket, direction: "long", quantity: "1." })).toBeNull();
    expect(validateOrderTicket({ ...baseTicket, direction: "long", quantity: "0.00000001" })).toBeNull();
    expect(validateOrderTicket({ ...baseTicket, direction: "long", quantity: "0.0000000001" })).toBeNull();
    expect(validateOrderTicket({ ...baseTicket, direction: "long", quantity: "2.5" })).toBeNull();
  });
});

describe("quantity draft parsing", () => {
  it("parses finite positive drafts on action and rejects everything else", () => {
    expect(parsePositiveQuantity("1")).toBe(1);
    expect(parsePositiveQuantity("1.")).toBe(1);
    expect(parsePositiveQuantity("0.5")).toBe(0.5);
    expect(parsePositiveQuantity("0.00000001")).toBe(1e-8);
    expect(parsePositiveQuantity("0.")).toBeNull();
    expect(parsePositiveQuantity(".")).toBeNull();
    expect(parsePositiveQuantity("")).toBeNull();
    expect(parsePositiveQuantity("  ")).toBeNull();
    expect(parsePositiveQuantity("abc")).toBeNull();
    expect(parsePositiveQuantity("1e999")).toBeNull();
    expect(parsePositiveQuantity("-1")).toBeNull();
    expect(parsePositiveQuantity("0")).toBeNull();
  });

  it("renders persisted quantities without precision loss", () => {
    expect(quantityDraft(1)).toBe("1");
    expect(quantityDraft(0.5)).toBe("0.5");
    expect(quantityDraft(1e-8)).toBe("1e-8");
    expect(quantityDraft(5e-324)).toBe("5e-324");
    expect(quantityDraft(Number.NaN)).toBe("");
  });

  it("accepts float dust but rejects materially oversized closes", () => {
    expect(closeQuantityExceedsRemainder(0.2, 0.19999999999999998)).toBe(false);
    expect(closeQuantityExceedsRemainder(0.30000000000000004, 0.3)).toBe(false);
    expect(closeQuantityExceedsRemainder(0.3000000001, 0.3)).toBe(true);
    expect(closeQuantityExceedsRemainder(0.4, 0.3)).toBe(true);
    expect(closeQuantityExceedsRemainder(1.0, 1.0 - 5e-13)).toBe(true);
  });
});

describe("live focus bounds", () => {
  const first = "2025-02-03T14:00:00.000Z";
  const last = "2025-02-03T14:10:00.000Z";

  it("accepts a trade span inside the revealed payload", () => {
    expect(focusWithinLiveBounds("2025-02-03T14:01:00Z", "2025-02-03T14:09:00Z", first, last)).toBe(true);
    expect(focusWithinLiveBounds(first, last, first, last)).toBe(true);
  });

  it("rejects spans that slid outside the live bounds", () => {
    expect(focusWithinLiveBounds("2025-02-03T13:59:00Z", "2025-02-03T14:02:00Z", first, last)).toBe(false);
    expect(focusWithinLiveBounds("2025-02-03T14:02:00Z", "2025-02-03T14:11:00Z", first, last)).toBe(false);
  });

  it("rejects when the live payload has no bounds", () => {
    expect(focusWithinLiveBounds("2025-02-03T14:01:00Z", "2025-02-03T14:02:00Z", undefined, undefined)).toBe(false);
    expect(focusWithinLiveBounds("2025-02-03T14:01:00Z", "2025-02-03T14:02:00Z", first, undefined)).toBe(false);
  });
});

describe("focus viewport clamping", () => {
  it("clamps a truncated trade's span to the returned window's last bar", () => {
    // The trade reaches 5000s but the bounded window ends at 2000s.
    expect(clampFocusViewport(1000, 5000, { first: 1000, last: 2000 }))
      .toEqual({ from: 1000, to: 2000 });
  });

  it("clamps a window that starts after the trade's entry", () => {
    expect(clampFocusViewport(900, 1800, { first: 1000, last: 2000 }))
      .toEqual({ from: 1000, to: 1800 });
  });

  it("keeps the breathing margin inside the returned bar bounds", () => {
    // Margin wants 300s but only 200s of slack exists on the right.
    expect(clampFocusViewport(1500, 1700, { first: 1000, last: 2000 }))
      .toEqual({ from: 1200, to: 2000 });
    // Margin is fully available on both sides.
    expect(clampFocusViewport(1500, 1600, { first: 1000, last: 2000 }))
      .toEqual({ from: 1200, to: 1900 });
  });

  it("never extends past the window edges even for single-candle trades", () => {
    expect(clampFocusViewport(1000, 1000, { first: 1000, last: 2000 }))
      .toEqual({ from: 1000, to: 1000 });
    expect(clampFocusViewport(2000, 2000, { first: 1000, last: 2000 }))
      .toEqual({ from: 2000, to: 2000 });
  });

  it("shows the whole window when the trade span does not intersect it", () => {
    expect(clampFocusViewport(300, 400, { first: 1000, last: 2000 }))
      .toEqual({ from: 1000, to: 2000 });
    expect(clampFocusViewport(3000, 3100, { first: 1000, last: 2000 }))
      .toEqual({ from: 1000, to: 2000 });
  });

  it("applies the plain margin for a live zoom without server bounds", () => {
    expect(clampFocusViewport(1500, 1600, null)).toEqual({ from: 1200, to: 1900 });
  });
});

describe("step size options", () => {
  it("offers the standard list for persisted values it contains", () => {
    expect(stepSizeOptions(5)).toEqual(REPLAY_STEP_SIZES);
  });

  it("retains a valid off-list persisted step size instead of blanking the control", () => {
    expect(stepSizeOptions(7)).toEqual([1, 2, 3, 5, 7, 10, 15]);
    expect(stepSizeOptions(20)).toEqual([1, 2, 3, 5, 10, 15, 20]);
  });

  it("ignores invalid persisted values", () => {
    expect(stepSizeOptions(0)).toEqual(REPLAY_STEP_SIZES);
    expect(stepSizeOptions(-2)).toEqual(REPLAY_STEP_SIZES);
    expect(stepSizeOptions(Number.NaN)).toEqual(REPLAY_STEP_SIZES);
    expect(stepSizeOptions(2.5)).toEqual(REPLAY_STEP_SIZES);
  });
});

describe("shortcut semantics", () => {
  it("suppresses transport shortcuts while busy or once completed", () => {
    expect(canActShortcut(false, "active")).toBe(true);
    expect(canActShortcut(true, "active")).toBe(false);
    expect(canActShortcut(false, "completed")).toBe(false);
    expect(canActShortcut(true, "completed")).toBe(false);
  });

  it("leaves keystrokes alone while typing in editable controls", () => {
    expect(isEditableKeyboardTarget({ tagName: "INPUT" })).toBe(true);
    expect(isEditableKeyboardTarget({ tagName: "textarea" })).toBe(true);
    expect(isEditableKeyboardTarget({ tagName: "SELECT" })).toBe(true);
    expect(isEditableKeyboardTarget({ tagName: "BUTTON" })).toBe(true);
    expect(isEditableKeyboardTarget({ isContentEditable: true })).toBe(true);
    expect(isEditableKeyboardTarget({ tagName: "DIV" })).toBe(false);
    expect(isEditableKeyboardTarget(null)).toBe(false);
  });
});

describe("chart series update classification", () => {
  it("uses tail updates for one new bar and a changed aggregate tail", () => {
    const previous = { length: 2, first: 100, last: 200, penultimate: 100 };
    expect(classifyChartSeriesUpdate(
      previous,
      { length: 3, first: 100, last: 300, penultimate: 200 },
      true,
    )).toBe("update");
    expect(classifyChartSeriesUpdate(previous, previous, true)).toBe("update");
    expect(classifyChartSeriesUpdate(previous, previous, false)).toBe("none");
  });

  it("replaces rolling, gapped, multi-step, and cleared series", () => {
    const previous = { length: 2, first: 100, last: 200, penultimate: 100 };
    expect(classifyChartSeriesUpdate(
      previous,
      { length: 2, first: 200, last: 300, penultimate: 200 },
      true,
    )).toBe("replace");
    expect(classifyChartSeriesUpdate(
      previous,
      { length: 2, first: 100, last: 300, penultimate: 100 },
      true,
    )).toBe("replace");
    expect(classifyChartSeriesUpdate(
      previous,
      { length: 4, first: 100, last: 400, penultimate: 300 },
      true,
    )).toBe("replace");
    expect(classifyChartSeriesUpdate(
      previous,
      { length: 0, first: null, last: null, penultimate: null },
      true,
    )).toBe("replace");
    expect(classifyChartSeriesUpdate(
      { length: 0, first: null, last: null, penultimate: null },
      { length: 1, first: 100, last: 100, penultimate: null },
      true,
    )).toBe("replace");
  });
});

describe("replay progress", () => {
  it("uses revealed and remaining bars and clamps invalid values", () => {
    expect(replayProgress(-1, 10)).toBe(0);
    expect(replayProgress(4, 5)).toBe(50);
    expect(replayProgress(9, 0)).toBe(100);
    expect(replayProgress(-5, -2)).toBe(0);
  });
});

describe("adaptive numeric formatting", () => {
  it("keeps configured commissions and supported fractional quantities visible", () => {
    expect(formatAdaptiveNumber(0.004).replace(",", ".")).toBe("0.004");
    expect(formatAdaptiveNumber(0.00001).replace(",", ".")).toBe("0.00001");
    expect(formatAdaptiveNumber(0.00000001).replace(",", ".")).toBe("0.00000001");
    expect(formatAdaptiveNumber(1)).toBe("1");
  });
});

describe("statistic formatting", () => {
  it("formats counts, percentages, money, R values, and metric acronyms by meaning", () => {
    expect(formatStatistic("trades_completed", 10, "USD")).toBe("10");
    expect(formatStatistic("win_rate", 60, "USD")).toBe("60.00%");
    expect(formatStatistic("long_pnl", 4.125, "USD")).toBe("4.13 USD");
    expect(formatStatistic("total_r", -0.5, "USD")).toBe("-0.50 R");
    expect(formatStatistic("profit_factor", 1.6962, "USD")).toBe("1.70");
    expect(formatStatistic("average_r", null, "USD")).toBe("—");
    expect(formatStatistic("median_r", 0.25, "USD")).toBe("0.25 R");
    expect(formatStatistic("average_win_r", 1.2, "USD")).toBe("1.20 R");
    expect(formatStatistic("average_losing_r", -0.4, "USD")).toBe("-0.40 R");
    expect(formatStatistic("average_holding_seconds", 3725, "USD")).toBe("1h 2m 5s");
    expect(formatMetricLabel("long_pnl")).toBe("Long P&L");
    expect(formatMetricLabel("average_r")).toBe("Average R");
    expect(formatMetricLabel("average_holding_seconds")).toBe("Average holding");
    expect(formatMetricLabel("median_r")).toBe("Median R");
  });
});

describe("displayed candle timestamp bounds", () => {
  const first = "2025-02-03T14:00:00Z";
  const last = "2025-02-03T14:02:00Z";

  it("includes markers on the bounds and rejects markers outside them", () => {
    expect(isTimestampWithinRange(first, first, last)).toBe(true);
    expect(isTimestampWithinRange("2025-02-03T14:01:00Z", first, last)).toBe(true);
    expect(isTimestampWithinRange(last, first, last)).toBe(true);
    expect(isTimestampWithinRange("2025-02-03T13:59:59Z", first, last)).toBe(false);
    expect(isTimestampWithinRange("2025-02-03T14:02:01Z", first, last)).toBe(false);
  });

  it("rejects markers when candle bounds or marker timestamps are invalid", () => {
    expect(isTimestampWithinRange(first, undefined, last)).toBe(false);
    expect(isTimestampWithinRange(first, first, undefined)).toBe(false);
    expect(isTimestampWithinRange("not-a-time", first, last)).toBe(false);
  });
});

describe("history count labels", () => {
  it("reports honest totals and flags capped response history", () => {
    expect(historyCountLabel(1503, 1000, "fills")).toBe("1,503 fills · showing latest 1,000");
    expect(historyCountLabel(250, 200, "closed")).toBe("250 closed · showing latest 200");
    expect(historyCountLabel(3, 3, "closed")).toBe("3 closed");
    // Backend omits totals only for pre-truncation responses; fall back to the
    // array length so the label stays truthful.
    expect(historyCountLabel(undefined, 5, "fills")).toBe("5 fills");
  });
});

describe("API error detail", () => {
  it("turns FastAPI validation details into a readable field message", () => {
    expect(apiErrorDetail({
      detail: [{ loc: ["body", "initial_balance"], msg: "Input should be greater than 0" }],
    }, "Request failed")).toBe("initial_balance: Input should be greater than 0");
  });

  it("preserves backend detail strings", () => {
    expect(apiErrorDetail({ detail: "session is completed" }, "Request failed"))
      .toBe("session is completed");
  });
});

describe("execution time precision formatting", () => {
  it("formats exact fills with the full timestamp", () => {
    expect(formatExecutionTime({
      timestamp: "2026-01-02T17:01:00Z",
      time_precision: "exact",
      execution_window_start: "2026-01-02T17:01:00Z",
      execution_window_end: "2026-01-02T17:01:00Z",
    })).toBe("2026-01-02 17:01:00 UTC");
  });

  it("formats bar_interval fills as the known candle window", () => {
    expect(formatExecutionTime({
      timestamp: "2026-01-02T17:00:00Z",
      time_precision: "bar_interval",
      execution_window_start: "2026-01-02T17:00:00Z",
      execution_window_end: "2026-01-02T17:01:00Z",
    })).toBe("17:00-17:01 UTC");
  });

  it("includes dates when the interval crosses midnight", () => {
    expect(formatExecutionTime({
      timestamp: "2026-01-02T23:59:00Z",
      time_precision: "bar_interval",
      execution_window_start: "2026-01-02T23:59:00Z",
      execution_window_end: "2026-01-03T00:00:00Z",
    })).toBe("2026-01-02 23:59-2026-01-03 00:00 UTC");
  });

  it("shows legacy fills' recorded timestamp without claiming precision", () => {
    expect(formatExecutionTime({
      timestamp: "2026-01-02T17:00:00Z",
      time_precision: "legacy",
    })).toBe("2026-01-02 17:00:00 UTC");
  });

  it("falls back to the timestamp when an interval window is missing", () => {
    expect(formatExecutionTime({
      timestamp: "2026-01-02T17:00:00Z",
      time_precision: "bar_interval",
      execution_window_start: "2026-01-02T17:00:00Z",
    })).toBe("2026-01-02 17:00:00 UTC");
  });
});

describe("utcDateTime", () => {
  it("formats ISO timestamps in UTC regardless of the local timezone", () => {
    expect(utcDateTime("2026-01-02T17:01:05Z")).toBe("2026-01-02 17:01:05");
  });
});

describe("tag parsing", () => {
  it("splits on commas and whitespace, trims, and drops empty parts", () => {
    expect(parseTags("  momentum,  gap up ,  ")).toEqual(["momentum", "gap", "up"]);
  });

  it("de-duplicates case-insensitively while preserving first-seen order", () => {
    expect(parseTags("Momentum momentum MOMENTUM scalping")).toEqual(["Momentum", "scalping"]);
  });

  it("caps at 20 tags", () => {
    const tags = parseTags(Array.from({ length: 25 }, (_, index) => `tag${index}`).join(" "));
    expect(tags).toHaveLength(20);
    expect(tags[0]).toBe("tag0");
  });

  it("returns an empty list for blank input", () => {
    expect(parseTags("   ,  ")).toEqual([]);
  });
});

describe("duration formatting", () => {
  it("renders days, hours, minutes, and seconds compactly", () => {
    expect(formatDuration(90061)).toBe("1d 1h 1m 1s");
    expect(formatDuration(3725)).toBe("1h 2m 5s");
    expect(formatDuration(95)).toBe("1m 35s");
    expect(formatDuration(12)).toBe("12s");
  });

  it("handles zero and invalid input", () => {
    expect(formatDuration(0)).toBe("0s");
    expect(formatDuration(Number.NaN)).toBe("—");
    expect(formatDuration(-5)).toBe("—");
  });
});
