# Review — GEX Skip-Day Gate & the MEIC Optimisation Programme

**Date:** 2026-07-25
**Reviewing:** `GEX Skip Rule Method.pdf` (methodology, 2026-07-24) and `GEX skip rule method for MEIC.pdf` (results, 2026-07-24)
**Project state at review:** spec v1.88 (2026-07-21), commit `f2ee986`, branch `main`
**Commissioned as:** research only — audit, programme design, and a proposal outline. **No spec edit, no code, no amendment.** Nothing here has been ratified.

---

## 0. Epistemic status — read this first

I could not verify a single figure in these documents.

The trade log (`Trades (93).csv`), the GEX history (`gex_daily_history.csv` / `gex_lookup.parquet`), and the two production scripts (`daily_gex_signal.py`, `03_pure_gate.py`) live in your Theta Data folders, which are not connected to this session. Everything below is derived from the numbers **as written** plus the bot spec in this repo.

That constrains what this review can be. It can check internal consistency, interrogate the methodology, and test whether the conclusions follow from the stated premises. It cannot tell you whether the stated premises are what the code actually computed. **Every finding below that depends on unseen code is marked `[UNVERIFIED]`.**

One structural consequence worth naming up front: the most likely place for a result like this to be wrong is not in the statistics, it is in a single line of the pipeline — a percentile computed against a history that includes day *t*, a merge that shifts the GEX series by one day in the helpful direction, a skip mask applied to the wrong date column. Those bugs are invisible from a summary document and produce exactly this signature: modest in-sample, spectacular out-of-sample. I am not alleging one exists. I am saying that the audit I can run from PDFs is the *least* powerful audit available, and it should not be mistaken for the strong one.

---

## 1. What holds up

Let me give the credit before the criticism, because a fair amount is due.

**The arithmetic is airtight.** I recomputed the month-by-month table and cross-checked it against the headline and split tables. It reconciles to the dollar:

| Check | Result |
|---|---|
| Sum of 19 monthly Δ values | **$183,984** — exactly the headline Δ |
| Sum of monthly days | 369 — matches covered days |
| Sum of monthly skips | 77 — matches blocked days |
| Sum of monthly ungated / gated | $510,930 / $694,914 — matches headline (±$2 rounding) |
| In-sample + out-of-sample days, skips, P&L | 258+111, 49+28, and both P&L columns reconcile |
| Blocked-day averages × blocked days | −$562×49 = −$27,538 ≈ Δ$27,537; −$5,587×28 = −$156,436 ≈ Δ$156,447 |
| Win-rate decomposition | 56.8%×292 + 41.6%×77 = 197.9 winning days ≈ 53.9%×369 = 198.9 |
| Gross − net vs stated commission | $85,340 on 369 covered days vs $85,847 on all 373 — $507 gap, consistent with 4 partial/half days |

Nineteen months of independently-tabulated numbers summing exactly to the headline is not something you get by accident. Whatever else is true, the tables in that document describe one coherent computation.

**The threshold was pre-registered and the documents refuse to re-tune it.** "Re-tuning the threshold against these results would destroy the out-of-sample property that makes the finding credible" is the correct instinct, stated plainly, and it is rarer than it should be.

**The documents volunteer their own weaknesses.** The sign convention is flagged as an assumption. The gate is explicitly described as *not* tail protection. Regime-dependence is called out. The methodology doc reports that the gate's native out-of-sample test on butterflies was ~breakeven — a result that makes the headline look worse and is disclosed anyway. That is honest documentation and it is why this review can be as specific as it is.

**Commission is modelled, not waved away.** At 13.3% of gross it is the kind of cost that quietly kills 0DTE studies, and it is carried explicitly with a stated per-position schedule.

**No entry-time selection.** This genuinely removes the single largest overfitting channel in 0DTE research. "Best N entry times chosen by backtest" is the classic way these studies lie, and this one didn't do it.

**The mechanism is a real market-structure story, not a data-mined pattern.** Low dealer gamma → hedging chases moves → larger intraday range → bad for short premium. That is a prior you could have written down before looking at the data, which is worth more than any p-value in the document.

---

## 2. The audit

Twelve findings, ordered by how much they should change your confidence. F1–F3 are the ones that matter.

### F1 — The out-of-sample claim is not independent evidence. It is the same 28 days, re-weighted.

This is the finding that most reduces my confidence, and it is not addressed anywhere in either document.

The methodology doc states the gate's own pre-registered out-of-sample test: Feb–Jul 2026, 13:05 entry, **butterflies**, gated strategy ~breakeven, "2026 skips would have lost ~$430/day."

The results doc presents Feb–Jul 2026, all entry times, **iron condors**, blocked days losing **−$5,587/day**, and calls it out-of-sample confirmation at p=0.0052.

These are the same calendar days, selected by the same GEX series, using the same threshold. The only thing that changed is the instrument the losses are measured in — and both instruments are short-premium 0DTE SPX structures whose daily P&L is near-perfectly correlated in sign. A day where dealer hedging amplifies an intraday move hurts a butterfly and an iron condor for the same reason at the same time.

