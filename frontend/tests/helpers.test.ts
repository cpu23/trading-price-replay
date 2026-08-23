import { describe, expect, it } from "vitest";
import { apiErrorDetail } from "../src/api";
import {
  formatAdaptiveNumber,
  formatDuration,
  formatExecutionTime,
  formatMetricLabel,
  formatStatistic,
  fromDateTimeLocalValue,
  historyCountLabel,
  isTimestampWithinRange,
  parseTags,
  replayProgress,
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
    quantity: 1,
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
    expect(validateOrderTicket({ ...baseTicket, direction: "long", quantity: 0 }))
      .toBe("Quantity must be greater than zero.");
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
