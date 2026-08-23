// Generated types live in ./api-types (regenerated from the backend's OpenAPI
// schema via `npm run generate:types`); this module only adds the names the
// components import.
import type { components } from "./api-types";

type Schemas = components["schemas"];
/** M1-resampled timeframe options. */
export type Timeframe = NonNullable<Schemas["SettingsRequest"]["visible_timeframe"]>;
export type TimeframeProfile = Schemas["ImportRequest"]["default_profile"];

export type Direction = Schemas["Trade"]["direction"];
export type TradeDirection = Schemas["Trade"]["direction"];

export type IndicatorPoint = Schemas["IndicatorPoint"];

/** The full bounded authoritative state of a session (creation, resume,
 * reconciliation). */
export type ReplaySnapshot = Schemas["ReplaySnapshot"];
/** A mutation delta that updates an installed snapshot without re-sending
 * recent history. */
export type ReplayUpdate = Schemas["ReplayUpdate"];

export type DisplayBar = Schemas["DisplayBar"];
export type Trade = Schemas["Trade"];
export type Fill = Schemas["Fill"];
export type SessionStats = Schemas["SessionStats"];
/** Session statistics exposed by the snapshot/update wire responses. */
export type ReplayStats = Schemas["SessionStats"];
/** Alias kept for components that display the session state. */
export type ReplayState = ReplaySnapshot;

export type SymbolMetadata = Schemas["SymbolMetadata"];
export type SessionSummary = Schemas["SessionSummary"];
export type ImportResponse = Schemas["ImportBatch"];
export type InspectPathResponse = Schemas["InspectPathResponse"];

/** Bounded page of a session's closed or open trade history. */
export type TradeHistoryPage = Schemas["TradeHistoryPage"];
/** Bounded page of a session's fill history. */
export type FillHistoryPage = Schemas["FillHistoryPage"];
/** Bounded historical chart window anchored around a (possibly old) trade. */
export type ChartHistoryResponse = Schemas["ChartHistoryResponse"];
/** Persisted review record returned by the review-note endpoint. */
export type ReviewRecord = Schemas["ReviewRecord"];