So the MEIC study is not a second experiment. It is the first experiment's day-selection, re-priced onto a larger book. The ratio −$5,587 / −$430 ≈ 13× is almost entirely position-size arithmetic: 25 entries × 5 contracts × 2 spreads versus one butterfly.

**Why this matters:** the results doc's Finding #2 — "the contamination concern is resolved, because the out-of-sample window alone is significant and carries the bulk of the effect" — is the load-bearing claim of the whole write-up, and it does not survive this. The out-of-sample window is significant *within* the MEIC log, but that window was already the out-of-sample window of the butterfly study, where the honest conclusion was "~breakeven, being forward-tested, not a proven edge." Running a correlated strategy over the same days does not convert a forward-test-in-progress into a confirmed result. It restates it in bigger numbers.

The number of genuinely independent out-of-sample days remains 111, containing 28 skips, and it has now been looked at twice.

### F2 — "The strongest result the gate has produced across the strategies it has been overlaid on" is an admission of a best-of-N search, presented as a strength.

That sentence appears in the summary as a credential. Read literally, it says: the gate has been overlaid on multiple strategies, the results varied, and this is the best one.

If the gate was tried on N strategy/parameter combinations and this is the maximum, then the reported p-values are the p-values of a maximum, not of a single pre-registered test. The correction is roughly multiplicative: at N=5, p=0.0089 becomes ≈0.044; at N=8, ≈0.068; at N=10, ≈0.087. The out-of-sample p=0.0052 corrects to ≈0.026 at N=5 and ≈0.051 at N=10.

**This is answerable and you are the only person who can answer it.** How many strategy configurations has this gate been overlaid on? Count honestly — different underlyings, different structures, different stop multiples, different entry times, different contract counts all count as separate draws if the result of each informed whether to keep looking. If the answer is "two, and I reported both," F2 dissolves and the p-values stand roughly as written. If the answer is "a dozen, and this is the one I wrote up," the finding is not statistically distinguishable from noise and the whole document needs a different headline.

I want to be precise about what I am and am not saying. I am not saying you fished. I am saying the document's own sentence establishes that a selection process occurred, and a selection process has a correction, and the correction is not applied. The honest fix is one line in the write-up: "this gate has been overlaid on N configurations; this is the k-th reported."

### F3 — The bootstrap null is mis-specified. Serial clustering means p=0.0089 is optimistic, probably by a lot.

The stated null is "random equal-size skip" — draw 77 days at random from 369, compute the gated P&L, repeat.

But the gate does not draw days at random. GEX percentile is a persistent, autocorrelated quantity, so low-GEX days arrive in clusters. Your own monthly table proves it decisively:

- **2026-03: 11 skips in 21 days** (52% of the month)
- **2026-04: 0 skips in 20 days** (zero)

Adjacent months, 11 and 0. Under an independent-draw model with an overall 20.9% skip rate, getting 11-of-21 in one month and 0-of-20 in the next is wildly improbable. The selection is clustered.

This breaks the test in a specific, quantifiable way. An i.i.d. bootstrap of 77 independent days produces a null distribution for the aggregate P&L that is far *tighter* than the true null, because independent draws average away month-level effects while clustered draws do not. Measuring the observed −$183,984 against a too-tight null makes it look far more extreme than it is. Both p-values are inflated. I cannot say by how much without the daily series, but with clustering this severe, a shift from p≈0.009 to p≈0.05–0.15 would not surprise me at all.

**The fix is standard and cheap:** replace the i.i.d. bootstrap with a **circular block bootstrap** (or stationary bootstrap), block length chosen from the autocorrelation length of the GEX percentile series — likely 5–20 trading days. Or, simpler and almost as good: resample *contiguous runs of skip-days* rather than individual days, preserving the observed run-length distribution. Either takes an afternoon and is the single highest-value statistical repair available.

Note that F3 compounds with F1 and F2 rather than overlapping with them. They are three independent reasons the significance is overstated.

### F4 — The document contradicts itself on the mechanism, and the contradiction is decidable.

Qualification #1: *"It is not tail protection. The gate caught only 3 of the 12 worst days... The mechanism is distributional — it removes a broad population of below-average days."*

Split Finding #3: *"The blocked days got much worse in 2026. Blocked-day average went from −$562 to −$5,587 — a 10× deterioration. The gate was identifying genuinely damaging days in the later regime."*

These cannot both describe the same 2026 window. A *distributional* filter removing a broad population of mildly-below-average days produces a modest, stable per-day cost — that is the in-sample −$562. A filter whose blocked-day average is −$5,587 while the ungated book only averages +$821/day is picking up **large individual losses**, which is the tail. And qualification #1 says the gate demonstrably does *not* reliably catch large losses (3 of 12 worst days; the biggest losses passed through at percentiles 25–64).

Reconciling: the base book deteriorated 2× from in-sample to out-of-sample ($1,627/day → $821/day). The blocked days deteriorated 10×. Blocked days got 5× worse than the environment. Two readings:

