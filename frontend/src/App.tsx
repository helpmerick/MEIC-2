import { CommandPanel } from "./components/CommandPanel";
import { Dashboard } from "./components/Dashboard";
import { DayReportView } from "./components/DayReportView";
import { useLivePanel } from "./useLivePanel";

export function App() {
  const { state, report, connected, error, refresh } = useLivePanel();

  return (
    <div className="app">
      <header className="app-header">
        <h1>MEIC <span className="muted">control panel</span></h1>
        {state && <span className={`mode-tag ${state.trading_mode}`}>{state.trading_mode.toUpperCase()}</span>}
      </header>

      {error && <div className="banner banner-error">Backend unreachable: {error}</div>}

      <main className="grid">
        <Dashboard state={state} connected={connected} />
        <CommandPanel state={state} onChange={refresh} />
        <DayReportView report={report} />
      </main>

      <footer className="app-footer muted">
        Read-only projections + commands · no trading logic in the frontend (UI-03) ·
        localhost-bound (NFR-06)
      </footer>
    </div>
  );
}
