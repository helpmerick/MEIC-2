// UI-31 (v1.73, queue slice 5) -- THE ONE vocabulary map for activity-feed
// hover tooltips. Keyed by the backend event-class name exactly as it
// appears in adapters/api/app.py's `_describe` table (mirrored onto each
// ActivityLine's additive `type` field) -- never by the human-readable
// `label` text, which is free to reword without breaking this lookup.
//
// Every entry is plain English, doc-12 vocabulary, written for a reader with
// zero prior knowledge of the bot (DOC-02 standard) -- the same standard the
// how-it-works guide (spec/12-how-it-works.md) holds itself to, so the feed
// and the guide "speak one language" (v1.73 ruling). COMPLETENESS IS
// ENFORCED: activityVocabulary.test.ts walks app.py's own `_describe` table
// and fails, naming the event type, the moment one exists there with no
// entry here (TC-UI-09 scenario 3) -- this file must never fall behind that
// table.
//
// This is also the SINGLE source slice 6's "Getting started" guide (and any
// future re-stamp of the how-it-works content) must draw from for the same
// concepts, so the tooltip wording and the guide prose can never fork.
export const ACTIVITY_VOCABULARY: Record<string, string> = {
  DayArmed:
    "The day's schedule went live: every armed row will try to fire at its scheduled time today, unless a safety check blocks it.",

  EntryWindowOpened:
    "A scheduled entry's two-minute firing window has begun. If the bot can't fire within it, that entry is skipped for good -- never fired late.",

  CondorProposed:
    "The bot picked strikes for a four-legged options trade (a condor) and is about to send all four legs to the broker as one single order.",

  CondorFilled:
    "All four legs of the condor filled. The trade is now open, and in this same instant the bot places its two protective stop orders.",

  StopPlaced:
    "A stop-loss order was sent to the broker for one leg of this trade. It rests at the broker, not inside the bot, so it keeps working even if the bot goes offline.",

  StopConfirmed:
    "The broker confirmed the stop order is live and working -- \"protected\" specifically means this: not just sent, but acknowledged by the broker.",

  // ShortStopped is journaled by MORE than the stop path: the event's
  // `initiator` is "resting_stop" (the broker's own stop order filled),
  // "watchdog_escalation" (the STP-03b backup fired the close after the
  // broker's trigger proved slow), or "decay" (the decay buyback, DCY-01/02
  // -- a PROACTIVE near-zero buyback with no trigger involved at all). The
  // definition below leads with what the event MEANS (the short was bought
  // back; that side is closed) and names both ways it happens, so it is
  // true of every path. The row's `detail` already carries the initiator
  // text; a future slice could specialize this wording per initiator --
  // deliberately NOT built now (feature freeze).
  ShortStopped:
    "This side's short option was bought back and is now closed. That happens one of two ways: the protective stop-loss fired because the market moved against it, or the bot proactively bought the short back for pennies after it had decayed to nearly nothing (the decay buyback, locking in the win early).",

  LongSaleStarted:
    "A stop just fired, so the bot began selling that side's surviving long option -- its insurance -- to recover whatever value it still has. This is the start of LEX (see \"Long sold (LEX)\").",

  // Delta 11, DRAFT-DOC-12-DELTA-v1.77-2026-07-15.md (docs/doc06-draft-and-
  // guide-delta branch, "What LEX means") -- the canonical passage: LEX
  // names the *long exit*. A stopped short's insurance (the long leg) is
  // still in the account and now worth more than it cost; LEX sells it and
  // turns that value into cash, walking the price down every 15 seconds
  // toward the buyer's price before guaranteeing a fill. Condensed here for
  // tooltip length -- if the ratified wording changes, this follows it, never
  // forks from it (per the delta's own vocabulary note).
  // The trailing clause names BOTH sale paths, so the one definition is true
  // of every LongSold: the normal ladder walk (LEX-03/05) AND the EC-LEX-08
  // no-bid case, where LEX never walks -- it RESTS a single order at the
  // option's fair floor value (the LEX-04 intrinsic floor) until a buyer
  // appears. Delta 11's own passage carries the same floor branch, so the
  // tooltip and the guide stay in parity.
  LongSold:
    "LEX names the long exit. When a stop fires, the short's insurance leg is still in the account and now worth more than it cost -- LEX sells it and turns that value back into cash, usually by walking the price down every fifteen seconds until it fills, or resting at its fair floor value if there's truly no buyer.",

  SideClosed:
    "This side of the trade -- a short option and its matching long -- is now fully wound down. Nothing is left open on it.",

  SideExpired:
    "This leg simply expired worthless at the close. No money changed hands on it.",

  SettlementRecorded:
    "The broker's own settlement record has been captured for a position held to expiry. The bot uses this real, broker-reported number instead of its own estimate.",

  EntryClosed:
    "This whole trade was closed out -- every leg either bought back, sold, or expired -- and is now finished for the day.",

  EntrySkipped:
    "A scheduled or manual entry did not fire. The reason is shown alongside it (for example, a safety check failed, or its window passed).",

  WatchdogEscalated:
    "The stop watchdog stepped in: a short's price passed its trigger and the broker's own stop hadn't fired yet, so the bot placed its own closing order as a backup.",

  DayCompleted:
    "The trading day's end-of-day checklist is done: every order the bot placed today is accounted for, and the day is finished.",

  ModeSwitchStaged:
    "A switch between paper (practice) and live (real money) mode was requested. It takes effect at the start of the next trading day, never mid-day.",
};