- **Generous:** the signal genuinely sharpened as the gamma regime turned hostile — low-GEX days became more dangerous, exactly as the mechanism predicts.
- **Skeptical:** with 28 observations and a filter that admittedly misses most big losses, the gate happened to land on a handful of large ones. That is a draw, not a mechanism.

**These are distinguishable with one test.** Run a **rank-based test** (Mann–Whitney U) on blocked-day P&L versus traded-day P&L, in-sample and out-of-sample separately.

- If the rank test is significant, the effect is genuinely distributional — the typical blocked day really is worse — and the doc's qualification #1 is the true description.
- If only the *sum* test is significant while the rank test is not, the entire result is carried by a few outlier days, the mechanism is tail-capture, and everything downstream changes: the benefit is not repeatable, sizing implications reverse, and forward-testing needs years not months.

This is the cheapest decisive experiment in this review. It requires only the daily P&L series and the skip mask, and it resolves the document's central ambiguity. **Run this one first.**

### F5 — 83% of the out-of-sample benefit is two months.

From the monthly table, the Feb–Jul 2026 Δ of $156,447 decomposes:

| Month | Skips | Δ | Δ per skip |
|---|---|---|---|
| 2026-02 | 5 | +$2,627 | +$525 |
| 2026-03 | 11 | +$79,977 | +$7,271 |
| 2026-04 | 0 | $0 | — |
| 2026-05 | 4 | +$50,480 | **+$12,620** |
| 2026-06 | 7 | +$23,021 | +$3,289 |
| 2026-07 | 1 | +$343 | +$343 |

March and May together are **$130,457 of $156,447 = 83.4%**, from 15 of the 28 blocked days. February's five skips returned $525 each — essentially nothing. April contributed nothing at all because the gate never fired.

A per-skip benefit ranging from $343 to $12,620 across six months is not a stable effect size. It is a small number of large days. This is F4's skeptical reading showing up in the data, and it means the confidence interval on "expected benefit per skipped day" is enormous — wide enough that its lower bound is plausibly near zero.

### F6 — The expanding percentile is confounded with the secular trend in 0DTE open interest.

The gate ranks yesterday's total GEX against *all* prior readings, expanding, forever. This is non-stationary by construction.

Total GEX = Σ (gamma × open interest × 100 × spot). Two of those three terms have trended hard over the study window: SPX 0DTE open interest has grown enormously since 2024, and spot has risen. So the *level* of total GEX carries a strong secular trend that has nothing to do with dealer positioning on any given day.

Against an expanding history, a trending series produces a drifting gate. If GEX trends up, later days rank progressively higher and the gate fires less over time. If it trends down relative to accumulated history, it fires more. Your data shows the latter: **3.7 skips/month in 2025, 4.7 skips/month in 2026** — the gate became 27% more aggressive in the second period, which is also the period carrying the entire result.

That is a serious confound. It means "2026 was a low-GEX regime" and "the expanding window drifted" are not separable in this design. The gate may be partly a slow-moving trend detector wearing a dealer-gamma costume.

**Fix:** rank against a **rolling window** (250 trading days, say) instead of expanding history, and/or **detrend** the GEX series before ranking. Then re-run. If the result survives a rolling window, F6 is dead and the finding is meaningfully stronger. If it evaporates, you have learned that the gate was reading the calendar, not the market. Either way this is a cheap, high-information test — and note it is a *robustness check on a frozen rule*, not a re-tune, so it does not violate the pre-registration discipline.

### F7 — Threshold sensitivity is never shown, and showing it is not the same as re-tuning.

Only the 20% threshold is reported. That leaves the most basic robustness question unanswered: does the gate degrade gracefully?

A real signal produces a broad plateau — 15%, 20%, 25%, 30% should all help, roughly monotonically decreasing in benefit as you skip more days. A fitted artefact produces a spike: 20% works, 15% and 25% don't.

The documents correctly refuse to *re-optimise* the threshold. But that is a different act from *reporting the sensitivity curve while committing to 20%*. Reporting the curve costs nothing epistemically — you are not changing the rule, you are characterising it. Not reporting it leaves the reader unable to distinguish "bottom-quintile dealer gamma is dangerous" from "the 20th percentile of this particular series happened to line up with some bad days."

**Do this:** publish Δ at 10/15/20/25/30/35% alongside the frozen 20% result, and state in advance that 20% remains the committed rule regardless of what the curve shows. That is honest and maximally informative.

### F8 — The backtested strategy is not the strategy your bot trades. This is the largest practical gap in the study.

Neither document mentions this, and it may be the most consequential thing in this review.

| Dimension | BYOB backtest | Your bot (spec v1.88) |
|---|---|---|
| **Stop basis** | 1.5× SL — exit at 2.5× credit, i.e. lose 1.5× credit | `total_credit` @ **95%** — each short stops at 0.95 × **net** credit |
| **Stop placement** | Modelled exit on spread value | Broker-resting stop-market on **short legs only** |
| **Long after stop** | Assumed with the spread | LEX reprice ladder, always sold, floor max(bid, intrinsic) |
| **Decay buyback** | None | Shorts bought back at ask ≤ $0.05, 15:55 cutoff |
| **Profit management** | None | TPF floor + TPT target |
| **Strike selection** | Target premium 1.50–3.00 | STK probe walk, min_short_premium $1.00, ≤3 up / ≤25 down probes |
| **Entries/day** | 25 | Operator-composed schedule (~6) |
| **Contracts** | 5 | Pre-fill 1 |

