# Deploying the MEIC panel (Docker)

The bot ships as a single container: the React panel is built with Node and
served by the Python backend (FastAPI + uvicorn). Paper mode only — the live
adapter is a separate wiring and is never constructed here (SIM-01/EC-RSK-04).

## Run

```bash
docker compose up --build
```

Then open **http://127.0.0.1:8010/**. The compressed paper day starts on its own
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

## Live mode

The `live_app` entrypoint binds the real Tastytrade adapter + DXLink feed
(never the simulator), persists REC-07 state to SQLite at `MEIC_DATA_DIR`, is
token-gated (NFR-06), and boots with **safe defaults** (DISARMED, Confirm Live
OFF) so nothing trades until you deliberately arm and confirm. It connects to
the broker on startup but a connect failure never takes the panel down —
`GET /broker/health` reports status and `POST /broker/connect` retries.

```bash
# CERT sandbox (default) — real broker session, fake money:
MEIC_USER_PASSWORD=<pick-a-token> \
  uvicorn meic.adapters.api.server:live_app --factory --host 127.0.0.1 --port 8010
```

Credentials live in a gitignored `.env` (BOM-tolerant, NFR-05), **never in the
command line or source**. Keys by environment:

- CERT (default, `MEIC_LIVE_IS_TEST=true`): `TT_CERT_PROVIDER_SECRET`,
  `TT_CERT_REFRESH_TOKEN`, optional `TT_CERT_ACCOUNT`.
- PRODUCTION (`MEIC_LIVE_IS_TEST=false`): `TT_PROD_PROVIDER_SECRET`,
  `TT_PROD_REFRESH_TOKEN`, optional `TT_PROD_ACCOUNT`.

**Real money needs TWO deliberate switches, never one.** Alongside
`MEIC_LIVE_IS_TEST=false` you must set `MEIC_ALLOW_PRODUCTION=I_UNDERSTAND_REAL_MONEY`,
or the wiring refuses to build. The guards are symmetric and run before any
network call: the cert wiring refuses a production token, and the production
wiring refuses a cert token (a mis-slotted token fails loudly, not at auth time).

**Reconcile-on-boot.** On connect the bot adopts broker truth (REC-02/04): any
position its durable OWN ledger cannot account for is **FOREIGN** — quarantined
(never stopped, closed or counted, even a naked short), critical-alerted, and it
**blocks new entries** until you resolve it. See `GET /reconcile`, `/alerts`,
`/broker/health`. A fresh bot on an account with existing positions will
therefore refuse to trade — by design.

**Trading runtime.** `LiveRuntime` drives the wall-clock entry cadence (warm-up
at T-60, the ENT-03 gate chain, plus reconcile-block and clock-drift blocks) and
is wired in `live_app` with the real chain selector and real market gates:

- **selector** — snapshots the live SPXW 0DTE chain over DXLink, applies DAT-02
  freshness → STK-10 completeness → probe walk → STK-09 collisions → credit gates
  re-run on the FINAL strikes. Any degraded input returns a **named skip reason
  and no Condor**; it never estimates a missing mark.
- **gates** — exchange calendar (DAY-01/02, ET), snapshot freshness, a broker
  session probe, and `derivative_buying_power` vs `MEIC_MIN_BUYING_POWER`
  (default 5000). Every provider **defaults to the blocking answer**: a gate that
  cannot be evaluated blocks the entry rather than waving it through.
- **rails** — RSK-04 (`max_day_risk` from the schedule panel), RSK-08 (daily order
  cap, default 380), and the ENT-03 buying-power gate are all armed by
  `_wire_live_day`; `tests/composition/test_live_wiring.py` fails if any is left
  unwired.

**Clock verification (DAY-03, v1.48 — automatic, no manual step).** Drift is
measured against the **broker's `Date` header** on the authenticated session
probe that already runs every ~60 s — continuous, no NTP, no env var. Until the
first probe lands the clock is **unverified** (infinite drift) and the pre-flight
**blocks a live arm**; a reading older than 300 s is treated the same. The only
knob is the tolerance:

```
MEIC_MAX_CLOCK_DRIFT_MS=2000    # RSK-07 tolerance (default 2000; range 1000-10000).
                                # ~1s Date-header resolution, so sub-1000 is noise.
```

### Verify at the market open (read-only, places nothing)

```bash
pytest -m contract tests/contract/test_live_selection_cert.py -s
```

It prints spot, expiration, band/marked counts, completeness, and either the
selected condor or the named skip reason. It asserts the one invariant that
matters: **a Condor is never returned from stale or incomplete data.** Run it at
the open; with the market closed you will correctly see `data_unavailable`.

### Run a trading day

```
POST /day/start    # starts the wall-clock cadence (token-gated)
GET  /day/status
POST /day/stop
```

Starting the day does **not** arm it — every entry still runs the full gate
chain. Verified against cert: fully ARMED with Confirm Live ON, at a live entry
time with the market closed, the runtime skipped `data_unavailable` and
submitted **zero orders**.

Go-live order: pass the STP-05a cert drill (`pytest -m contract`) → run cert
`live_app` for a track record → run the UC-12 outage drill on your account →
promote to live (typed `LIVE`, flat book, next-day). `tastytrade` must be
installed for live (`pip install -r backend/requirements.txt`); it is not in
the paper image.
