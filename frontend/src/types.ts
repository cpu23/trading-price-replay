// Wire types are generated from the backend's OpenAPI contract
// (backend/openapi.json) — regenerate with `npm run generate:types`. The rest
// of the app imports these named aliases, so the FastAPI schema stays the
// single source of truth for every request/response shape.
import type { components } from "./api-types";

type Schemas = components["schemas"];

/** M1-resampling timeframes the backend accepts, from the settings request. */
export type Timeframe = NonNullable<Schemas["SettingsRequest"]["visible_timeframe"]>;
export type ReplayStatus = Schemas["StateResponse"]["status"];
export type TradeDirection = Schemas["Trade"]["direction"];

export type DisplayBar = Schemas["DisplayBar"];
export type Trade = Schemas["Trade"];
export type Fill = Schemas["Fill"];
export type ReplayStats = Schemas["SessionStats"];
export type ReplayState = Schemas["StateResponse"];
export type SymbolMetadata = Schemas["SymbolMetadata"];
export type SessionSummary = Schemas["SessionSummary"];
export type InspectPathResponse = Schemas["InspectPathResponse"];
export type ImportResponse = Schemas["ImportBatch"];
