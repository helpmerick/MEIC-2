// API client — the ONLY place the frontend talks to the backend.
// No trading logic lives here or anywhere in the frontend (UI-03): it reads
// state and sends commands; the backend validates everything.

import type { DayReport, PanelState } from "./types";

// NFR-06: when the operator has set an api_token, mutating requests must carry
// it. Read it from a meta tag or localStorage; empty on a plain localhost bind.
function apiToken(): string {
  return localStorage.getItem("meic_api_token") ?? "";
}

async function get<T>(path: string): Promise<T> {
  const r = await fetch(path, { headers: { accept: "application/json" } });
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return (await r.json()) as T;
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { "content-type": "application/json" };
  const token = apiToken();
  if (token) headers["x-api-token"] = token;
  const r = await fetch(path, {
    method: "POST",
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) {
    const detail = await r.json().catch(() => ({}));
    throw new ApiError(r.status, detail?.detail ?? r.statusText);
  }
  return (await r.json()) as T;
}

export class ApiError extends Error {
  constructor(public status: number, public detail: unknown) {
    super(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
}

export const api = {
  getState: () => get<PanelState>("/state"),
  getReport: () => get<DayReport>("/report"),
  arm: () => post<PanelState>("/arm"),
  disarm: () => post<PanelState>("/disarm"),
  stopTrading: (on: boolean) => post<PanelState>(`/stop-trading?on=${on}`),
  confirmLive: (on: boolean) => post<PanelState>(`/confirm-live?on=${on}`),
  updateConfig: (patch: Record<string, unknown>) =>
    post<{ accepted: Record<string, unknown> }>("/config", patch),
};

// Discrete stop-pct set (UI-04) — in production generated from the config
// schema the backend serves, so UI and backend cannot drift.
export const STOP_PCT_SET: number[] = Array.from({ length: (300 - 95) / 5 + 1 }, (_, i) => 95 + i * 5);
