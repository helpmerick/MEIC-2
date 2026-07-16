# DRAFT — DOC-12 delta package, v1.72 → v1.77

**Status: amendment-proposal for the adviser.** `spec/12-how-it-works.md` is
hash-locked; nothing in it has been edited. This file lists, chapter by
chapter, the exact prose the adviser should paste into the ratified guide so
its rendered content matches spec v1.77 instead of the v1.72 it is currently
stamped with. Every delta below is sourced only from `spec/README.md`'s
v1.73→v1.77 changelog entries and the underlying rule text in
`spec/01-strategy-rules.md`, `spec/03-use-cases.md`, `spec/05-architecture-ddd.md`,
`spec/11-trading-calendar.md`, and `spec/12-how-it-works.md` itself. The
DOC-02 language standard (plain English, zero-background reader, rule IDs
only as parenthetical citations) is maintained throughout. The house
$4.00-credit example is **not** changed — no v1.73–v1.77 ruling altered its
math; see Delta 5 for the one honest sentence that was added instead.

## Index of every delta, and every [ADVISER] marker

| # | Chapter | Source ruling | Change |
|---|---|---|---|
| 1 | Version stamp line | — | "describes spec v1.72" → "describes spec v1.77" |
| 2 | New paragraph before Ch. 1 | UI-29/30/32 (tab layout, v1.75) | Add the one place tabs are enumerated, naming Getting started |
| 3 | Chapter 3 | NFR-06a | **No change** — already fully reflected since v1.72; nothing in v1.73–v1.77 touched it |
| 4 | Chapter 5 | ORD-09a (v1.74) | Add the one honest "what gets recorded" sentence |
| 5 | Chapter 6 | ORD-09a (v1.74) | Add matching cross-reference sentence to the long-recovery-ladder paragraph |
| 6 | Chapter 7 | v1.76 (drill placement) | Rewrite the outage-drill paragraph to state where the button now lives |
| 7 | Chapter 8 | REC-01 journal-first (v1.74) | Add one sentence to the restart-story paragraph |
| 8 | Chapter 9 | UI-31 (v1.73) | Add a new paragraph on the activity feed's day headers and hover explanations |
| 9 | Chapter 10 | CAL-09 (v1.77) | Amend the opening paragraph + insert a new paragraph on daily auto-refresh |
| 10 | Master flowchart | DOC-05 (v1.77, readable diagrams) | Add one sentence noting the flowchart (and every diagram) is click-to-zoom |
| 11 | Chapter 6 | Operator request 2026-07-16 (LEX-01→09, LEX-07, EC-LEX-04/08, DCY-03) | Add a "What LEX means" definition passage at the top of the long-recovery-ladder entry |

**[ADVISER] markers in this file:** none. Every fact used below is stated
directly in the ratified spec text cited beside it — there was nothing to
guess at for a delta-only pass. (Contrast with the DOC-06 draft, which is new
ground and carries several.)

---

## Delta 1 — Version stamp

