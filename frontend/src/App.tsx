import { useCallback, useState } from "react";
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

// NFR-06: the panel's API token. `live_app` requires it — every command (Arm,
// Confirm Live, ▶, Flatten) carries it in the x-api-token header. Stored in this
// browser's localStorage only; it's the SAME string you put in MEIC_API_TOKEN.
function ApiTokenControl() {
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState(getApiToken());
  const set = getApiToken().length > 0;

  function save() {
    setApiToken(value);
    setOpen(false);
    // reload so the very next command uses it (and the WS reconnects with it)
    window.location.reload();
  }

  if (!open) {
    return (
      <button className="theme-toggle" onClick={() => setOpen(true)}
              title={set ? "API token is set — click to change" : "Set the panel API token (MEIC_API_TOKEN)"}
              aria-label="API token">
        {set ? "🔐" : "🔓"}
      </button>
    );
  }
  return (
    <span className="token-field">
      <input
        aria-label="api token"
        type="password"
        autoFocus
        placeholder="paste MEIC_API_TOKEN"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter") save(); if (e.key === "Escape") setOpen(false); }}
      />
      <button className="btn" aria-label="save api token" onClick={save}>Save</button>
    </span>
  );
}
