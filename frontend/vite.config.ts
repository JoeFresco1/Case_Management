import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "VITE_");
  // Alternate ports support parallel local demo instances with fixed dev identities.
  const apiPort = Number(env.VITE_DEMO_API_PORT || env.VITE_LOCAL_API_PORT || 8000);
  if (!Number.isInteger(apiPort) || apiPort < 1 || apiPort > 65535) throw new Error("Invalid local API port");
  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        "/api": {
          target: `http://127.0.0.1:${apiPort}`,
          changeOrigin: true,
          // Local identity is supplied by the dev proxy; the browser never
          // embeds a synthetic tenant ID or claims a server-side role.
          headers: { "X-Dev-User": env.VITE_DEV_USER || "intake" },
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
  };
});
