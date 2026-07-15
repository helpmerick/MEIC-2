# 05 — Domain-Driven Design Architecture

Python backend (asyncio), React/TypeScript frontend. Hexagonal (ports & adapters) with an event-sourced core domain. The domain layer is pure Python — no I/O, no broker SDK imports, no clock reads — which is what makes every rule in doc 01 unit-testable.

---

## 1. Bounded contexts

```
┌─────────────────────────────────────────────────────────────┐
│                        TRADING (core domain)                │
│  Entry scheduling · condor construction · stop policy ·     │
│  LEX ladder · EOD · position lifecycle                      │
└──────┬──────────────┬──────────────┬───────────────┬────────┘
       │              │              │               │
┌──────▼─────┐ ┌──────▼─────┐ ┌──────▼──────┐ ┌──────▼──────┐
│ MARKET DATA │ │ BROKERAGE  │ │ RISK &      │ │ OPERATIONS  │
│ (supporting)│ │ (supporting│ │ SAFETY      │ │ (generic)   │
│ chain, greeks│ │ orders,    │ │ (supporting)│ │ config, UI  │
│ quotes, spot │ │ positions, │ │ stop trading│ │ API, alerts,│
│ staleness    │ │ fills,     │ │ loss limits,│ │ reporting,  │
│              │ │ reconcile  │ │ gates       │ │ audit       │
└─────────────┘ └────────────┘ └─────────────┘ └─────────────┘
```

- **Trading** is the core domain: all doc-01 strategy rules live here and nowhere else.
- **Brokerage** wraps tastytrade (REST + account streaming). It speaks in domain terms (`PlaceStopOrder`, `OrderFilled`) and hides tastytrade's payloads entirely.
- **Market Data** wraps DXLink; owns staleness (DAT-02) so the core never sees a stale quote without knowing it.
- **Risk & Safety** is deliberately separate from Trading: gates (RSK-*) are checked by the application layer *around* the core, so no strategy code can bypass them.
- **Operations** hosts config versioning, the REST/WebSocket API for the React UI, alerting, reports.

Context mapping: Trading ← (conformist) — Brokerage/Market Data adapters translate inward via anti-corruption layers. Operations is downstream of everything (read models).

## 2. Ubiquitous language (enforced in code names)

TradingDay, EntryAttempt, IronCondor, Side (PUT/CALL), Leg, ShortLeg/LongLeg, Credit, StopPolicy, StopTrigger, RepriceLadder, LongRecovery (the LEX procedure), Whipsaw, Unprotected, KillSwitch, ReconciliationMismatch, PaperMode/LiveMode. If a term isn't in docs 01–03, it doesn't belong in a class name.

## 3. Aggregates (Trading context)

**TradingDay** (aggregate root)
- Identity: trading date. Owns: entry schedule state, day-level gates snapshot, mode, day P&L rollup, stop-trading day flags.
- Invariants: DAY-04/05, ENT-05, ENT-07 (one attempt at a time), RSK-04 evaluated before admitting a new entry.
- Emits: `DayArmed`, `EntryWindowOpened`, `EntrySkipped(reason)`, `DailyLossLimitBreached`, `DayCompleted`.

**CondorEntry** (aggregate root — one per filled/attempted entry)
- Identity: (date, entry number). Owns: 4 legs (always entered as one complex order, ORD-01), actual fill credits (total and per-side from allocated leg fills), per-side state machines, stop triggers per `stop_basis`, LEX ladder state.
- Invariants: STK-01→08 at construction; STP-02 trigger math; STP-06 (no long stops); LEX-04 floor; per-side lifecycle legality (below).
- Emits: `CondorProposed`, `CondorFilled`, `StopPlaced`, `StopConfirmed`, `SideUnprotected`, `ShortStopped(side, fill, slippage)`, `LongSaleStarted`, `LongSaleRepriced(step, price)`, `LongSold(recovery)`, `SideClosed`, `SideExpired`, `EntryCompleted`.

