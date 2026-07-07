# Deploying the MEIC panel (Docker)

The bot ships as a single container: the React panel is built with Node and
served by the Python backend (FastAPI + uvicorn). Paper mode only — the live
adapter is a separate wiring and is never constructed here (SIM-01/EC-RSK-04).

## Run

```bash
docker compose up --build
```

Then open **http://127.0.0.1:8000/**. The compressed paper day starts on its own
and loops, so the panel always has activity to watch; use the per-entry **Close**
buttons and header **Flatten all** (typed `FLATTEN`) to act on the book.

Stop it (the state volume persists):

```bash
docker compose down
```

## What the image contains

- `backend/src` — the domain + application + adapters (PYTHONPATH root)
- `frontend/dist` — the built panel, served at `/`
- `spec/` — the locked contract the build is defined by
- runtime deps only (`requirements-runtime.txt`: fastapi, uvicorn, websockets,
  tzdata). The `tastytrade` SDK is **not** in the image — it is needed only for
  live mode and the `-m contract` sandbox drills.

Image size ≈ 200 MB (python:3.13-slim base).

## Security (NFR-06)

The port is published to **host 127.0.0.1 only** — the panel is never exposed to
the network. Inside the container uvicorn binds `0.0.0.0` (required to publish),
but nothing reaches it except the localhost-mapped port. If you ever bind it
beyond localhost, config validation requires an API token (`validate_bind`).

## Durable state (REC-07)

The named volume `meic-data` is mounted at `/data` and survives `down`/`up` and
container recreation. The paper demo runtime is self-resetting (in-memory) so it
does not write there; a live/persisted session wires the `SqliteEventStore` /
`SqliteStateStore` (in `adapters/persistence/event_store.py`) at `MEIC_DATA_DIR`
(`/data`) so ARMED/Stop-Trading/schedule/ledger restore exactly on restart —
this is the container-recovery story TC-ENT-07 asserts against the stores.

## Live mode (later)

Live trading needs `tastytrade` installed and cert/production credentials in a
gitignored `.env`. That is a deliberately separate composition and deployment
step — not part of this paper image.
