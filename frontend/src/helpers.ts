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

export type OrderTicketValues = {
  direction: TradeDirection;
  quantity: number;
  stop: string;
  target: string;
  currentPrice: number | null;
  canEnter: boolean;
};

export function validateOrderTicket(values: OrderTicketValues): string | null {
  if (!values.canEnter || values.currentPrice === null || !Number.isFinite(values.currentPrice)) {
    return "A causal market price is required before placing an order.";
  }
  if (!Number.isFinite(values.quantity) || values.quantity <= 0) {
    return "Quantity must be greater than zero.";
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

const INTEGER_STATISTICS: Record<string, true> = {
  trades_opened: true,
  trades_completed: true,
};
const CURRENCY_STATISTICS: Record<string, true> = {
  average_win: true,
  average_loss: true,
  long_pnl: true,
  short_pnl: true,
  max_drawdown: true,
};
const R_STATISTICS: Record<string, true> = {
  total_r: true,
  average_r: true,
};

export function formatMetricLabel(name: string): string {
  return name
    .split("_")
    .map((word, index) => {
      if (word === "pnl") return "P&L";
      if (word === "r") return "R";
      return index === 0 ? `${word.charAt(0).toUpperCase()}${word.slice(1)}` : word;
    })
    .join(" ");
}

export function formatStatistic(name: string, value: number, currency: string): string {
  if (INTEGER_STATISTICS[name]) return formatNumber(value, 0);
  if (name === "win_rate") return `${formatNumber(value)}%`;
  if (CURRENCY_STATISTICS[name]) return `${formatNumber(value)} ${currency}`;
  if (R_STATISTICS[name]) return `${formatNumber(value)} R`;
  return formatNumber(value);
}
