"""AlertRelay — NFR-08 (v1.90) late-binding alert sink.

Components capture the SINK AT CONSTRUCTION, but the real operator-facing
sink (`_PanelAlerts`, `adapters/api/server.py`) is only installed later, once
the FastAPI app is assembled. A direct object reference therefore froze a
no-op (`_NullAlerts`) into every component constructed inside a composition
root's `__post_init__` — `ProtectPosition`'s STP-04 "post-fill infeasible
stop" critical, `ExecuteEntryAttempt`'s ORD-09/REC-01/lost-submit criticals,
`CloseEntry`'s CLS-06 partial-close criticals — and silently killed every one
of them in live and paper, because `server.py:3037`'s `comp.alerts = alerts`
rebinds the COMPOSITION's own attribute, not the reference each component
already captured.

`AlertRelay` is handed out to every alert-raising component AT construction
and RETARGETED IN PLACE (`set_target`) once the real sink exists, so a late
install reaches every holder — the relay's identity never changes, only what
it forwards to.

Never swallows: with no target installed yet, `alert()` still records the
call (bounded, so it cannot grow without limit) and emits it via the module
logger (`meic.alerts`), so a critical raised during boot/reconcile is at
worst log-visible, never /dev/null. Once a target is installed, every
recorded pre-target alert is replayed to it, in order, so nothing raised
before wiring completed is lost. A raising target itself can never break a
trading path: `alert()` catches and logs, never propagates.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("meic.alerts")

_DEFAULT_CAP = 100


class AlertRelay:
    """NFR-08 (v1.90): components capture the SINK AT CONSTRUCTION, but the
    real operator-facing sink is only installed later (server.py's
    `_PanelAlerts`). A direct reference therefore froze a no-op into every
    component and silently killed critical alerts in production. This relay
    is handed out at construction and RETARGETED in place, so a late
    install reaches every holder. Never swallows: with no target it records
    the alert and emits it via the module logger so it is at worst
    log-visible, never /dev/null.
    """

    def __init__(self, cap: int = _DEFAULT_CAP) -> None:
        self._target = None
        self._cap = cap
        # Bounded record of every alert raised before a target was installed
        # (or while the target itself is broken) — replayed to a newly
        # installed target so a critical raised during boot/reconcile is
        # never silently lost.
        self._pending: list[tuple[str, str, dict]] = []

    def alert(self, level: str, message: str, **context) -> None:
        target = self._target
        if target is None:
            self._record(level, message, context)
            logger.error("ALERT[%s] %s %s (no sink installed yet)", level, message, context)
            return
        try:
            target.alert(level, message, **context)
        except Exception:  # noqa: BLE001 -- an alert sink must never break a trading path
            self._record(level, message, context)
            logger.exception("ALERT[%s] %s %s (target sink raised)", level, message, context)

    def recent(self) -> list[dict]:
        """Mirrors `_PanelAlerts.recent()`'s shape (newest first) so callers
        that read `comp.alerts.recent()` for operator visibility keep working
        unchanged whether or not a real sink has been installed yet. Once a
        target that itself exposes `recent()` is installed, this delegates to
        it (the authoritative, durable record); before that (or against a
        target with no such method), it reports this relay's own bounded
        pending buffer."""
        target = self._target
        if target is not None and hasattr(target, "recent"):
            return target.recent()
        return [{"level": level, "message": message,
                "context": {k: str(v) for k, v in context.items()}}
                for level, message, context in reversed(self._pending)]

    def set_target(self, sink) -> None:
        """Install (or replace) the real sink and replay every alert recorded
        before it existed, in order, so boot/reconcile-time criticals are not
        lost. Clears the pending buffer only for entries that were
        successfully handed to the new target (a raising target keeps them
        recorded, via `alert`'s own fallback)."""
        self._target = sink
        pending, self._pending = self._pending, []
        for level, message, context in pending:
            self.alert(level, message, **context)

    def _record(self, level: str, message: str, context: dict) -> None:
        self._pending.append((level, message, dict(context)))
        if len(self._pending) > self._cap:
            del self._pending[: len(self._pending) - self._cap]
