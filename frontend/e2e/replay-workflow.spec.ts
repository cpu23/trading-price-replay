import { fileURLToPath } from "node:url";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

const SYMBOL = "E2E";
// Set by playwright.config.ts in the same process.
const fixturePath = process.env.E2E_FIXTURE ?? "";
// Deterministic 700-bar fixture for testing historical chart focus beyond
// the live chart context.
const longFixturePath = fileURLToPath(
  new URL("../../backend/tests/fixtures/dukascopy_1m_700.csv", import.meta.url),
);

let sessionId: string;

test.beforeAll(async ({ request }) => {
  const response = await request.post("/api/imports", {
    data: { path: fixturePath, symbol: SYMBOL },
  });
  expect(response.status()).toBe(200);
  const long = await request.post("/api/imports", {
    data: { path: longFixturePath, symbol: "E2E-LONG" },
  });
  expect(long.status()).toBe(200);
});

test.beforeEach(async ({ request }) => {
  // One fresh 12-bar session per test; the fixture's candles run
  // 16:55 -> 17:06 UTC, so the first reveal lands at 16:56.
  const response = await request.post("/api/replay/sessions", {
    data: {
      symbol: SYMBOL,
      start: "2026-01-02T16:55:00Z",
      end: "2026-01-02T17:07:00Z",
    },
  });
  expect(response.status()).toBe(200);
  sessionId = (await response.json()).id as string;
});

test.afterEach(async ({ request }) => {
  await request.delete(`/api/replay/sessions/${sessionId}`);
});

async function openWorkspace(page: Page) {
  await page.goto("/");
  await page.locator(".session-list").getByRole("button", { name: "Resume" }).click();
  await page.getByRole("heading", { name: "Order ticket" }).waitFor();
}

async function seedClosedTrade(request: APIRequestContext) {
  await request.post(`/api/replay/sessions/${sessionId}/step`);
  await request.post(`/api/replay/sessions/${sessionId}/orders/market`, {
    data: { direction: "long", quantity: 1 },
  });
  await request.post(`/api/replay/sessions/${sessionId}/close-all`);
}

test("replay steps reveal causal time and execute a full trade round-trip", async ({ page }) => {
  await openWorkspace(page);

  // No causal price before the first candle is revealed.
  await expect(page.locator(".market-clock strong")).toContainText("No candle revealed");

  await page.getByRole("button", { name: "Step" }).click();

  // The candle opened at 16:55; its close only becomes causal at 16:56.
  await expect(page.locator(".market-clock strong")).toContainText("2026-01-02 16:56:00 UTC");
  await expect(page.locator(".market-clock-candle")).toContainText("16:55");
  // The revealed causal price is the candle's close (1.1002 in the fixture).
  await expect(page.locator(".ticket-price")).toContainText("1.10020");

  await page.getByLabel("Quantity").fill("1");
  await page.getByRole("button", { name: "Buy / Long" }).click();

  // Entry fill carries the exact reveal timestamp.
  const entryRow = page.getByLabel("Fill ledger").getByRole("row").filter({ hasText: "entry" });
  await expect(entryRow).toContainText("2026-01-02 16:56:00 UTC");
  await expect(page.getByLabel("Open trades").getByText("1 open")).toBeVisible();

  await page.getByRole("button", { name: "Close all positions" }).click();
  await page.getByRole("button", { name: "Confirm close all" }).click();
  await expect(page.getByText("No open trades")).toBeVisible();

  // The completed trade now has a review card.
  const review = page.getByLabel("Closed trades").locator(".trade-review").first();
  await expect(review.getByRole("heading", { name: "1 closed" })).toBeVisible();
  await expect(review.getByText("MFE (close)")).toBeVisible();
  await expect(review.getByText("MAE (close)")).toBeVisible();
});

test("persists review notes and tags across a reload", async ({ page, request }) => {
  await seedClosedTrade(request);
  await openWorkspace(page);

  const review = page.getByLabel("Closed trades").locator(".trade-review").first();
  await review.getByLabel("Review note").fill("e2e review note");
  await review.getByLabel("Tags").fill("e2e, deterministic");
  await review.getByRole("button", { name: "Save review" }).click();

  await expect(review.getByText("Saved", { exact: true })).toBeVisible();
  await expect(review.locator(".tag-chip", { hasText: "e2e" })).toBeVisible();
  await expect(review.locator(".tag-chip", { hasText: "deterministic" })).toBeVisible();

  await page.reload();
  await page.getByRole("heading", { name: "Order ticket" }).waitFor();

  const reloaded = page.getByLabel("Closed trades").locator(".trade-review").first();
  await expect(reloaded.getByLabel("Review note")).toHaveValue("e2e review note");
  await expect(reloaded.locator(".tag-chip", { hasText: "e2e" })).toBeVisible();
  await expect(reloaded.locator(".tag-chip", { hasText: "deterministic" })).toBeVisible();
});

