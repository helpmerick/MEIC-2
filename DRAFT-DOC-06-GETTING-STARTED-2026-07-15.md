# DRAFT — DOC-06 "Getting Started" tab content

**Status: DRAFT, awaiting adviser verification and operator ratification.**
This file is the agent's first-pass draft of the fifth tab's content, per the
doc-12 build ritual stated in DOC-06 itself: *"agent drafts FROM THE RATIFIED
SPEC and the build's true run procedure, adviser verifies, operator
ratifies, then it renders (version-stamped per DOC-05)."* It is written
entirely from the ratified text of DOC-06 (spec/12-how-it-works.md), UI-32
(spec/03-use-cases.md), the DOC-06 completeness contract's five required
sections, TC-DOC-01's scenarios (spec/04-test-cases.md), and the mandatory
config values in spec/06-configuration.md. No code was read to produce this
draft, per the coding-agent contract's read-only boundary on `spec/`.

**Every place the ratified spec does not itself state a concrete fact — a
command, a version number, a file path, a port — is marked `[ADVISER: …]`
inline and MUST be filled in (or corrected) by the adviser from the actual
build before this goes to the operator for ratification.** Nothing has been
guessed or filled in from memory of the codebase.

## Every [ADVISER] marker in this draft (index)

1. Section 1 — the exact prerequisite software/versions to install.
2. Section 1 — the exact command(s) that start the paper-mode server.
3. Section 1 — the exact command(s) that start the live-mode server, and how
   the operator confirms they are two genuinely separate processes/wirings.
4. Section 1 — the port number and URL the operator should open in a browser.
5. Section 1 — whether Docker (mentioned by REC-07/TC-ENT-07 as a recovery
   scenario) is the normal way this is run, or an optional deployment path.
6. Section 2 — the exact `.env` variable names tastytrade credentials are
   read from (the spec only states the naming *pattern* `TT_CERT_*` /
   `TT_PROD_*`, not the literal variable names field-by-field).
7. Section 2 — the exact variable name and default value for the data
   directory (the spec requires a durable store per REC-07 but does not name
   its configuration variable).
8. Section 2 — whether `bind_host` / `api_token` (NFR-06, doc 06) are set via
   `.env` or via a separate config file, since doc 06 lists them as "config"
   without naming a delivery mechanism.
9. Section 4 — the exact URL/button sequence for "start the bot" as a
   concrete first click, consistent with marker 2/4 above.

---

## 1. Prerequisites, and how this build actually runs

Before anything else, get the software installed and running on the machine
that will trade. This section describes only what the spec requires to be
true of the running system — the exact commands are filled in by the
adviser from the real build.

**What must be installed:**
`[ADVISER: state the exact prerequisite runtimes/tools and version numbers —
e.g. a specific Python version, a specific Node.js version, and however
dependencies are installed (a lockfile-driven install command for the
backend and for the frontend). The ratified spec states the technology
choices (Python backend, React/TypeScript frontend, spec/05-architecture-ddd.md
line 3) but not install steps.]`

**What the spec itself guarantees about how this runs, regardless of the
exact commands:**

- Paper (practice) mode and live (real money) mode are **built as two
  structurally separate wirings**, not one program with a switch flipped
  inside it — the part of the software that can send a real order to the
  broker is not even present when running in paper mode (EC-RSK-04, SIM-01).
  In practice this means starting "the bot" is really a choice between two
  different things to run: the paper build and the live build.
- Whichever one is running, the control panel by default only listens on
  the trading machine itself (`127.0.0.1`, meaning "this computer only") —
  it does not expose itself to the rest of a network unless deliberately
  configured to, and doing so requires an access token to be set first; this
  is enforced, not merely recommended (NFR-06).
- The **live** build specifically refuses to even finish starting up unless
  the panel password is configured (Section 2, NFR-06a) — there is no such
  thing as a live version of this bot with no password.

**The actual step-by-step run procedure:**

`[ADVISER: fill in the concrete, current procedure — e.g. "open a terminal
in the project folder, run <command>, then open <URL> in a browser" — for
both the paper build and the live build. State the port number and whether
the two builds run on the same port at different times or different ports
simultaneously.]`

