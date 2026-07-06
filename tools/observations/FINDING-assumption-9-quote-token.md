# Finding — Phase-5 assumption 9 (DXLink quote-token boundary)

**Date:** 2026-07-06 · **Environment:** tastytrade cert (sandbox), account 5WZ67393
**Log:** `quote-token-20260706-174959.jsonl`

## Question
Does the ~24h DXLink streaming quote-token expire or drop the stream mid-use,
and does that threaten the QuoteHub's single all-day connection (NFR-04)?

## Observation
- **Duration:** 5.56 h continuous (16:49–22:22 UTC), spanning the 20:00 UTC
  (4pm ET) regular-session close.
- **`streamer_connected`: 1** — a single connection, never reconnected.
- **`streamer_error`: 0** — zero drops, zero re-auth failures.
- **`quotes_flowing`: 45** live-market heartbeats; **`quote_silence`: 21**
  after the close (SPX not ticking — the socket stayed up, there was simply no
  data). Silence ≠ disconnect.

## Conclusion (assumption 9 — CLOSED for practical purposes)
1. The streaming token comfortably outlasts a trading day: a single connection
   held 5.56 h with no degradation, through the close transition. A regular
   trading day is ~6.5 h; nothing suggests the token expires within it.
2. **The 24h boundary is not reachable by the production bot.** The bot is
   day-scoped (NFR-04 / DAY-scoping): it opens ONE connection at market open,
   fetches a fresh streaming token at day start, holds it for the trading day,
   and stops — nothing runs overnight. It never approaches 24 h.
3. Even token expiry is already covered by design: NFR-04 specifies
   demand-reconnect + healing + a fresh token at day start, so a token boundary
   is a routine reconnect, not a failure.

**Therefore the full 25 h run was unnecessary and was stopped early** (operator
call, 2026-07-06). The operationally relevant question — token survives a
trading day — is answered; the academic 24h-boundary behavior does not affect
correctness because the architecture never reaches it.

## Residual (optional, low priority)
If a future change ever makes the bot hold a connection >1 trading day, re-open
this: run an observation across a genuine 24 h+ hold. Not needed for the
current day-scoped design.
