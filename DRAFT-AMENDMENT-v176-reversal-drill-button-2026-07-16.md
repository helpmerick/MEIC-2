# DRAFT — Amendment proposal: v1.76 reversal, the drill button is removed

**Status: amendment-proposal for the adviser.** The spec is hash-locked;
nothing in it has been edited. Find/replace deltas below are quoted verbatim
from the CURRENT ratified texts (spec at v1.79, 2026-07-16).

## The never-silent record: the order and the ruling it reverses, side by side

**The operator's order tonight (2026-07-16, verbatim):**

> "remove it completely, the wiring can stay but the button must go."

This is an **informed reversal**: the operator was shown the v1.76 ruling's
own "Removal was rejected" text before repeating the order.

**The v1.76 ruling being reversed (spec/README changelog, verbatim):**

> v1.76 changes (operator-ruled UI placement, one line): the UC-12
> outage-drill button moves into a COLLAPSED "Operational tools" disclosure
> on the trading tab — hidden from new users by default, discoverable when
> DOC-06's first-run sequence sends them there, typed-DRILL gate unchanged.
> Removal was rejected: the drill is a mandated first-run and
> post-API-change proof; a button the Getting Started guide commands cannot
> not exist. DOC-06 cross-reference updated.

The operator overriding the operator's own prior ruling is sovereignty, not
a breach — but it is never silent (the calendar's CAL-06 principle applied
to the rulebook itself). This file IS the never-silent half.

**Scope of the order:** the BUTTON goes; the wiring stays. The drill
machinery survives whole — the auth-gated endpoint (`POST /drill/outage`,
guarded by the panel password on the NFR-06a header convention and the
typed-DRILL confirmation), the disconnect/evidence/clean-reconcile pipeline,
the reports record, and the NFR-07 wiring registration. What the order does
NOT itself decide is what happens to DOC-06's first-run drill MANDATE — the
Getting Started guide currently commands every new operator to run the
drill once before trusting live, via a button that will no longer exist.
That decision is presented as two honest options below, marked [ADVISER].

## Known-truthful staleness, listed explicitly

The UI removal ships ahead of ratification under operator sovereignty.
Until this amendment lands and the tabs re-render, the running panel and
the rendered docs will disagree — and because the running build's spec
version still equals the tabs' stamps (both v1.79), **DOC-05's mismatch
banner will NOT fire**: the staleness below is known and accepted, not
banner-covered. Specifically, until ratification:

1. The **How it works** tab, Chapter 7, "The outage drill" paragraph tells
   the reader the button "lives inside a collapsed Operational tools
   section" and "never goes away" — a button the panel no longer shows.
2. The **Getting started** tab, first-run step 9, says "Open 'Operational
   tools'... and run the outage drill" — pointing at the removed button.
3. spec/03's UC-12 Flow line says the operator "presses 'Simulate bot
   outage'" and its v1.76 Placement paragraph says "It is never removed."

## Every [ADVISER] marker in this file

- **[ADVISER-1]** (Deltas 3, 4, 5, and the changelog draft): pick the
  invocation path — **(a)** document the endpoint command in Getting
  started, or **(b)** drop the first-run drill mandate from DOC-06
  entirely. The operator's order constrains only the BUTTON; (b) is a
  bigger ruling (the drill was a mandated proof) and needs its own
  operator decision.
- **[VERIFIED — was ADVISER-2, closed 2026-07-16]** (Delta 5, option (a)
  only): the request shape is now code-verified, coordinator-supplied:
  endpoint `POST /drill/outage` (app.py:1122); body field `confirmation`,
  which in LIVE mode must carry the typed word `DRILL` — absent or wrong,
  the drill is REFUSED with a 400 and never run (the v1.56 typed-DRILL
  gate, app.py:1127–1129); optional body field `outage_seconds` defaults
  to the `drill_outage_seconds` dial. Delta 5's option-(a) one-liner uses
  these real names; the adviser re-verifies against app.py:1122–1133 at
  ratification rather than re-deriving them. No ruling needed — this
  marker is closed.
- **[ADVISER-3]** (Deltas 1 and 4): does the collapsed "Operational tools"
  disclosure itself survive (holding whatever else it contains, now or
  later), or does it go with the button if the drill was its only content?
  The order names the button; the disclosure's fate follows the build.

## Index of deltas

| # | Where | Change |
|---|---|---|
| 1 | spec/03-use-cases.md, UC-12 Post/Placement paragraph | v1.76 placement REVERSED; no UI trigger; machinery/endpoint/NFR-07 remain |
| 2 | spec/03-use-cases.md, UC-12 Flow line | "operator presses" → the real invocation (no UI trigger) |
| 3 | spec/12, DOC-06 rule text (first-run sequence clause) | drill clause re-pointed (option a) or dropped (option b) — [ADVISER-1] |
| 4 | spec/12, guide Chapter 7, outage-drill paragraph | rewritten to the real remaining invocation path — both options |
| 5 | spec/12, GETTING STARTED section, step 9 | rewritten to the endpoint command (a, shape code-verified) or removed (b) — [ADVISER-1] |
| 6 | spec/README changelog | new version entry recording the reversal |