Per-side state machine (the legality table IS the spec — transitions not listed are bugs):
```
PENDING → WORKING → OPEN_UNCONFIRMED_STOP → PROTECTED
WORKING → SKIPPED | PARTIAL_RESOLVING → (PROTECTED | FLATTENED)
OPEN_UNCONFIRMED_STOP → UNPROTECTED → (PROTECTED | FLATTENED)
PROTECTED → SIDE_STOPPED → LONG_LIQUIDATING → SIDE_CLOSED
PROTECTED → DECAY_CLOSING → (SIDE_CLOSED_DECAY → SIDE_EXPIRED | PROTECTED)  # DCY-02/03; re-inflation guard returns to PROTECTED
PROTECTED → SIDE_EXPIRED
LONG_LIQUIDATING → SIDE_EXPIRED        # EOD-04
any → MANUAL (UC-08) → resumes at reconciled state
any → SUSPENDED (OWN-06/09/10/11: operator intervened at the broker) → resumes on acknowledgment
```

**Order** (entity, owned by CondorEntry / referenced by Brokerage)
- Identity: idempotency key (ORD-04). Value: intent, prices, quantity, state driven only by broker events (ORD-05).

**OwnershipLedger** (domain service, OWN-01→06)
- Per-symbol owned quantity derived exclusively from the bot's own fill events; computes foreign_delta against broker positions; classifies unmatched positions FOREIGN. The single order-construction path consults it to cap every exit order (OWN-04). No adapter may bypass it.

Value objects (immutable, heavily unit-tested): `Strike`, `Premium`, `Tick` (STK-08 rounding), `Delta`, `StopPolicy(pct ∈ {95..300 step 5})`, `RepriceLadder(start, step_fn, attempts, interval)`, `EntrySchedule`, `Slippage`, `FeeModel`, `Money`.

## 4. Domain events & event sourcing (REC-01)

Every aggregate mutation is an event appended to a per-day, per-aggregate stream (SQLite or Postgres; single-writer, fsync before side effects). Rules:

1. Decide (pure domain function: state + command → events)
2. Persist events
3. Execute side effects (adapter calls) from a process manager reacting to persisted events
4. Broker responses come back as new commands

This ordering is what makes crash recovery (REC-02/03, TC-RSK-07) and the replay invariant (TC-REC-01) possible. Read models for the UI and reports project from the same streams.

## 5. Application layer (use-case orchestration)

Application services — thin, no business logic, enforce gates:

- `RunTradingDay` — arms schedule, opens entry windows (FakeClock-able `Clock` port).
- `ExecuteEntryAttempt` — runs ENT-03 gate chain via RiskGate, asks domain to propose a condor from a `ChainSnapshot`, drives the ORD-02 ladder.
- `ProtectPosition` — reacts to `CondorFilled`, drives STP-01→04 (including retry/UNPROTECTED escalation).
- `RecoverLong` — reacts to `ShortStopped`, drives the LEX ladder (LEX-01→09).
- `CloseEntry(entry_id, initiator)` — the **only** code path that closes positions (CLS-01/02). Invoked by: UI Close-trade command (`manual`), Flatten All (`manual_flatten`), TPF trigger (`take_profit`), decay buybacks (`decay`, short-only), and `CloseDay` (`eod`). Architecture test: no other module imports the close-order submission path.
- `CloseDay` — EOD-01→05, routes closes through `CloseEntry`.
- `Reconcile` — REC-02→05, runs at startup, reconnect, and periodically.
- `KillSwitch`, `ManualAction` (UC-05/08).

A `RiskGate` decorator wraps every order-submitting path: stop trading, flatten-in-progress, exposure, fat-finger, mode checks (RSK-01/01a/04/05). Structurally impossible to submit without passing it (constructor-injected; there is no other route to the broker port).

## 6. Ports (domain-defined interfaces) and adapters

