# MEIC control panel (frontend)

React + TypeScript + Vite. **No trading logic** (UI-03): it renders read-model
projections from the backend and sends commands; the backend validates
everything. Localhost-bound (NFR-06).

## Run

Requires Node 18+ (not installed on the build machine — install it first).

```bash
# 1. start the backend control panel (FastAPI) on :8000, e.g.
#    uvicorn meic.adapters.api.app:create_app --factory  (once wired to a live PaperComposition)
# 2. then, in frontend/:
npm install
npm run dev            # Vite dev server on http://127.0.0.1:5173, proxying API -> :8000
```

Open http://127.0.0.1:5173.

## Auth (NFR-06)

If the backend was started with an `api_token`, set it once in the browser
console so mutating commands carry the header:

```js
localStorage.setItem("meic_api_token", "your-token")
```

On a plain localhost bind no token is needed.

## Structure

- `src/api.ts` — the only place that talks to the backend (`/state`, `/report`,
  and the command endpoints). Carries the `x-api-token` header when set.
- `src/useLivePanel.ts` — polls the read model (the doc-05 §8 WebSocket delta
  transport is a drop-in replacement; the render layer is unchanged).
- `src/components/` — `Dashboard` (durable enabling states + mode),
  `CommandPanel` (arm/disarm, stop-trading, confirm-live, stop-pct config),
  `DayReportView` (EOD-05 figures + per-entry P&L + skips).

Types in `src/types.ts` mirror the FastAPI contract; in production they are
generated from the backend's OpenAPI schema so UI and backend cannot drift.
