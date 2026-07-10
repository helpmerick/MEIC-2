# MEIC Bot — Security Review (2026-07-10)

Scope: defensive review of the operator's own live-money trading system at
`c:\Users\ashle\MEIC_BOT_2.0`. Read-and-report only — nothing below has been
implemented. Every improvement is a proposal for the operator to APPROVE or
REJECT; none should be assumed accepted.

Reviewed: `spec/01-strategy-rules.md`, `spec/05-architecture-ddd.md` (§7a,
NFR-01→06), `backend/src/meic/adapters/api/{server.py,app.py,reports.py}`,
`backend/src/meic/config/validation.py`, `backend/src/meic/adapters/tastytrade/adapter.py`,
`backend/src/meic/application/{protect_position.py,execute_entry.py,recover_long.py,
decay_watcher.py,idempotency.py}`, `frontend/src/{api.ts,App.tsx}`, `Dockerfile`,
`docker-compose.yml`, `DEPLOY.md`, `.github/workflows/ci.yml`,
`scripts/verify_spec_lock.py`, `tools/observe_quote_token.py`, `.env` (key names
only), `.gitignore`, `git log --all -- .env`, live Docker/port state on this host,
`pip list --outdated`, `npm audit --production`, the installed `tastytrade` SDK
source (`.venv/Lib/site-packages/tastytrade/{session.py,utils.py}`).

## Executive summary

The panel's headline safety story (localhost bind, origin checks, two-switch
production opt-in, JWT-issuer guards, a structurally read-only reconciler) is
real and well-built. The most important findings are not in that story — they
are (1) a **live, unpatched recurrence of the exact order-duplication bug the
operator already hit and fixed once**, in a sibling code path (LEX recovery)
that the fix commit didn't reach, and (2) the **NFR-06 "structural" bind
protection is not actually wired to the real startup path** — `validate_bind`
exists and is unit-tested but nothing calls it when uvicorn is actually
launched. Everything else is smaller: exception-detail exposure on
unauthenticated endpoints, a missing Referer check, no password policy, token
timing comparison, localStorage exposure, a stale Docker artifact, and routine
dependency hygiene.

**Counts by severity:** CRITICAL: 2 · HIGH: 3 · MEDIUM: 7 · LOW: 3

## Findings table

| ID | Severity | Area | One-liner |
|----|----------|------|-----------|
| F1 | CRITICAL | Trading | LEX recovery ladder reprices without re-confirming fill first — same bug class as the fixed incident #2, unpatched here |
| F2 | CRITICAL | Panel auth / startup | `validate_bind`'s "structural" non-localhost-requires-token guarantee is never invoked on the real uvicorn startup path |
| F3 | HIGH | Trading | ORD-04 "query broker before resubmit" logic is unwired everywhere; stop-order retry loop blind-resubmits on any exception |
| F4 | HIGH | Credentials | Exception `repr()`s (incl. `broker_error`, alert `error=` context) surface on unauthenticated GET endpoints with no redaction layer |
| F5 | HIGH | Panel auth | `origin_allowed()` checks Origin only; NFR-06(2)'s "Origin/Referer" text is only half-implemented |
| F6 | MEDIUM | Panel auth | `x-api-token` compared with plain `!=`, not constant-time |
| F7 | MEDIUM | Panel auth | No strength/length policy on `MEIC_USER_PASSWORD` |
| F8 | MEDIUM | Panel auth | Token kept in `localStorage`; readable/exfiltrable by any XSS in the panel |
| F9 | MEDIUM | Panel auth | GETs and the WebSocket feed are unauthenticated even when a token is configured — full trade/P&L/schedule/alert visibility to any local process |
| F10 | MEDIUM | Process/FS | Stale `meic-bot` Docker container (old `meic.web:app` entrypoint) present, `restart: unless-stopped`, could silently reappear |
| F11 | MEDIUM | Trading | No operator-facing emergency runbook for the curl+token Stop-Trading/Flatten fallback — it only exists inside read-only `spec/` |
| F12 | MEDIUM | Supply chain | Runtime Python deps pinned lower-bound only, no lockfile/hashes; no `pip-audit`/`npm audit` in CI |
| F13 | LOW | Credentials | `tools/observe_quote_token.py` logs `repr(e)` to a plaintext JSONL with no redaction (same pattern as F4, no confirmed leak found) |
| F14 | LOW | Supply chain | Minor outdated packages in the active venv (numpy, pydantic_core, tzdata, uvicorn, websockets, pip) |
| F15 | LOW | Panel auth | CSV export fields are all system-generated today (no formula-injection risk yet), but no sanitization exists if a free-text field is ever added |

