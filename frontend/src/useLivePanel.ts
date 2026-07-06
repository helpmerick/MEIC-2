import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import type { DayReport, PanelState } from "./types";

// Polls the read-model endpoints. (Doc 05 §8 specifies a WebSocket for deltas;
// polling is the interim transport until the WS route lands — the render layer
// is identical either way.)
export function useLivePanel(intervalMs = 2000) {
  const [state, setState] = useState<PanelState | null>(null);
  const [report, setReport] = useState<DayReport | null>(null);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [s, r] = await Promise.all([api.getState(), api.getReport()]);
      setState(s);
      setReport(r);
      setConnected(true);
      setError(null);
    } catch (e) {
      setConnected(false);
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    refresh();
    timer.current = window.setInterval(refresh, intervalMs);
    return () => {
      if (timer.current) window.clearInterval(timer.current);
    };
  }, [refresh, intervalMs]);

  return { state, report, connected, error, refresh };
}