test("focuses the chart on a closed trade and returns to the latest area", async ({ page, request }) => {
  await seedClosedTrade(request);
  await openWorkspace(page);

  const review = page.getByLabel("Closed trades").locator(".trade-review").first();
  await review.getByRole("button", { name: "Focus on chart" }).click();

  const backToLatest = page.getByRole("button", { name: "Return chart to the latest replay area" });
  await expect(backToLatest).toBeVisible();

  await backToLatest.click();
  await expect(backToLatest).toBeHidden();
});

test("loads older closed-trade and fill history, then keeps stepping live", async ({ page, request }) => {
  test.setTimeout(120_000);
  // Seed 501 closed trades (1002 fills) at the first revealed price — past
  // both first-page caps (200 closed trades / 1000 fills). Open every trade
  // first, then close them in one mutation to keep CI setup bounded and cover
  // the large close-all response path.
  await request.post(`/api/replay/sessions/${sessionId}/step`);
  for (let i = 0; i < 501; i++) {
    const opened = await request.post(`/api/replay/sessions/${sessionId}/orders/market`, {
      data: { direction: "long", quantity: 1 },
    });
    expect(opened.status()).toBe(200);
  }
  const closed = await request.post(`/api/replay/sessions/${sessionId}/close-all`);
  expect(closed.status()).toBe(200);

  await openWorkspace(page);

  const closedPanel = page.getByLabel("Closed trades");
  await expect(closedPanel).toContainText("501 closed · showing latest 200");
  await closedPanel.getByRole("button", { name: "Load older trades" }).click();
  await expect(closedPanel).toContainText("501 closed · showing latest 400");
  // 200 window + one 200-row page, de-duplicated by id: exactly 400 cards.
  await expect(closedPanel.locator(".trade-review")).toHaveCount(400);

  // One more click reaches the oldest trade and hides the button.
  await closedPanel.getByRole("button", { name: "Load older trades" }).click();
  await expect(closedPanel.getByRole("button", { name: "Load older trades" })).toBeHidden();
  await expect(closedPanel.locator(".trade-review")).toHaveCount(501);

  const fillsPanel = page.getByLabel("Fill ledger");
  await expect(fillsPanel).toContainText("1,002 fills · showing latest 1,000");
  await fillsPanel.getByRole("button", { name: "Load older fills" }).click();
  await expect(fillsPanel).toContainText("1,002 fills");
  await expect(fillsPanel.getByRole("button", { name: "Load older fills" })).toBeHidden();

  // A new live mutation still works after history was expanded.
  await page.getByRole("button", { name: "Step" }).click();
  await expect(page.locator(".market-clock strong")).toContainText("2026-01-02 16:57:00 UTC");
  await page.getByLabel("Quantity").fill("1");
  await page.getByRole("button", { name: "Buy / Long" }).click();
  await expect(page.getByLabel("Open trades").getByText("1 open")).toBeVisible();
});

test("focuses a closed trade older than the live chart window via a bounded historical window", async ({ page, request }) => {
  const created = await request.post("/api/replay/sessions", {
    data: {
      symbol: "E2E-LONG",
      start: "2026-01-02T08:00:00Z",
      end: "2026-01-02T20:00:00Z",
      advance_step_minutes: 5,
      chart_context_1m_bars: 500,
    },
  });
  expect(created.status()).toBe(200);
  const longSessionId = (await created.json()).id as string;
  try {
    // Close a trade at bar 100, then replay 525 bars past it — beyond the
    // 500-bar live chart context, so focusing must fetch a historical window.
    for (let i = 0; i < 20; i++) {
      await request.post(`/api/replay/sessions/${longSessionId}/step`);
    }
    await request.post(`/api/replay/sessions/${longSessionId}/orders/market`, {
      data: { direction: "long", quantity: 1 },
    });
    await request.post(`/api/replay/sessions/${longSessionId}/close-all`);
    for (let i = 0; i < 105; i++) {
      await request.post(`/api/replay/sessions/${longSessionId}/step`);
    }

    await page.goto("/");
    await page.locator(".session-list li", { hasText: "E2E-LONG" }).getByRole("button", { name: "Resume" }).click();
    await page.getByRole("heading", { name: "Order ticket" }).waitFor();

    const chartHistory = page.waitForResponse((res) => res.url().includes("/chart-history") && res.status() === 200);
    const review = page.getByLabel("Closed trades").locator(".trade-review").first();
    await review.getByRole("button", { name: "Focus on chart" }).click();
    await chartHistory;

    const backToLatest = page.getByRole("button", { name: "Return chart to the latest replay area" });
    await expect(backToLatest).toBeVisible();
    await backToLatest.click();
    await expect(backToLatest).toBeHidden();
  } finally {
    await request.delete(`/api/replay/sessions/${longSessionId}`);
  }
});