The stop is the headline divergence. Your ratified outcome contract — **one side hit ⇒ small profit; both sides hit ⇒ lose ≈ the premium, never more** — is a fundamentally different return distribution from a 1.5×-credit stop. Yours stops far earlier, far more often, for far less each time. The BYOB configuration takes 1.5× credit losses that your bot structurally cannot take.

**Consequence:** every dollar figure, win rate, blocked-day average, and max-drawdown number in the results document describes a system you do not run. The gate's *directional* conclusion (bad days are bad for short premium) transfers fine — that is instrument-agnostic. The *magnitudes* do not transfer at all, and the drawdown comparison in particular is meaningless for your book.

I want to flag this explicitly because it cuts both ways and I don't know which: a 95%-of-net-credit stop is much tighter, so a violent low-gamma day may hurt you *less* than the backtest (you're out early, repeatedly, for small amounts) — or *more* (you get whipsawed out of both sides on every entry all day and pay the premium 25 times over). Your own spec anticipates this: *"Trade-off accepted: closer triggers ⇒ more frequent small stop-outs."* On a high-range low-gamma day, "more frequent" could be every entry. **It is entirely possible that the GEX gate is worth more to your configuration than to the backtested one** — a whipsaw day is precisely the failure mode a tight stop is most exposed to. But that is a hypothesis, not a finding, and this study cannot test it.

### F9 — The study's book is roughly two orders of magnitude larger than what you run live.

25 entries × 5 contracts versus ~6 entries × 1 contract. Per entry-contract-traded-day, the gated result is $694,914 / (292 × 25 × 5) = **$19.04**.

Scaled to a 6-entry, 1-contract schedule over the same 292 traded days: **≈ $33,400 over 18 months**, or roughly $1,850/month — with a corresponding gate benefit of about **$8,800** over the whole period rather than $184,000.

Two caveats on that arithmetic. The sum scales linearly (P&L of a book is the sum of its entries), so the expectation is right. But your six chosen entry times are not a random sample of the 25 — they may be systematically better or worse. And it assumes the entries are the same trades, which per F8 they are not.

This matters for framing. $184,000 reads as transformative. $8,800 over eighteen months, on a result with the significance problems in F1–F3, reads as what it is: an interesting hypothesis worth forward-testing. Also relevant to `max_day_risk` — which per v1.81 is now the *sole* numeric bound on a cascade day. This study cannot inform that number, because it never reports the single-day loss distribution.

### F10 — The gate has no verified relationship to dealer gamma, and a trivial proxy might do the same job. `[UNVERIFIED]`

The methodology doc honestly flags the calls-positive/puts-negative convention as an assumption. I want to push harder on what follows from that.

Because the gate uses a *percentile of its own construction over time*, a wrong sign convention does not necessarily break it — it just means the ranked quantity is not "dealer gamma exposure." It is *some* function of the option chain's gamma-weighted open-interest profile. That function might be a proxy for something much simpler and much cheaper to obtain.

**The test that should have been run first:** replace total GEX with each of these and re-run the identical gate machinery —

- prior-day realised range (high−low / close)
- prior-day absolute return
- VIX close
- VIX9D / VIX ratio (term-structure slope — the standard 0DTE regime proxy)
- total 0DTE open interest alone, with no gamma weighting at all

If any of these reproduces most of the benefit, then the Theta Data subscription, the noon chain snapshot, the Brent-method IV solver, and the per-strike Black–Scholes gamma are elaborate machinery around a signal you could get free from a VIX quote — with far fewer failure modes and no fragile data dependency to spec into your bot.

If none of them does, and GEX beats all of them, that is *powerful* evidence for the mechanism and would substantially raise my confidence. It is the strongest positive result available from data you already have.

Right now, the study has never established that the gamma calculation is doing any work.

### F11 — The gate's overlap with your existing calendar is untested, and you may already own the signal.

You have built, ratified, and shipped a full calendar system: CAL-01→11 with computed OpEx and quad-witch (v1.83), event proximity warnings at T-1/2/3 (v1.84), tagging, and standing rules, with CAL-05 as the enforcement path.

**Cross-tabulate the 77 blocked days against your calendar.** What fraction fall on or adjacent to FOMC, CPI, OpEx, quad-witch, or month-end? Dealer gamma is mechanically low around large expiries and high-uncertainty macro prints — the two signals should overlap substantially.

If most of the gate's benefit sits on days your calendar already knows about, you can capture it **deterministically, with no external data dependency, using rules you have already built and ratified.** That is a strictly better outcome than a new data pipeline: no staleness, no fetch failures, no NFR-07 wiring risk, no fail-open surface. And it would explain the clustering in F3 — macro and expiry events cluster.