`[ADVISER: state whether this normally runs inside Docker (mentioned only in
passing in the spec, as a recovery scenario the bot must survive — REC-07,
TC-ENT-07 — not as the documented normal way to start it) or as a plain
process, and give the one true procedure.]`

Once it's running, the very first thing the panel will ask for is the
password (Section 2) — nothing else is possible until it's unlocked.

## 2. The `.env` file — names and where to get them, never values

The bot reads its secrets and machine-specific settings from a file named
`.env`, kept on the trading machine only. **This tab shows you the template
below — the variable names and what each one is for — and nothing else. It
will never show a current value, a password, a token, or any other secret,
under any circumstance.** That's deliberate: a screen that could ever display
a live secret is a screen that could leak one.

The handling rule, stated plainly, because it matters more than any other
sentence on this tab: **every value in this file is typed by hand, directly
into the file, on the trading machine itself — never pasted into a chat
window, an AI coding assistant's conversation, a support ticket, a
screenshot, or anywhere else that leaves the machine.** If a credential is
ever compromised or simply needs to change, the entire rotation procedure is:
edit the `.env` file with the new value, then restart the bot. There is no
other step.

Annotated template (names and explanations only — **do not** put real values
in a copy of this table anywhere they could be seen by anyone else):

| Variable name | What it is | Where you get it |
|---|---|---|
| `MEIC_USER_PASSWORD` | The password that unlocks this control panel. Nothing that changes anything — arming, saving a schedule, firing an entry, closing a trade — is accepted until the panel is unlocked with it. The **live** build will not finish starting without this set at all (NFR-06a). | You choose this yourself. Pick something only you know; it is not issued by anyone. |
| `TT_CERT_*` (a group of variables) | Your tastytrade **practice/sandbox** credentials: a provider secret, a refresh token, and an account identifier, used only for practice-mode/contract testing against tastytrade's non-real-money sandbox. `[ADVISER: list the exact literal variable names in this group.]` | tastytrade's own OAuth application settings, in your tastytrade developer/API account area — request sandbox (certification) credentials there. |
| `TT_PROD_*` (a group of variables) | The same kind of credentials — a provider secret, a refresh token, an account identifier — but for your **real, production** tastytrade account. These are what the live build authenticates with. `[ADVISER: list the exact literal variable names in this group.]` | The same tastytrade OAuth application settings, but the production application/credentials, not the sandbox ones. |
| `MEIC_LIVE_IS_TEST` | The first of the two deliberate switches that must both be set before the bot will ever place a real order (Section 5). It must be explicitly set to mean "this is not a test" before production trading is even possible — one switch alone is never enough (NFR-06a). | You set this yourself, deliberately, only when you intend to go live. Leave it alone otherwise. |
| `MEIC_ALLOW_PRODUCTION` | The second of the two deliberate switches (Section 5) — a separate, explicit opt-in that must also be set, alongside `MEIC_LIVE_IS_TEST`, before production trading is possible (NFR-06a). | Same as above — you set this yourself, only when you deliberately mean to. |
| *(data directory location)* | Where the bot keeps its permanent, durable record of everything: armed/disarmed state, the schedule, every trade and event ever logged, calendar tags, and more — everything that must survive a restart exactly as it was (REC-07). `[ADVISER: state the exact variable name and default location.]` | Chosen by you (or left at its default) — a folder on the trading machine with enough disk space that you back up like any other important data. |
| *(panel network binding, if you ever need remote access)* | Controls whether the panel is reachable only from this machine (the default and recommended setting) or from elsewhere on a network, and the access token required if you do open it up (NFR-06). `[ADVISER: confirm the exact variable names, e.g. bind_host / api_token, and whether they belong in this file.]` | You set these yourself only if you have a specific, deliberate reason to reach the panel remotely; otherwise leave the default alone. |

## 3. The numbers live mode refuses to trade without

Three settings have no safe value the bot can silently assume for real money
— you must set them yourself before live trading is possible, because
guessing on your behalf would be worse than refusing to start:

- **`max_day_risk`** — the absolute dollar ceiling on how much the day's
  trading, all together, could lose in the worst case. This has **no
  default at all**; the bot will not let live mode turn on without it
  (spec/06-configuration.md validation rule 4, RSK-04). It exists so that no
  combination of entries you compose can ever add up to a risk you never
  actually agreed to.
- **`reporting_capital_base`** — the dollar figure the results dashboard
  measures your return against (return on capital, Sharpe, and similar
  figures). It also has no default, precisely because your account's total
  balance is deliberately *not* used automatically — mixing in money that
  has nothing to do with this strategy would quietly distort every
  performance number the dashboard shows you (RPT-04).
- **`min_buying_power`** — the buying-power floor below which the bot will
  skip an entry rather than risk running your account too thin
  (`insufficient_bp`). This one does ship with a conservative default
  ($5,000), but it should be tuned to the actual size of your account — the
  spec's own reference example runs it much lower, at $2,000, for a smaller
  account (spec/06-configuration.md, ENT-03).

## 4. The paper-first first-run sequence

Every fresh install starts in the same safe state, on purpose: **disarmed,
Confirm Live off, and in paper (practice) mode** — nothing can trade for
real money until you deliberately change all three of those, one at a time
(Chapter 3 of the "How it works" tab). The recommended order for a brand-new
machine:

1. `[ADVISER: state the exact first click/command — starting the paper
   build, per Section 1.]`
2. **Unlock the panel** with the password you set in Section 2.
3. **Confirm you're in PAPER mode.** A fresh install already boots this way
   — practice money only, nothing real at stake yet.
4. **Read the "How it works" tab.** It explains, in plain language, exactly
   what every part of the bot does before you ever compose a real trade.
5. **Compose a day's schedule** on the Trading tab — at least one entry row
   (time, contracts, target premium, wing width, stop settings).
6. **Run the pre-flight check** and confirm it passes.
7. **Press Arm — in paper mode.** Watch at least one full paper trading day
   run end to end before you touch live mode at all.
8. **Import the trading calendar** (Calendar tab) and set any standing
   blackout rules you want (for example, "always block FOMC days").
9. **Open "Operational tools"** (a collapsed section on the Trading tab,
   deliberately tucked out of the way so a brand-new install's screen shows
   only ordinary trading controls) and run the **outage drill** at least
   once — it proves, on demand, that your stop orders really do rest at the
   broker and keep working even if the bot itself is disconnected. Do this
   before you ever trust live mode with real money, and again any time
   tastytrade's API changes.

## 5. Going live — the two-switch ritual, and a plain warning

Everything above can be done safely with nothing but practice money at
stake. Going live is a deliberate, separate act, guarded by more than one
switch on purpose, so that no single mistake can ever aim this bot at a real
account by accident.

**The three switches that must all read "go" before any entry can ever fire**
(explained fully in Chapter 3 of "How it works"):

- **Armed** — the standing schedule is live.
- **Confirm Live** — a second, independent "yes, really" toggle.
- **Stop Trading** — must be *off* (its very purpose is to block new entries
  when turned on).

**Underneath all three, and specific to real money:** turning this software
loose on your actual brokerage account requires **two separate, deliberate
switches in its configuration**, not one — `MEIC_LIVE_IS_TEST` must be
explicitly set to mean "this is not a test," **and** `MEIC_ALLOW_PRODUCTION`
must also be explicitly turned on. The bot additionally double-checks, on
its own, that the broker credentials it was given are actually production
credentials and not sandbox ones. Flip only one of these and the bot refuses,
with a plain explanation of what's missing — by design, there is no way to
end up live by mistake (NFR-06a).

**The plain warning:** once every switch above is set, live means live —
the very next entry your schedule fires will be a real order, for real
money, at your real broker, arriving within minutes of arming. There is no
practice-mode safety net once these switches are thrown. If you have any
doubt at all, leave `MEIC_ALLOW_PRODUCTION` off and keep running in paper
until you don't.
