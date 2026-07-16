# Security & Code Review — 2026-07-17 (overnight)

**Scope:** full security + correctness audit of the MEIC bot, operator-authorized to run
unattended overnight. **Read-only, flag-never-fix** — no code, spec, `.env`, or broker
action was touched. Nothing was changed; this is a findings report for the operator and
the human reviewer to action.

**Method:** five specialized reviewers in parallel (API/auth, secrets/credentials,
external-input/injection, money-path/concurrency, supply-chain/controls), then an
independent adversarial verification pass (different model) that confirmed-or-refuted the
two load-bearing findings with concrete exploit chains. Every reviewer cross-checked the
prior in-repo `SECURITY-REVIEW-2026-07-10.md`, so this report distinguishes *carried-over*
from *new*.

**What this is not:** one automated night complements, it does not replace, the human
reviewer or a live `pip-audit`/`npm audit`. Dependency-CVE statements are training-cutoff
bound.

---

## TOP-LINE VERDICT — is it safe to arm for real money?

**Yes for a SUPERVISED arm day (operator watching the alert feed), as you have been running.
No actively-exploitable critical vulnerability was found in the process actually wired to
real credentials (`live_app`, loopback-bound, token-gated).** The genuinely dangerous shapes
— a request-time path to real money, command injection over the WebSocket, a double-buy in
the order paths, a credential leak — were each traced and **do not exist**.

**One finding must be fixed before any UNSUPERVISED / walk-away live session:**

> **A — a lost-response on the first order submit can leave a live, stopless iron condor that
> no intraday loop detects until the next restart.** Verified: caught only on reboot, and even
> then only quarantine-alerted, not auto-protected. The trigger is a low-probability network
> event; the blast radius is one entry at up to wing-width max loss, unstopped. The fix is a
> ~1-2h wiring job on code that is *already built and tested*.

If tomorrow's graduation-clock day-one is armed-and-watched, that is consistent with running
now. If the plan is arm-and-leave, fix A first.

---

## Findings, ranked

| # | Sev | Verified | Finding | Live today? |
|---|-----|----------|---------|-------------|
| A | **HIGH** | CONFIRMED (Opus) | First-submit timeout → naked, unrecorded, stopless position until reboot | Latent; low-prob trigger, high consequence |
| B | **MED**\* | CONFIRMED (measured) | Quadratic-regex parse of a hostile .gov response stalls the whole event loop | Gated: needs .gov-origin/TLS compromise, 1×/day, BLS already WAF-blocked |
| C | **HIGH** | CONFIRMED (3 reviewers) | `validate_bind` "non-localhost requires a token" is dead code; `paper_app` ships tokenless | Only bites on a non-loopback/tokenless launch — not the current run |
| D | **HIGH** | CONFIRMED | Unredacted `repr(exc)` on broker-connect failures reaches unauthenticated `GET /broker/health` **and** the durable log | Safe *only* by current SDK behavior, not by code |
| E | **HIGH** | CONFIRMED | No Content-Security-Policy or security headers — no XSS backstop behind the markdown/mermaid render | No live XSS found; blast-radius amplifier |
| F | **MED** | CONFIRMED | CAL-09 unbounded rejection-reason (redirect `Location` header) poisons the append-only journal | Needs .gov-origin compromise |
| G | **MED** | CONFIRMED | Floating Python deps, no lockfile, no hashes, no `pip-audit` in CI — on the stack that places orders | Supply-chain latent |
| H | **MED** | CONFIRMED (2 reviewers) | Non-constant-time token compare + no brute-force throttle | Loopback-gated |
| I | **MED-LOW** | CONFIRMED | DCY-02.3 / concurrent-close TOCTOU can rest a phantom stop on a flat leg → could open an unintended short | Narrow race |
| — | LOW/INFO | — | Inconsistent day-regex on two reconcile routes; `/drill/outage` unbounded seconds; IPv6-loopback origin bug (fails safe); guide-error path disclosure | minor |

\*B is SEVERE in raw CPU impact but MEDIUM in effective priority due to the reachability bar — see the finding.

---

## A — Naked unrecorded position on a first-submit timeout · HIGH · CONFIRMED

**Where:** `application/execute_entry.py:359-361` (first-rung submit, no try/except),
`application/idempotency.py:14` (`resolve_submit_after_timeout` — built, tested, **zero
production callers**), `adapters/tastytrade/adapter.py:188-193` & `adapters/sim/simulated_broker.py:176`
(both ignore the `idempotency_key` the intent carries), `application/attempt_crash.py:92-115`
(the crash callback).