This is the highest-value test in the entire review measured by *what you'd do differently* if it came back positive.

### F12 — Small internal discrepancies, and a rule that differs between the doc and the test.

None of these change the conclusions; all should be tidied before this is treated as a reference document.

1. **Traded-day win rate is quoted twice, differently.** The headline table says gated win rate **56.8%**; qualification #1 says **57.2% traded**. On 292 days that is 165.9 vs 167.0 — a one-day difference. Most likely gross-of-commission vs net, but it should be labelled, because two numbers for one quantity is how reconciliation errors announce themselves.

2. **Commission reconciles only under an assumption.** Gross − net on covered days = $85,340; stated full-log commission = $85,847; gap $507 over 4 excluded days = $127/day. A complete 50-position day costs at least $170 (50 × $3.40). So at least one excluded day must have been partial — plausible if they were half-days, but it should be confirmed rather than inferred.

3. **`[UNVERIFIED]` The missing-reading rule differs between doc and test.** The methodology states *"a missing reading forces a skip."* The results state 4 days were **excluded** as outside GEX coverage. Excluding a day and skipping it are different operations, and the backtest used the one the production rule does not specify. Immaterial at 4 days out of 369 — but it means the tested code and the documented rule diverge in at least one place, which is the kind of thing that is rarely unique.

4. **The recommendation quietly assumes something the study did not test.** The closing line recommends running the gate *"for select entry times."* The study's central methodological defence is that it used **all** entry times with no selection. Whether the gate's benefit is uniform across the trading day or concentrated in particular hours is unknown and untested — and if it is concentrated in, say, the morning entries, a selected afternoon-weighted schedule may capture very little of it. **Per-entry-time Δ should be reported before anyone acts on that recommendation.**

---

## 3. The six tests that would change my mind

Ranked by information gained per hour spent. All use data you already have; none requires re-tuning anything.

| # | Test | Resolves | Cost |
|---|---|---|---|
| **1** | **Mann–Whitney rank test**, blocked vs traded day P&L, IS and OOS separately | F4 — settles distributional vs tail, the document's central contradiction | ~1 hour |
| **2** | **Cheap-proxy horse race** — rerun the identical gate on realised range, abs return, VIX, VIX9D/VIX, raw 0DTE OI | F10 — is the gamma calculation doing any work at all? | ~half a day |
| **3** | **Calendar overlap cross-tab** — 77 blocked days vs your CAL event set | F11 — can you get this from rules you already own, with no data dependency? | ~1 hour |
| **4** | **Block bootstrap** replacing the i.i.d. bootstrap, block length from the GEX autocorrelation | F3 — the real p-value | ~half a day |
| **5** | **Rolling 250-day percentile** instead of expanding, and/or detrended GEX | F6 — trend confound | ~2 hours |
| **6** | **Threshold sensitivity curve** at 10/15/20/25/30/35%, with 20% pre-committed regardless | F7 — plateau or spike | ~2 hours |

Plus one thing only you can supply: **the honest N for F2.** How many strategy/parameter configurations has this gate been overlaid on?

If tests 1, 2, and 3 all come back favourably — the rank test is significant, GEX beats every cheap proxy, and the blocked days are *not* mostly calendar events — then I would revise sharply upward, and F1's independence problem becomes the only major objection standing. If test 1 fails or test 2 shows VIX9D/VIX matching the gate, the correct action is to stop building the GEX pipeline and use the simpler thing.

---

## 4. The optimisation programme

You asked for a design, not results. Here is how I would structure "MEIC Optimisation & Backtesting" as a workstream, given everything above.

### 4.1 The governing principle

You already solved this problem once. The spec discipline — every rule has an ID, every ID has a Gherkin test, amendments are proposed by the agent and ratified by you with exact text, nothing is improvised — is *precisely* the discipline that research needs and almost never gets. The failure mode of quantitative optimisation is identical to the failure mode the spec protocol was built to prevent: a plausible-sounding change made without a recorded decision, which nobody can later audit.

**Apply the amendment protocol to research.** A study is a spec amendment: pre-registered, ID'd, ratified, versioned, and never silently revised after seeing the answer.

### 4.2 Pre-registration protocol

Before any parameter sweep touches data, commit a document to the repo (same convention as `PROPOSAL-*.md`) stating:

1. **The question**, as a falsifiable claim.
2. **The parameter and its grid** — exact values, fixed in advance, no "I'll extend the range if the edge is at the boundary."
3. **The metric** — one primary, declared in advance. Secondary metrics are reported but cannot change the decision.
4. **The decision rule** — the number that means adopt, stated before the number is known.
5. **The out-of-sample window** — held out and *not looked at* until the in-sample work is finished and frozen.
6. **The falsification** — what result would make you abandon this. If you can't name one, it isn't a study.
7. **The N** — how many prior studies have been run on this data, cumulatively. Maintain a running count in the repo. This is the single most valuable thing on the list and the one everyone omits.

That last point deserves emphasis. Every study run against this trade log spends some of its statistical power. A running study register turns F2 from an unanswerable objection into a number you can look up.

