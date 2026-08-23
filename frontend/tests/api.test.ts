import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../src/api";

describe("typed API error normalization", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("preserves structured validation details and response status", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response(JSON.stringify({
      detail: [{ loc: ["body", "quantity"], msg: "must be greater than zero" }],
    }), {
      status: 422,
      statusText: "Unprocessable Entity",
      headers: { "Content-Type": "application/json" },
    }));

    const request = api.closeTrade("t1", { session_id: "s1", quantity: -1 });

    await expect(request).rejects.toEqual(
      expect.objectContaining<ApiError>({
        name: "ApiError",
        status: 422,
        message: "quantity: must be greater than zero",
      }),
    );
  });

  it("normalizes network failures with status zero", async () => {
    vi.mocked(fetch).mockRejectedValue(new TypeError("network offline"));

    const request = api.getSessionState("s1");

    await expect(request).rejects.toEqual(
      expect.objectContaining<ApiError>({
        name: "ApiError",
        status: 0,
        message: "network offline",
      }),
    );
  });
});
