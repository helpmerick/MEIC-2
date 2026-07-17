import { useCallback, useEffect, useState } from "react";
import { api, ApiError, getApiToken, setApiToken, type OutageDrill } from "./api";
import { ActivityFeed } from "./components/ActivityFeed";
import { CalendarPage } from "./components/CalendarPage";
import { ControlPanel } from "./components/ControlPanel";
import { DayDrilldown } from "./components/results/DayDrilldown";
import { GettingStartedPage } from "./components/GettingStartedPage";
import { HowItWorksPage } from "./components/HowItWorksPage";
import { ResultsPage } from "./components/results/ResultsPage";
import { DayReportView } from "./components/DayReportView";
import { DayTradesTable } from "./components/DayTradesTable";
import { EntryCards } from "./components/EntryCards";
import { ManualTradeCard } from "./components/ManualTradeCard";
import { NextEntryCountdown } from "./components/NextEntryCountdown";
import { SchedulePanel } from "./components/SchedulePanel";
import { useHashRoute } from "./router";
import { useLiveBot } from "./useLiveBot";
import { useTheme } from "./useTheme";

export function App() {
  const { state, report, entries, activity, connected, error, optimistic, refresh } = useLiveBot();
  const [theme, toggleTheme] = useTheme();
  // UI-27/CAL-08/UI-30/DOC-05/UI-32: every non-Trading page is a separate
  // client-side route sharing this one shell (header, auth, theme, mode tag)
  // — never a new app. The v1.75 commission fixes the nav at exactly five
  // tabs (Getting started joined the v1.71 four).
  const route = useHashRoute();
  const onTrading = route.page === "trading";
  // CAL-06 (v1.71): today's ET NO-TRADE label (or null), read straight off the
  // read model (UI-03/DAY-03 — the frontend never computes an ET trading day
  // itself) and threaded to both ▶ manual-fire surfaces below.
  const todayBlackoutLabel = state?.today_blackout_label ?? null;
  const [toast, setToast] = useState<{ text: string; kind: "ok" | "err" } | null>(null);
  const [drill, setDrill] = useState<OutageDrill | null>(null);
  const [drilling, setDrilling] = useState(false);

  const flash = useCallback((text: string, kind: "ok" | "err") => {
    setToast({ text, kind });
    window.setTimeout(() => setToast(null), 3000);
  }, []);

  // UI-16: Close fires instantly, no dialog; failures land as a toast, never a
  // blocking modal. The backend does all validation (UI-03).
  const closeEntry = useCallback(async (id: string) => {
    try {
      const r = await api.closeEntry(id);
      // CLS-03: a WORKING entry's Close is a Cancel entry — `cancelled` is its
      // clean outcome; `race_detected` means the entry FILLED in the
      // click→cancel window (the backend already raised the critical alert and
      // journaled the mismatch) and must never read like a clean cancel.
      if (r.result === "race_detected") {
        flash(`${id}: cancel raced a fill — position may be live, check alerts`, "err");
      } else if (r.result === "cancelled") {
        flash(`Cancelled entry ${id}`, "ok");
      } else {
        flash(r.result === "closed" ? `Closed ${id}` : `${id}: ${r.result}`, "ok");
      }
      refresh();
    } catch (e) {
      flash(`Close failed: ${e instanceof ApiError ? e.detail : String(e)}`, "err");
    }
  }, [flash, refresh]);

  // v1.58 TPF/TPT: set/raise/lower/clear per entry (UI-13/14/15). Server-side
  // gap validation is authoritative (UI-03) -- a 422 lands as a toast, never
  // a blocking dialog, exactly like Close's own failure path above.
  // A rejected level (the gap rule, TPF-02/TPT-03) comes back as ApiError(422)
  // with a precise `detail.reason` -- the backend is authoritative (UI-03),
  // this only relays its verdict as a toast, never a blocking dialog.
  const setFloor = useCallback(async (id: string, level: number) => {
    try { await api.setTpf(id, level); flash(`Floor ${level}% armed on ${id}`, "ok"); refresh(); }
    catch (e) {
      const detail = e instanceof ApiError ? e.detail : String(e);
      const reason = typeof detail === "object" && detail && "reason" in detail
        ? String((detail as { reason: unknown }).reason) : String(detail);
      flash(`Floor rejected: ${reason}`, "err");
    }
  }, [flash, refresh]);

  const clearFloor = useCallback(async (id: string) => {
    try { await api.clearTpf(id); flash(`Floor cleared on ${id}`, "ok"); refresh(); }
    catch (e) { flash(`Clear failed: ${e instanceof ApiError ? e.detail : String(e)}`, "err"); }
  }, [flash, refresh]);

  const setTarget = useCallback(async (id: string, level: number) => {
    try { await api.setTpt(id, level); flash(`Target ${level}% armed on ${id}`, "ok"); refresh(); }
    catch (e) {
      const detail = e instanceof ApiError ? e.detail : String(e);
      const reason = typeof detail === "object" && detail && "reason" in detail
        ? String((detail as { reason: unknown }).reason) : String(detail);
      flash(`Target rejected: ${reason}`, "err");
    }
  }, [flash, refresh]);

  const clearTarget = useCallback(async (id: string) => {
    try { await api.clearTpt(id); flash(`Target cleared on ${id}`, "ok"); refresh(); }
    catch (e) { flash(`Clear failed: ${e instanceof ApiError ? e.detail : String(e)}`, "err"); }
  }, [flash, refresh]);

  // Flatten all is the one action gated on a typed FLATTEN confirmation (TC-FLT-01).
  const flattenAll = useCallback(async () => {
    const typed = window.prompt('Type FLATTEN to close every open entry:');
    if (typed === null) return;
    try {
      const r = await api.flatten(typed);
      flash(`Flattened ${r.entries?.length ?? 0} entr${(r.entries?.length ?? 0) === 1 ? "y" : "ies"}`, "ok");
      refresh();
    } catch (e) {
      flash(e instanceof ApiError && e.status === 400 ? "Flatten needs the typed FLATTEN confirmation"
        : `Flatten failed: ${e instanceof ApiError ? e.detail : String(e)}`, "err");
    }
  }, [flash, refresh]);


  // UC-12: the stop-independence drill — simulate a bot outage and show the
  // evidence that resting stops stayed working throughout.
  //
  // v1.56: LIVE mode requires the operator to type DRILL (mirroring the
  // typed FLATTEN confirmation above) — a deliberate, supervised action
  // against the real broker, never a stray one-click. PAPER needs none: it
  // proves less (SIM-06) and touches no real session.
  const runOutageDrill = useCallback(async () => {
    let confirmation = "";
    if (state?.trading_mode === "live") {
      const typed = window.prompt("LIVE drill — type DRILL to sever the bot's own broker/data sessions:");
      if (typed === null) return;
      confirmation = typed;
    }
    setDrilling(true);
    try {
      const r = await api.outageDrill(confirmation);
      setDrill(r);
      flash(r.survived ? "Outage drill passed — stops stayed working"
        : "Outage drill: no resting stops to test", r.survived ? "ok" : "err");
    } catch (e) {
      flash(e instanceof ApiError && e.status === 400 ? "Drill needs the typed DRILL confirmation"
        : `Drill failed: ${e instanceof ApiError ? e.detail : String(e)}`, "err");
    } finally {
      setDrilling(false);
    }
  }, [flash, state?.trading_mode]);

  // 2026-07-16 operator order (informed reversal of v1.76): the drill's
  // visible trigger is gone from the UI (see the note at the header render
  // below), but the WIRING above stays untouched and ready — runOutageDrill
  // with its typed-DRILL gate, api.outageDrill, the /drill/outage endpoint
  // and every backend UC-12 piece, and the evidence banner below. These void
  // references keep tsc's noUnusedLocals honest about deliberately-retained,
  // currently-untriggered machinery.
  void runOutageDrill;
  void drilling;

  return (
    <div className="app">
      <header className="app-header">
        <div className="brand">
          <h1>MEIC<span className="dot">.</span></h1>
          <span className="muted">control panel</span>
        </div>
        {/* UI-27/CAL-08/UI-30/DOC-05/UI-32: instant client-side switching
            between every page — a hash change, never a server round trip.
            Exactly five tabs, in the ruled order (v1.75 operator
            commission): Trading | Results | Calendar | How it works |
            Getting started. */}
        <nav className="app-nav" aria-label="pages">
          <a className={`nav-link ${onTrading ? "active" : ""}`} href="#/">Trading</a>
          <a className={`nav-link ${route.page === "results" || route.page === "results-day" ? "active" : ""}`}
             href="#/results">Results</a>
          <a className={`nav-link ${route.page === "calendar" ? "active" : ""}`} href="#/calendar">Calendar</a>
          <a className={`nav-link ${route.page === "how-it-works" ? "active" : ""}`}
             href="#/how-it-works">How it works</a>
          <a className={`nav-link ${route.page === "getting-started" ? "active" : ""}`}
             href="#/getting-started">Getting started</a>
        </nav>
        <div className="spacer" />
        {onTrading && (
          /* 2026-07-16 operator order (an INFORMED reversal of v1.76's
             "never removed" ruling — the operator saw that text and repeated
             the order): the "Operational tools" disclosure AND the
             Outage-drill button are removed from the UI entirely. Only the
             visible trigger is gone — the drill WIRING stays untouched (see
             the retention note by runOutageDrill above). Until the adviser's
             spec delta lands, DOC-06 step 9 / guide ch.7 describe a control
             that no longer exists — a known, flagged spec-text staleness,
             handled by amendment, not by code. Flatten all is unchanged. */
          <button className="btn danger flatten-btn" onClick={flattenAll} title="Close every open entry (typed confirmation)">
            Flatten all
          </button>
        )}
        <button
          className="theme-toggle"
          onClick={toggleTheme}
          title={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}
          aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}
        >
          {theme === "dark" ? "☀️" : "🌙"}
        </button>
        <ApiTokenControl />
        <span className={`live-dot ${connected ? "" : "off"}`} title={connected ? "live" : "offline"} />
        {state && (
          // The mode reflects which PROCESS is running (paper_app = simulator,
          // live_app = real broker). You don't toggle it here — you launch the
          // corresponding app. It's a status indicator, not a switch.
          <span className={`mode-tag ${state.trading_mode}`}
                title={state.trading_mode === "live"
                  ? "LIVE — bound to the real broker (this is the live_app process)"
                  : "PAPER — simulator (the paper_app demo). Launch live_app for real trading."}>
            {state.trading_mode === "live" ? "● LIVE" : "○ PAPER"}
          </span>
        )}
      </header>

      {error && <div className="banner-error">Backend unreachable — {error}</div>}

      {onTrading && drill && (
        <div className={`drill-result ${drill.survived ? "ok" : "warn"}`}>
          <button className="drill-x" onClick={() => setDrill(null)} aria-label="Dismiss">×</button>
          <strong>{drill.survived ? "✓ Stop independence drill passed" : "⚠ Drill inconclusive"}</strong>
          <span> — {drill.stops_before.length} resting stop(s), {drill.outage_seconds}s simulated outage:{" "}
            {drill.survived ? "all still working" : "no stops to test"}
            {drill.survived && `, timestamps ${drill.timestamps_unbroken ? "unbroken" : "CHANGED"}`}.</span>
          <div className="drill-note">{drill.honesty_note}</div>
          {drill.guidance?.length > 0 && (
            <ul className="drill-guidance" data-testid="drill-guidance">
              {drill.guidance.map((g) => <li key={g}>⚠ {g}</li>)}
            </ul>
          )}
        </div>
      )}

      {onTrading ? (
        <main className="grid">
          <ControlPanel state={state} connected={connected} optimistic={optimistic} refresh={refresh} />
          {/* ENT-11/UI-25: the ad-hoc lane sits beside Control (operator layout
              2026-07-12) — fire NOW with explicit parameters, plus a read-only
              Simulate. Follows the same entries-enabled gate. */}
          <ManualTradeCard entriesEnabled={state?.entries_enabled ?? false}
                           todayBlackoutLabel={todayBlackoutLabel} />
          {/* ENT-10 / UI-24: visible evidence the schedule is being watched. */}
          <NextEntryCountdown />
          {/* UC-02 composition + ENT-09 fire. The fire button follows the three
              trade-enabling states, exactly as UI-22 requires. */}
          <SchedulePanel entriesEnabled={state?.entries_enabled ?? false}
                         todayBlackoutLabel={todayBlackoutLabel} />
          <div className="report"><DayReportView report={report} entries={entries} /></div>
          <div className="entries-col">
            <EntryCards entries={entries} onClose={closeEntry}
                        onSetFloor={setFloor} onClearFloor={clearFloor}
                        onSetTarget={setTarget} onClearTarget={clearTarget} />
          </div>
          <div className="feed-col"><ActivityFeed activity={activity} /></div>
          {/* RPT-17/UI-33 (v1.82): the day-trades table + Timing & Unmanaged
              report sit at the BOTTOM of the Trading tab (operator
              commission, pulled through the freeze by explicit fiat). */}
          <div className="day-table-col"><DayTradesTable /></div>
        </main>
      ) : route.page === "results-day" ? (
        <DayDrilldown date={route.date} />
      ) : route.page === "calendar" ? (
        <CalendarPage />
      ) : route.page === "how-it-works" ? (
        <HowItWorksPage />
      ) : route.page === "getting-started" ? (
        <GettingStartedPage />
      ) : (
        <ResultsPage entries={entries} />
      )}

      {toast && <div className={`toast ${toast.kind}`}>{toast.text}</div>}

      <footer className="app-footer">
        Read-only projections + commands · no trading logic in the frontend (UI-03) · localhost-bound (NFR-06)
      </footer>
    </div>
  );
}

