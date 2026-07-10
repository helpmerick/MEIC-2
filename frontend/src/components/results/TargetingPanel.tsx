import { GapNote } from "./shared";

// RPT-05 targeting quality (selection gap / execution gap / wing drag,
// probe-depth histogram). KNOWN API-SHAPE GAP: neither /reports/summary nor
// /reports/day surfaces this decomposition in this slice — the backend's
// reporting.targeting module exists but reports.py never wires it into a
// response. Rendered as an honest gap, never a fabricated chart (UI-26).
export function TargetingPanel() {
  return (
    <div className="gap-block" data-testid="targeting-gap">
      <GapNote>
        Not yet captured by the API: RPT-05's selection-gap / execution-gap / wing-drag
        decomposition and the probe-depth histogram aren't exposed by /reports/summary or
        /reports/day in this slice, even though backend/src/meic/reporting/targeting.py already
        computes the underlying values. This needs a small backend addition (a
        `targeting` block on the summary response) before the frontend can render it honestly.
      </GapNote>
    </div>
  );
}
