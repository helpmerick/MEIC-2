# DRAFT — DOC-06 delta: sandbox-credential removal (live-only build)

**Status: amendment-proposal for the adviser.** Operator commission,
verbatim: *"This version will only trade live so in the getting started i
want to take out the reference to the sandbox and confirm that a trader
with a fresh instance wont need to enter it to trade real money."*

`spec/12-how-it-works.md` is hash-locked; nothing in it has been edited.
The find/replace blocks below are written against the **ratified GETTING
STARTED section as it actually stands** (v1.78 stamp, spec/12 line 679
onward — quoted verbatim, not from the earlier draft file), for the adviser
to paste through the ritual.

**The confirmation the operator asked for, up front:** a trader setting up
a fresh instance to trade real money **never enters sandbox credentials**.
Code-verified fact supplied with this commission (for the adviser to
re-verify at ratification): the live boot selects its credential set by
`kind = "CERT" if is_test else "PROD"` (`server.py`) — with the two
production switches set (`MEIC_LIVE_IS_TEST=false` + `MEIC_ALLOW_PRODUCTION`,
NFR-06a) it reads **only** the `TT_PROD_*` variables, and `TT_CERT_*`
appears nowhere else in production code. Delta 2 puts this into the
template itself; Delta 3 adds the one honest caveat that makes it true
*without* qualification only when the ritual is completed.

## Adviser notes (preamble)

