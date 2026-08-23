import type { TradeDirection } from "./types";

const DATE_TIME_LOCAL_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?$/;

export function toDateTimeLocalValue(isoTimestamp: string): string {
  const date = new Date(isoTimestamp);
  if (Number.isNaN(date.getTime())) return "";
  return date.toISOString().slice(0, 16);
}

export function fromDateTimeLocalValue(value: string): string | null {
  if (!DATE_TIME_LOCAL_PATTERN.test(value)) return null;
  const date = new Date(`${value}${value.length === 16 ? ":00" : ""}Z`);
  if (Number.isNaN(date.getTime())) return null;
  return date.toISOString();
}

export function validateReplayRange(
  startLocal: string,
  endLocal: string,
  availableStart: string,
  availableEnd: string,
): string | null {
  const start = fromDateTimeLocalValue(startLocal);
  const end = fromDateTimeLocalValue(endLocal);
  if (!start || !end) return "Enter a valid start and end time.";

  const startTime = Date.parse(start);
  const endTime = Date.parse(end);
  if (startTime >= endTime) return "End time must be later than start time.";
  if (startTime < Date.parse(availableStart) || endTime > Date.parse(availableEnd)) {
    return "Replay range must stay within the imported data range.";
  }
  return null;
}

/** The quantity controls are string-backed so empty and incomplete decimal
 * drafts (`""`, `.`, `0.`, `1.`) survive editing; the draft is parsed only
 * when the user acts on it. */
const DECIMAL_NUMBER = /^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/;

export function parsePositiveQuantity(raw: string): number | null {
  const trimmed = raw.trim();
  if (!DECIMAL_NUMBER.test(trimmed)) return null;
  const parsed = Number(trimmed);
  if (!Number.isFinite(parsed) || parsed <= 0) return null;
  return parsed;
}

/** Render a persisted finite quantity without grouping or precision loss. */
export function quantityDraft(value: number): string {
  return Number.isFinite(value) ? String(value) : "";
}

/** Mirror the backend's ULP-scaled oversize distinction for immediate ticket
 * feedback. The server remains authoritative and canonicalizes the result. */
export function closeQuantityExceedsRemainder(requested: number, remaining: number): boolean {
  const scale = Math.max(Math.abs(requested), Math.abs(remaining), Number.MIN_VALUE);
  const tolerance = 256 * Number.EPSILON * scale;
  return requested - remaining > tolerance;
}

export type OrderTicketValues = {
  direction: TradeDirection;
  quantity: string;
  stop: string;
  target: string;
  currentPrice: number | null;
  canEnter: boolean;
};

export function validateOrderTicket(values: OrderTicketValues): string | null {
  if (!values.canEnter || values.currentPrice === null || !Number.isFinite(values.currentPrice)) {
    return "A causal market price is required before placing an order.";
  }
  const quantityText = values.quantity.trim();
  if (quantityText === "") {
    return "Enter a quantity greater than zero.";
  }
  const quantity = parsePositiveQuantity(quantityText);
  if (quantity === null) {
    if (DECIMAL_NUMBER.test(quantityText) && Number(quantityText) <= 0) {
      return "Quantity must be greater than zero.";
    }
    return "Quantity must be a finite decimal number greater than zero.";
  }

  const stop = values.stop.trim() === "" ? null : Number(values.stop);
  const target = values.target.trim() === "" ? null : Number(values.target);
  if (stop !== null && !Number.isFinite(stop)) return "Stop must be a finite price.";
  if (target !== null && !Number.isFinite(target)) return "Target must be a finite price.";

  if (values.direction === "long") {
    if (stop !== null && stop >= values.currentPrice) return "A long stop must be below the market price.";
    if (target !== null && target <= values.currentPrice) return "A long target must be above the market price.";
  } else {
    if (stop !== null && stop <= values.currentPrice) return "A short stop must be above the market price.";
    if (target !== null && target >= values.currentPrice) return "A short target must be below the market price.";
  }
  return null;
}