**Find** (the guide's own header line, immediately above "The master flowchart"):

```
# THE GUIDE (ratified content, v1.72 — describes spec v1.72; DOC-05 stamp)
```

**Replace with:**

```
# THE GUIDE (ratified content, v1.72 — describes spec v1.77; DOC-05 stamp)
```

*(Only the "describes spec vX.YY" clause is in scope for this delta, per the
commission. Note for the adviser: the "ratified content, v1.72" clause
records when the base prose was last ratified as a whole; once this delta
package itself is ratified, you may also want to bump that to v1.77 or to
whatever version this ratification pass lands on — that's a judgment call
for the ritual, not something this pass changes unilaterally.)*

Separately, worth flagging (not a requested delta, just an observation): the
document's own masthead line near the very top —

```
**v1.71, operator-commissioned 2026-07-15.** A complete, non-technical
explanation of everything the bot can do...
```

— is also stale (the Rules section above it already documents DOC-06 at
v1.75, the drill relocation at v1.76, and DOC-05's zoom rule at v1.77). Since
the commission only named the "describes spec v1.72" stamp, this is left for
the adviser's judgment rather than changed here.

---

## Delta 2 — Where the tabs are enumerated (new, v1.75 UI-32)

The current guide prose never actually lists the SPA's tabs anywhere in the
chapters — only the rule text in spec/03 does. Since DOC-06 commissions "one
sentence wherever tabs are enumerated," and no such sentence currently
exists in the guide body, add the enumeration itself, once, right after the
flowchart and before Chapter 1:

**Insert** (new paragraph, immediately before `## 1. What the bot trades, and the shape of the trade`):

```
This guide is itself one of five tabs across the top of the control panel:
**Trading** (composing and watching the day), **Results** (the dashboard,
Chapter 9), **Calendar** (Chapter 10), **How it works** (this guide), and
**Getting started** (a separate, one-time walkthrough for setting the bot up
on a new machine — this guide assumes that part is already done and picks up
from there) (UI-29, UI-30, UI-32).
```

---

## Delta 3 — Chapter 3 / the password node — NO CHANGE

Checked against every v1.73–v1.77 changelog entry: NFR-06a (the panel
password and the two-switch production opt-in) was ratified in **v1.72**,
and the guide's Chapter 3 already states all of it correctly — the mandatory
`MEIC_USER_PASSWORD`, the Locked/Unlocked control, and the two-switch
production ritual (`MEIC_LIVE_IS_TEST` + `MEIC_ALLOW_PRODUCTION` plus the
issuer assertion) are all present in the current Chapter 3 text and in the
flowchart's `Access` node. Nothing in v1.73 (UI-31), v1.74 (ORD-09a/REC-01),
v1.75 (DOC-06), v1.76 (drill placement), or v1.77 (CAL-09/DOC-05) touches
NFR-06a, the password, or the production switches. **No delta required.**

---

## Delta 4 — Chapter 5, the honest note on what gets recorded (ORD-09a, v1.74)

ORD-09a (v1.74) ruled that every journaled execution price — entry fills,
stop buybacks, long-leg sales, decay buybacks, floor fills, closes — is
always the broker's actual fill price, never an order's limit, rung, or
intent price. Chapter 5's outcome-contract dollars ($3.80 trigger, $20
one-sided profit, ~$360 both-sides loss) are clean, idealized figures built
from the configured percentage and the house example's fill prices — worth
one honest sentence saying that the real, permanent number always comes from
what the broker actually did, not the plan.

**Find** (end of the outcome-contract bullet list in Chapter 5, the paragraph
that currently follows it):

```
An operator can add a small dollar markup to the trigger to pre-credit some of
the expected long-leg recovery, and can switch the trigger math to be based
on each leg's own price instead of the whole trade's total — but the
95%-of-total-credit default above is what produces the clean "small win /
bounded loss / never worse" outcome contract described here (STP-02, STP-02b).
```

**Replace with** (one new paragraph inserted immediately before it, that
paragraph itself left unchanged after):

```
One honest note belongs here before moving on: the dollars above are the
trade's built-in math — target numbers the design is built around. What
actually gets written to the permanent record for every stop, buyback, and
long-leg sale is always the broker's own real execution price, never the
bot's order price or an estimate along the way (ORD-09a) — so the real
figures on a given day can differ slightly, usually in your favor, from the
clean percentages described here.

An operator can add a small dollar markup to the trigger to pre-credit some of
the expected long-leg recovery, and can switch the trigger math to be based
on each leg's own price instead of the whole trade's total — but the
95%-of-total-credit default above is what produces the clean "small win /
bounded loss / never worse" outcome contract described here (STP-02, STP-02b).
```

---

## Delta 5 — Chapter 6, cross-reference in the long-recovery-ladder paragraph (ORD-09a, v1.74)

**Find** (inside "The long-recovery ladder, and its floor orders" in Chapter 6):

```
Every stopped-out long is **always** sold — there is
no "too cheap to bother" threshold, because a residual long is still risk
sitting on the book (LEX-07).
```

**Replace with:**

```
Every stopped-out long is **always** sold — there is
no "too cheap to bother" threshold, because a residual long is still risk
sitting on the book (LEX-07). Whatever price it actually sells at is exactly
the number written to that day's permanent record — never the ladder's
asking price along the way (ORD-09a, Chapter 5).
```

---

## Delta 6 — Chapter 7, outage drill's new location (v1.76)

v1.76 moved the drill button into a collapsed "Operational tools" disclosure
on the Trading tab — hidden from a brand-new install by default, one click
to reveal, found there because DOC-06's first-run sequence sends new
operators looking for it.

**Find** (Chapter 7, "The outage drill" paragraph, in full):

```
**The outage drill.** A supported way to prove, on demand, that the stops
really do live at the broker and not inside the bot: the operator can
deliberately sever the bot's own connections for a short window and watch the
stop orders keep resting, untouched, the whole time (UC-12).
```

**Replace with:**

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

---

## Delta 7 — Chapter 8, restart story (REC-01 journal-first, v1.74)

v1.74 pinned REC-01's internal ordering as journal-first — an event is
durably written to the permanent log *before* anything acts on it or any
in-memory state reflects it, and a failure to write raises rather than
letting the bot carry on believing something that was never actually
recorded. The operator-visible consequence worth stating plainly: there is
no window after a restart where the bot's memory can be lying to it, because
nothing is ever acted on before it's safely written down.

**Find** (opening of the restart paragraph in Chapter 8):

```
**What the bot does when it restarts mid-day:** it rebuilds its entire
picture of the day from its own saved history, then checks that picture
against what the broker actually shows.
```

**Replace with:**

```
**What the bot does when it restarts mid-day:** before acting on anything at
all — a fill, a stop, a new trade — the bot writes it to its own permanent
record first; if that write itself were ever to fail, the bot stops rather
than act on something it can't actually prove happened (REC-01). That
discipline is what makes the rest of this possible: it rebuilds its entire
picture of the day from its own saved history, then checks that picture
against what the broker actually shows.
```

---

## Delta 8 — Chapter 9, the activity feed (UI-31, v1.73)

v1.73 added two usability rulings to the activity feed that the current
guide doesn't describe at all: date headers separating days, and a
plain-English hover explanation on every single event, enforced for
completeness (an event type with no explanation fails the test suite).

**Insert** (new paragraph, appended to the end of Chapter 9, after the
existing "A number is **never silently corrected**" paragraph):

```
**The activity feed** — the running list of everything the bot has done —
groups its entries under a date heading for each day, so two different
trading days are never visually mistaken for one continuous stream; each row
still shows its own time within that day. Hover over any entry and it
explains itself in the same plain language as this guide — including
spelling out any acronym in the row (LEX, TPF, TPT, decay, standdown,
reconcile, and the rest) as if you'd never seen it before, not merely
confirming that something happened. Every kind of event the feed can show
is required to have one of these explanations — an event type with none is
treated as a defect, the same as a chapter of this guide going unwritten
(UI-31).
```

---

## Delta 9 — Chapter 10, calendar auto-population (CAL-09, v1.77)

CAL-09 (v1.77) resolved CAL-01's previously-open question of *how* the
official tier-1 calendars get imported: instead of only an operator-triggered
manual fetch, the bot now fetches them itself automatically, daily, subject
to strict fail-safe rules. This is a real behavior change the guide's opening
paragraph currently describes ambiguously ("imported from official published
schedules" — previously true only via a manual operator action).

**Find** (Chapter 10, opening paragraph, in full):

```
The calendar is a year-view list of scheduled market-moving events — the
Fed's rate decisions, and the government's inflation, jobs, and growth
releases, imported from official published schedules; Fed-speaker
appearances are shown too, but honestly labeled as a best-effort layer, since
no reliable official machine feed exists for them (CAL-01). The calendar
never invents events for a day it has no data for — it shows "no data
imported" instead of a blank that could be mistaken for "nothing happening"
(CAL-02).
```

**Replace with:**

```
The calendar is a year-view list of scheduled market-moving events — the
Fed's rate decisions, and the government's inflation, jobs, and growth
releases, kept up to date automatically; Fed-speaker appearances are shown
too, but honestly labeled as a best-effort layer, since no reliable official
machine feed exists for them (CAL-01). The calendar never invents events for
a day it has no data for — it shows "no data imported" instead of a blank
that could be mistaken for "nothing happening" (CAL-02).

**Kept current on its own, from official sources only.** Once a day, the
bot fetches the official published calendars directly from the Federal
Reserve, the Bureau of Labor Statistics, and the Bureau of Economic
Analysis — the same three government sources a human would check by hand —
and folds in anything new (CAL-09). A few fail-safe rules govern this so an
automatic feed can never quietly make the calendar less trustworthy than a
human-maintained one: a fetch that comes back malformed, empty, or
implausible is thrown out **whole**, never allowed to partially overwrite or
thin out what's already there; if a date that was previously on the calendar
is simply missing from a new fetch, it is never silently dropped — it's
marked **disputed** and flagged for you, and any no-trade tag on it stays in
force until you say otherwise. Every fetch — successful or rejected — is
logged, and the calendar raises a persistent alert if it fails several days
in a row. This automatic refresh can be switched off if you'd rather manage
the calendar by hand entirely; either way, pasting in a table of dates
yourself remains available as a fallback at all times. And to be clear about
what this feature does and doesn't change: it makes the calendar more
convenient and more current, not more authoritative — you're still the one
responsible for knowing what's on the macro calendar; this just does more of
the legwork for you.
```

*(This lands as a new paragraph inserted directly after the amended opening
paragraph, before the "no-trade tags" paragraph that begins "The operator
can tag any day...".)*

---

## Delta 10 — Master flowchart intro, readable diagrams (DOC-05, v1.77)

**Find** (the sentence introducing the flowchart, in full):

```
The full trading day, from boot through the next day self-starting. Every
node uses plain-English chapter vocabulary, not rule IDs, per DOC-04; the
gates that matter most for correctness — the market-halt check and the
calendar blackout check — are called out explicitly, including that they
lean in *opposite* directions on purpose (see Chapters 4 and 10 for why).
```

**Replace with:**

```
The full trading day, from boot through the next day self-starting. Every
node uses plain-English chapter vocabulary, not rule IDs, per DOC-04; the
gates that matter most for correctness — the market-halt check and the
calendar blackout check — are called out explicitly, including that they
lean in *opposite* directions on purpose (see Chapters 4 and 10 for why).
This diagram, and every other diagram in this guide, can be clicked to open
a full-screen view with pan and zoom (scroll or pinch), with explicit zoom
controls, for anyone who finds the default size too small to read
comfortably; the full-screen view closes with a keypress (DOC-05).
```

---

## Delta 11 — Chapter 6, "What LEX means" (operator request, 2026-07-16)

Operator request, verbatim: *"Please make sure we add a section defining
what lex is in the how this works."* The current Chapter 6 entry describes
what the long-recovery ladder **does** (watches/acts/never) but never
defines what "LEX" **is** — the acronym appears in the fill-detector entry
and throughout the panel with no introduction. This delta adds a definition
passage at the top of the ladder's entry, before the existing
watches/acts/never text, which is left intact.

**Acronym expansion, verified against the spec:** the spec never spells the
acronym out letter-by-letter, but it names the mechanism itself in two
places — spec/01's section 6 is titled "**Long leg exit** after short stop
(LEX)", and spec/02's section C is "**Long-exit** edge cases (EC-LEX)"; the
README's doc index likewise lists "long exit" as the LEX prefix's subject.
The passage below therefore says LEX names the *long exit* (the spec's own
title for it) rather than inventing a letter-by-letter expansion. No
[ADVISER] marker needed.

**Vocabulary note for the adviser:** this passage is also the canonical
plain-English definition the UI-31 hover tooltips (queue slice 5) must
reuse — UI-31 requires the feed and the guide to "speak one language," and
its operator-verbatim example already calls LEX "the long-recovery ladder."
One definition, two surfaces: if this wording is amended at ratification,
the tooltip wording follows it, never forks from it.

**Composition note:** Delta 5 amends a *later* sentence of this same
watcher entry (the LEX-07 sentence). The two deltas touch disjoint text and
apply independently, in either order.

**Find** (Chapter 6, the opening of the long-recovery ladder's entry):

```
**The long-recovery ladder, and its floor orders.** *Watches:* the market
price of a stopped side's surviving long leg. *Acts:* it sells that long
starting at the market midpoint, walking the price down toward the buyer's
price every fifteen seconds if unfilled, for a bounded number of steps,
before falling back to a price aggressive enough to guarantee a fill
(LEX-01 through LEX-06).
```

**Replace with** (the definition passage, then the original opening
unchanged):

```
**What LEX means.** This chapter, the activity feed, and the day report all
use the short name **LEX** for the mechanism this page describes. It names
the **long exit** — the rulebook's own title for this machinery is "long-leg
exit after a short stop" (LEX-01 through LEX-09) — and the whole idea fits
in a paragraph.

Every side of the condor is a pair: a short option that collected money up
front, and a long option bought further out as that short's insurance. When
a side stops out, the broker's stop order buys back the **short** — but the
insurance, the long, is still sitting in the account, and because the market
just moved toward it, it is now worth *more* than the small amount it cost
at entry. LEX exists for exactly that moment: the instant a stop fills, it
sells that side's surviving long and turns its remaining value back into
cash (LEX-01).

It sells carefully, not desperately: it offers the long at the market's
midpoint and, if nobody takes it, lowers the asking price a notch every
fifteen seconds toward what buyers are actually bidding, a bounded number of
times, before switching to a price aggressive enough to be certain of
filling (LEX-03, LEX-05). It never dumps the leg with an uncontrolled
market order, and it never sells below what the option is genuinely worth —
the higher of the current best bid and the option's built-in payout value
(LEX-04, LEX-05). If there is truly no buyer at all, it does not invent a
price: it rests a single sell order at the option's floor value and raises
a one-time critical alert, so a degraded-liquidity recovery is announced
rather than discovered (EC-LEX-04, EC-LEX-08).

The one rule it never breaks: after a stop-out, the surviving long is
**always** sold — there is no "too cheap to bother," and the leg is never
abandoned to rot on the book (LEX-07). The single deliberate exception is
not a stop-out at all: when a side is closed early by the decay buyback
because its short had already decayed to nearly nothing, that side's
far-out long — by then worth essentially zero — is intentionally left to
expire as a free hedge (DCY-03). The always-sell promise is about
stop-outs, where the long still has real value.

And what the money means, on the house trade from Chapter 1: the condor
banked $4.00 up front, and a one-sided stop-out pays $3.80 to close the
stopped short — a guaranteed minimum of $20 kept, before costs (Chapter 5).
Whatever LEX then recovers for that side's surviving long — an option that
cost just $0.50 at entry and has been gaining value as the market moved
toward it — is real dollars added on top of that minimum (STP-02). That
recovery is why a one-sided stop day ends better than its guaranteed floor,
and why the both-sides "whipsaw" loss in Chapter 5 usually lands below its
stated maximum.

**The long-recovery ladder, and its floor orders.** *Watches:* the market
price of a stopped side's surviving long leg. *Acts:* it sells that long
starting at the market midpoint, walking the price down toward the buyer's
price every fifteen seconds if unfilled, for a bounded number of steps,
before falling back to a price aggressive enough to guarantee a fill
(LEX-01 through LEX-06).
```

---

## Verified: no dollar-math changes needed anywhere else

Checked every v1.73–v1.77 ruling against the guide's worked numbers
specifically for anything that would change a dollar figure the guide
states as fact:

- **CAL-09** — process change only (how the calendar gets populated); no
  dollar impact.
- **ORD-09a** — does not change what the *design* targets (still 95% ×
  $4.00 = $3.80, etc.); it changes what gets **recorded** once real fills
  happen, which is exactly Delta 4/5 above — no change to the house example
  itself.
- **REC-01 journal-first** — internal ordering guarantee; no dollar impact.
- **v1.76 drill relocation** — UI placement only; no dollar impact.
- **UI-31** — feed presentation only; no dollar impact.
- **RPT-12** (v1.77, entry/stop-fill marker glyph colors on the intraday
  timeline) was also checked: it's a visual-marker ruling on the Results
  dashboard's drill-down timeline (spec/10-results-dashboard.md), a level of
  chart detail the guide's Chapter 9 doesn't describe today (Chapter 9 stays
  at the badge / gross-fees-net level). No existing guide sentence needs
  amending for it, and no delta is proposed here for it — flagged for the
  adviser in case a future draft wants to add timeline-marker detail to
  Chapter 9.

The $4.00-credit house example (Chapter 1: $3.00/$0.50 put, $2.00/$0.50
call, $4.00 net) and every dollar figure derived from it in Chapters 1 and 5
are **untouched**.
