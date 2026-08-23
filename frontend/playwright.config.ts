import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "@playwright/test";

const here = fileURLToPath(new URL(".", import.meta.url));

// Ephemeral backend data root: e2e never touches a real session database.
const dataRoot = process.env.PRICE_REPLAY_E2E_DATA_ROOT
  ?? mkdtempSync(join(tmpdir(), "trading-replay-e2e-"));
process.env.PRICE_REPLAY_E2E_DATA_ROOT = dataRoot;

const backendUrl = "http://127.0.0.1:8123";
const frontendUrl = "http://localhost:5199";
// Exposed to the specs (same process) so they can point the import API at
// the deterministic 12-bar fixture.
process.env.E2E_FIXTURE = resolve(here, "../backend/tests/fixtures/dukascopy_1m.csv");
process.env.PRICE_REPLAY_DATA_ROOT = dataRoot;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: process.env.CI
    ? [["github"], ["json", { outputFile: "test-results/report.json" }]]
    : "list",
  use: {
    baseURL: frontendUrl,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: [
    {
      command: "uv run uvicorn app.main:app --port 8123",
      cwd: resolve(here, "../backend"),
      env: { ...process.env, PRICE_REPLAY_DATA_ROOT: dataRoot },
      url: `${backendUrl}/api/health`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: "npm run dev -- --port 5199 --strictPort",
      cwd: resolve(here, "."),
      env: { ...process.env, BACKEND_URL: backendUrl },
      url: frontendUrl,
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ],
});