1. **First-run sequence checked — no sandbox reference found.** Section 4's
   "paper mode" is the SIM fill simulator fed by real production market
   data (`paper_app`, SIM-01) — it is **not** tastytrade's cert sandbox,
   which the spec restricts to contract tests only (doc 01 §12: "never
   tastytrade's cert sandbox, whose unconditional instant fills make
   economics meaningless"). No first-run step directs the reader at
   cert/sandbox mode, so no step is flagged and none is reworded here.
2. **Developer-only sandbox use survives elsewhere, deliberately out of
   scope:** the operator-triggered contract suite (`pytest -m contract`)
   still runs against the tastytrade sandbox. That is a developer concern,
   not an operator concern — the adviser may keep one parenthetical for
   developers in the tab or remove the sandbox cleanly per the operator's
   intent; their call, and nothing in these deltas depends on the choice.
3. **Implementation consequence the adviser should know:** the
   no-secret-leak test (TC-DOC-01) pins that every template variable NAME
   renders on the tab. When this delta lands, that test's expected-name
   list must drop the three `TT_CERT_` names (`TT_CERT_PROVIDER_SECRET`,
   `TT_CERT_REFRESH_TOKEN`, `TT_CERT_ACCOUNT`) **in the same change** — a
   small code PR will accompany ratification. It is not written now.
4. **Version stamp (DOC-05):** the section's stamp line — "GETTING STARTED
   (ratified content, v1.78 — describes spec v1.78 and the build's true run
   procedure; DOC-05 stamp)" — bumps to whatever spec version this
   ratification lands as; that is part of the ratification act itself, not
   a delta below.

## Index of deltas

| # | Where | Change |
|---|---|---|
| 1 | Section 2, `.env` template table | Remove the `TT_CERT_*` row entirely |
| 2 | Section 2, `TT_PROD_*` row | Reword to stand alone (it currently leans on the removed row) + state the code-verified live-boot fact |
| 3 | Section 5, two-switch ritual paragraph | Drop the sandbox contrast from the issuer-check sentence; add the honest MEIC_LIVE_IS_TEST-defaults-to-true caveat |

**[ADVISER] markers in this file:** none (the one candidate — the first-run
sequence — was checked and did not need flagging; see preamble note 1).

---

## Delta 1 — Section 2: remove the `TT_CERT_*` template row

**Find** (one table row, quoted in full):

```
| `TT_CERT_*` (a group of variables) | Your tastytrade **practice/sandbox** credentials: a provider secret, a refresh token, and an account identifier, used only for practice-mode/contract testing against tastytrade's non-real-money sandbox. [the literal names: `TT_CERT_PROVIDER_SECRET`, `TT_CERT_REFRESH_TOKEN`, `TT_CERT_ACCOUNT`] | tastytrade's own OAuth application settings, in your tastytrade developer/API account area — request sandbox (certification) credentials there. |
```

**Replace with:** nothing — delete the row. (The template then goes
straight from `MEIC_USER_PASSWORD` to `TT_PROD_*`.)

---

## Delta 2 — Section 2: the `TT_PROD_*` row stands alone and states the fact

The current row opens "The same kind of credentials" and closes "The same
tastytrade OAuth application settings … not the sandbox ones" — both leaning
on the row Delta 1 removes. It becomes the row that *introduces* the
credential group, and it carries the code-verified confirmation the
operator asked for.

**Find** (one table row, quoted in full):

```
| `TT_PROD_*` (a group of variables) | The same kind of credentials — a provider secret, a refresh token, an account identifier — but for your **real, production** tastytrade account. These are what the live build authenticates with. [the literal names: `TT_PROD_PROVIDER_SECRET`, `TT_PROD_REFRESH_TOKEN`, `TT_PROD_ACCOUNT`] | The same tastytrade OAuth application settings, but the production application/credentials, not the sandbox ones. |
```

**Replace with:**

```
| `TT_PROD_*` (a group of variables) | Your broker credentials: a provider secret, a refresh token, and an account identifier, all for your **real, production** tastytrade account. These are the only broker credentials this bot asks you for — with the two production switches below set, the live build reads exactly this group and nothing else, so a fresh instance set up to trade real money never enters practice or test credentials of any kind. [the literal names: `TT_PROD_PROVIDER_SECRET`, `TT_PROD_REFRESH_TOKEN`, `TT_PROD_ACCOUNT`; code-verified: credential selection is `kind = "CERT" if is_test else "PROD"` in server.py — with the production switches set, only the `TT_PROD_*` names are read, and `TT_CERT_*` appears nowhere else in production code] | tastytrade's own OAuth application settings, in your tastytrade developer/API account area — the production application and its credentials. |
```

---

## Delta 3 — Section 5: the issuer check, and the honest caveat

Two things in one paragraph. First, the existing sentence "actually
production credentials and not sandbox ones" loses its sandbox contrast
(the check itself — the bot independently asserting the credentials are
production — is ratified NFR-06a fact and stays). Second, the honest
caveat: `MEIC_LIVE_IS_TEST` **defaults to true**, so a boot that skips the
ritual will ask for the practice-account credentials this guide no longer
describes — completing the two-switch ritual is precisely what makes them
unnecessary. Stating that plainly is what makes "you'll never need sandbox
credentials" an honest sentence instead of a hopeful one.

**Find** (Section 5, the two-switch paragraph, quoted in full):

```
**Underneath all three, and specific to real money:** turning this software
loose on your actual brokerage account requires **two separate, deliberate
switches in its configuration**, not one — `MEIC_LIVE_IS_TEST` must be
explicitly set to mean "this is not a test," **and** `MEIC_ALLOW_PRODUCTION`
must also be explicitly turned on. The bot additionally double-checks, on
its own, that the broker credentials it was given are actually production
credentials and not sandbox ones. Flip only one of these and the bot refuses,
with a plain explanation of what's missing — by design, there is no way to
end up live by mistake (NFR-06a).
```

**Replace with:**

```
**Underneath all three, and specific to real money:** turning this software
loose on your actual brokerage account requires **two separate, deliberate
switches in its configuration**, not one — `MEIC_LIVE_IS_TEST` must be
explicitly set to mean "this is not a test," **and** `MEIC_ALLOW_PRODUCTION`
must also be explicitly turned on. The bot additionally double-checks, on
its own, that the broker credentials it was given really are production
credentials. Flip only one of these and the bot refuses,
with a plain explanation of what's missing — by design, there is no way to
end up live by mistake (NFR-06a).

One honest caveat, so the credentials story above stays true without fine
print: out of the box, `MEIC_LIVE_IS_TEST` is set to "this IS a test" — the
safe direction. If you start the live build without completing the
two-switch ritual, it will therefore ask for test-account credentials this
guide doesn't cover, and refuse to touch real money. Completing the ritual
is exactly what makes those unnecessary: with both switches deliberately
set, the bot reads your real-account credentials from Section 2's template,
and nothing else, ever.
```

---

## Verified against the operator's intent

- After Deltas 1–3, the Getting Started section contains **no instruction
  to obtain or enter sandbox credentials anywhere** — the only remaining
  mention of a test credential set is Delta 3's deliberate honest caveat,
  which exists to tell the trader why they can ignore it.
- A fresh instance following the section end-to-end enters exactly one
  broker credential group: `TT_PROD_*`.
- The paper-first first-run sequence is untouched (it runs on the SIM
  simulator, not the sandbox — preamble note 1).
