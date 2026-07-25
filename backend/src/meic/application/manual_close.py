"""Manual close / cancel command — UC-14 / UI-16 / CLS-02.

The operator's Close action fires INSTANTLY with no confirmation dialog (Bug
#16): it routes through the one canonical CloseEntry (initiator `manual`),
clears any armed TPF floor for that entry, and is idempotent — a rapid
double-click produces exactly one close (ORD-04/CLS-03). A WORKING (pre-fill)
entry is CANCELLED instead (CLS-03), also instant, with no close orders placed
for its unfilled legs. Flatten-all is the ONE control that still requires a
typed `FLATTEN` confirmation (TC-FLT-01). Failures are returned to the caller
so the UI can render a toast — never a blocking dialog.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from meic.application.close_entry import CloseEntry, LiveLeg
from meic.application.execute_entry import _fill_matches  # reused normalizer, never a new one
from meic.application.persistent_state import PersistentState
from meic.domain.events import CondorFilled, EntryClosed, ReconciliationMismatch

FLATTEN_CONFIRMATION = "FLATTEN"


class _NoOpAlerts:
    def alert(self, level: str, message: str, **context) -> None:  # pragma: no cover - trivial
        pass


def _cancel_confirmed(cancel_result) -> bool:
    """CLS-03(a) v1.87 (Fix 2): "a confirmed cancel" — guarded/defensive
    access (`.get`, never subscript) so a broker returning a non-dict, or
    nothing at all, cannot raise. `None` (a broker that returns nothing —
    fakes/legacy) and a dict with no/`None` "result" key are treated as
    confirmed (pre-v1.87 behaviour, unchanged); `{"result": "cancelled"}` is
    the explicit confirmed shape. Anything else — SimulatedBroker's
    `{"result": "terminal", ...}`, TastytradeAdapter's `{"result": "error",
    ...}` for ANY cancel failure, or any other value — is NOT proof of a
    clean cancel and fails closed."""
    if cancel_result is None:
        return True
    result = cancel_result.get("result") if hasattr(cancel_result, "get") else None
    return result is None or result == "cancelled"


@dataclass(frozen=True)
class CloseResult:
    result: str      # "closed" | "cancelled" | "already_done" | "race_detected" | "cancel_superseded"
    initiator: str   # "manual" | "cancel_entry"


@dataclass
class ManualClose:
    close_entry: CloseEntry
    broker: object
    state: PersistentState
    _done: set = field(default_factory=set)
    # REPRICE-RACE SWEEP (2026-07-11) / CLS-02 wiring (2026-07-11, current):
    # `ManualClose` IS wired into the live/paper composition —
    # `panel_commands.PanelCommands._cancel_service()` constructs it, and
    # `_cancel_working_entry` calls `cancel_working` on this instance for
    # every real Close of a pre-fill (WORKING) entry: the one ratified
    # cancel path (CLS-02/CLS-03). `alerts` and `events` are therefore LIVE
    # sinks in production, not the preventative no-ops this comment used to
    # describe before the wiring landed — `events` is the real journal
    # (CLS-03(a)'s terminal `EntryClosed(initiator="cancelled")` is appended
    # through it) and `alerts` is the composition's real alert sink (the
    # race-detected critical alert below fires for real). Defaults
    # (`_NoOpAlerts`, an empty list) remain only for callers/tests that
    # construct this class standalone.
    alerts: object = field(default_factory=_NoOpAlerts)
    events: list = field(default_factory=list)
    # ORD-11: injected clock for lifecycle `at` timestamps — mirrors
    # `CloseEntry._at()`/`CloseEntry.__init__`'s `clock` exactly. None
    # (default) is legacy/replay-safe, same convention as CloseEntry.
    clock: object | None = None
    # CLS-03(b) v1.87: bounded retry for the supersession race below — a
    # precedent set by CloseEntry's own `replace_retry_attempts`, not a
    # `spec/06-configuration.md` config key.
    cancel_attempts: int = 3

    def _at(self) -> str | None:
        """ORD-11: mirrors `CloseEntry._at()` — ISO-8601 UTC from the
        injected clock, never `datetime.now()` directly. None (legacy/
        replay-safe) when no clock is threaded through."""
        return self.clock.now().isoformat() if self.clock is not None else None

    def requires_close_confirmation(self) -> bool:
        """UI-16 / Bug #16: Close never asks — it fires instantly, no dialog."""
        return False

    async def close(self, entry_id: str, *, live_legs: list[LiveLeg],
                    resting_stop_ids: dict[str, str], close_price) -> CloseResult:
        """Close a filled entry via CLS (initiator `manual`); clear its TPF
        floor. Idempotent: a second call is a no-op (no duplicate orders)."""
        if entry_id in self._done:
            return CloseResult("already_done", "manual")
        self._done.add(entry_id)
        await self.close_entry.close(
            entry_id, "manual", resting_stop_ids=resting_stop_ids,
            live_legs=live_legs, close_price=close_price)
        self._clear_tpf_floor(entry_id)
        return CloseResult("closed", "manual")

    async def cancel_working(self, entry_id: str, order_id: str, *, current_id=None) -> CloseResult:
        """CLS-03: a WORKING entry is cancelled (instant) — no close orders are
        placed for its unfilled legs. Idempotent like close().

        CLS-03(a)/(b) v1.87: the bounded retry loop below MUST live INSIDE
        this method, not wrap it — the `_done` idempotency guard immediately
        above returns `already_done` on any re-entry, so an outer retry would
        short-circuit after the first attempt and never actually cancel a
        newly-superseded id. `current_id` is an optional zero-arg callable
        resolving the ladder registry's CURRENT working order id for this
        entry (or None); when omitted, behaviour is unchanged from pre-v1.87
        (single target, no supersession detection), so existing callers/tests
        are unaffected.
        """
        if entry_id in self._done:
            return CloseResult("already_done", "cancel_entry")
        # CLS-03(a)/(b) v1.87 (Fix 3 correction): latch EAGERLY so a
        # concurrent second press (two rapid clicks = two tasks on the same
        # event loop) cannot issue a second broker cancel between this check
        # and the first `await` below — "idempotent like close()", a
        # double-click yields exactly ONE cancel. But an UNPROVEN outcome
        # must not strand the operator behind `already_done`, so the latch
        # is RELEASED in `finally` unless the outcome was genuinely terminal
        # (`cancelled` / `race_detected`) — this also covers an exception
        # escaping `broker.cancel()` itself: a raise leaves `terminal`
        # False, so the latch is released and a retry can reach the broker.
        self._done.add(entry_id)
        terminal = False
        try:
            target = str(order_id)
            for _ in range(self.cancel_attempts):
                cancel_result = await self.broker.cancel(target)
                # REPRICE-RACE SWEEP (2026-07-11): the entry can fill in the window
                # between the operator's click and this cancel — neither adapter's
                # cancel() reliably reports "it was already filled" (SimulatedBroker:
                # {"result": "terminal", ...}; TastytradeAdapter: {"result": "error",
                # ...} for any cancel failure). Trusting it blindly would report
                # "cancelled" for a condor that is, in fact, live and unprotected — no
                # CondorFilled, no stop, no alert. This module has no strike/leg
                # information to reconstruct the entry (ORD-09), so it never guesses;
                # it surfaces the race loudly, same as reconcile.py's own boot-cancel
                # guard, and returns a distinct result so a caller never treats it as
                # a clean cancel. (EC-ENT-06 owns the fill itself — no terminal event
                # is journaled on this path.)
                if any(_fill_matches(f, target) for f in await self.broker.fills_since(None)):
                    detail = (f"CLS-03 cancel of working entry {entry_id} (order {target}) "
                             "raced a fill — position may be unprotected; operator must "
                             "reconcile manually")
                    self.events.append(ReconciliationMismatch(detail=detail))
                    self.alerts.alert("critical", detail, entry_id=entry_id, order_id=target)
                    self._clear_tpf_floor(entry_id)
                    terminal = True  # CLS-03(b) v1.87: race_detected IS terminal
                    return CloseResult("race_detected", "cancel_entry")

                # CLS-03(b) v1.87: SUPERSESSION CHECK — the ladder may have minted
                # a NEW working order id inside the `await` above (a reprice
                # racing our cancel). Re-resolve and retry against the current
                # id; never report a clean cancel for an id we know is stale.
                now = current_id() if current_id is not None else None
                if now is not None and str(now) != target:
                    target = str(now)
                    continue

                # CLS-03(a) v1.87 (Fix 2): a terminal journal follows ONLY a
                # CONFIRMED cancel. Per this module's own REPRICE-RACE SWEEP
                # comment above, SimulatedBroker returns {"result": "terminal",
                # ...} and TastytradeAdapter returns {"result": "error", ...}
                # for ANY cancel failure — trusting an unconfirmed/failed cancel
                # blindly would journal "cancelled" for an order that may still
                # be live. `None`/a missing "result" key stays confirmed (fakes
                # and legacy brokers that return nothing — pre-v1.87 behaviour,
                # unchanged); anything else non-"cancelled" fails closed with NO
                # terminal journal, distinct from a clean cancel. `terminal`
                # stays False (Fix 3) — an unconfirmed cancel is not terminal,
                # so a second press must be able to try again.
                if not _cancel_confirmed(cancel_result):
                    return CloseResult("cancel_superseded", "cancel_entry")

                # PROVEN-CANCEL: the id we just cancelled is still the current
                # one (or there is no resolver to say otherwise), and the broker
                # confirmed the cancel.
                #
                # CLS-03(a) GUARD: if the ladder already journaled a CondorFilled
                # for this entry, it genuinely filled — EC-ENT-06 territory, not
                # a cancel at all. We cannot prove a clean cancel here, so fail
                # closed rather than mislabel a fill as "cancelled". `terminal`
                # stays False (Fix 3) — same reasoning as the unconfirmed case.
                if any(isinstance(ev, CondorFilled) and ev.entry_id == entry_id
                       for ev in self.events):
                    return CloseResult("cancel_superseded", "cancel_entry")

                self._clear_tpf_floor(entry_id)
                # CLS-03(a) v1.87: the terminal event for a genuinely clean,
                # pre-fill, CONFIRMED cancel — the ratified "cancelled" initiator.
                self.events.append(EntryClosed(entry_id=entry_id, initiator="cancelled", at=self._at()))
                terminal = True  # CLS-03(b) v1.87 (Fix 3): genuinely terminal — keep the latch
                return CloseResult("cancelled", "cancel_entry")

            # CLS-03(b) v1.87: retry bound exhausted while still superseded —
            # never a clean cancel, never a terminal journal entry. `terminal`
            # stays False.
            return CloseResult("cancel_superseded", "cancel_entry")
        finally:
            if not terminal:
                self._done.discard(entry_id)

    def _clear_tpf_floor(self, entry_id: str) -> None:
        floors = dict(self.state.tpf_floors)
        if floors.pop(entry_id, None) is not None:
            self.state.tpf_floors = floors

    @staticmethod
    def may_flatten(confirmation: str) -> bool:
        """TC-FLT-01: flatten-all is the one action gated on a typed FLATTEN."""
        return confirmation == FLATTEN_CONFIRMATION
