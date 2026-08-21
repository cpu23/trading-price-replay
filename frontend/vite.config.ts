import { loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  return {
    plugins: [react()],
    server: { proxy: { "/api": env.BACKEND_URL || "http://localhost:8000" } },
    test: { exclude: ["**/node_modules/**", "e2e/**"] },
  };
});
