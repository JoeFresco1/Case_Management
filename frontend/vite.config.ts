import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
  test: {
    // Frontend test conventions (mirrors backend AGENTS.md §4.R5):
    //   test_<unit>_<scenario>__<expectation>
    //   // given / // when / // then
    // jsdom is the React component harness; node is the cheaper
    // default for pure-function tests.
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
    include: [
      "src/**/*.{test,spec}.{ts,tsx}",
      "src/**/__tests__/**/*.{ts,tsx}",
    ],
    coverage: {
      provider: "v8",
      reporter: ["text", "html"],
      include: ["src/**/*.{ts,tsx}"],
      exclude: [
        "src/**/*.test.{ts,tsx}",
        "src/**/__tests__/**",
        "src/main.tsx",
        "src/test-setup.ts",
        "src/api/types.gen.ts",
      ],
      thresholds: {
        lines: 70,
        functions: 70,
        branches: 60,
        statements: 70,
      },
    },
  },
});