// NFR-06: the User Password. `live_app` requires it — every command (Arm, Confirm
// Live, ▶, Flatten) carries it in the x-api-token header. Stored in this browser's
// localStorage only; it's the SAME string you put in MEIC_USER_PASSWORD in .env.
// Each request reads it live, so there is NO reload — we VALIDATE it against the
// backend (/auth/check) and tell the operator whether it was accepted.
type TokenStatus = "idle" | "checking" | "ok" | "bad";

function ApiTokenControl() {
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState(getApiToken());
  const [status, setStatus] = useState<TokenStatus>("idle");
  const [toast, setToast] = useState<{ text: string; ok: boolean } | null>(null);

  // A little popup that flashes the result, then fades on its own.
  function flash(text: string, ok: boolean) {
    setToast({ text, ok });
    window.setTimeout(() => setToast((t) => (t?.text === text ? null : t)), 3500);
  }

  // On mount, verify any already-stored password so the control reflects reality
  // (Unlocked only when the backend actually accepts it), not just "a string exists".
  useEffect(() => {
    if (!getApiToken()) return;
    setStatus("checking");
    api.authCheck()
      .then(() => { setStatus("ok"); flash("Password accepted", true); })
      .catch(() => { setStatus("bad"); flash("Wrong password", false); });
  }, []);

  async function save() {
    setApiToken(value);            // each request reads this live — no reload needed
    if (!value.trim()) { setStatus("idle"); setToast(null); setOpen(false); return; }
    setStatus("checking");
    try {
      await api.authCheck();       // 200 only if the password matches MEIC_USER_PASSWORD
      setStatus("ok");
      setOpen(false);
      flash("Password accepted", true);
    } catch {
      setStatus("bad");            // 401 -> wrong password; stay open so it can be fixed
      flash("Wrong password", false);
    }
  }

  // Intuitive direction: UNLOCKED (🔓) = authenticated, you CAN command; LOCKED
  // (🔒) = you must enter the password first. A green ✓ / red ✗ sits right next to
  // it, and a popup flashes the result on each check.
  const mark = status === "ok"
    ? <span className="auth-mark ok" role="status" aria-label="password correct">✓</span>
    : status === "bad"
      ? <span className="auth-mark bad" role="alert" aria-label="password wrong">✗</span>
      : null;

  const popup = toast && (
    <span className={`auth-toast ${toast.ok ? "ok" : "bad"}`} role="status">
      {toast.ok ? "✓" : "✗"} {toast.text}
    </span>
  );

  if (!open) {
    const unlocked = status === "ok";
    const label = unlocked ? "Unlocked"
      : status === "bad" ? "Rejected"
      : status === "checking" ? "Checking…" : "Locked";
    const icon = unlocked ? "🔓" : status === "bad" ? "⚠️" : "🔒";
    const title = unlocked
      ? "Password accepted — commands are enabled (Arm, Confirm Live, ▶). Click to change or clear it."
      : status === "bad" ? "Password was rejected — click to re-enter."
      : "Enter your User Password to enable commands.";
    return (
      <span className="auth-wrap">
        <button className={`auth-pill ${status}`} onClick={() => setOpen(true)}
                title={title} aria-label="user password">
          <span aria-hidden>{icon}</span> {label} {mark}
        </button>
        {popup}
      </span>
    );
  }
  return (
    <span className="auth-wrap">
      <span className="token-field">
        <input
          aria-label="user password"
          type="password"
          autoFocus
          placeholder="User Password"
          value={value}
          onChange={(e) => { setValue(e.target.value); if (status === "bad") setStatus("idle"); }}
          onKeyDown={(e) => { if (e.key === "Enter") void save(); if (e.key === "Escape") setOpen(false); }}
        />
        <button className="btn" aria-label="save user password"
                disabled={status === "checking"} onClick={() => void save()}>
          {status === "checking" ? "Checking…" : "Save"}
        </button>
        {mark}
      </span>
      {popup}
    </span>
  );
}
