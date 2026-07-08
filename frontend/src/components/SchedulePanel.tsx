// SCHEDULE & PARAMETERS — UC-02 composition + ENT-09 manual fire (UI-22).
//
// NO TRADING LOGIC LIVES HERE (UI-03). Every rule — contracts 1-10, the discrete
// stop-% set, wing-width steps, times strictly increasing and legal, per_side
// refused, RSK-04 — is enforced by the backend. This form's job is to render the
// rows, send them, and show back exactly what the server said, cell by cell.
//
// The day-total worst case beside `max_day_risk` is an ESTIMATE (v1.46):
// (width - target premium) x 100 x contracts. No strikes exist before selection,
// so the true number cannot be known here. RSK-04 re-prices from real strikes at
// fire time and can still veto an entry this panel showed as fitting.

import { useCallback, useEffect, useState } from "react";
import { api, ApiError, STOP_PCT_SET } from "../api";
import type {
  FirePreview,
  FireResult,
  Preflight,
  ScheduleError,
  ScheduleRow,
  ScheduleView,
} from "../types";

const BLANK: ScheduleRow = { time: "", contracts: "", target_premium: "", wing_width: "", stop_loss_pct: "" };

// Server-side is authoritative; this only decides which cell to outline in red.
function errorFor(errors: ScheduleError[], index: number, field: string): string | undefined {
  return errors.find((e) => e.index === index && e.field === field)?.reason;
}

function scheduleErrors(errors: ScheduleError[]): ScheduleError[] {
  return errors.filter((e) => e.index === null);
}

