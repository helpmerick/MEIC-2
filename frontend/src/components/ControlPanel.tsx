import { CommandPanel } from "./CommandPanel";
import { Dashboard } from "./Dashboard";
import type { PanelState } from "../types";

// One "Control" card combining live status and the command buttons (operator
// request 2026-07-12: they were two adjacent cards saying overlapping things).
// The status reads at the top as the headline; the commands sit below a divider.
// Both children are card-less content components, so this owns the single card.
export function ControlPanel({
  state, connected, optimistic, refresh,
}: {
  state: PanelState | null;
  connected: boolean;
  optimistic: (patch: Partial<PanelState>) => void;
  refresh: () => void;
}) {
  return (
    <section className="card control-card">
      <h2>Control</h2>
      <Dashboard state={state} connected={connected} />
      <div className="control-divider" />
      <CommandPanel state={state} optimistic={optimistic} refresh={refresh} />
    </section>
  );
}