**Chain (independently re-traced and confirmed):** the first order submit is not wrapped in
the query-broker-before-resubmit safety that `protect_position.py` built for *its* path and
whose absence elsewhere it explicitly documents. If `await submit(intent)` raises on a
**client-side timeout while tastytrade nonetheless accepted the order** (the classic
lost-response-after-commit failure), the fire-and-forget attempt task crashes; its callback
finds no `CondorFilled`, so it journals `EntrySkipped(reason="attempt_crashed:…")` and alerts
**"no position was taken"** — with no broker-position reconciliation. The bot's book now says
nothing happened while a live 4-leg SPX 0DTE condor rests at the broker **with no stop**.

**The severity question — caught same-session or only reboot? → only reboot.** Every intraday
loop was checked: the ~15s stop-fill poll only looks at shorts that already carry a journaled
`StopPlaced` (a naked position has none — structurally invisible); the health tick never
enumerates positions for unknowns; RSK-04 is computed from the bot's own journaled entries;
EOD reconcile *does* fetch `positions()` but only runs at 16:15 ET — **after** 0DTE expiry, so
it corrects after the fact and protects nothing. Only `reconcile_on_boot` enumerates unknown
positions, and it fires on process start / `/broker/connect` only — and even then classifies
the legs FOREIGN → **quarantine alert, not an auto-placed stop** (the `OwnershipLedger` it
compares against is empty in production). Net: **hours of naked exposure until restart, which
for a 0DTE position usually comes after expiry.**

**Trigger frequency:** low per-entry (needs a lost response on the single `place_order` POST),
but non-negligible over months of daily entries, with **zero mitigation** despite the ORD-04
helper being built and merely unwired.

**Recommendation (do not implement without operator approval — freeze):** wire
`resolve_submit_after_timeout` (or `protect_position`'s `_find_resting` scan) into the
first-submit path of the entry ladder, and likewise LEX/decay/watchdog first submits and
CloseEntry's bare-submit branches; on a submit exception, query `working_orders()`/`positions()`
before journaling a skip. **Priority: before any unsupervised live session.**

## B — Event-loop stall via quadratic regex on a hostile .gov response · SEVERE mechanism / MEDIUM effective · CONFIRMED (measured)

**Where:** `adapters/calendar_sources/bea.py:38`, `bls.py:42` — `re.compile(r'…(.*?)…', re.S)`
row parsers.

**Measured** (regex literal only, throwaway process): cleanly quadratic — 64KB→1.8s,
232KB→~20s, 400KB→63s; extrapolated to the 5MB body cap ≈ **~2.7h of CPU**. Because `parse()`
is synchronous CPU work, the module's own `asyncio.wait_for` per-source/total budgets **cannot
preempt it** — a timeout only fires at an await point, and there is none inside the regex. The
refresh shares the uvicorn event loop with the trading tick, so a single hostile response
freezes entry-laddering, fill detection, and stop placement for the duration.

**Why it's MEDIUM effective, not arm-blocking:** `fetch_text` pins https + exact host and
refuses cross-host redirects, so the input must come from the **genuine .gov origin over valid
TLS** — a CDN/origin compromise or TLS/DNS MITM with a valid cert. `should_run` gates to **one
attempt per source per ET day**. **BLS is currently WAF-403'd** on every GET, so its worse
regex never runs today; **BEA (and FOMC, same shape, unflagged but wired) is the live path.**
CAL-07 fail-open guarantees this can never *block* trading — but "cannot stall the trading
loop," which the spec explicitly promises, does not hold.

**Recommendation:** replace the whole-document lazy-DOTALL scan with FOMC's linear
anchor-then-slice pattern (`fomc.py:118`), or run `parse()` in a thread / behind an
input-shape guard. Apply to `bea.py`, `bls.py`, **and** `fomc.py`. Fix on normal cadence.

## C — `validate_bind` safety guarantee is unenforced dead code · HIGH · CONFIRMED (3 reviewers)

`config/validation.py:86` (`validate_bind`) is called only from the inert `POST /config` path,
never at process boot; `paper_app()` ships `api_token=None` and nothing stops `--host 0.0.0.0`.
The ratified docs (`spec/12`, `DEPLOY.md`) describe "the panel cannot be exposed unauthenticated,
structurally" — that claim is not wired. A tokenless non-loopback launch of `paper_app` (or any
future composition) = full unauthenticated command access. **Not the current run** (`live_app`,
loopback, token-gated), but the exact gap the threat model names. Carried over as **F2** from
2026-07-10, re-verified still open. Fix: call `validate_bind` at real `--host` resolution in
both factories.