export function replayProgress(currentIndex: number, remainingBars: number): number {
  const revealed = Math.max(0, currentIndex + 1);
  const total = revealed + Math.max(0, remainingBars);
  return total === 0 ? 0 : Math.min(100, Math.max(0, (revealed / total) * 100));
}

export function historyCountLabel(total: number | undefined, shown: number, unit: string): string {
  const count = total ?? shown;
  const label = `${count.toLocaleString()} ${unit}`;
  if (total !== undefined && total > shown) {
    return `${label} · showing latest ${shown.toLocaleString()}`;
  }
  return label;
}

export function formatPrice(value: number | null | undefined, precision: number): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return value.toLocaleString(undefined, {
    minimumFractionDigits: precision,
    maximumFractionDigits: precision,
  });
}

export function formatNumber(value: number | null | undefined, fractionDigits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return value.toLocaleString(undefined, {
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  });
}

export function formatAdaptiveNumber(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return value.toLocaleString(undefined, {
    minimumFractionDigits: 0,
    maximumFractionDigits: 8,
  });
}

export function isTimestampWithinRange(
  timestamp: string,
  firstTimestamp: string | undefined,
  lastTimestamp: string | undefined,
): boolean {
  if (!firstTimestamp || !lastTimestamp) return false;
  const value = Date.parse(timestamp);
  const first = Date.parse(firstTimestamp);
  const last = Date.parse(lastTimestamp);
  return Number.isFinite(value)
    && Number.isFinite(first)
    && Number.isFinite(last)
    && first <= value
    && value <= last;
}

/** Whether a focused trade's entry-to-exit span still lies inside the live
 * chart payload's revealed bar bounds. A live-window focus needs no server
 * fetch while this holds; once the replay slides the trade out, a bounded
 * window must be fetched instead. */
export function focusWithinLiveBounds(
  fromTimestamp: string,
  toTimestamp: string,
  firstBarTimestamp: string | undefined,
  lastBarTimestamp: string | undefined,
): boolean {
  if (!firstBarTimestamp || !lastBarTimestamp) return false;
  return Date.parse(fromTimestamp) >= Date.parse(firstBarTimestamp)
    && Date.parse(toTimestamp) <= Date.parse(lastBarTimestamp);
}

/** Chart bar time bounds in epoch seconds. */
export type BarTimeBounds = { first: number; last: number };

/** Derive the visible range for a focused trade. With a bounded server
 * window the viewport is clamped to the returned bar bounds — the trade's
 * full span may reach beyond the window (truncated trades, causal clamps),
 * and the viewport must never show unavailable bars. Without bounds (a live
 * zoom) the trade span plus a margin is used as before. */
export function clampFocusViewport(
  tradeFrom: number,
  tradeTo: number,
  barBounds: BarTimeBounds | null,
): { from: number; to: number } {
  let from = tradeFrom;
  let to = tradeTo;
  if (barBounds) {
    from = Math.max(from, barBounds.first);
    to = Math.min(to, barBounds.last);
    if (to < from) {
      // The trade's span does not intersect the returned window at all:
      // show the window itself instead of an empty viewport.
      return { from: barBounds.first, to: barBounds.last };
    }
  }
  const span = Math.max(to - from, 60);
  let margin = Math.max(span * 0.1, 300);
  if (barBounds) {
    margin = Math.max(Math.min(margin, from - barBounds.first, barBounds.last - to), 0);
    return {
      from: Math.max(from - margin, barBounds.first),
      to: Math.min(to + margin, barBounds.last),
    };
  }
  return { from: from - margin, to: to + margin };
}

/** Standard step-size choices offered by the step-size control. */
export const REPLAY_STEP_SIZES = [1, 2, 3, 5, 10, 15];

/** The step-size select options. A persisted step size outside the standard
 * list (e.g. an older session) is kept selectable instead of rendering a
 * blank control. */
