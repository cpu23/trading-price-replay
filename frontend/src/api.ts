import createClient from "openapi-fetch";
import type { components, operations, paths } from "./api-types";


export function apiErrorDetail(body: unknown, fallback: string): string {
  if (typeof body === "string" && body.trim()) return body;
  if (!body || typeof body !== "object") return fallback;

  const detail = "detail" in body ? body.detail : body;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => {
        if (typeof item === "string") return item;
        if (item && typeof item === "object" && "msg" in item) {
          const location = "loc" in item && Array.isArray(item.loc)
            ? `${item.loc.filter((part: unknown) => part !== "body").join(" → ")}: `
            : "";
          return `${location}${String(item.msg)}`;
        }
        return "";
      })
      .filter(Boolean);
    if (messages.length) return messages.join("; ");
  }
  try {
    return JSON.stringify(detail) ?? fallback;
  } catch {
    return fallback;
  }
}

/** A failed HTTP request; `status` is the response code (0 for network
 * failures) so callers can distinguish e.g. 409 conflicts from other errors.
 */
export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

type ApiResult<T> = {
  data?: T;
  error?: unknown;
  response: Response;
};

async function execute<T>(pending: Promise<ApiResult<T>>): Promise<T> {
  let result: ApiResult<T>;
  try {
    result = await pending;
  } catch (error) {
    throw new ApiError(errorMessage(error), 0);
  }
  if (result.error !== undefined) {
    throw new ApiError(
      apiErrorDetail(
        result.error,
        result.response.statusText || `Request failed (${result.response.status})`,
      ),
      result.response.status,
    );
  }
  return result.data as T;
}

const apiBaseUrl = typeof location === "undefined" ? "http://localhost" : location.origin;
export const client = createClient<paths>({
  baseUrl: apiBaseUrl,
  fetch: (request: Request) => globalThis.fetch(request),
});

type Schemas = components["schemas"];
type TradeHistoryQuery =
  operations["session_trades_api_replay_sessions__session_id__trades_get"]["parameters"]["query"];
type FillHistoryQuery =
  operations["session_fills_api_replay_sessions__session_id__fills_get"]["parameters"]["query"];

/** Route-specific transport. Every path, parameter, body, and response is
 * checked against the generated OpenAPI contract at its declaration. */
export const api = {
  getSymbols: () => execute(client.GET("/api/symbols")),
  getSessions: () => execute(client.GET("/api/replay/sessions")),
  createSession: (body: Schemas["SessionRequest"]) => execute(
    client.POST("/api/replay/sessions", { body }),
  ),
  getSessionState: (sessionId: string) => execute(
    client.GET("/api/replay/sessions/{session_id}/state", {
      params: { path: { session_id: sessionId } },
    }),
  ),
  stepSession: (sessionId: string) => execute(
    client.POST("/api/replay/sessions/{session_id}/step", {
      params: { path: { session_id: sessionId } },
    }),
  ),
  closeAll: (sessionId: string) => execute(
    client.POST("/api/replay/sessions/{session_id}/close-all", {
      params: { path: { session_id: sessionId } },
    }),
  ),
  updateSettings: (sessionId: string, body: Schemas["SettingsRequest"]) => execute(
    client.PATCH("/api/replay/sessions/{session_id}/settings", {
      params: { path: { session_id: sessionId } },
      body,
    }),
  ),
  toggleIndicator: (sessionId: string, indicatorId: string) => execute(
    client.POST("/api/replay/sessions/{session_id}/indicators/{indicator_id}/toggle", {
      params: { path: { session_id: sessionId, indicator_id: indicatorId } },
    }),
  ),
  placeMarketOrder: (sessionId: string, body: Schemas["MarketOrderRequest"]) => execute(
    client.POST("/api/replay/sessions/{session_id}/orders/market", {
      params: { path: { session_id: sessionId } },
      body,
    }),
  ),
  closeTrade: (tradeId: string, body: Schemas["CloseRequest"]) => execute(
    client.POST("/api/trades/{trade_id}/close", {
      params: { path: { trade_id: tradeId } },
      body,
    }),
  ),
  updateTradeStop: (tradeId: string, body: Schemas["PriceRequest"]) => execute(
    client.PUT("/api/trades/{trade_id}/stop", {
      params: { path: { trade_id: tradeId } },
      body,
    }),
  ),
  updateTradeTarget: (tradeId: string, body: Schemas["PriceRequest"]) => execute(
    client.PUT("/api/trades/{trade_id}/target", {
      params: { path: { trade_id: tradeId } },
      body,
    }),
  ),
  updateTradeReview: (tradeId: string, body: Schemas["ReviewRequest"]) => execute(
    client.PATCH("/api/trades/{trade_id}/review", {
      params: { path: { trade_id: tradeId } },
      body,
    }),
  ),
  getTrades: (sessionId: string, query: TradeHistoryQuery) => execute(
    client.GET("/api/replay/sessions/{session_id}/trades", {
      params: { path: { session_id: sessionId }, query },
    }),
  ),
  getFills: (sessionId: string, query: FillHistoryQuery) => execute(
    client.GET("/api/replay/sessions/{session_id}/fills", {
      params: { path: { session_id: sessionId }, query },
    }),
  ),
  getChartHistory: (sessionId: string, tradeId: string) => execute(
    client.GET("/api/replay/sessions/{session_id}/trades/{trade_id}/chart-history", {
      params: { path: { session_id: sessionId, trade_id: tradeId } },
    }),
  ),
  deleteSession: async (sessionId: string): Promise<void> => {
    await execute(client.DELETE("/api/replay/sessions/{session_id}", {
      params: { path: { session_id: sessionId } },
    }));
  },
  inspectImportPath: (body: Schemas["PathRequest"]) => execute(
    client.POST("/api/imports/inspect-path", { body }),
  ),
  createImport: (body: Schemas["ImportRequest"]) => execute(
    client.POST("/api/imports", { body }),
  ),
};

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