## D — Unredacted exception repr reaches an unauthenticated endpoint + the durable log · HIGH · CONFIRMED

`server.py` (~10 sites) does `broker_error = repr(exc)`, returned by `GET /broker/health` —
which the middleware does **not** gate (GETs are unauthenticated by design) — and, since the
2026-07-14 logging change, **also written to `logs/meic-*.log`**. If any future SDK version or a
raw httpx error ever embeds outbound request state (which can carry the secret/refresh token) in
its repr, it lands in an unauthenticated response and on disk. **Currently safe only because the
pinned SDK builds errors from the response body, not the request** — a property of a third-party
version, not a code guarantee. Carried over as **F4**. **Fix before any `TT_PROD_*` cutover:**
a redactor that classifies credential-adjacent exceptions to type only.

## E — No Content-Security-Policy / security headers · HIGH · CONFIRMED

No CSP, `X-Content-Type-Options`, `X-Frame-Options`, or `Referrer-Policy` anywhere. The panel
renders markdown + mermaid + live operator/broker data. The rendering *today* is careful
(mermaid `securityLevel:strict`, no `rehype-raw`, react auto-escaping — all verified clean, see
below), but with no CSP backstop, any future regression (a dep bump, a new `dangerouslySetInnerHTML`,
a compromised transitive package) has unconstrained blast radius — inline script, token
exfiltration (the auth token also lives in `localStorage`, per 07-10 F8). Recommend
`default-src 'self'; script-src 'self'; connect-src 'self' ws://127.0.0.1:*` + `nosniff`.

## F — CAL-09 unbounded rejection-reason journal poisoning · MEDIUM · CONFIRMED

`calendar_store.record_refresh_rejected` (calendar_store.py:180) appends `reason` verbatim, no
bound — contradicting the module's own stated "no unbounded scraped text in the journal"
invariant (which *is* implemented for the success path). The concrete vector: a compromised
allowed origin returns a redirect whose `Location:` header is echoed, unbounded, through
`WrongHostRefused` → `fetch_failed:{exc!r}` → the append-only journal (grows forever, once/day)
and the alert ring + log. Not rendered as raw HTML anywhere (so journal/log poisoning + disk
growth, **not** XSS). Fix: apply the success-path truncation to `reason`, and cap the URL in the
exception messages at construction.

## G — Floating Python deps, no lockfile/hashes, no CI vuln scan · MEDIUM · CONFIRMED

`tastytrade>=13,<14` is correctly bounded (best-pinned = highest-trust, good). But
`fastapi`/`uvicorn`/`websockets`/`tzdata` are lower-bound-only, no `requirements.lock`, no hashes,
and `tastytrade` itself floats its transitive `httpx`/`websockets`/`pydantic` minimums. A
compromised/yanked release of the HTTP/WS stack that carries every order auto-resolves on the
next `pip install`, with no `pip-audit` in CI to catch it. (Frontend is better: `package-lock.json`
committed, SRI-hashed, `npm ci` verifies.) Fix: hash-locked runtime requirements + `pip-audit` in
the `guards` job.

## H — Non-constant-time token compare + no throttle · MEDIUM · CONFIRMED (2 reviewers)

`app.py:514` `header != api_token` short-circuits (timing side-channel); no rate-limit/lockout
anywhere, and `POST /auth/check` plus every 401 is an oracle. Loopback-gated today; matters on any
non-loopback bind. Fix: `hmac.compare_digest` + attempt throttling. Carried over as F6/F7.

## I — DCY-02.3 / concurrent-close TOCTOU rests a phantom stop · MEDIUM-LOW · CONFIRMED

`server.py:1791` / `decay_watcher.py:160`: an `await` gap between the `still_resting` snapshot
and the reinflation guard's cancel+submit; the guard checks `fills_since` (a fill) but not
*replaced-away* (a cancel), and ignores `cancel()`'s return. If a concurrent close replaces the
buyback in that window, the guard rests a **new stop-market on a now-flat leg** — which, if the
strike is revisited, opens an unintended naked short. Narrow (needs interleave within ~1-2s), no
alert for this outcome. Fix: re-confirm the leg is open (position truth) before re-protecting,
mirroring the existing `_long_still_held` pattern.