---

## Detail

### F1 — CRITICAL — LEX recovery ladder reprices a possibly-filled order

**Evidence:** `backend/src/meic/application/recover_long.py:84-97`

```python
for rung in ladder.prices():
    ...
    order_id = await self._broker.submit(intent) if order_id is None \
        else await self._broker.replace(order_id, intent)
    ...
    if await self._filled(order_id):
        ...
```

Every rung after the first calls `broker.replace(order_id, intent)`
**before** checking whether the previous rung already filled. Commit
`d3453c9` ("fix: reprice ladder must not reprice a filling order (incident
#2)") fixed exactly this pattern in `execute_entry.py:250-255` (now
re-confirms `await self._filled(working_id)` immediately before replacing),
with the commit message describing the live incident: a zero-gap reprice
loop repriced an order that had already filled-but-not-yet-registered,
producing a duplicate order → `margin_check_failed` → stops never ran.
`git show --stat d3453c9` confirms that fix touched `execute_entry.py` and
its test harness only — `recover_long.py` was never touched and still has
the pre-fix shape.

**Risk scenario:** A short leg stops out, LEX starts selling the orphaned
long. The first reprice rung's `replace()` races a live fill (exactly the
condition incident #2 already reproduced). The ladder blindly reprices the
now-filled order, which either double-sells the long (naked/oversold
position) or hits a broker rejection that leaves the long unresolved and
the LEX bookkeeping (`LongSaleRepriced` events) out of sync with reality —
on the recovery leg of a stop-out, which is already the most time-pressured,
highest-stakes path in the system.

**Proposed improvement:** Port the identical fix — poll/re-check `_filled()`
immediately before every `replace()` call, exactly as `execute_entry.py`
now does — into `recover_long.py`'s ladder loop, and add a live-shaped test
mirroring `tests/application/test_live_fill_path.py` for the LEX path.
**Effort: small** (the fix pattern and its test harness — `tests/harness/live_broker.py`
— already exist; this is porting it, not designing it).
**APPROVE / REJECT:** ______

---

### F2 — CRITICAL — the NFR-06 non-localhost bind guard is not wired to the real startup path