```python
class BrokerGateway(Protocol):        # implemented by TastytradeAdapter, FakeBroker
    async def submit(self, order: OrderIntent) -> BrokerOrderId: ...
    async def cancel(self, id) -> CancelResult: ...
    async def replace(self, id, new: OrderIntent) -> BrokerOrderId: ...
    async def working_orders(self) -> list[BrokerOrder]: ...
    async def positions(self) -> list[BrokerPosition]: ...
    async def fills_since(self, cursor) -> list[Fill]: ...
    def order_events(self) -> AsyncIterator[OrderEvent]: ...   # account stream

class MarketDataFeed(Protocol):       # DXLinkAdapter, FakeMarketData
    async def chain(self, underlying, expiration) -> ChainSnapshot: ...
    def quotes(self, symbols) -> AsyncIterator[Quote]: ...     # staleness-stamped
    def spot(self, index) -> AsyncIterator[IndexTick]: ...

class Clock(Protocol): ...            # SystemClock (NTP-checked, DAY-03), FakeClock
class EventStore(Protocol): ...       # SqliteEventStore, InMemoryEventStore
class AlertSink(Protocol): ...        # UI/webhook/email fan-out (RSK-06)
class ExchangeCalendar(Protocol): ... # DAY-01/02
```

Adapter notes:
- **TastytradeAdapter**: REST for orders/positions, account WebSocket for fills/status, DXLink for quotes. Owns auth/token renewal (REC-06), client-side rate limiting with exit-priority classes (EC-API-02), payload translation (ACL). Verify empirically during implementation: complex-order (multi-leg) endpoints, stop order support per instrument, whether stops rest independent of session (STP-05 / TC-STP-08), sandbox behaviour differences.
- **Paper vs live** (EC-RSK-04/SIM-01): paper binds `BrokerGateway` to the **SimulatedBroker** adapter (trade-through fill model, simulated stops with slippage, cash/margin ledger — SIM-01→06) while consuming the real production DXLink feed; live wiring is not constructed in paper mode. The tastytrade cert sandbox is used only by the contract-test suite, never by paper mode.

## 7. Process/runtime layout

Single Python process, asyncio:
```
composition root (wiring, mode)
 ├─ event store (single writer task)
 ├─ broker adapter (REST client + account stream task)
 ├─ market data adapter (DXLink task)
 ├─ process managers (ProtectPosition, RecoverLong, Reconcile, ...)
 ├─ scheduler (RunTradingDay, driven by Clock)
 └─ API server (FastAPI): REST for commands/config, WebSocket pushing read-model
     deltas to the React UI
```
Concurrency rule: all domain decisions for one aggregate are serialized through a per-aggregate mailbox; adapter I/O is concurrent. This gives ENT-07 and race-safety (LEX-08) by construction.

## 7a. Runtime hardening — binding non-functional requirements (NFR)

Adopted from the predecessor system's production incidents (Bug Record #4, #17–20 — an unbounded cold connect froze the single event loop for 11 minutes and a scheduled entry was missed). These are requirements, not suggestions; each has a test in doc 04.

