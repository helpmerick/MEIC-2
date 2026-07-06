import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import type { DayReport, PanelState } from "./types";

// Reactive read-model feed over the /ws snapshot stream (doc 05 §8). The server
// pushes a {state, report} snapshot on connect and on each ping; we ping fast
// for near-real-time freshness and fall back to REST polling if the socket
// can't hold. Commands update the UI optimistically for instant feedback.
export function useLiveBot() {
  const [state, setState] = useState<PanelState | null>(null);
  const [report, setReport] = useState<DayReport | null>(null);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ws = useRef<WebSocket | null>(null);
  const ping = useRef<number | null>(null);
  const poll = useRef<number | null>(null);
  const alive = useRef(true);

  const applySnapshot = useCallback((snap: { state: PanelState; report: DayReport }) => {
    setState(snap.state);
    setReport(snap.report);
    setConnected(true);
    setError(null);
  }, []);

  // optimistic patch — reflect a command instantly, before the server confirms
  const optimistic = useCallback((patch: Partial<PanelState>) => {
    setState((s) => (s ? { ...s, ...patch } : s));
  }, []);

  const restPoll = useCallback(async () => {
    try {
      const [s, r] = await Promise.all([api.getState(), api.getReport()]);
      applySnapshot({ state: s, report: r });
    } catch (e) {
      setConnected(false);
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [applySnapshot]);

  const startPolling = useCallback(() => {
    if (poll.current) return;
    restPoll();
    poll.current = window.setInterval(restPoll, 1500);
  }, [restPoll]);

  const stopPolling = useCallback(() => {
    if (poll.current) { window.clearInterval(poll.current); poll.current = null; }
  }, []);

  const connect = useCallback(() => {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    let sock: WebSocket;
    try {
      sock = new WebSocket(`${proto}://${location.host}/ws`);
    } catch {
      startPolling();
      return;
    }
    ws.current = sock;
    sock.onopen = () => {
      stopPolling();
      ping.current = window.setInterval(() => sock.readyState === 1 && sock.send("p"), 700);
    };
    sock.onmessage = (ev) => {
      try { applySnapshot(JSON.parse(ev.data)); } catch { /* ignore */ }
    };
    sock.onerror = () => { setConnected(false); };
    sock.onclose = () => {
      if (ping.current) { window.clearInterval(ping.current); ping.current = null; }
      setConnected(false);
      if (!alive.current) return;
      startPolling();                         // fall back immediately
      window.setTimeout(() => alive.current && connect(), 2500); // and try to heal the socket
    };
  }, [applySnapshot, startPolling, stopPolling]);

  useEffect(() => {
    alive.current = true;
    connect();
    return () => {
      alive.current = false;
      ws.current?.close();
      if (ping.current) window.clearInterval(ping.current);
      stopPolling();
    };
  }, [connect, stopPolling]);

  // nudge the socket for an immediate snapshot (used right after a command)
  const refresh = useCallback(() => {
    const s = ws.current;
    if (s && s.readyState === 1) s.send("p");
    else restPoll();
  }, [restPoll]);

  return { state, report, connected, error, optimistic, refresh };
}
