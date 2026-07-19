# AI Workbench — frontend

The React 19 + Vite + TypeScript UI for the AI Orchestrator. A single-page app
(`src/App.tsx`) with a conversation sidebar, a fast/smart/auto mode picker,
token-by-token streaming answers with markdown rendering, dark mode, and an
optional auth panel (static token or username/password login).

## Develop

```bash
npm install
npm run dev      # http://localhost:5173
```

The dev server proxies `/api/*` to the backend at `http://127.0.0.1:8000`
(stripping the `/api` prefix), so run the backend too (see the root README).

## Scripts

| Command | What it does |
| --- | --- |
| `npm run dev` | Vite dev server with HMR |
| `npm run build` | Type-check (`tsc -b`) and build to `dist/` |
| `npm run preview` | Serve the production build locally |
| `npm run lint` | ESLint |
| `npm test` | Vitest (unit + component tests) |

## Auth modes

The UI reads `GET /v1/status`. When the backend reports `jwt_enabled`, the
sidebar shows a login/register/logout panel and stores the JWT; otherwise it
shows an optional static-token field. Either credential is attached as
`Authorization: Bearer <token>` on every request.

Notable modules: `src/sse.ts` (incremental Server-Sent Events parser) and
`src/format.ts` (local-time timestamps) are extracted so they're unit-tested in
isolation (`src/*.test.ts`).
