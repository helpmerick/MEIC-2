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
// fire time and can still veto an entry this panel showed as fitting. Every
// surface that shows the number says so.

import { useCallback, useEffect, useState } from "react";
import { api, ApiError, DEFAULT_STOP_PCT, STOP_PCT_SET } from "../api";
import { isValidStopRebateMarkup, normalizeMoneyInput, stopRebateMarkupWorstCase } from "../money";
import { Tooltip } from "./Tooltip";
import {
  etToZone,
  isMilitaryTime,
  RTH_CLOSE_LABEL,
  RTH_OPEN_LABEL,
  withinMarketHours,
} from "../time";
import type {
  FirePreview,
  FireResult,
  Preflight,
  ScheduleError,
  ScheduleRow,
  ScheduleView,
} from "../types";

// A new row carries the stop-% default outright. Every other cell stays blank so
// the backend inherits its global (doc 06 section 37) — stop % is the one field
// the backend echoes back resolved, so showing "default" here promised an
// inheritance the round-trip never delivered.
// Operator-preferred prefills (2026-07-11): width 50 and a $0.30 long-recovery
// buffer on every NEW row (explicit row values the backend validates as
// overrides — doc 06's own config defaults are unchanged). Other cells stay
// blank so the backend inherits its global (doc 06 section 37).
const BLANK: ScheduleRow = { time: "", contracts: "", target_premium: "", wing_width: "50",
                             stop_loss_pct: DEFAULT_STOP_PCT, stop_rebate_markup: "0.30" };

// Server-side is authoritative; this only decides which cell to outline in red.
function errorFor(errors: ScheduleError[], index: number, field: string): string | undefined {
  return errors.find((e) => e.index === index && e.field === field)?.reason;
}

// A locally-detectable bad time: not 24-hour military, or outside market hours.
// The backend re-checks both; this just outlines the cell early.
function timeInvalid(value?: string): boolean {
  const raw = (value ?? "").trim();
  if (!raw) return false; // empty is "unfilled", not "wrong" — the backend flags it
  return !isMilitaryTime(raw) || !withinMarketHours(raw);
}

// Under each ET time: the operator's LOCAL equivalent (DST-aware, read live from
// the browser), or a precise reason the time is not yet valid. Times are ET; a
// UK operator entering 11:53 sees "≈ 16:53 local" and knows exactly when it
// fires their time. Labelled "local" rather than the IANA zone's city name
// (operator request 2026-07-11: "London" misleads a Manchester reader — the
// whole UK shares Europe/London). Military-only + market-open (09:30-16:00 ET)
// are enforced here for instant feedback and by the backend authoritatively.
function TimeHint({ value }: { value?: string }) {
  const raw = (value ?? "").trim();
  if (!raw) return null;
  if (!isMilitaryTime(raw)) {
    return (
      <span className="time-hint bad" data-testid="time-hint">
        24-hour HH:MM (e.g. 09:32)
      </span>
    );
  }
  if (!withinMarketHours(raw)) {
    return (
      <span className="time-hint bad" data-testid="time-hint">
        outside market hours ({RTH_OPEN_LABEL}–{RTH_CLOSE_LABEL} ET)
      </span>
    );
  }
  return (
    <span className="time-hint" data-testid="time-hint">
      ≈ {etToZone(raw)} local
    </span>
  );
}

// A locally-detectable bad stop_rebate_markup: outside $0.00-$5.00 or not a
// $0.05 step (STP-02b, doc 06 §60). The backend (domain/schedule.py) is
// authoritative and re-checks on Save; this only outlines the cell early —
// mirrors `timeInvalid` above exactly. Empty is "unfilled" (inherits the
// global default), never wrong.
function markupInvalid(value?: string): boolean {
  const raw = (value ?? "").trim();
  if (!raw) return false;
  return !isValidStopRebateMarkup(raw);
}