### 4.3 Walk-forward, not split-sample

A single in/out split gives you one out-of-sample number and, as F5 shows, that number can be 83% two months.

Use **anchored walk-forward**: fit on everything up to month *m*, apply to month *m+1*, roll forward. Report the *concatenated live-equivalent equity curve* — the P&L you would actually have earned making decisions in real time with only prior information. That curve is the only backtest number I would let influence a real decision, because it is the only one whose every point was genuinely out-of-sample when it was generated.

It also produces something a split cannot: a **distribution** of monthly out-of-sample results, which lets you say "the benefit was positive in 11 of 15 walk-forward months" instead of "the benefit was $156,447, of which $130,457 was March and May."

### 4.4 What is worth optimising, and what is bait

**Legitimately tunable — high value, low parameter count, structural:**

- **Number of entries and the schedule shape.** Since ENT-05 was retired (v1.81), entry volume is bounded only by `max_day_risk` and the order cap. Entry count is now your primary risk dial, and it is under-studied. Structural, not fitted.
- **Wing width.** Drives margin, `max_day_risk` consumption, and the worst case. Three or four candidate values, each with a clear economic interpretation.
- **Day-level gates** (GEX, VIX regime, calendar). One binary decision per day, very few parameters, mechanistically motivated. This is the most defensible category — but pre-register or it becomes the least.
- **`max_day_risk` itself.** This is not really an optimisation, it is a decision you owe the go-live checklist, and the backtest's job is to give you the *single-day loss distribution* so you can set it deliberately. Ask the data for the 1st and 5th percentile daily loss on your configuration, not for a profit-maximising value.

**Handle with explicit care — you have ratified positions here:**

- **`stop_loss_pct` and `stop_basis`.** Optimisation will have opinions. Your `total_credit` @ 95% default is ratified twice and rests on an *outcome contract* — a statement about the shape you want returns to have (one side hit ⇒ small profit; both hit ⇒ lose ≈ premium, never more). **A backtest cannot overrule a preference.** It can tell you what that preference costs, and that is worth knowing precisely — but "95% underperforms 150% by $X" is a price tag on a deliberate choice, not an error to correct. Study it; report the cost; do not let an optimiser silently reverse a decision you made twice on purpose. *(Flagging per your standing instruction that reversals of locked decisions be surfaced explicitly rather than absorbed.)*
- **Target premium / `min_short_premium`.** The $1.00 floor ("never sell a short below $1") is yours and explicit. Same treatment.

**Overfit bait — expect a strong signal and do not trust it:**

- **"Best N entry times."** The single most reliable way to manufacture a fake 0DTE edge. Twenty-five candidates, one year, pick the top six — you will find a beautiful curve every time and it will not repeat. The study's refusal to do this is its best feature; do not undo it.
- **Day-of-week rules.** ~78 observations per weekday over 18 months. Anything you find is noise.
- **Month or seasonality filters.** Fewer observations still.
- **Anything with more than two free parameters fitted jointly.** The parameter count is the overfitting budget.

### 4.5 Metrics

**Do not optimise total net profit.** F5 is the demonstration: a sum is hostage to a handful of days, so a profit-maximising search will reliably select the parameter set that best fits your largest outliers.

Better primary metrics, in rough order of preference:

- **Median daily P&L** — robust to the outliers that drive the sum.
- **Bootstrap lower confidence bound on mean daily P&L** (block bootstrap per F3) — optimises for what you can *defend*, not what you observed.
- **Net profit / max drawdown** — if you must use a sum, at least make it risk-adjusted.

And always report the **distribution**, not the total: median, IQR, 5th percentile day, worst day, and the count of days contributing more than 10% of the result. If two or three days carry a third of the answer, the metric should say so out loud.

### 4.6 Cost realism

Two costs, in very different states of knowledge.

**Fees: solved.** PNL-01a is verified by falsification — you predicted $5.60/$34.40 before observing it and hit to the cent. Any backtest should use that schedule, and the BYOB $3.40/$6.80 per 5-contract position should be reconciled against it. At 13.3% of gross, a 2× error in the fee model is an $85,000 swing on the study's book.

**Slippage: unknown, and it is the bigger number.** `ProfitLossAfterSlippage` carries an unstated assumption. For a 25-entry 4-leg SPX 0DTE book, the slippage assumption plausibly determines whether the strategy is profitable *at all*, gate or no gate.

But you are in an unusually strong position here, and I don't think you've noticed it. **Your bot is already generating the ground truth.** ORD-09a records broker-actual prices on every execution; the entry reprice ladder records where in the mid→floor walk each fill landed; ORD-11 timestamps the lifecycle; the LEX ladder records long-recovery fills against the chain. That is a live, growing, instrument-specific dataset of *your actual fill quality on your actual orders*.

**Recommendation: calibrate the backtest's slippage from your own ORD-09a record rather than assuming a constant.** This converts your biggest modelling unknown into a measured quantity, and it gets better every day you trade. It is also the single strongest argument for keeping the live book running through the research phase — not for the P&L, but because it is a fill-quality instrument.