**A version-number note:** deltas below write "v1.80" for the reversal;
as with every stamp, the adviser applies whatever the changelog head
actually is at ratification time.

---

## Delta 1 — spec/03-use-cases.md, UC-12 Placement paragraph

**Find** (the Post line's placement ruling, quoted in full):

```
**Placement (v1.76, operator-ruled):** the drill button lives inside a COLLAPSED "Operational tools" disclosure on the trading tab — hidden by default (a new user's screen shows only trading controls), one click to reveal, typed-DRILL confirmation unchanged behind it. It is never removed: DOC-06's first-run sequence directs every new operator to run it once before trusting live, and it re-runs after API changes.
```

**Replace with:**

```
**Placement — REVERSED (v1.80, operator-ordered 2026-07-16, superseding v1.76):** the drill has NO UI trigger. v1.76 placed the button in a collapsed "Operational tools" disclosure and recorded "Removal was rejected"; the operator, shown that exact text, repeated the order — "remove it completely, the wiring can stay but the button must go" — an informed reversal, recorded here per the never-silent rule. The button is removed from the panel entirely. The drill MACHINERY remains whole and mandatory: the auth-gated endpoint (POST /drill/outage; panel-password header per NFR-06a; typed-DRILL confirmation), the disconnect/evidence/clean-reconcile pipeline, the reports record, and the NFR-07 wiring registration. Invocation is by operator command against the endpoint; DOC-06 states the current invocation guidance.
```

*(If the adviser rules option (b) under [ADVISER-1], the final clause
"DOC-06 states the current invocation guidance" becomes "the first-run
drill mandate is withdrawn per the same ruling" — keep the two texts
consistent.)* **[ADVISER-3]** also lands here: if the disclosure itself is
gone from the build, strike the words "in a collapsed 'Operational tools'
disclosure" from nothing — the replacement text above already avoids
asserting the disclosure exists; verify Chapter 7's option texts (Delta 4)
match the built reality.

---

## Delta 2 — spec/03-use-cases.md, UC-12 Flow line

**Find** (opening of the Flow line — fragment, unique in the file):

```
**Flow:** With an open condor, operator presses "Simulate bot outage" → backend disconnects
```

**Replace with:**

```
**Flow:** With an open condor, operator invokes the drill (v1.80: by command against the auth-gated endpoint — there is no UI trigger) → backend disconnects
```

*(Everything after "→ backend disconnects" in the Flow line is untouched —
the typed-DRILL live-mode gate, the near-trigger warnings, and the
never-touches-orders guarantee all stand.)*

---

## Delta 3 — spec/12, DOC-06 rule text (line ~62), first-run clause — [ADVISER-1]

**Find** (fragment of the DOC-06 rule's section-4 clause, unique):

```
, and the outage drill once before trusting live (found under the collapsed "Operational tools" disclosure on the trading tab, v1.76)
```

**Option (a) — keep the mandate, document the endpoint. Replace with:**

```
, and the outage drill once before trusting live (no panel button exists — v1.80 reversal; run by operator command against the auth-gated drill endpoint, and the Getting-started step shows the exact command, naming no secrets)
```

**Option (b) — drop the mandate (bigger ruling; needs its own operator
decision). Replace with:** nothing — delete the fragment, so the clause
reads "...then calendar import + standing blackout rules; (5) going
live...". Note what (b) gives up: DOC-06 loses its mandated
proof-before-live that stops genuinely rest at the broker (STP-05 / UC-12
— "recommended before first live day" would survive only as UC-12
guidance, not as a Getting-started command).

---

## Delta 4 — spec/12, guide Chapter 7, "The outage drill" — [ADVISER-1, -3]

**Find** (the full paragraph, quoted verbatim from the v1.79 guide):

```
**The outage drill.** A supported way to prove, on demand, that the stops
really do live at the broker and not inside the bot: the operator can
deliberately sever the bot's own connections for a short window and watch the
stop orders keep resting, untouched, the whole time (UC-12). The button that
starts it lives inside a collapsed **"Operational tools"** section on the
Trading tab — tucked out of the way so a brand-new install's screen shows
only ordinary trading controls, one click to reveal, with the same typed
"DRILL" confirmation as before. It never goes away: the Getting Started
guide (Chapter 3 of that tab's own walkthrough) sends every new operator
here to run it once before trusting live mode, and it is worth re-running
after any tastytrade API change.
```

**Option (a) — Replace with:**

```
**The outage drill.** A supported way to prove, on demand, that the stops
really do live at the broker and not inside the bot: the operator can
deliberately sever the bot's own connections for a short window and watch
the stop orders keep resting, untouched, the whole time (UC-12). There is
no button for it anywhere on the panel — the operator ordered it removed —
so it runs by a single typed command sent to the bot on the trading
machine, still guarded by the panel password and a typed "DRILL"
confirmation exactly as before; the Getting started tab shows the exact
command. It is worth running once before ever trusting live mode, and
again after any tastytrade API change.
```

**Option (b) — Replace with:**

```
**The outage drill.** A supported way to prove, on demand, that the stops
really do live at the broker and not inside the bot: the operator can
deliberately sever the bot's own connections for a short window and watch
the stop orders keep resting, untouched, the whole time (UC-12). There is
no button for it anywhere on the panel — the operator ordered it removed —
and running it is a deliberate operator command sent to the bot itself,
still guarded by the panel password and a typed "DRILL" confirmation
exactly as before. It is worth running after any tastytrade API change.
```

---

## Delta 5 — spec/12, GETTING STARTED, step 9 — [ADVISER-1]

**Find** (the full step, quoted verbatim from the v1.79 section):

```
9. **Open "Operational tools"** (a collapsed section on the Trading tab,
   deliberately tucked out of the way so a brand-new install's screen shows
   only ordinary trading controls) and run the **outage drill** at least
   once — it proves, on demand, that your stop orders really do rest at the
   broker and keep working even if the bot itself is disconnected. Do this
   before you ever trust live mode with real money, and again any time
   tastytrade's API changes.
```

**Option (a) — Replace with** (keeps the mandate; the command names no
secret — the password is the operator's own, typed by them into their own
terminal, consistent with the section's handling rule):

```
9. **Run the outage drill** at least once — it proves, on demand, that your
   stop orders really do rest at the broker and keep working even if the
   bot itself is disconnected. There is no button for this: it runs by one
   typed command on the trading machine, guarded by your panel password
   and the typed word DRILL. In a terminal on the trading machine:

   curl -X POST http://127.0.0.1:8010/drill/outage
        -H "X-User-Password: <your panel password>"
        -H "Content-Type: application/json"
        -d "{\"confirmation\": \"DRILL\"}"

   (One line; type your own panel password where the placeholder is —
   exactly like the .env rule in Section 2, it is never written into a
   saved script or pasted anywhere else. Typing the word DRILL in the
   command is the same deliberate confirmation the old dialog required:
   in live mode the bot refuses to run the drill without it. You may add
   "outage_seconds": <10–300> to the body to override the default
   disconnect length.)

   Do this before you ever trust live mode with real money, and again any
   time tastytrade's API changes.
```

*(Request shape is code-verified, not guessed: `POST /drill/outage` is
app.py:1122; the `confirmation` field's typed-DRILL live-mode gate —
refused with a 400 if absent or wrong, never run — is app.py:1127–1129;
optional `outage_seconds` defaults to the `drill_outage_seconds` dial.
Adviser: re-verify against app.py:1122–1133 at ratification.)*

**Option (b) — Replace with:** nothing — delete step 9 (it is the last
step; no renumbering needed). This is only coherent together with Delta 3
option (b): the DOC-06 completeness contract currently REQUIRES the drill
in the first-run sequence, so the step cannot be silently deleted while
the contract still mandates it.

---

## Delta 6 — spec/README changelog entry (draft)

**Insert** at the changelog head (version number = the head at
ratification; drafted as 1.80):

```
- Version: 1.80 — 2026-07-16
- v1.80 changes (operator order, an INFORMED REVERSAL of v1.76): **UC-12 drill button REMOVED from the panel entirely** — the operator was shown v1.76's "Removal was rejected" ruling and repeated the order: "remove it completely, the wiring can stay but the button must go." The drill machinery survives whole and mandatory: auth-gated POST /drill/outage (panel-password header per NFR-06a, typed-DRILL confirmation), the disconnect/evidence/clean-reconcile pipeline, the reports record, NFR-07 registration. The UI removal shipped ahead of ratification under operator sovereignty — never silent: recorded in the amendment draft and here. DOC-06's first-run drill step [option (a): re-pointed at the documented endpoint command, naming no secrets | option (b): WITHDRAWN — the mandated proof-before-live is given up, ruled explicitly]; guide Chapter 7 and Getting-started step 9 rewritten to the real invocation path; UC-12 Flow/Placement text updated. Sovereignty precedent: an operator reversing the operator's own prior ruling is recorded side-by-side with the text it reverses, never applied silently.
```

*(The adviser strikes whichever [ADVISER-1] option the operator does not
rule, in both this entry and Deltas 3/4/5.)*

---

## Anchor verification

All five find-anchors above were checked byte-for-byte against the current
v1.79 spec texts on 2026-07-16 (spec/03-use-cases.md lines 135–136; spec/12
lines ~62, ~612–623, ~906–912) — each matches and is unique in its file.