**Evidence:** `backend/src/meic/config/validation.py:36-40` defines
`validate_bind()`. Its only caller is `validate_config()` at
`validation.py:55-56`, which is only reached via `POST /config`
(`backend/src/meic/adapters/api/app.py:397-404`). That handler
(`update_config`) validates the patch and returns `{"accepted": patch}` —
it never applies `bind_host` to anything; there is no config-driven bind at
all. The actual bind address is chosen by whoever launches uvicorn on the
command line (`server.py`'s own docstring, lines 5 and 12: `--host
127.0.0.1`), and `server.py`/`app.py`/`live_app()`/`paper_app()` never call
`validate_bind` or read a `bind_host`/`MEIC_BIND_HOST` env var at startup.
`grep -rn "validate_bind|bind_host"` across `backend/src` returns only the
definition and its own unit tests (`tests/adapters/test_api.py:82-85`) —
zero production call sites.

**Risk scenario:** Doc 05 §7a NFR-06(3) states this as a structural
guarantee: "the panel cannot be exposed unauthenticated, structurally."
It is not. If the operator (or a future script, a copy-pasted command from
notes, or a mistaken `--host 0.0.0.0` for remote debugging) starts
`live_app`/`paper_app` bound to a non-loopback address without
`MEIC_USER_PASSWORD` set, nothing in the code objects — the "structural"
protection the spec and the Docker docs (`DEPLOY.md`'s "Security (NFR-06)"
section) both describe as enforced does not exist on the bare-metal (non-Docker)
launch path this repo's own `DEPLOY.md`/docstrings document as the live
entrypoint (`uvicorn meic.adapters.api.server:live_app --factory --host
127.0.0.1 --port 8010`).

**Proposed improvement:** Read the effective bind host at process start
(env var, e.g. `MEIC_BIND_HOST`, matching doc 06's `bind_host` config key)
and call `validate_bind(bind_host, token)` before constructing/starting the
app in both `paper_app()` and `live_app()` — raising the same way the
missing-credential checks already do at `server.py:652-653`. Optionally also
assert `--host` matches at runtime via `uvicorn.Config`/ASGI server
introspection if the operator wants the CLI flag itself covered, not just an
env-var-driven one. **Effort: small** (the validation function and its
tests already exist; this wires one call site).
**APPROVE / REJECT:** ______

---

### F3 — HIGH — ORD-04's "query-before-resubmit" is unwired; stop retries blind-resubmit on exception

**Evidence:** `backend/src/meic/application/idempotency.py:14-19` defines
`resolve_submit_after_timeout()` — exactly the ORD-04 mechanism the
docstring describes ("query the broker by that key; if it exists, adopt it
and do NOT resubmit"). Its only callers repo-wide are
`tests/bdd/test_tc_ord_02.py:28,37` — it is never called from
`execute_entry.py`, `close_entry.py`, `protect_position.py`,
`recover_long.py`, `decay_watcher.py`, or `watchdog.py`, the only places
`broker.submit()` is actually invoked. Separately, `idempotency_key` is
plumbed onto every `OrderIntent` (e.g. `protect_position.py:146`,
`execute_entry.py:238`) but `TastytradeAdapter._build_order()`
(`backend/src/meic/adapters/tastytrade/adapter.py:123-161`) never reads
`intent.idempotency_key` when building the SDK's `NewOrder` — it is not
sent to the broker in any form, so the broker itself has no idempotency
key to dedupe against either.

Concretely, the one place that *does* retry a submit is
`protect_position.py:_place_and_verify` (`backend/src/meic/application/protect_position.py:155-181`):

```python
for attempt in range(self._retry_attempts):
    try:
        order_id = await self._broker.submit(intent)
    except Exception:
        order_id = None
    ...
```

On any exception (a client-side timeout is explicitly guaranteed to happen
under load per NFR-03's own timeout config) the loop simply calls
`submit()` again with the identical intent — with no query of
`working_orders()`/`resolve_submit_after_timeout` beforehand to check
whether the first attempt actually landed.

**Risk scenario:** A stop-market submission times out client-side (NFR-03:
`http_timeout_seconds`, default 10s) after the order actually posted at the
broker. The retry loop resubmits unconditionally, resting a **second**
protective stop-market order on the same short leg. Best case this is
merely confusing; worst case (depending on cert/prod fill semantics) it can
over-hedge, cause an unexpected close, or complicate reconciliation.

**Proposed improvement:** Before each retry in `_place_and_verify` (and
ideally at every `broker.submit()` call site that can be retried by an
outer loop), call `resolve_submit_after_timeout(intent.idempotency_key,
[o.idempotency_key for o in await broker.working_orders()])` and adopt
the existing order instead of resubmitting when found. This also requires
actually transmitting the idempotency key to the broker (e.g. as an SDK
`NewOrder` client-tag/comment field, if tastytrade's API exposes one) so
"query by that key" has something real to match against —
**verify with the operator whether the tastytrade API/SDK exposes a
client-order-id field** before implementing; if it does not, the fallback
is matching on (symbol, side, trigger, contracts) recency instead, which is
weaker. **Effort: medium** (needs a sandbox/cert check for whether a
client-tag field exists, plus wiring at each retry site).
**APPROVE / REJECT:** ______

---

### F4 — HIGH — exception reprs surfaced on unauthenticated GET endpoints, no redaction

**Evidence:**
- `backend/src/meic/adapters/api/server.py:732,736,743,752,766,802`: every
  `except Exception as exc: app.state.broker_error = repr(exc)`, read back
  by `GET /broker/health` (`server.py:805-809`) — a GET route, so per the
  app's own security middleware (`app.py:212-225`) it requires **no**
  Origin check and **no** token, ever, even when one is configured.
- `server.py:342,364,904` and numerous `alerts.alert(..., error=repr(...))`
  call sites (`close_entry.py:206`, `live_runtime.py:59`,
  `manual_entry.py:62`) land in `_PanelAlerts` and are readable via
  `GET /alerts` (`server.py:821-823`) — same unauthenticated-GET exposure.

I traced the one plausible secret-bearing exception path — a broker
auth/reconnect failure (`comp.connect()` → `TastytradeAdapter.connect()` →
tastytrade SDK's `Session(...)`/`.refresh()`,
`.venv/Lib/site-packages/tastytrade/session.py:345-381`). The SDK's own
`validate_response()` (`.venv/Lib/site-packages/tastytrade/utils.py:253-276`)
raises `TastytradeError` built from the **broker's own response body**
(error code/message), not from the request the bot sent — so with the
*currently installed* SDK version, an auth failure does not appear to echo
`provider_secret`/`refresh_token` back into the exception text. This is a
property of the third-party SDK, though, not of anything this codebase
controls or tests for.

**Risk scenario:** There is no confirmed secret leak today. But there is
zero defense-in-depth here: any future SDK version, any different exception
type (e.g. a raw `httpx` connection error whose `repr()` can include
request state in some versions/configs), or a future adapter change would
silently start leaking through this exact code path — to an endpoint that
requires no authentication at all, reachable by any other local process or
account on the machine, and easy to accidentally paste into a support
ticket/chat log during a live incident (the operator's own recent
`LIVE-TEST-LOG-2026-07-09.md`-style debugging habit).

**Proposed improvement:** Wrap credential-adjacent `repr(exc)` call sites
(the ones downstream of `comp.connect`/broker auth) in a small allow-list
redactor (e.g. strip anything matching the provider secret/refresh token by
value, or simply truncate/classify to an error *type* name rather than the
full message for auth-stage failures) before it reaches `broker_error`/
alert context. **Effort: small.**
**APPROVE / REJECT:** ______

---

### F5 — HIGH — origin check omits Referer; NFR-06(2) is half-implemented

**Evidence:** `backend/src/meic/adapters/api/app.py:73-96`
(`origin_allowed`) inspects only `request.headers.get("origin")`
(`app.py:217`) / `sock.headers.get("origin")` (`app.py:259`). Doc 05 §7a's
NFR-06(2) text: "reject any mutating request... whose **Origin/Referer** is
not the panel's own host" (`spec/05-architecture-ddd.md:156`). Referer is
never read anywhere in `app.py`.

**Risk scenario:** Modern browsers reliably send `Origin` on cross-origin
POST/fetch, so exploiting the gap needs a client that omits Origin but still
somehow drives a browser-authenticated request (uncommon, but the spec's own
text anticipated exactly this gap by naming Referer as a second signal).
Low practical exploitability today; worth closing because the code's
`origin_allowed` docstring documents the loopback/DNS-rebinding defense in
detail but silently drops half of what NFR-06(2) actually asks for.

**Proposed improvement:** When `Origin` is absent, fall back to checking
`Referer`'s host the same way, before treating the request as "not a
browser." **Effort: small.**
**APPROVE / REJECT:** ______

---

### F6 — MEDIUM — token comparison is not constant-time

**Evidence:** `backend/src/meic/adapters/api/app.py:223`:
`if api_token and request.headers.get("x-api-token") != api_token:`

**Risk scenario:** A timing side-channel against a plain `!=` string
comparison. On a localhost-only bind this requires an attacker who can
already measure request timing from the same machine — a low-value
attack surface here, but it protects the same token that gates
Stop-Trading/Flatten.

**Proposed improvement:** `hmac.compare_digest(header_value or "",
api_token)`. **Effort: trivial.**
**APPROVE / REJECT:** ______

---

### F7 — MEDIUM — no password-strength policy on `MEIC_USER_PASSWORD`

**Evidence:** `backend/src/meic/adapters/api/server.py:651-653`:
```python
token = env.get("MEIC_USER_PASSWORD")
if not token:
    raise RuntimeError(...)
```
Any non-empty string — `"1"`, `"a"` — is accepted as the token.

**Risk scenario:** Low on a single-operator localhost box, but this token
is the only thing standing between "any local process" and
Stop-Trading/Flatten/order-submission once set. A trivially short token
also weakens the (already non-constant-time, F6) comparison's practical
security margin further.

**Proposed improvement:** A soft minimum-length check (e.g. reject <12
chars) at the same `if not token` guard, with a clear error message.
**Effort: trivial.**
**APPROVE / REJECT:** ______

---

### F8 — MEDIUM — token kept in `localStorage`

**Evidence:** `frontend/src/api.ts:12-21` (`TOKEN_KEY = "meic_api_token"`,
`localStorage.getItem/setItem`).

**Risk scenario:** Any script-injection into the panel origin (a
compromised npm dependency shipped in a future `frontend/dist` build, a
malicious browser extension with page access, etc.) can read
`localStorage` and exfiltrate the token to a remote server — distinct from,
and in addition to, that same XSS already being able to act directly
in-page. An httpOnly cookie set by the backend would not be readable by
injected JS at all, closing the *exfiltration-to-a-remote-attacker* half of
this risk (it wouldn't stop in-page abuse, since that already has `fetch`).
`npm audit --production` currently reports 0 vulnerabilities in the shipped
dependency tree, so this is a defense-in-depth item, not a response to a
known compromised package today.

**Proposed improvement:** Consider issuing the token as an httpOnly,
`SameSite=Strict` cookie from a login-style endpoint instead of
client-readable `localStorage`, OR explicitly accept the current
tradeoff (simpler, no session/cookie machinery, matches "curl fallback"
design) and document the residual risk. **Effort: medium** if changed
(touches auth flow + CORS/cookie handling); **effort: none** if the
operator simply accepts the tradeoff.
**APPROVE / REJECT:** ______

---

### F9 — MEDIUM — GETs and the WebSocket feed carry no token even when one is configured

**Evidence:** `app.py:212-225` — the security middleware only inspects
`request.method in ("POST","PUT","DELETE","PATCH")`; every `@app.get(...)`
route (`/state`, `/entries`, `/report`, `/activity`, `/schedule`,
`/alerts` at `server.py:821-823`, `/broker/health`, `/reconcile`,
`/reports/*`) and the `/ws` WebSocket (`app.py:254-272`) require only that
the Origin (when present) match, never the token — confirmed intentional by
several docstrings ("GETs are origin-open like every other read model").

**Risk scenario:** A non-browser local client (any script, scheduled task,
or other OS-level process/account on this machine) sends a plain HTTP GET
with no `Origin` header — explicitly allowed by `origin_allowed(None) →
True` — and reads full current positions, net credit/P&L per entry, the
**future** entry schedule (times, sizes, stop settings), critical alerts
(including any `UNPROTECTED`/naked-position alerts), and reconciliation
mismatches, all without ever presenting the configured `MEIC_USER_PASSWORD`.
On a genuinely single-operator personal machine with no other untrusted
local accounts this is low severity, but it is a materially different
posture than "the token gates the panel" — it gates only the ability to
*act*, not to *observe* the full trading picture.

**Proposed improvement:** No change needed if the operator's threat model
is "no other untrusted process/account on this machine" (a common and
reasonable stance for a personal box) — but worth an explicit
APPROVE/REJECT since it's a real, if narrow, gap. If desired: require the
token on GETs too whenever one is configured, with a one-time WS
handshake message carrying it (since WS can't send custom headers from a
browser easily) instead of relying on Origin alone.
**Effort: medium** (WS token handshake needs a small protocol addition).
**APPROVE / REJECT:** ______

---

### F10 — MEDIUM — stale Docker artifact with an old, unaudited entrypoint

**Evidence:** `docker ps -a` on this host shows container `meic-bot`
(image `meic-bot:latest`, created 2026-07-08T21:52:33Z, `Exited (137)`).
`docker inspect meic-bot` shows:
- `Cmd = ["uvicorn", "meic.web:app", "--host", "0.0.0.0", "--port", "8000"]`
  — a **different, older** module path than the current
  `meic.adapters.api.server:paper_app`/`live_app` (`Dockerfile:29-30`
  currently uses the new path), meaning this container was built from a
  pre-refactor version of the code.
- `PortBindings: {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8001"}]}`
  — bound to loopback only, not network-exposed, and on port 8001, not the
  documented 8010.
- `RestartPolicy: {"Name": "unless-stopped"}` — will auto-restart on a
  Docker daemon restart unless it was the last thing explicitly
  `docker stop`-ped.

The current `docker-compose.yml`/`Dockerfile` (host `127.0.0.1:8010:8000`,
container binds `0.0.0.0` only *inside* the container, per
`Dockerfile:28` and `docker-compose.yml`'s own comments) are correctly
loopback-scoped. This finding is about the **leftover artifact**, not the
current compose config.

**Risk scenario:** Loopback-only, so no remote exposure today. The
practical risk is confusion during an incident (a second, out-of-date panel
answering on 8001 with unknown/unaudited security properties from an older
code revision) and the possibility it silently reappears after a host
reboot or Docker Desktop restart without anyone noticing it's not the
current build.

**Proposed improvement:** `docker rm meic-bot` (and prune the stale
`meic-bot:latest` image layer that predates the module rename) as routine
cleanup; consider a `docker compose down --remove-orphans` habit after
Dockerfile entrypoint changes. **Effort: trivial** (this is a cleanup
action, not a code change — flagged here for visibility, not proposed as
something this review performs).
**APPROVE / REJECT:** ______

---

### F11 — MEDIUM — no operator-facing emergency runbook for the curl/token kill-switch fallback

**Evidence:** NFR-06(4) (`spec/05-architecture-ddd.md:156`) requires "the
Stop-Trading/Flatten-All curl fallback documentation (UI-09/17) includes
the token header whenever a token is set." `grep -rl curl -- *.md` at repo
root finds it only in `spec/04-test-cases.md`, `spec/03-use-cases.md`, and
`spec/05-architecture-ddd.md` — all read-only/hash-locked spec prose, not
operational documentation. `DEPLOY.md` (150 lines, reviewed in full),
`HANDOFF.md` (24 lines), and `README.md` (14 lines) contain no curl
example, no emergency section, and no mention of Stop-Trading/Flatten at
all outside the UI.

**Risk scenario:** The actual mechanism is sound — `origin_allowed(None) →
True` (`app.py:92-93`) means a curl request with the right
`x-api-token` header reaches Stop-Trading/Flatten even with a cleared
browser/localStorage. But if the operator's browser state is gone
*during a live incident* (the exact moment this fallback exists for), the
runbook they'd need to remember or reconstruct the exact command lives only
inside spec test-case prose they are contractually forbidden from editing
and unlikely to open under pressure.

**Proposed improvement:** Add a short "Emergency: Stop Trading /
Flatten without the browser" section to `DEPLOY.md` (not `spec/`, which
stays read-only) with the exact copy-pasteable curl commands, sourcing the
token from `.env` by name (never printing/hardcoding its value in the doc).
**Effort: trivial** (documentation only, zero code change).
**APPROVE / REJECT:** ______

---

### F12 — MEDIUM — dependency pinning / CI supply-chain hygiene

**Evidence:** `requirements-runtime.txt`, `requirements-dev.txt`:
`fastapi>=0.110`, `uvicorn>=0.29`, `websockets>=12`, `tzdata>=2024.1` — all
lower-bound only, no upper bound, no `requirements.lock`/hash pinning.
`backend/requirements.txt`: `tastytrade>=13,<14` — correctly bounded.
`.github/workflows/ci.yml` installs via plain `pip install -r
requirements-dev.txt` with no `pip-audit`/`safety`/Dependabot step; no
equivalent `npm audit` step for the frontend either.

**Risk scenario:** A `pip install -r requirements-runtime.txt` today vs. in
six months can silently resolve to a different, potentially
vulnerable or behavior-changed release of fastapi/uvicorn/websockets — on a
system that places real orders, an unreviewed dependency bump landing
silently is a meaningful supply-chain exposure. (Current state is healthy:
`npm audit --production` → 0 vulnerabilities; `pip list --outdated` shows
only minor, non-security-flagged bumps — see F14.)

**Proposed improvement:** Pin upper bounds (or better, a `pip-compile`/
`uv pip compile` generated lockfile with hashes) for runtime deps, and add
a `pip-audit`/`npm audit` step to the `ci.yml` `guards` job.
**Effort: small.**
**APPROVE / REJECT:** ______

---

### F13 — LOW — unredacted exception logging in the quote-token observation tool

**Evidence:** `tools/observe_quote_token.py:104`:
`_log(fh, "streamer_error", error=repr(e), reconnects=reconnects)` — written
to a plaintext JSONL file under `tools/observations/` (gitignored per
`.gitignore:8`, good). The tool is otherwise careful — it explicitly
redacts token-named session attributes (`"<redacted>" if "token" in attr
else str(val)`, line 83) — but the one `except Exception as e` catch-all at
line 102 has no equivalent guard. I scanned the one existing log
(`tools/observations/quote-token-20260706-174959.jsonl`, 68 lines) for
`bearer|authorization|refresh_token|provider.secret` and `streamer_error`
events: zero matches for either — no evidence of an actual leak in this
run.

**Risk scenario:** Same latent class as F4 — DXLink/websocket connection
exceptions are less likely than HTTP auth exceptions to embed secrets, but
there's no redaction to fall back on if one ever does.

**Proposed improvement:** Same redaction wrapper as F4, applied here too.
**Effort: trivial.**
**APPROVE / REJECT:** ______

---

### F14 — LOW — routine dependency updates available

**Evidence:** `pip list --outdated` in the active `.venv`:
`gherkin-official 29.0.0→41.0.0`, `numpy 2.5.0→2.5.1`,
`pip 24.3.1→26.1.2`, `pydantic_core 2.46.4→2.47.0`, `tzdata 2026.2→2026.3`,
`uvicorn 0.50.2→0.51.0`, `websockets 16.0→16.1`. All minor/patch bumps; none
individually investigated for a specific CVE, but none jumped out as
security-flagged either.

**Proposed improvement:** Routine `pip install -U` sweep on the next
maintenance window. **Effort: trivial.**
**APPROVE / REJECT:** ______

---

### F15 — LOW — CSV export formula-injection surface (currently inert)

**Evidence:** `backend/src/meic/adapters/api/reports.py:331-368`
(`export_csv`) — every written field across the three tables (`daily`,
`entries`, `corrections`) is system-generated: ISO dates, `mode`
("paper"/"live"), Decimal strings, `entry_id` (`{day}#{n}`, bot-formatted),
enum-like `status`/`outcome`/`trust.status` strings. None accept arbitrary
operator free text today, so a cell value starting with
`=`/`+`/`-`/`@` (the classic Excel/LibreOffice formula-injection trigger)
cannot currently occur.

**Risk scenario:** None today. Flagging so it's on record: if a future
slice adds any operator-authored free-text field (e.g. a trade note) to
one of these CSV tables, it must be sanitized (prefix a `'` or strip
leading formula characters) before being written.

**Proposed improvement:** No action needed now; add a sanitizer at the
point any free-text column is introduced to these CSV writers.
**Effort: none today.**
**APPROVE / REJECT:** ______

---

## Things that are already good

- **Two-switch + issuer-guard production opt-in is genuinely
  defense-in-depth, three independent layers**: `MEIC_LIVE_IS_TEST=false`
  alone does nothing; it additionally requires
  `MEIC_ALLOW_PRODUCTION=I_UNDERSTAND_REAL_MONEY`
  (`server.py:657-661`), and even then the adapter independently refuses a
  mis-slotted token by decoding the refresh token's JWT `iss` claim
  (`adapter.py:56-74`, `assert_cert_token`/`assert_production_token`) —
  a cert token in the prod slot (or vice versa) is refused **before any
  network call**, per the constructor at `adapter.py:95-98`.
- **`.env` has never been committed and has no stray backup copies.**
  `git log --all --oneline -- .env` returns nothing; `.gitignore:8` covers
  it; a repo-wide `find -iname "*.env*"` found exactly one file.
- **The read-only reconciler facade is structurally clean.**
  `_BrokerReadFacade` (`server.py:367-388`) exposes exactly
  `positions()`/`day_fills()`/`cash_and_fees()` — no submit/cancel/replace
  is reachable through it, and this is enforced by a dedicated structural
  test (`tests/application/test_report_reconciler_structural.py`, per the
  class's own docstring) asserting `report_reconciler.py` imports nothing
  from `meic.adapters` at all.
- **Origin-allowed logic correctly defeats DNS rebinding.** The
  loopback-host condition tied to `origin == host` (`app.py:73-96`) is
  exactly right: an attacker's own domain resolving to 127.0.0.1 sends
  `Origin == Host`, but that Host string is the attacker's domain, not one
  of `_LOOPBACK`, so it's refused.
- **CI runs on zero secrets.** `.github/workflows/ci.yml` never installs
  broker credentials, never runs `-m contract`, and the `suite` job is
  explicitly informational until the operator flips it required — no
  secret-exposure surface in CI at all.
- **`npm audit --production` → 0 vulnerabilities** in the shipped frontend
  dependency tree.
- **Alert volume is bounded, not unbounded.** `_PanelAlerts.alert()`
  (`server.py:60-63`) caps at 100 entries (`del self._alerts[:-self._cap]`),
  and the day-supervisor's own tick-failure alerting explicitly dedupes to
  "alert once per distinct error" (`server.py:361-364`) rather than
  spamming every interval — a deliberate anti-flood design already in
  place.
- **Config validation fails loud, never silently.** Removed/tombstoned
  config keys (`TOMBSTONE_KEYS`, `TOMBSTONE_KEYS_V151` in
  `validation.py:13-22`) are explicitly rejected rather than silently
  ignored if a stale config ever tries to revive them.
- **File ACLs on this host are unremarkable/correct** — `.env` and
  `data/*.db*` inherit only SYSTEM, Administrators, and the single owner
  account (`icacls` output), no broad "Everyone"/"Users" grant found.
- **The idempotency-key *naming* scheme is sound**, even though its
  wiring has gaps (F3): deterministic composite keys
  (`stop:{entry_id}:{side}`, `entry:{entry_id}`, `lex:{entry_id}:{side}`)
  are exactly the right shape for a future broker-side or client-side
  dedupe check — the gap is that nothing queries by them yet, not that the
  scheme itself is unsafe.

## Approval checklist (for the operator)

| ID | Improvement | Effort | Decision |
|----|-------------|--------|----------|
| F1 | Port the incident-#2 "re-check filled before reprice" fix into `recover_long.py`'s LEX ladder | Small | APPROVE / REJECT |
| F2 | Wire `validate_bind()` into the actual `paper_app()`/`live_app()` startup path | Small | APPROVE / REJECT |
| F3 | Wire `resolve_submit_after_timeout` into retry loops; investigate a broker-side client-order-id field | Medium | APPROVE / REJECT |
| F4 | Redact credential-adjacent exception reprs before they reach `broker_error`/alerts | Small | APPROVE / REJECT |
| F5 | Check `Referer` when `Origin` is absent | Small | APPROVE / REJECT |
| F6 | Constant-time token comparison (`hmac.compare_digest`) | Trivial | APPROVE / REJECT |
| F7 | Minimum-length check on `MEIC_USER_PASSWORD` | Trivial | APPROVE / REJECT |
| F8 | Consider httpOnly cookie instead of `localStorage` for the token | Medium | APPROVE / REJECT |
| F9 | Require token on GET/WS routes too, when one is configured | Medium | APPROVE / REJECT |
| F10 | Remove the stale `meic-bot` container/image | Trivial (cleanup) | APPROVE / REJECT |
| F11 | Add an emergency curl/token runbook section to `DEPLOY.md` | Trivial | APPROVE / REJECT |
| F12 | Pin/lock runtime deps; add `pip-audit`/`npm audit` to CI | Small | APPROVE / REJECT |
| F13 | Redact exceptions in `tools/observe_quote_token.py` | Trivial | APPROVE / REJECT |
| F14 | Routine `pip install -U` sweep | Trivial | APPROVE / REJECT |
| F15 | No action now; sanitize if/when a free-text CSV field is added | None | ACKNOWLEDGE |

## Top 5 improvements (by impact)

1. **F1** — Port the already-fixed incident-#2 reprice-guard into the LEX
   recovery ladder (`recover_long.py`) — this is a proven bug class,
   currently live in a second location.
2. **F2** — Wire `validate_bind()` into the real startup path — the
   spec's "structurally cannot be exposed unauthenticated" claim currently
   has no code behind it.
3. **F3** — Wire the ORD-04 query-before-resubmit check into the stop
   retry loop — closes the one confirmed blind-retry-on-exception path.
4. **F4** — Add a redaction layer around credential-adjacent exception
   reprs before they reach unauthenticated GET endpoints.
5. **F11** — Write the emergency curl/token runbook into `DEPLOY.md` —
   trivial effort, directly serves the "kill switch reachable if
   localStorage got cleared" scenario the operator already designed for
   but never documented outside the spec.