export function SchedulePanel({ entriesEnabled }: { entriesEnabled: boolean }) {
  const [view, setView] = useState<ScheduleView | null>(null);
  const [rows, setRows] = useState<ScheduleRow[]>([]);
  const [maxDayRisk, setMaxDayRisk] = useState("");
  const [errors, setErrors] = useState<ScheduleError[]>([]);
  const [saved, setSaved] = useState<string | null>(null);
  const [preflight, setPreflight] = useState<Preflight | null>(null);
  const [dialog, setDialog] = useState<FirePreview | null>(null);
  const [fireResult, setFireResult] = useState<{ n: number; result: FireResult } | null>(null);
  const [firing, setFiring] = useState(false);

  const load = useCallback(async () => {
    const v = await api.getSchedule();
    setView(v);
    setRows(v.rows.length ? v.rows : [{ ...BLANK }]);
    setMaxDayRisk(v.max_day_risk ?? "");
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const patch = (i: number, field: keyof ScheduleRow, value: string) =>
    setRows((rs) => rs.map((r, j) => (j === i ? { ...r, [field]: value } : r)));

  async function save() {
    setErrors([]);
    setSaved(null);
    try {
      const v = await api.saveSchedule(rows, maxDayRisk);
      setView(v);
      setRows(v.rows);
      setSaved(v.config_version);
      setPreflight(await api.getPreflight());
    } catch (e) {
      // 422 carries every error at once, so the operator fixes the form in one pass
      const detail = e instanceof ApiError ? (e.detail as { errors?: ScheduleError[] }) : null;
      setErrors(detail?.errors ?? [{ field: "form", reason: String(e), index: null }]);
    }
  }

  async function openFireDialog(n: number) {
    setFireResult(null);
    setDialog(await api.firePreview(n));
  }

  async function confirmFire() {
    if (!dialog) return;
    setFiring(true);
    try {
      // The press_id came from the preview. Confirming the same press twice is
      // ONE attempt (ENT-09) — the backend, not this button, guarantees that.
      const result = await api.fire(dialog.entry_number, dialog.press_id);
      setFireResult({ n: dialog.entry_number, result });
    } finally {
      setFiring(false);
      setDialog(null);
      void load();
    }
  }

  if (!view) return <section className="panel">Loading schedule…</section>;

  const rowErrors = errors.filter((e) => e.index !== null);
  const formErrors = scheduleErrors(errors);

  return (
    <section className="panel schedule-panel">
      <header className="panel-head">
        <h2>Schedule &amp; Parameters</h2>
        {view.config_version && <span className="version">config {view.config_version}</span>}
      </header>

      <table className="schedule">
        <thead>
          <tr>
            <th>Time (ET)</th>
            <th>Target $</th>
            <th>Width</th>
            <th>Stop %</th>
            <th>Count</th>
            <th>Worst case (est.)</th>
            <th aria-label="actions" />
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} data-testid={`row-${i}`}>
              <td>
                <input
                  aria-label={`time ${i + 1}`}
                  value={row.time ?? ""}
                  placeholder="10:00"
                  onChange={(e) => patch(i, "time", e.target.value)}
                  className={errorFor(rowErrors, i, "time") ? "invalid" : ""}
                />
              </td>
              <td>
                <input
                  aria-label={`target premium ${i + 1}`}
                  value={row.target_premium ?? ""}
                  placeholder="3.00"
                  onChange={(e) => patch(i, "target_premium", e.target.value)}
                  className={errorFor(rowErrors, i, "target_premium") ? "invalid" : ""}
                />
              </td>
              <td>
                <input
                  aria-label={`wing width ${i + 1}`}
                  value={row.wing_width ?? ""}
                  placeholder="50"
                  onChange={(e) => patch(i, "wing_width", e.target.value)}
                  className={errorFor(rowErrors, i, "wing_width") ? "invalid" : ""}
                />
              </td>
              <td>
                {/* The discrete set is the ONLY stop-% the backend accepts (STP-02) */}
                <select
                  aria-label={`stop pct ${i + 1}`}
                  value={String(row.stop_loss_pct ?? "")}
                  onChange={(e) => patch(i, "stop_loss_pct", e.target.value)}
                  className={errorFor(rowErrors, i, "stop_loss_pct") ? "invalid" : ""}
                >
                  <option value="">default</option>
                  {STOP_PCT_SET.map((p) => (
                    <option key={p} value={p}>{p}%</option>
                  ))}
                </select>
              </td>
              <td>
                {/* ENT-04 (v1.44): each row trades its OWN size */}
                <input
                  aria-label={`contracts ${i + 1}`}
                  type="number"
                  min={1}
                  max={10}
                  value={row.contracts ?? ""}
                  placeholder="1"
                  onChange={(e) => patch(i, "contracts", e.target.value)}
                  className={errorFor(rowErrors, i, "contracts") ? "invalid" : ""}
                />
              </td>
              <td className="numeric" data-testid={`wc-${i}`}>
                {row.worst_case_estimate ? `$${row.worst_case_estimate}` : "—"}
              </td>
              <td className="actions">
                <button
                  aria-label={`fire entry ${i + 1}`}
                  title={entriesEnabled ? "Fire this entry now (ENT-09)" : "Blocked: entries are not enabled"}
                  disabled={!entriesEnabled}
                  onClick={() => void openFireDialog(i + 1)}
                >
                  ▶
                </button>
                <button
                  aria-label={`delete entry ${i + 1}`}
                  onClick={() => setRows((rs) => rs.filter((_, j) => j !== i))}
                >
                  ×
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {rowErrors.map((e, i) => (
        <p key={i} className="error" role="alert">
          Row {(e.index ?? 0) + 1}: {e.field} — {e.reason}
        </p>
      ))}
      {formErrors.map((e, i) => (
        <p key={`f${i}`} className="error" role="alert">
          {e.field} — {e.reason}
        </p>
      ))}

      <div className="schedule-foot">
        <button onClick={() => setRows((rs) => [...rs, { ...BLANK }])}>+ Add entry</button>

        <label className="risk">
          Max day risk
          <input
            aria-label="max day risk"
            value={maxDayRisk}
            placeholder="required before live"
            onChange={(e) => setMaxDayRisk(e.target.value)}
          />
        </label>

        {/* v1.46: the ceiling sits beside the composed day total, so adding a row
            visibly eats headroom. */}
        <dl className="risk-readout" data-testid="risk-readout">
          <dt>Day total (est.)</dt>
          <dd className={view.exceeds_max_day_risk ? "over" : ""}>${view.day_total_estimate}</dd>
          <dt>Headroom</dt>
          <dd data-testid="headroom">{view.headroom === null ? "—" : `$${view.headroom}`}</dd>
        </dl>

        <button className="primary" onClick={() => void save()}>Save</button>
      </div>

      {view.exceeds_max_day_risk && (
        <p className="warn" role="alert">
          Composed day total exceeds max day risk. RSK-04 will veto entries at fire time.
        </p>
      )}
      <p className="note">{view.estimate_note}</p>
      {saved && <p className="ok" role="status">Saved as config {saved}.</p>}

      {preflight && <PreflightList preflight={preflight} />}

      {dialog && (
        <FireDialog
          preview={dialog}
          busy={firing}
          onCancel={() => setDialog(null)}
          onConfirm={() => void confirmFire()}
        />
      )}

      {fireResult && (
        <p className={fireResult.result.result === "filled" ? "ok" : "warn"} role="status">
          Entry {fireResult.n}:{" "}
          {fireResult.result.result === "filled"
            ? `filled (${fireResult.result.initiator})`
            : `${fireResult.result.result}${fireResult.result.reason ? ` — ${fireResult.result.reason}` : ""}`}
        </p>
      )}
    </section>
  );
}

// UC-02: the pre-flight checklist, pass/fail per item, in the backend's order.
export function PreflightList({ preflight }: { preflight: Preflight }) {
  return (
    <ul className="preflight" data-testid="preflight">
      {preflight.checks.map((c) => (
        <li key={c.name} className={c.passed ? "pass" : "fail"}>
          <span aria-hidden>{c.passed ? "✓" : "✗"}</span>
          <strong>{c.name}</strong> <em>({c.rule})</em>
          {c.detail && <span className="detail"> — {c.detail}</span>}
        </li>
      ))}
    </ul>
  );
}

// UI-22: a simple OK dialog (operator-ratified: NOT typed), shown in BOTH paper
// and live. Nothing is submitted until OK is pressed.
export function FireDialog({
  preview,
  busy,
  onCancel,
  onConfirm,
}: {
  preview: FirePreview;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Confirm manual entry">
      <div className="modal">
        <h3>Fire entry {preview.entry_number} now?</h3>
        <p className="muted">{new Date(preview.now).toLocaleTimeString()} — outside any scheduled window (ENT-09)</p>

        <dl className="fire-params">
          <dt>Contracts</dt><dd>{preview.contracts}</dd>
          <dt>Target premium</dt><dd>${preview.target_premium}</dd>
          <dt>Wing width</dt><dd>{preview.wing_width}</dd>
          <dt>Stop</dt><dd>{preview.stop_loss_pct}%</dd>
        </dl>

        <p className="estimate" data-testid="fire-estimate">
          <strong>Worst case (ESTIMATE): ${preview.worst_case_estimate}</strong>
        </p>
        <p className="note">
          {preview.estimate_formula}. Strikes are not selected until the entry fires, so
          this is an estimate — the RSK-04 check runs on the real strikes and may still
          veto this entry.
        </p>

        <div className="modal-actions">
          <button onClick={onCancel} disabled={busy}>Cancel</button>
          <button className="primary" onClick={onConfirm} disabled={busy} autoFocus>
            {busy ? "Firing…" : "OK"}
          </button>
        </div>
      </div>
    </div>
  );
}
