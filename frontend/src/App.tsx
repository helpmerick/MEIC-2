import { useCallback, useState } from "react";
import { api, ApiError } from "./api";
import { ActivityFeed } from "./components/ActivityFeed";
import { CommandPanel } from "./components/CommandPanel";
import { Dashboard } from "./components/Dashboard";
import { DayReportView } from "./components/DayReportView";
import { EntryCards } from "./components/EntryCards";
import { useLiveBot } from "./useLiveBot";
import { useTheme } from "./useTheme";

export function App() {
  const { state, report, entries, activity, connected, error, optimistic, refresh } = useLiveBot();
  const [theme, toggleTheme] = useTheme();
  const [toast, setToast] = useState<{ text: string; kind: "ok" | "err" } | null>(null);

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

  return (
    <div className="app">
      <header className="app-header">
        <div className="brand">
          <h1>MEIC<span className="dot">.</span></h1>
          <span className="muted">control panel</span>
        </div>
        <div className="spacer" />
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
        <span className={`live-dot ${connected ? "" : "off"}`} title={connected ? "live" : "offline"} />
        {state && <span className={`mode-tag ${state.trading_mode}`}>{state.trading_mode}</span>}
      </header>

      {error && <div className="banner-error">Backend unreachable — {error}</div>}

      <main className="grid">
        <Dashboard state={state} connected={connected} />
        <CommandPanel state={state} optimistic={optimistic} refresh={refresh} />
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
