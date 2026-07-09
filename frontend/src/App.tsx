import { useCallback, useEffect, useState } from "react";
import { api, ApiError, getApiToken, setApiToken, type OutageDrill } from "./api";
import { ActivityFeed } from "./components/ActivityFeed";
import { CommandPanel } from "./components/CommandPanel";
import { Dashboard } from "./components/Dashboard";
import { DayReportView } from "./components/DayReportView";
import { EntryCards } from "./components/EntryCards";
import { SchedulePanel } from "./components/SchedulePanel";
import { useLiveBot } from "./useLiveBot";
import { useTheme } from "./useTheme";

export function App() {
  const { state, report, entries, activity, connected, error, optimistic, refresh } = useLiveBot();
  const [theme, toggleTheme] = useTheme();
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
      flash(r.result === "closed" ? `Closed ${id}` : `${id}: ${r.result}`, "ok");
      refresh();
    } catch (e) {
      flash(`Close failed: ${e instanceof ApiError ? e.detail : String(e)}`, "err");
    }
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
  const runOutageDrill = useCallback(async () => {
    setDrilling(true);
    try {
      const r = await api.outageDrill();
      setDrill(r);
      flash(r.survived ? "Outage drill passed — stops stayed working"
        : "Outage drill: no resting stops to test", r.survived ? "ok" : "err");
    } catch (e) {
      flash(`Drill failed: ${e instanceof ApiError ? e.detail : String(e)}`, "err");
    } finally {
      setDrilling(false);
    }
  }, [flash]);

  return (
    <div className="app">
      <header className="app-header">
        <div className="brand">
          <h1>MEIC<span className="dot">.</span></h1>
          <span className="muted">control panel</span>
        </div>
        <div className="spacer" />
        <button className="btn drill-btn" onClick={runOutageDrill} disabled={drilling}
                title="UC-12: simulate a bot outage and verify stops stay working">
          {drilling ? <span className="spin" /> : null}{drilling ? "Drilling…" : "Outage drill"}
        </button>
        <button className="btn danger flatten-btn" onClick={flattenAll} title="Close every open entry (typed confirmation)">
          Flatten all
        </button>
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

      {drill && (
        <div className={`drill-result ${drill.survived ? "ok" : "warn"}`}>
          <button className="drill-x" onClick={() => setDrill(null)} aria-label="Dismiss">×</button>
          <strong>{drill.survived ? "✓ Stop independence drill passed" : "⚠ Drill inconclusive"}</strong>
          <span> — {drill.stops_before.length} resting stop(s), {drill.outage_seconds}s simulated outage:{" "}
            {drill.survived ? "all still working" : "no stops to test"}
            {drill.survived && `, timestamps ${drill.timestamps_unbroken ? "unbroken" : "CHANGED"}`}.</span>
          <div className="drill-note">{drill.honesty_note}</div>
        </div>
      )}

      <main className="grid">
        <Dashboard state={state} connected={connected} />
        <CommandPanel state={state} optimistic={optimistic} refresh={refresh} />
        {/* UC-02 composition + ENT-09 fire. The fire button follows the three
            trade-enabling states, exactly as UI-22 requires. */}
        <SchedulePanel entriesEnabled={state?.entries_enabled ?? false} />
        <div className="report"><DayReportView report={report} /></div>
        <div className="entries-col"><EntryCards entries={entries} onClose={closeEntry} /></div>
        <div className="feed-col"><ActivityFeed activity={activity} /></div>
      </main>

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