// UI-18: whenever a row's buffer is set, valid, and > 0, disclose the exact
// worst-case consequence BEFORE saving. The dollar worst-case figure stays
// VISIBLE under the box (that is what TC-STP-14 pins: "displays the
// worst-case increase before saving") — it mirrors domain/stop_policy.py's
// `markup_worst_case_increase` exactly, computed with exact BigInt digit
// arithmetic (money.ts), never float, on the SAME `stop_loss_pct` the row
// itself carries (or the panel's default, if the row's own cell is blank).
// The full UI-18 shortfall sentence lives behind a styled, focus- AND
// tap-capable Tooltip (role="tooltip") anchored to the visible figure —
// NEVER a native `title` attribute, which a touch or keyboard-only operator
// can never reach (v1.63 UI-18a Presentation ruling; wording unchanged).
function markupTooltip(row: ScheduleRow): string | undefined {
  const raw = (row.stop_rebate_markup ?? "").trim();
  if (!raw || markupInvalid(raw)) return undefined;
  const val = normalizeMoneyInput(raw);
  if (!(Number(val) > 0)) return undefined;
  const pct = row.stop_loss_pct || DEFAULT_STOP_PCT;
  return `If the long recovers less than $${val}, your net loss exceeds ${pct}% by the shortfall.`;
}

// Shared id between the Tooltip bubble and the buffer <input>'s
// aria-describedby, so a keyboard/screen-reader operator tabbed into the
// input reaches the same disclosure as the visible figure's trigger.
function markupTooltipId(index: number): string {
  return `markup-tooltip-content-${index}`;
}

function MarkupHint({ row, index }: { row: ScheduleRow; index: number }) {
  const raw = (row.stop_rebate_markup ?? "").trim();
  if (!raw) return null;
  if (markupInvalid(raw)) {
    return (
      <span className="time-hint bad" data-testid={`markup-hint-${index}`}>
        $0.00–$5.00, $0.05 steps
      </span>
    );
  }
  const val = normalizeMoneyInput(raw);
  if (!(Number(val) > 0)) return null;
  const contracts = Number(row.contracts) || 1;
  const sentence = markupTooltip(row);
  return (
    <span className="time-hint" data-testid={`markup-hint-${index}`}>
      worst case +${stopRebateMarkupWorstCase(val, contracts)}
      {sentence && (
        <Tooltip
          id={markupTooltipId(index)}
          content={sentence}
          testId={`markup-tooltip-${index}`}
          label={`shortfall detail, row ${index + 1}`}
        />
      )}
    </span>
  );
}

// Machine reason codes -> operator words (UI-06 spirit: a machine-readable
// reason is always shown in human words). Unknown codes fall through
// VERBATIM — never hide what the server actually said.
const REASON_TEXT: Record<string, string> = {
  not_strictly_increasing:
    "must be later than the row above — rows run in time order, earliest first",
  out_of_range: "outside the allowed range",
  bad_step: "not on the allowed step",
};

/** Fraction of the ceiling the composed day already consumes. */
function usedFraction(view: ScheduleView): number | null {
  if (view.max_day_risk === null) return null;
  const ceiling = Number(view.max_day_risk);
  if (!(ceiling > 0)) return null;
  return Number(view.day_total_estimate) / ceiling;
}

