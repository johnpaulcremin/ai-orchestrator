import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Kept separate from vite.config.ts: Vitest bundles its own Vite, whose plugin
// types conflict with the project's Vite 8 under `tsc -b`. This file is not part
// of the tsconfig build, so the runtime-only config never trips type-checking.
export default defineConfig({
  plugins: [react()],
  // Force the automatic JSX runtime so .tsx test files don't need `import React`.
  esbuild: {
    jsx: "automatic",
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    coverage: {
      provider: "v8",
      reporter: ["text", "text-summary"],
      include: ["src/**/*.{ts,tsx}"],
      exclude: ["src/main.tsx", "src/test/**", "src/**/*.test.{ts,tsx}"],
      // Measured baseline (2026-07-29): 90.75% statements/lines, 88.99%
      // functions, 81.02% branches. Set with headroom below each — real
      // numbers per the reasoning this comment used to describe waiting
      // for, matching pyproject.toml's backend gate. `npm run test:coverage`
      // (see .github/workflows/ci.yml) fails the build below these. Raise
      // over time as coverage naturally climbs -- only ever up, never down
      // for convenience.
      thresholds: {
        // Aggregate only, not per-file (vitest's default is per-file): a
        // type-only file (types.ts) and an error boundary that's hard to
        // exercise without contrived thrown errors (ErrorBoundary.tsx) both
        // sit at 0% and would fail any per-file floor outright, without
        // reflecting a real regression. The backend gate (pyproject.toml)
        // is aggregate-only for the same reason.
        perFile: false,
        statements: 85,
        lines: 85,
        functions: 80,
        branches: 75,
      },
    },
  },
});