export function stepSizeOptions(persistedStepMinutes: number): number[] {
  if (Number.isInteger(persistedStepMinutes) && persistedStepMinutes > 0
    && !REPLAY_STEP_SIZES.includes(persistedStepMinutes)) {
    return [...REPLAY_STEP_SIZES, persistedStepMinutes].sort((left, right) => left - right);
  }
  return REPLAY_STEP_SIZES;
}

export type ChartSeriesUpdate = "none" | "update" | "replace";
export type ChartSeriesBoundary = {
  length: number;
  first: number | null;
  last: number | null;
  penultimate: number | null;
};

/** Classify the minimum exactly-equivalent Lightweight Charts series change.
 * Revealed bars are immutable except for the current aggregate tail. */
export function classifyChartSeriesUpdate(
  previous: ChartSeriesBoundary,
  next: ChartSeriesBoundary,
  tailChanged: boolean,
): ChartSeriesUpdate {
  if (next.length === 0) return previous.length === 0 ? "none" : "replace";
  if (previous.length === 0 || previous.first === null || previous.last === null) return "replace";
  if (next.first !== previous.first) return "replace";
  if (next.length === previous.length) {
    if (next.last !== previous.last) return "replace";
    return tailChanged ? "update" : "none";
  }
  if (next.length === previous.length + 1 && next.penultimate === previous.last) return "update";
  return "replace";
}

/** Transport shortcuts are suppressed while a mutation is busy and once the
 * replay is completed; Space also always consumes the key so the page does
 * not scroll. */
export function canActShortcut(busy: boolean, status: string): boolean {
  return !busy && status !== "completed";
}

/** A keydown whose target is an editable control is left alone so typing in
 * quantity/price fields never triggers a replay shortcut. */
export function isEditableKeyboardTarget(
  target: { isContentEditable?: boolean; tagName?: string } | null,
): boolean {
  if (!target) return false;
  if (target.isContentEditable) return true;
  const tag = target.tagName?.toUpperCase();
  return tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA" || tag === "BUTTON";
}

const INTEGER_STATISTICS: Record<string, true> = {
  trades_opened: true,
  trades_completed: true,
};
const CURRENCY_STATISTICS: Record<string, true> = {
  balance: true,
  equity: true,
  net_pnl: true,
  gross_pnl: true,
  unrealized_pnl: true,
  trading_costs: true,
  commission_paid: true,
  spread_cost: true,
  slippage_cost: true,
  average_win: true,
  average_loss: true,
  long_pnl: true,
  short_pnl: true,
  max_drawdown: true,
};
const R_STATISTICS: Record<string, true> = {
  total_r: true,
  average_r: true,
  median_r: true,
  average_win_r: true,
  average_losing_r: true,
};

const METRIC_LABELS: Record<string, string> = {
  balance: "Balance",
  equity: "Equity",
  net_pnl: "Net P&L",
  gross_pnl: "Gross P&L",
  unrealized_pnl: "Unrealized P&L",
  trading_costs: "Trading costs",
  commission_paid: "Commission",
  spread_cost: "Spread cost",
  slippage_cost: "Slippage cost",
  trades_opened: "Trades opened",
  trades_completed: "Trades completed",
  win_rate: "Win rate",
  profit_factor: "Profit factor",
  total_r: "Total R",
  average_r: "Average R",
  median_r: "Median R",
  average_win_r: "Average win R",
  average_losing_r: "Average losing R",
  average_holding_seconds: "Average holding",
  average_win: "Average win",
  average_loss: "Average loss",
  long_pnl: "Long P&L",
  short_pnl: "Short P&L",
  max_drawdown: "Max drawdown",
};

export function formatMetricLabel(name: string): string {
  const known = METRIC_LABELS[name];
  if (known) return known;
  return name
    .split("_")
    .map((word, index) => {
      if (word === "pnl") return "P&L";
      if (word === "r") return "R";
      return index === 0 ? `${word.charAt(0).toUpperCase()}${word.slice(1)}` : word;
    })
    .join(" ");
}

