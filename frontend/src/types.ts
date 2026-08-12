export type Timeframe = "1m" | "5m" | "15m" | "1h" | "4h" | "1d";
export type ReplayStatus = "active" | "completed";
export type TradeDirection = "long" | "short";

export type DisplayBar = {
  timestamp: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  is_partial: boolean;
};

export type Trade = {
  id: string;
  direction: TradeDirection;
  initial_quantity: number;
  remaining_quantity: number;
  entry_time: string;
  entry_market_price: number;
  entry_price: number;
  stop_price: number | null;
  target_price: number | null;
  realized_pnl: number;
  status: "open" | "closed";
};

export type Fill = {
  id: string;
  trade_id: string;
  timestamp: string;
  market_price: number;
  price: number;
  quantity: number;
  reason: "entry" | "manual" | "stop" | "target" | "session_end" | string;
  gross_pnl: number;
  commission: number;
  spread_cost: number;
  slippage_cost: number;
  pnl: number;
};

export type ReplayStats = {
  gross_pnl: number;
  net_pnl: number;
  trading_costs: number;
  commission_paid: number;
  spread_cost: number;
  slippage_cost: number;
  unrealized_pnl: number;
  balance: number;
  equity: number;
  [name: string]: number;
};

export type ReplayState = {
  id: string;
  symbol: string;
  profile: string;
  account_currency: string;
  initial_balance: number;
  spread: number;
  slippage: number;
  commission_per_quantity: number;
  visible_timeframe: Timeframe;
  advance_step_minutes: number;
  current_index: number;
  current_market_time: string | null;
  current_price: number | null;
  status: ReplayStatus;
  remaining_bars: number;
  displayed_bars: DisplayBar[];
  enabled_indicators: string[];
  indicators: { sma_close_35?: { time: string; value: number }[] };
  warnings?: string[];
  trades: Trade[];
  fills: Fill[];
  stats: ReplayStats;
  // Pinned instrument display metadata: formatting must follow the session's
  // snapshot, never the current symbol row after a re-import. Null on legacy
  // sessions, which fall back to the symbol metadata.
  price_precision?: number | null;
  pnl_currency?: string | null;
  contract_multiplier?: number | null;
  // Response-history bounds: every open trade is present, only closed trades and
  // fills may be capped. Totals are the full session counts; truncation flags
  // tell the UI that the arrays show only the most recent entries.
  closed_trades_total?: number;
  fills_total?: number;
  closed_trades_truncated?: boolean;
  fills_truncated?: boolean;
};

export type SymbolMetadata = {
  symbol: string;
  asset_class: string;
  pnl_currency: string;
  price_precision: number;
  contract_multiplier: number;
  default_profile: string;
  first_timestamp: string;
  last_timestamp: string;
};

export type SessionSummary = {
  id: string;
  symbol: string;
  start: string;
  end: string;
  status: ReplayStatus;
  current_index: number;
  updated_at: string;
};

export type InspectPathResponse = {
  kind: "file" | "folder";
  files: string[];
};

export type ImportResponse = {
  id: string;
  status: string;
  rows_imported: number;
  validation?: {
    duplicates: number;
    invalid_ohlc: number;
    gap_count: number;
    source_non_monotonic: boolean;
  };
};
