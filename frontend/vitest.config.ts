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
  },
});
