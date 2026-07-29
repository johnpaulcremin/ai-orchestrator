import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Overridable so the E2E suite (frontend/../e2e) can point a `vite preview`
// instance at its own backend on a different port, without colliding with a
// developer's normal `npm run dev` backend on the default 8000.
const apiProxyTarget = process.env.VITE_API_PROXY_TARGET ?? "http://127.0.0.1:8000";

const apiProxy = {
  "/api": {
    target: apiProxyTarget,
    changeOrigin: true,
    rewrite: (path: string) => path.replace(/^\/api/, ""),
  },
};

export default defineConfig({
  plugins: [react()],
  server: { proxy: apiProxy },
  // `vite preview` (serving the production build, as the E2E suite does)
  // does not fall back to `server.proxy` -- it needs its own, identical entry.
  preview: { proxy: apiProxy },
});