---

## Verified CLEAN — evidence of safety, not padding

- **No request-time path to real money.** Production double opt-in (`MEIC_LIVE_IS_TEST` +
  `MEIC_ALLOW_PRODUCTION`) is strictly boot-env; no HTTP handler touches it. `/mode-switch` is a
  cosmetic label — `entries_enabled()` never reads `trading_mode`; broker wiring is fixed at
  construction. `assert_cert/production_token` independently decode the JWT issuer and refuse a
  mis-slotted token before any network call.
- **`/ws` is strictly read-only** (received text discarded, no command path); Origin-checked.
- **DNS-rebinding correctly defended** (Origin must equal scheme://host *and* host be loopback).
- **No path traversal** in `/guide` /`/getting-started` (fixed repo paths; `spec_root` is a
  constructor kwarg, never HTTP-reachable). The served text is proven byte-for-byte a substring of
  the hash-locked spec, and an adversarial planted-`.env` test proves neither endpoint ever serves
  a secret.
- **Credentials never persisted or journaled.** `.env` loader is non-logging; broker creds live
  only as private adapter attributes; **git history has never contained `.env`** (`.gitignore`
  covered it from the first commit) and a full-history high-entropy scan found only npm SRI hashes
  and obviously-fake test sentinels. Boot logging proven not to leak secrets by a real pinned test.
- **No double-buy/double-sell** in the race-guarded paths (CLS-01 replace, LEX reprice, entry
  reprice, DCY-02 buyback-vs-fill). ORD-09a broker-actual pricing is honest everywhere (falls back
  to intent only when the broker record has no price). **OWN-01/OWN-03 scoping is airtight against
  the −$534.46 class** — including the retraction trap. Decimal discipline clean end-to-end; STP-02
  tick+cage math correct; REC-01 journal-first genuine.
- **SSRF hardening is genuinely strong** — exact host equality (not suffix), https-only, single
  validated redirect hop, second redirect refused, `isprintable()` label gate that also blocks
  U+202E Trojan-Source spoofing.
- **Frontend XSS clean** — the only innerHTML is mermaid-strict output from hash-locked spec text;
  no `rehype-raw`; all operator/scraped strings render as auto-escaped JSX.
- **Integrity machinery is unusually strong** — the NFR-07 wiring/constant-signal audit (behavioral
  proofs, honest heuristic-limit labeling), the spec hash-lock, and CODEOWNERS are well above
  typical rigor for a project this size.

---

## Operator-action items (outside the code — only you can verify)

1. **GitHub branch protection.** The entire hash-lock/CODEOWNERS root-of-trust depends on branch
   protection actually *requiring* the `guards` + `suite` CI jobs and CODEOWNERS review. That
   setting lives in GitHub Settings, invisible to this audit. **Confirm it is enabled** — without
   it, a red or skipped CI run could merge and the whole integrity design is moot.
2. **Run `pip-audit` and `npm audit`** against the exact installed versions — the only authoritative,
   current CVE answer (this review is training-cutoff bound).
3. **Decide the disposition of the carried-over 2026-07-10 items** (F2/F4/F5/F6/F7/F8/F9) — none
   regressed, none were silently fixed; several (F2=C, F4=D, F6=H here) are re-confirmed still open.

---

## Continuity vs 2026-07-10

- **Fixed since:** F1 (LEX reprice-without-reconfirm), commit `6e70603` — verified.
- **Still open, re-confirmed tonight:** F2 (→C), F4 (→D), F5, F6/F7 (→H), F8, F9.
- **New tonight:** A (naked-position-on-timeout), B (ReDoS stall), E (no CSP), F (CAL-09 reason
  poisoning), G (dep pinning), I (DCY TOCTOU) — the CAL-09 items are new because CAL-09 itself is
  new since the last review.

---

## Bottom line

The money-critical core is sound and has clearly been hardened by real incidents. Nothing found
mandates staying flat for a **supervised** arm. The single item that should gate an **unsupervised**
live session is **A** — a small wiring job on already-built code. Everything else is defense-in-depth,
deployment posture, or a compromised-.gov threat model — real, worth fixing on cadence, none
arm-blocking today. Fixes are the operator's and the reviewer's to authorize; the freeze holds and
nothing here was changed.

*Generated by an overnight automated review (5 parallel reviewers + independent verification).
Complements, does not replace, human review and a live dependency scan.*
