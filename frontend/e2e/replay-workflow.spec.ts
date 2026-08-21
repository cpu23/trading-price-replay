import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

const SYMBOL = "E2E";
// Set by playwright.config.ts in the same process.
const fixturePath = process.env.E2E_FIXTURE ?? "";

let sessionId: string;

test.beforeAll(async ({ request }) => {
  const response = await request.post("/api/imports", {
    data: { path: fixturePath, symbol: SYMBOL },
  });
  expect(response.status()).toBe(200);
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