- **NFR-01 No blocking calls on the event loop.** Every synchronous tastytrade SDK/REST call (including OAuth login) runs on one dedicated worker thread — off-loop and serialized, so the shared HTTP session is never used concurrently. A stalled call delays only itself; scheduler, streams, UI and process managers keep running.
- **NFR-02 Session health loop.** During market hours: probe the session with a lightweight authenticated call every `config.session_probe_seconds` (default 60); proactively refresh every `config.session_refresh_seconds` (default 300 — well under the ~15-minute token lifetime; verify actual lifetime in sandbox). Layered with the ENT-08 pre-entry warm-up refresh and a fire-time refresh-and-retry. This keeps the account stream (stop-fill events → LEX) alive all day, not just at entries. Day-scoped: nothing probes outside market hours.
- **NFR-03 Timeouts on everything.** Explicit connect/read/write/pool timeouts (`config.http_timeout_seconds`, default 10) on every HTTP client; the ENT-08 warm-up runs under a hard wall-clock cap (`asyncio.wait_for`-style) so a stalled prime can never run into the fire window. No network operation in the system may wait unboundedly.
- **NFR-04 Persistent QuoteHub — single writer, generation-guarded, demand-reconnect, scoped fetcher.** (Final design, debated and adopted.)
  - **Normal operation:** one DXLink connection opened at market open, held all day with protocol keepalives (the SDK's heartbeat; DXLink is designed for long-lived connections — the ~24 h `/api-quote-tokens` streaming token is fetched at day start). All consumers (chain fetch, spot, LEX/TPF/DCY marks, P&L) read one shared marks table. The hub manager owns exactly ONE live socket at a time, holds the master subscription list (declarative — any new socket subscribes from it), and is the **only writer** to the marks table.
  - **Generation guard:** every socket gets a monotonically increasing generation number; every tick is tagged; ticks from any generation other than current are discarded on arrival. A zombie socket can never time-travel the marks table.
  - **Sickness & healing:** sick = socket closed, heartbeat missed, or per-instrument tick staleness. Healing (automatic, background): new socket → re-auth → re-subscribe from master list → healthy only when real ticks flow. Failed attempts back off exponentially (1s → ~30s cap). Health state on the UI pill; reconnects are routine events, logged not alarmed.
  - **Decision moment while sick** (entry fire, LEX reprice, TPF/DCY evaluation): (1) **demand-reconnect** — skip the backoff wait, one immediate attempt bounded by `config.feed_demand_reconnect_seconds` (default 2); success ⇒ proceed on the healed hub. (2) **Scoped one-shot fetcher** — fetch exactly what the requester needs (chain snapshot / specific quotes) over a throwaway connection, return it **directly to the caller, never writing the marks table** (two-writers structurally impossible); all data gates apply (staleness, sanity, chain-completeness, adjacency). (3) **Give up safely** — entry skips `data_unavailable`; LEX freezes repricing (working limit stays live at the broker); TPF/DCY pause; alert (informational — nothing ever waits on the operator); resume on heal.
  - **Safety backdrop:** broker-resting stops are untouched by any feed failure — this design protects income reliability, never blow-up risk.
  - **Sandbox verification items:** observed connection uptime across full market days, negotiated keepalive interval, quote-token behavior at the 24 h boundary.

- **NFR-05 File integrity — tolerate the known case, refuse the rest.** All bot-read files on disk (`.env`, any config/backup files) are decoded BOM-tolerantly (`utf-8-sig`) — the host has twice demonstrated a tool that silently prepends BOMs (Bug Record #21/#25). Beyond that one known-harmless case: at startup the bot hashes its critical files and compares against the hashes recorded at last shutdown; any change not made by the bot itself ⇒ **refuse to arm**, name the file, and require operator confirmation. Never silently tolerate unexplained file modification on a machine that places orders. (Host-level open item, pre-live: identify the process that mangles files — antivirus/sync client — and exclude the bot's directories; BOM tolerance is symptom relief, not the cure.)

- **NFR-06 Control-panel security.** The backend can flatten an account; it is secured accordingly: (1) bind to `127.0.0.1` by default (`config.bind_host`); (2) reject any mutating request (and WebSocket upgrade) whose Origin/Referer is not the panel's own host — a hostile webpage can fire requests at localhost from inside the operator's browser; (3) `config.api_token` optional on localhost, but **config validation refuses a non-localhost bind unless the token is set** — the panel cannot be exposed unauthenticated, structurally; (4) the Stop-Trading/Flatten-All curl fallback documentation (UI-09/17) includes the token header whenever a token is set; (5) remote access guidance: VPN (Tailscale/WireGuard) to the localhost-bound panel — never widen the bind for convenience.
- **NFR-06a Panel password & production double opt-in (v1.72 — wired-but-unspecced, ratified during the doc-12 verification).** (1) The LIVE composition REFUSES TO BOOT without the panel password (`MEIC_USER_PASSWORD`) — mandatory regardless of bind address (stronger than NFR-06's localhost-optional token, deliberately); every trade-changing request carries it (`X-User-Password`); the UI exposes a Locked/Unlocked control that validates it with explicit feedback; it is environment configuration and therefore survives restarts; it is PANEL ACCESS, not a per-arm ritual. (2) Wiring PRODUCTION (real money) requires TWO deliberate switches, never one: `MEIC_LIVE_IS_TEST=false` AND the explicit `MEIC_ALLOW_PRODUCTION` opt-in value — and the adapter separately asserts the broker token's issuer is production. Boot announcements log the environment kind, never any secret.
- **NFR-07 Wiring-audit gate (v1.67, operator-ratified — the cure for the exists-but-unwired class, found SEVEN times: RSK-04, day loop, TPF, fill detection, plus the 07-13 six incl. the stop watchdog, and DecayWatcher found by this review).** A registry test walks the spec's live-component list and asserts each is provably CONSTRUCTED and TICKED inside `live_app()` — not merely unit-tested. Traceability proves every rule has a test; NFR-07 proves the code that test covers is REACHED in production composition. Every spec rule that mandates a runtime component (monitors, watchers, sweeps, loops, samplers, reconcilers) MUST appear in the registry; adding such a rule without registering its component fails CI. A component in the registry that `live_app()` does not construct-and-tick fails CI. This gate is a locked guard (scripts/), operator-maintained like the traceability checker. **Constant-signal species (v1.68 — NFR-07's first pass found an EIGHTH instance of a NEW kind: RSK-01a's flatten_in_progress gate wired to a dead `lambda: False`, present and called and green forever):** the audit also asserts that every safety-gate INPUT is bound to a live signal source, never a constant/default — a gate that cannot ever say no is worse than a missing gate, because it looks alive.

## 8. Frontend (React/TS)

- Read-only projections over WebSocket (dashboard, entry cards, LEX ladder view) + command endpoints (config, stop trading, flatten, manual mode).
- The frontend holds **no trading logic**: it renders backend state and sends commands. All validation is duplicated server-side (UI-03); the discrete stop-pct selector (UI-04) is generated from the config schema served by the backend, so UI and backend can't drift.
- Types generated from the backend's OpenAPI/JSON-schema.

## 9. Testing strategy (matches doc 04 harness)

- **Domain unit tests**: pure — state machines, trigger math, ladders, tick rounding. Fast, exhaustive, property-based where numeric (e.g. STP-02 across the whole pct set × random credits).
- **Application tests**: services + process managers against fakes with FakeClock; all EC-* scenarios scripted here, including crash simulation (drop the process manager, keep the fake broker + event store, boot a new instance).
- **Contract tests**: TastytradeAdapter against the sandbox/paper API (auth, order shapes, stream events, stop persistence drill TC-STP-08). Run separately from CI-fast.
- **Replay tests**: TC-REC-01 determinism.
- **E2E**: full paper-mode day with compressed FakeClock time against fakes; UI driven headlessly for TC-UI-*.

## 10. Suggested repository layout

```
meic-bot/
  backend/
    src/meic/
      domain/            # pure: aggregates, VOs, events, state machines, policies
      application/       # services, process managers, RiskGate, ports (Protocols)
      adapters/
        tastytrade/      # broker ACL
        dxlink/          # market data ACL
        persistence/     # event store, read models
        api/             # FastAPI + WebSocket
      config/            # schema (single source of truth, doc 06), versioning
    tests/
      domain/ application/ contract/ replay/
  frontend/              # React + TS, generated API types
  spec/                  # these documents — the source of truth
  tools/traceability/    # CI script: rule-ID coverage check (doc 04)
```

**Build order for the coding AI:** domain value objects + state machines → domain aggregates against doc-04 unit tests → event store + replay tests → application services with fakes against EC-* tests → tastytrade/DXLink adapters + contract tests → API + UI → paper-mode E2E → stop-independence drill (UC-12) → live.

## Verification mode & the graduation clock (v1.74, operator-ratified)

Each calendar trading day classifies as exactly one of: **CLEAN** (counts —
the bot was GIVEN THE CHANCE to trade: armed, schedule enabled through the
windows; AND zero RPT-15 corrections attributable to bot error, zero
contract breaches, zero new unwired findings, EOD audit clean), **DIRTY**
(resets the count to zero — any of those failed on a day the bot ran), or
**NULL** (neither — disarmed/held through the windows; no evidence either
way; NULLs stretch the calendar, never count). Ten CLEANs graduate the bot
to unattended-eligible (Phase D checklist still applies). A build change
RESTARTS the clock at zero — a streak certifies the binary that earned it,
not its successor. Clock day one: 2026-07-16, on the deployed v1.73 batch.