export function formatDuration(totalSeconds: number): string {
  if (!Number.isFinite(totalSeconds) || totalSeconds < 0) return "—";
  const seconds = Math.round(totalSeconds);
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const secs = seconds % 60;
  const parts: string[] = [];
  if (days) parts.push(`${days}d`);
  if (hours) parts.push(`${hours}h`);
  if (minutes) parts.push(`${minutes}m`);
  if (secs || parts.length === 0) parts.push(`${secs}s`);
  return parts.join(" ");
}

export function formatStatistic(name: string, value: number | null, currency: string): string {
  if (value === null || !Number.isFinite(value)) return "—";
  if (INTEGER_STATISTICS[name]) return formatNumber(value, 0);
  if (name === "win_rate") return `${formatNumber(value)}%`;
  if (name === "average_holding_seconds") return formatDuration(value);
  if (CURRENCY_STATISTICS[name]) return `${formatNumber(value)} ${currency}`;
  if (R_STATISTICS[name]) return `${formatNumber(value)} R`;
  return formatNumber(value);
}

export type ExecutionPrecision = "exact" | "bar_interval" | "legacy";

export type ExecutionTimestamp = {
  timestamp: string;
  time_precision?: ExecutionPrecision | null;
  execution_window_start?: string | null;
  execution_window_end?: string | null;
};

const pad2 = (value: number): string => String(value).padStart(2, "0");

/** "YYYY-MM-DD HH:MM:SS" in UTC, parsed from an ISO timestamp. */
export function utcDateTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return `${date.getUTCFullYear()}-${pad2(date.getUTCMonth() + 1)}-${pad2(date.getUTCDate())} `
    + `${pad2(date.getUTCHours())}:${pad2(date.getUTCMinutes())}:${pad2(date.getUTCSeconds())}`;
}

/** "HH:MM" in UTC, parsed from an ISO timestamp. */
export const utcClock = (iso: string): string => {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return `${pad2(date.getUTCHours())}:${pad2(date.getUTCMinutes())}`;
};

const utcDate = (date: Date): string =>
  `${date.getUTCFullYear()}-${pad2(date.getUTCMonth() + 1)}-${pad2(date.getUTCDate())}`;

/**
 * Format an execution timestamp by its time precision:
 * - exact: the true execution time, "2026-01-02 17:01:00 UTC";
 * - bar_interval: only the M1 candle interval is known, "17:00-17:01 UTC"
 *   (dates included when the interval crosses midnight);
 * - legacy / unknown: the recorded timestamp, without claiming precision.
 */
export function formatExecutionTime(fill: ExecutionTimestamp): string {
  if (fill.time_precision === "bar_interval" && fill.execution_window_start && fill.execution_window_end) {
    const start = new Date(fill.execution_window_start);
    const end = new Date(fill.execution_window_end);
    if (!Number.isNaN(start.getTime()) && !Number.isNaN(end.getTime())) {
      if (Math.floor(start.getTime() / 86400000) === Math.floor(end.getTime() / 86400000)) {
        return `${utcClock(fill.execution_window_start)}-${utcClock(fill.execution_window_end)} UTC`;
      }
      return `${utcDate(start)} ${utcClock(fill.execution_window_start)}-${utcDate(end)} ${utcClock(fill.execution_window_end)} UTC`;
    }
  }
  return `${utcDateTime(fill.timestamp)} UTC`;
}

/**
 * Parse a raw tag input: split on commas/whitespace, trim, drop empties,
 * de-duplicate case-insensitively while preserving first-seen order and case,
 * and cap at 20 tags (the backend enforces the same bound).
 */
export function parseTags(raw: string): string[] {
  const seen = new Set<string>();
  const tags: string[] = [];
  for (const part of raw.split(/[\s,]+/)) {
    const tag = part.trim();
    if (!tag) continue;
    const key = tag.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    tags.push(tag);
  }
  return tags.slice(0, 20);
}
