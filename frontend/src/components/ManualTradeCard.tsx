// ENT-11/UI-25 — ad-hoc manual entry: fire a trade NOW with explicit parameters
// (every per-row field of doc 06 section 37 EXCEPT time), plus a read-only
// Simulate that previews the strikes/credit the row would get if fired now.
//
// NO TRADING LOGIC LIVES HERE (UI-03): the backend validates every field,
// selects every strike, and decides every skip reason. This form only renders
// the row, sends it, and shows back exactly what the server said — including
// the worst case, which is NEVER computed client-side (the estimate-honesty
// precedent, v1.46): the confirm dialog shows the last Simulate result if one
// exists, or says plainly that none has been run yet, rather than fabricating
// a number the way the schedule row's static formula would.

import { useEffect, useState } from "react";
import { api, ApiError, DEFAULT_STOP_PCT, STOP_PCT_SET } from "../api";
import { contractDollarsPlain } from "../money";
import type { FireResult, FloorCandidateRow, FloorCandidates, ManualSimulation } from "../types";

interface ManualParams {
  contracts: number | "";
  target_premium: string | "";
  wing_width: string | "";
  stop_loss_pct: number | "";
}

const BLANK: ManualParams = {
  contracts: "", target_premium: "", wing_width: "", stop_loss_pct: DEFAULT_STOP_PCT,
};

/** Blank cells inherit the global (doc 06 section 37) — send only what's filled. */
function nonEmpty(params: ManualParams): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  if (params.contracts !== "") out.contracts = params.contracts;
  if (params.target_premium !== "") out.target_premium = params.target_premium;
  if (params.wing_width !== "") out.wing_width = params.wing_width;
  if (params.stop_loss_pct !== "") out.stop_loss_pct = params.stop_loss_pct;
  return out;
}

// ENT-09b (v1.57): minimum short-strike floors. Toggle default OFF — the
// dialog is unchanged until enabled. FLOOR semantics (never a pin): puts
// select short <= the floor, calls select short >= the floor; either, both,
// or neither side may be set. NO TRADING LOGIC HERE (UI-03) — the backend
// filters the walk, re-validates credit rules, and can refuse the fire
// outright (`floor_inside_spot`) if spot crosses a selected floor before OK;
// this form only collects the two optional numbers and shows back whatever
// the server decided.
interface FloorParams {
  enabled: boolean;
  put_floor: string;
  call_floor: string;
}

const BLANK_FLOORS: FloorParams = { enabled: false, put_floor: "", call_floor: "" };

function floorFields(floors: FloorParams): Record<string, unknown> {
  if (!floors.enabled) return {};
  const out: Record<string, unknown> = {};
  if (floors.put_floor !== "") out.put_floor = floors.put_floor;
  if (floors.call_floor !== "") out.call_floor = floors.call_floor;
  return out;
}