Worth noting: this is the same move that made the fee model trustworthy. Predict, then observe, then check. It worked once.

### 4.7 Hard constraints on the search space

The optimiser must not be allowed to propose configurations the bot cannot execute.

- **Order cap 380/day (RSK-08).** 25 entries/day is likely *operationally infeasible* for your architecture. Count it honestly: each entry is one complex order **worked as a reprice ladder** (multiple replaces), plus two stop placements, plus stop replacements after decay buyback, plus LEX recovery ladders after each stop. Twenty-five entries could plausibly generate several hundred orders on an active day. **Before optimising over entry count, establish the empirical orders-per-entry distribution from your live logs and derive the true maximum feasible entry count.** That number is a hard ceiling on the search space, and it may be far below 25.
- **`max_day_risk` (RSK-04)** caps the bot's book — any candidate configuration must fit inside the value you set.
- **`min_buying_power`** — you run $2k.
- **STK-02c stop feasibility** — configurations whose stop trigger cannot clear the shorts by `min_stop_distance_ticks` produce skipped entries, and the backtest must model those skips or it will overstate fill rates.

A configuration that violates any of these is not a candidate, no matter how well it backtests.

---

## 5. Gate proposal outline — shadow mode first

You commissioned "research now, spec later," so this is a sketch of the shape a future amendment would take, not a draft amendment. Nothing here is ratified and I have not touched the spec.

### 5.1 The recommendation, stated plainly

**Do not build an acting gate. Build a shadow gate — or better, don't build anything yet.**

The study's own closing recommendation is to *"log GO/SKIP decisions plus realized daily P&L to extend the out-of-sample record."* That requires **recording** the decision. It does not require **acting** on it. And given F1 (the OOS window has been read twice), F3 (the p-value is inflated), and F4 (the mechanism is unresolved), acting on it now would be committing money on the strength of 28 clustered days, 83% of whose benefit came from two months.

Shadow mode gets you the forward record at zero trading risk, and it is the honest reading of your own documents.

### 5.2 Two ways to run shadow mode

**Option A — zero build. Use the calendar you already have.**

CAL supports day tagging and standing rules. Run `daily_gex_signal.py` as you already do, and record each day's percentile and GO/SKIP as a calendar tag — by hand, or via a small external script writing to the calendar store.

- Costs nothing in the repo. **The graduation clock keeps running undisturbed.**
- Uses ratified, shipped, tested machinery.
- Feeds F11's cross-tab automatically: GEX decisions and calendar events land in the same place, so the overlap analysis becomes a query rather than a project.
- Downside: manual daily entry is a burden and error-prone; a missed day is a hole in exactly the record you're trying to build. Mitigate by scripting the write.

**Option B — journal-only gate in the bot.**

A day-level component that computes the percentile at day start, journals a `DayGateEvaluated` event (percentile, threshold, decision, source-file timestamp, staleness), surfaces it on the Trading tab and in the UI-31 activity feed, and **changes no behaviour whatsoever.** No entry is blocked. The decision is recorded and displayed only.

- Automatic, complete, event-sourced, replay-safe — the forward record builds itself with zero operator effort, which is the difference between a record that exists in six months and one that doesn't.
- **But it is a build change, and per the ratified graduation-clock rule, a build change restarts the clock.** Even a journal-only component. You should decide that with eyes open rather than discover it.

**My view:** Option A first, because the clock matters more right now than the convenience does, and because the manual burden is bounded by however long the forward test runs. Option B becomes correct if and only if the six tests in §3 come back favourably and you decide the gate is a real candidate — at which point it graduates from "thing I'm watching" to "thing I'm building," and paying the clock is justified.

### 5.3 If it ever becomes an acting gate — the design constraints

These are the things a future amendment would have to get right. Recording them now so they aren't rediscovered later.

**The DAT-04a lesson applies directly, and this is the most important item here.** You *just* retired the halt gate (v1.80) because an external input became a can-never-say-no constant — the NFR-07 constant-signal defect — and you explicitly rejected a fail-open patch on a broker-unverifiable state. **A GEX gate is the same class of hazard: a trading decision driven by an external data dependency you do not control.** The failure modes rhyme exactly:

- The file is stale (yesterday's reading never written) — does the gate silently reuse it?
- The percentile computation returns a constant (empty history, parse failure, all-NaN chain) — does the gate become a permanent GO?
- Theta Data is down at the noon snapshot — what happens tomorrow?
- The chain snapshot is partial — what completeness threshold makes a reading valid? (STK-10's `chain_completeness_pct` is the existing precedent.)

The methodology's *"a missing reading forces a skip"* is **fail-closed**, which is the right polarity and matches your instincts everywhere else. But fail-closed has its own failure mode: a broken pipeline silently stops the business, and a skip-because-broken is indistinguishable in the log from a skip-because-low-gamma. **The amendment would need to separate those two outcomes explicitly** — distinct reason codes, distinct UI treatment, and a staleness alarm (RSK-06 class) that fires on the broken case and never on the legitimate case. Per F12.3, note the study *excluded* rather than *skipped* its 4 missing days, so this path has never been exercised even in backtest.

**NFR-07 obligation.** Any gate component enters the wiring registry and must be provably constructed and ticked in `live_app`, CI-gated. You have found *seven* built-but-unwired components; the registry exists precisely so an eighth doesn't happen. A gate that exists but never ticks is a gate that silently permits every day — which is the constant-signal defect wearing a different hat.

**Journal-first (REC-01).** The decision is journaled before it is acted on, so the forward-test record is complete even if the acting path fails. This also means Option B's journal-only mode is a genuine subset of the full gate, not throwaway work — the acting path is added later on top of an already-proven recording path.

**Threshold frozen in config, changes are amendments.** `gex_skip_percentile` defaults to 0.20 and lives in doc 06 like everything else. But given the pre-registration discipline, changing it should require a ratified amendment with a stated justification, not a config edit. The whole value of the finding rests on the threshold not moving.

**Precedent to follow: CAL-05.** It is your existing day-level NO-TRADE enforcement, and a GEX gate is the same shape. Reuse the mechanism rather than inventing a parallel one — and note the interaction question that would need ruling: if CAL-05 and a GEX gate disagree, which wins, and does a GEX skip get the same visual treatment as a calendar NO-TRADE?

**Multi-underlying (UND-01→06).** Your bot now trades SPX, RUT, and /ES. The GEX signal is computed on SPXW only. Does an SPX-derived gate block a RUT entry? A /ES entry? That is a ruling, not an implementation detail, and it would need making before any acting gate ships.

---

## 6. Bottom line

**On the gate.** The study is honestly written, arithmetically impeccable, and mechanistically plausible. It is also substantially less strong than it presents, for three independent and compounding reasons: the out-of-sample window is not independent evidence (it is the butterfly study's own OOS window, re-priced onto a correlated strategy — F1); the "strongest result across strategies it's been overlaid on" is an unreported best-of-N selection (F2); and the bootstrap null ignores severe serial clustering that your own 11-skips-in-March / 0-skips-in-April data makes undeniable (F3). Any one of these would move p meaningfully. Together they take a headline p=0.0089 somewhere I cannot compute without the data, but plausibly north of 0.05.

Against that: the direction is right, the discipline around the threshold is real, and there is a genuine market-structure story underneath. **The correct posture is exactly the one both documents recommend — forward-test with the threshold frozen — held with less confidence than the results document projects.**

**On what to do first.** Run tests 1, 2, and 3 from §3 (rank test, cheap-proxy horse race, calendar cross-tab). They are cheap, they use data you have, and between them they answer the three questions that determine whether this is worth any further investment: is the effect distributional or three lucky days; is the gamma machinery doing any work or would VIX9D/VIX do it free; and do you already own this signal in the calendar you built and shipped last week.

**On the bot.** Nothing changes yet. The study describes a different strategy (F8) at roughly 100× your live size (F9). Shadow-record the decisions — Option A, via calendar tagging, costing you nothing in clock — and revisit when there is a forward record and the §3 tests are in.

**On optimisation generally.** The most valuable thing available to you is not a parameter sweep. It is the slippage calibration in §4.6: you are already recording, via ORD-09a, the ground truth that every backtest of this strategy has to assume. Turning that into a measured fill model would improve every study you ever run on this book — including this one, if you re-run it properly.

---

### Appendix — arithmetic I verified

Recomputed from the results PDF's tables. All figures reconcile; no errors found.

```
Sum of 19 monthly Δ                      = $183,984   (headline Δ: $183,984)   ✓
Sum of monthly days / skips              = 369 / 77   (stated: 369 / 77)       ✓
Sum of monthly ungated / gated           = $510,930 / $694,914                 ✓
In-sample (Jan25–Jan26) from monthlies   = 258 days, 49 skips, Δ $27,536       ✓ (stated $27,537)
Out-of-sample (Feb–Jul 26) from monthlies= 111 days, 28 skips, Δ $156,448      ✓ (stated $156,447)
Blocked-day avg, full period             = −$183,984 / 77 = −$2,389.40         ✓
Blocked-day avg, IS / OOS                = −$561.98 / −$5,587.39               ✓
Ungated per-day, IS / OOS                = $1,627.28 / $820.65   (2.0× decay)
Avg net per traded day, gated / ungated  = $2,379.84 / $1,384.63  (+71.8%)     ✓
Win-day decomposition                    = 165.9 + 32.0 = 197.9 vs 198.9       ✓ (rounding)
Commission, covered days                 = $596,270 − $510,930 = $85,340
  vs stated full-log $85,847 → $507 over 4 excluded days ($127/day; a full
  50-position day costs ≥$170, so ≥1 excluded day was partial)                 ⚠
Per entry-contract-traded-day, gated     = $694,914 / (292×25×5) = $19.04
  → 6 entries × 1 contract × 292 days    ≈ $33,400 over 18 months
OOS concentration, Mar+May 2026          = $130,457 / $156,447 = 83.4%
```