export function SchedulePanel({
  entriesEnabled, todayBlackoutLabel = null,
}: {
  entriesEnabled: boolean;
  // CAL-06 (v1.71): today's ET NO-TRADE label, or null. Passed straight from
  // the read model (UI-03/DAY-03) to the ▶ fire dialog's warn-and-acknowledge
  // panel — never computed here.
  todayBlackoutLabel?: string | null;
}) {
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
      // A 422 carries every error at once, so the operator fixes the form in one pass
      const detail = e instanceof ApiError ? (e.detail as { errors?: ScheduleError[] }) : null;
      setErrors(detail?.errors ?? [{ field: "form", reason: String(e), index: null }]);
    }
  }

  async function openFireDialog(n: number) {
    setFireResult(null);
    setDialog(await api.firePreview(n));
  }

  // CAL-06: `ack` only ever travels to the backend when today is tagged (the
  // dialog forces it true before OK is reachable in that case) — an
  // untagged day's fire is byte-identical to every pre-v1.71 call.
  async function confirmFire(ack: boolean) {
    if (!dialog) return;
    setFiring(true);
    try {
      // The press_id came from the preview. Confirming the same press twice is
      // ONE attempt (ENT-09) — the backend, not this button, guarantees that.
      // Byte-identical call shape to every pre-v1.71 caller when untagged —
      // the third argument is omitted outright, never an explicit `undefined`.
      const result = todayBlackoutLabel
        ? await api.fire(dialog.entry_number, dialog.press_id, ack)
        : await api.fire(dialog.entry_number, dialog.press_id);
      setFireResult({ n: dialog.entry_number, result });
    } finally {
      setFiring(false);
      setDialog(null);
      void load();
    }
  }

  if (!view) return <section className="card schedule-panel">Loading schedule…</section>;

  const rowErrors = errors.filter((e) => e.index !== null);
  const formErrors = errors.filter((e) => e.index === null);
  const used = usedFraction(view);
  const meterClass = view.exceeds_max_day_risk ? "over" : used !== null && used > 0.8 ? "warn" : "";

  return (
    <section className="card schedule-panel">
      <div className="card-head">
        <h2>Schedule &amp; Parameters</h2>
        {view.config_version && <span className="chip">config {view.config_version}</span>}
      </div>

      <div className="sched-scroll">
        <table className="schedule">
          <thead>
            <tr>
              {/* row counter (operator request 2026-07-12): 1..N down the left */}
              <th className="num rownum-head" aria-label="row number">#</th>
              <th className="num">Time (ET)</th>
              <th className="num">Target $</th>
              <th className="num">Width</th>
              <th className="num">Stop %</th>
              <th className="num">Long-recovery buffer $</th>
              <th className="num">Count</th>
              <th className="num">Worst case (est.)</th>
              <th aria-label="actions" />
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              // data-entry + the per-cell data-labels drive the narrow-width
              // card layout (schedule.css @container): the thead hides and each
              // cell shows its own label, each row becomes a bordered "Entry N"
              // card. Both attributes are inert in the wide table layout.
              <tr key={i} data-testid={`row-${i}`} data-entry={i + 1}>
                {/* the row's number, 1..N. Hidden in the narrow card layout,
                    where the "Entry N" heading numbers each card instead. */}
                <td className="cell-rownum">
                  <span className="rownum-n">{i + 1}</span>
                </td>
                <td className="cell-num" data-label="Time (ET)">
                  <input
                    aria-label={`time ${i + 1}`}
                    value={row.time ?? ""}
                    placeholder="09:32"
                    inputMode="numeric"
                    onChange={(e) => patch(i, "time", e.target.value)}
                    className={
                      errorFor(rowErrors, i, "time") || timeInvalid(row.time) ? "invalid" : ""
                    }
                  />
                  <TimeHint value={row.time} />
                </td>
                <td className="cell-num" data-label="Target $">
                  <input
                    aria-label={`target premium ${i + 1}`}
                    value={row.target_premium ?? ""}
                    placeholder="3.00"
                    onChange={(e) => patch(i, "target_premium", e.target.value)}
                    onBlur={(e) => patch(i, "target_premium", normalizeMoneyInput(e.target.value))}
                    className={errorFor(rowErrors, i, "target_premium") ? "invalid" : ""}
                  />
                </td>
                <td className="cell-num" data-label="Width">
                  <input
                    aria-label={`wing width ${i + 1}`}
                    value={row.wing_width ?? ""}
                    placeholder="50"
                    onChange={(e) => patch(i, "wing_width", e.target.value)}
                    className={errorFor(rowErrors, i, "wing_width") ? "invalid" : ""}
                  />
                </td>
                <td className="cell-num" data-label="Stop %">
                  {/* The discrete set is the ONLY stop-% the backend accepts (STP-02) */}
                  <select
                    aria-label={`stop pct ${i + 1}`}
                    value={String(row.stop_loss_pct || DEFAULT_STOP_PCT)}
                    onChange={(e) => patch(i, "stop_loss_pct", e.target.value)}
                    className={errorFor(rowErrors, i, "stop_loss_pct") ? "invalid" : ""}
                  >
                    {STOP_PCT_SET.map((p) => (
                      <option key={p} value={p}>{p}%</option>
                    ))}
                  </select>
                </td>
                <td className="cell-num" data-label="Buffer $">
                  {/* STP-02b / UI-18: pre-credits expected long recovery into the
                      stop trigger — an operator-set constant, never model-driven. */}
                  <input
                    aria-label={`long recovery buffer ${i + 1}`}
                    value={row.stop_rebate_markup ?? ""}
                    placeholder="0.00"
                    aria-describedby={markupTooltip(row) ? markupTooltipId(i) : undefined}
                    onChange={(e) => patch(i, "stop_rebate_markup", e.target.value)}
                    onBlur={(e) => patch(i, "stop_rebate_markup", normalizeMoneyInput(e.target.value))}
                    className={
                      errorFor(rowErrors, i, "stop_rebate_markup") || markupInvalid(row.stop_rebate_markup)
                        ? "invalid"
                        : ""
                    }
                  />
                  <MarkupHint row={row} index={i} />
                </td>
                <td className="cell-num" data-label="Count">
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
                <td className="cell-wc" data-testid={`wc-${i}`} data-label="Worst case">
                  {row.worst_case_estimate ? `$${row.worst_case_estimate}` : "—"}
                  {/* STP-02b (v1.67): alongside the worst-case disclosure, not a
                      second surface -- server-computed
                      (schedule_service.effective_stop_pct_estimate), same
                      ESTIMATE honesty stance, same round-trip cadence (Save)
                      as the dollar figure above it. */}
                  {row.effective_stop_pct_estimate != null && (
                    <span className="time-hint" data-testid={`effective-pct-${i}`}>
                      effective {row.effective_stop_pct_estimate}%
                    </span>
                  )}
                </td>
                <td className="cell-actions" data-label="Actions">
                  <button
                    className="icon-btn fire"
                    aria-label={`fire entry ${i + 1}`}
                    title={entriesEnabled ? "Fire this entry now (ENT-09)" : "Blocked: entries are not enabled"}
                    disabled={!entriesEnabled}
                    onClick={() => void openFireDialog(row.id ?? i + 1)}
                  >
                    ▶
                  </button>
                  <button
                    className="icon-btn del"
                    aria-label={`delete entry ${i + 1}`}
                    title="Remove this entry"
                    onClick={() => setRows((rs) => rs.filter((_, j) => j !== i))}
                  >
                    ✕
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {rowErrors.map((e, i) => (
        <p key={i} className="msg err" role="alert">
          Row {(e.index ?? 0) + 1}: {e.field} — {REASON_TEXT[e.reason] ?? e.reason}
        </p>
      ))}
      {formErrors.map((e, i) => (
        <p key={`f${i}`} className="msg err" role="alert">
          {e.field} — {REASON_TEXT[e.reason] ?? e.reason}
        </p>
      ))}

      <div className="sched-foot">
        <button className="btn" onClick={() => setRows((rs) => [...rs, { ...BLANK }])}>
          + Add entry
        </button>

        <label className="field">
          <span>Max day risk</span>
          <input
            aria-label="max day risk"
            value={maxDayRisk}
            placeholder="required for live"
            onChange={(e) => setMaxDayRisk(e.target.value)}
          />
        </label>

        {/* v1.46: the ceiling sits beside the composed day total, so adding a row
            visibly eats headroom. The meter makes that visible, not just numeric. */}
        <div className="risk-readout grow" data-testid="risk-readout">
          <div className="risk-line">
            <span className="k">Day total (est.)</span>
            <span className={`v ${view.exceeds_max_day_risk ? "over" : ""}`}>
              ${view.day_total_estimate}
            </span>
          </div>
          <div className={`meter ${meterClass}`} aria-hidden>
            <i style={{ width: `${Math.min(100, (used ?? 0) * 100)}%` }} />
          </div>
          <div className="risk-line">
            <span className="k">Headroom</span>
            <span className={`v ${view.headroom === null ? "none" : view.exceeds_max_day_risk ? "over" : ""}`}
                  data-testid="headroom">
              {view.headroom === null ? "no ceiling set" : `$${view.headroom}`}
            </span>
          </div>
        </div>

        <button className="btn primary" onClick={() => void save()}>Save</button>
      </div>

      {view.exceeds_max_day_risk && (
        <p className="msg warn" role="alert">
          Composed day total exceeds max day risk. RSK-04 will veto entries at fire time.
        </p>
      )}
      {saved && <p className="msg ok" role="status">Saved as config {saved}.</p>}
      <p className="note">{view.estimate_note}</p>
      {/* RSK-04 (v1.49): disclose that the ceiling covers bot-placed risk only. */}
      <p className="note" data-testid="risk-scope">{view.risk_scope_note}</p>

      {preflight && <PreflightList preflight={preflight} />}

      {dialog && (
        <FireDialog
          preview={dialog}
          busy={firing}
          todayBlackoutLabel={todayBlackoutLabel}
          onCancel={() => setDialog(null)}
          onConfirm={(ack) => void confirmFire(ack)}
        />
      )}

      {fireResult && (
        <p className={`msg ${fireResult.result.result === "filled" ? "ok" : "warn"}`} role="status">
          Entry {fireResult.n}:{" "}
          {fireResult.result.result === "filled"
            ? `filled (${fireResult.result.initiator})${fireResult.result.blackout_overridden ? " — blackout override" : ""}`
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
          <span className="tick" aria-hidden>{c.passed ? "✓" : "✗"}</span>
          <strong>{c.name}</strong> <em>({c.rule})</em>
          {c.detail && <span className="detail">{c.detail}</span>}
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
  todayBlackoutLabel = null,
  onCancel,
  onConfirm,
}: {
  preview: FirePreview;
  busy: boolean;
  // CAL-06 (v1.71): today's ET NO-TRADE label, or null/omitted for every
  // pre-v1.71 caller and every untagged day — OK behaves exactly as before then.
  todayBlackoutLabel?: string | null;
  onCancel: () => void;
  onConfirm: (blackoutAck: boolean) => void;
}) {
  const [ack, setAck] = useState(false);
  // Informed consent tracks the LABEL (final review, 2026-07-15): if the tag
  // changes underneath an open dialog (FOMC -> CPI), a previously-ticked
  // checkbox must NOT stay ticked — the operator acknowledged a different
  // label than the one the override would now land on. Any label change
  // resets the acknowledgment, re-disabling OK until re-ticked.
  useEffect(() => setAck(false), [todayBlackoutLabel]);
  const params: [string, string][] = [
    ["Contracts", String(preview.contracts)],
    ["Target premium", `$${preview.target_premium}`],
    ["Wing width", preview.wing_width],
    ["Stop", `${preview.stop_loss_pct}%`],
  ];

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Confirm manual entry">
      <div className="modal">
        <h3>Fire entry {preview.entry_number} now?</h3>
        <p className="sub">
          {new Date(preview.now).toLocaleTimeString()} — outside any scheduled window (ENT-09)
        </p>

        {todayBlackoutLabel && (
          <div className="cal-blackout-warning" role="alert" data-testid="blackout-warning">
            <strong>⚠ Today is tagged NO-TRADE: {todayBlackoutLabel}</strong>
            <label className="cal-ack-check">
              <input
                type="checkbox"
                aria-label="acknowledge blackout override"
                checked={ack}
                onChange={(e) => setAck(e.target.checked)}
              />
              I acknowledge this fire overrides today's NO-TRADE tag (CAL-06)
            </label>
          </div>
        )}

        <div className="fire-params">
          {params.map(([k, v]) => (
            <div className="p-row" key={k}>
              <span className="k">{k}</span>
              <span className="v">{v}</span>
            </div>
          ))}
        </div>

        <div className="estimate-box" data-testid="fire-estimate">
          <div className="lab">Worst case (ESTIMATE)</div>
          <div className="val">${preview.worst_case_estimate}</div>
          {preview.effective_stop_pct_estimate != null && (
            <div className="sub" data-testid="fire-effective-pct">
              effective stop {preview.effective_stop_pct_estimate}% (ESTIMATE)
            </div>
          )}
        </div>
        <p className="note">
          {preview.estimate_formula}. Strikes are not selected until the entry fires, so
          this is an estimate — the RSK-04 check runs on the real strikes and may still
          veto this entry.
        </p>

        <div className="modal-actions">
          <button className="btn" onClick={onCancel} disabled={busy}>Cancel</button>
          <button
            className="btn primary"
            onClick={() => onConfirm(ack)}
            disabled={busy || (!!todayBlackoutLabel && !ack)}
            autoFocus
          >
            {busy ? "Firing…" : "OK"}
          </button>
        </div>
      </div>
    </div>
  );
}