export function ManualTradeCard({ entriesEnabled }: { entriesEnabled: boolean }) {
  const [params, setParams] = useState<ManualParams>({ ...BLANK });
  const [floors, setFloors] = useState<FloorParams>({ ...BLANK_FLOORS });
  const [floorCandidates, setFloorCandidates] = useState<FloorCandidates | null>(null);
  const [floorCandidatesError, setFloorCandidatesError] = useState<string | null>(null);
  const [loadingFloorCandidates, setLoadingFloorCandidates] = useState(false);
  const [simResult, setSimResult] = useState<ManualSimulation | null>(null);
  const [simError, setSimError] = useState<string | null>(null);
  const [simulating, setSimulating] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [firing, setFiring] = useState(false);
  const [fireResult, setFireResult] = useState<FireResult | null>(null);

  const patch = (field: keyof ManualParams, value: string) =>
    setParams((p) => ({
      ...p,
      [field]: field === "contracts" || field === "stop_loss_pct"
        ? (value === "" ? "" : Number(value))
        : value,
    }));

  // ENT-09b v1.57 (finished to spec): the floor dropdowns are populated from
  // the LIVE VALIDATED UNIVERSE via the same tested backend endpoint the
  // scheduled-row dialog uses (UI-03: nothing is decided here, only shown
  // back). Free numeric entry is gone — it could name a strike that doesn't
  // exist, which is exactly why the dropdown was ratified.
  async function loadFloorCandidates() {
    setFloorCandidatesError(null);
    setLoadingFloorCandidates(true);
    try {
      setFloorCandidates(await api.manualFloorCandidates(nonEmpty(params)));
    } catch (e) {
      setFloorCandidates(null);
      setFloorCandidatesError(e instanceof ApiError ? String(e.detail) : String(e));
    } finally {
      setLoadingFloorCandidates(false);
    }
  }

  // Populate (and re-populate on re-enable) when the operator turns the
  // floors toggle on — a stale set from a previous open would silently name
  // strikes that may no longer be live.
  useEffect(() => {
    if (floors.enabled) void loadFloorCandidates();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [floors.enabled]);

  const putCandidates: FloorCandidateRow[] = floorCandidates?.available ? floorCandidates.put ?? [] : [];
  const callCandidates: FloorCandidateRow[] = floorCandidates?.available ? floorCandidates.call ?? [] : [];
  // Honest note for every reason the dropdown can have nothing to offer: not
  // wired at all, a request that failed, or a wired-but-stale/empty snapshot.
  const floorCandidatesNote = loadingFloorCandidates
    ? "loading live candidates…"
    : floorCandidatesError
      ? `candidates unavailable — ${floorCandidatesError}`
      : floorCandidates && !floorCandidates.available
        ? "candidates unavailable — no live chain wired"
        : floorCandidates && putCandidates.length === 0 && callCandidates.length === 0
          ? "no live candidates yet — snapshot not fresh"
          : null;

  // Simulate is read-only and works regardless of armed state (UI-25) — it
  // never checks `entriesEnabled`.
  async function simulate() {
    setSimResult(null);
    setSimError(null);
    setSimulating(true);
    try {
      setSimResult(await api.manualSimulate(nonEmpty(params)));
    } catch (e) {
      setSimError(e instanceof ApiError ? String(e.detail) : String(e));
    } finally {
      setSimulating(false);
    }
  }

  async function confirmFire() {
    setFiring(true);
    try {
      const result = await api.manualFire({
        press_id: crypto.randomUUID(), confirmed: true,
        ...nonEmpty(params), ...floorFields(floors),
      });
      setFireResult(result);
    } finally {
      setFiring(false);
      setConfirmOpen(false);
    }
  }

  const lastWorstCase = simResult?.result === "ok" ? simResult.worst_case ?? null : null;

  return (
    <section className="card manual-trade-card" data-testid="manual-trade">
      {/* Always visible — no dropdown (operator request 2026-07-12). */}
      <h2>Fire manual trade</h2>
      <div className="manual-trade-body">
          <div className="manual-fields">
            <label className="field">
              <span>Target $</span>
              <input
                aria-label="manual target premium"
                value={params.target_premium}
                placeholder="3.00"
                onChange={(e) => patch("target_premium", e.target.value)}
              />
            </label>
            <label className="field">
              <span>Width</span>
              <input
                aria-label="manual wing width"
                value={params.wing_width}
                placeholder="50"
                onChange={(e) => patch("wing_width", e.target.value)}
              />
            </label>
            <label className="field">
              <span>Stop %</span>
              <select
                aria-label="manual stop pct"
                value={String(params.stop_loss_pct || DEFAULT_STOP_PCT)}
                onChange={(e) => patch("stop_loss_pct", e.target.value)}
              >
                {STOP_PCT_SET.map((p) => (
                  <option key={p} value={p}>{p}%</option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>Contracts</span>
              <input
                aria-label="manual contracts"
                type="number"
                min={1}
                max={10}
                value={params.contracts}
                placeholder="1"
                onChange={(e) => patch("contracts", e.target.value)}
              />
            </label>
          </div>

          <div className="manual-floors">
            <label className="field floor-toggle">
              <input
                type="checkbox"
                aria-label="enable minimum strike floors"
                checked={floors.enabled}
                onChange={(e) => setFloors((f) => ({ ...f, enabled: e.target.checked }))}
              />
              <span>Minimum strikes (ENT-09b)</span>
            </label>
            {floors.enabled && (
              <>
                <label className="field">
                  <span>Put floor</span>
                  <select
                    aria-label="manual put floor"
                    value={floors.put_floor}
                    disabled={putCandidates.length === 0}
                    onChange={(e) => setFloors((f) => ({ ...f, put_floor: e.target.value }))}
                  >
                    <option value="">none</option>
                    {/* live validated universe, puts descending from the money (backend-sorted) */}
                    {putCandidates.map((r) => (
                      <option key={r.strike} value={r.strike}>
                        {r.strike} ({r.distance_pts} pts) · ${r.mid}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field">
                  <span>Call floor</span>
                  <select
                    aria-label="manual call floor"
                    value={floors.call_floor}
                    disabled={callCandidates.length === 0}
                    onChange={(e) => setFloors((f) => ({ ...f, call_floor: e.target.value }))}
                  >
                    <option value="">none</option>
                    {/* live validated universe, calls ascending from the money (backend-sorted) */}
                    {callCandidates.map((r) => (
                      <option key={r.strike} value={r.strike}>
                        {r.strike} ({r.distance_pts} pts) · ${r.mid}
                      </option>
                    ))}
                  </select>
                </label>
                {floorCandidatesNote && <p className="note">{floorCandidatesNote}</p>}
                <p className="note">
                  Puts select short at or below the floor; calls select short at or above it.
                  A floor can only cause a skip — it never weakens the credit rules.
                </p>
              </>
            )}
          </div>

          <div className="manual-actions">
            <button className="btn" onClick={() => void simulate()} disabled={simulating}>
              {simulating ? "Simulating…" : "Simulate trade"}
            </button>
            <button
              className="btn danger"
              disabled={!entriesEnabled}
              title={entriesEnabled ? "Fire this trade now (ENT-11)" : "Blocked: entries are not enabled"}
              onClick={() => setConfirmOpen(true)}
            >
              Fire
            </button>
          </div>

          {simError && <p className="msg err" role="alert">{simError}</p>}

          {simResult?.result === "ok" && (
            <div className="fire-params" data-testid="manual-sim-result">
              <div className="p-row"><span className="v">P {simResult.put_short}/{simResult.put_long}</span></div>
              <div className="p-row"><span className="v">C {simResult.call_short}/{simResult.call_long}</span></div>
              <div className="p-row">
                <span className="k">Mids</span>
                <span className="v">P ${simResult.put_mid} · C ${simResult.call_mid}</span>
              </div>
              <div className="p-row">
                <span className="k">Net credit</span>
                {/* net_credit is per-share (condor.mid_credit); worst_case below is
                    ALREADY real dollars (domain/risk.py worst_case_loss applies
                    x100 x contracts itself) -- operator request 2026-07-11. */}
                <span className="v">${contractDollarsPlain(simResult.net_credit ?? "0", simResult.contracts ?? 1)}</span>
              </div>
              <div className="p-row">
                <span className="k">Worst case</span>
                <span className="v">${simResult.worst_case}</span>
              </div>
              <p className="note">{simResult.estimate_note}</p>
            </div>
          )}

          {simResult?.result === "skipped" && (
            <p className="msg warn" role="alert">Simulate: {simResult.reason}</p>
          )}

          {fireResult && (
            <p className={`msg ${fireResult.result === "filled" ? "ok" : "warn"}`} role="status">
              {fireResult.result === "filled"
                ? `filled (${fireResult.initiator})`
                : `${fireResult.result}${fireResult.reason ? ` — ${fireResult.reason}` : ""}`}
            </p>
          )}
      </div>

      {confirmOpen && (
        <ManualFireDialog
          params={params}
          floors={floors}
          worstCase={lastWorstCase}
          busy={firing}
          onCancel={() => setConfirmOpen(false)}
          onConfirm={() => void confirmFire()}
        />
      )}
    </section>
  );
}

// UI-22-style OK dialog (operator-ratified: NOT typed), mirroring SchedulePanel's
// FireDialog. Ad-hoc has no per-row worst-case estimate endpoint (UI-03: nothing
// is computed here), so this shows the LAST Simulate result when one exists, and
// says plainly when none has — never a fabricated number.
function ManualFireDialog({
  params,
  floors,
  worstCase,
  busy,
  onCancel,
  onConfirm,
}: {
  params: ManualParams;
  floors: FloorParams;
  worstCase: string | null;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const rows: [string, string][] = [
    ["Contracts", params.contracts === "" ? "(default)" : String(params.contracts)],
    ["Target premium", params.target_premium === "" ? "(default)" : `$${params.target_premium}`],
    ["Wing width", params.wing_width === "" ? "(default)" : params.wing_width],
    ["Stop", params.stop_loss_pct === "" ? "(default)" : `${params.stop_loss_pct}%`],
  ];
  if (floors.enabled) {
    rows.push(["Put floor", floors.put_floor === "" ? "(none)" : floors.put_floor]);
    rows.push(["Call floor", floors.call_floor === "" ? "(none)" : floors.call_floor]);
  }

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Confirm manual entry">
      <div className="modal">
        <h3>Fire this trade now?</h3>
        <p className="sub">Ad-hoc entry — outside any scheduled window (ENT-11)</p>

        <div className="fire-params">
          {rows.map(([k, v]) => (
            <div className="p-row" key={k}>
              <span className="k">{k}</span>
              <span className="v">{v}</span>
            </div>
          ))}
        </div>

        <div className="estimate-box" data-testid="manual-fire-estimate">
          <div className="lab">Worst case (ESTIMATE)</div>
          <div className="val">{worstCase ? `$${worstCase}` : "run Simulate for an estimate"}</div>
        </div>
        <p className="note">
          Strikes are not selected until the entry fires, so this is an estimate — the
          RSK-04 check runs on the real strikes and may still veto this entry.
        </p>

        <div className="modal-actions">
          <button className="btn" onClick={onCancel} disabled={busy}>Cancel</button>
          <button className="btn primary" onClick={onConfirm} disabled={busy} autoFocus>
            {busy ? "Firing…" : "OK"}
          </button>
        </div>
      </div>
    </div>
  );
}
