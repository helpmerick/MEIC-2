import { CommandPanel } from "./components/CommandPanel";
import { Dashboard } from "./components/Dashboard";
import { DayReportView } from "./components/DayReportView";
import { useLiveBot } from "./useLiveBot";

export function App() {
  const { state, report, connected, error, optimistic, refresh } = useLiveBot();

  return (
    <div className="app">
      <header className="app-header">
        <div className="brand">
          <h1>MEIC<span className="dot">.</span></h1>
          <span className="muted">control panel</span>
        </div>
        <div className="spacer" />
        <span className={`live-dot ${connected ? "" : "off"}`} title={connected ? "live" : "offline"} />
        {state && <span className={`mode-tag ${state.trading_mode}`}>{state.trading_mode}</span>}
      </header>

      {error && <div className="banner-error">Backend unreachable — {error}</div>}

      <main className="grid">
        <Dashboard state={state} connected={connected} />
        <CommandPanel state={state} optimistic={optimistic} refresh={refresh} />
        <div className="report"><DayReportView report={report} /></div>
      </main>

      <footer className="app-footer">
        Read-only projections + commands · no trading logic in the frontend (UI-03) · localhost-bound (NFR-06)
      </footer>
    </div>
  );
}
