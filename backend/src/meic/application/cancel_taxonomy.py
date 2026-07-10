"""Cancel-failure taxonomy — ORD-08 (pure decision).

Every failed cancel MUST be classified BEFORE any retry decision, into exactly
one of:
  (a) filled       — executed while the cancel was in flight ⇒ handle as a fill
                     (EC-API-06);
  (b) terminal     — the order no longer exists (expired, already cancelled,
                     rejected-dead) ⇒ mark dead, stop tracking, NEVER retry;
  (c) transient    — the request failed (timeout, rate limit, maintenance) but
                     the order is likely alive ⇒ bounded retry with backoff.
An unclassifiable failure defaults to transient with a hard retry cap and an
escalation alert. Rationale: treating "order doesn't exist" as retryable caused
an all-night retry loop in the predecessor system.
"""
from __future__ import annotations

_FILLED = {"already_filled", "order_filled", "filled", "order_not_cancellable"}
_TERMINAL = {"order no longer exists", "order_not_found", "already_cancelled",
             "expired", "rejected_dead", "terminal"}
_TRANSIENT = {"timeout", "timed_out", "rate_limited", "429", "maintenance",
              "service_unavailable"}


def classify_cancel_failure(reason: str) -> str:
    """Return one of: "filled" | "terminal" | "transient" | "unclassifiable"."""
    r = reason.strip().lower()
    if r in _FILLED:
        return "filled"
    if r in _TERMINAL:
        return "terminal"
    if r in _TRANSIENT:
        return "transient"
    return "unclassifiable"


def cancel_action(reason: str) -> dict:
    """The ORD-08 action for a failed cancel. `retry` is False for filled and
    terminal (never retried); transient and unclassifiable retry with backoff up
    to a hard cap — unclassifiable also escalates an alert."""
    kind = classify_cancel_failure(reason)
    if kind == "filled":
        return {"kind": kind, "retry": False, "route_as_fill": True}
    if kind == "terminal":
        return {"kind": kind, "retry": False, "mark_dead": True,
                "stop_tracking": True, "re_add_protection": False}
    if kind == "transient":
        return {"kind": kind, "retry": True, "backoff": True, "hard_cap": True,
                "alert": False}
    # unclassifiable -> transient-with-cap + escalation alert
    return {"kind": kind, "retry": True, "backoff": True, "hard_cap": True,
            "alert": True}


# --- CLS-01 replace() classification ---------------------------------------
#
# A BrokerGateway.replace(old_id, new_intent) races the SAME two outcomes as a
# bare cancel (ORD-08), because every replace is a cancel of the OLD order at
# its core (the real adapter's cancel-then-submit; the fakes' single atomic
# flip). `ReplaceFilled`/`ReplaceTerminal` let a broker's `replace()`
# implementation surface exactly which ORD-08 class it hit WITHOUT the caller
# (CloseEntry) re-deriving it from a raw exception string. Both subclass
# ValueError so any pre-existing `except ValueError` around a `.replace()`
# call (there was none relying on the exact type) keeps working unchanged.
#
# Any OTHER exception from `replace()` is, by ORD-08's own rule, "unclassifiable
# defaults to transient": the caller retries with the ORIGINAL stop presumed
# still resting (untouched), since neither of these two subclasses were raised.
class ReplaceFilled(ValueError):
    """ORD-08a: `replace()`'s target had already FILLED — a real stop-out beat
    the replace. The side is already closed; the caller MUST route it to
    SIDE_STOPPED + LEX and MUST NOT submit a second close on the same leg."""

    def __init__(self, order_id, fill_price=None) -> None:
        super().__init__(f"order {order_id!r} already filled — cannot replace (ORD-08a)")
        self.order_id = order_id
        self.fill_price = fill_price


class ReplaceTerminal(ValueError):
    """ORD-08b: `replace()`'s target is dead for any OTHER reason (already
    cancelled, rejected, expired, or never existed) — mark dead, never retry
    THIS order, reconcile. The leg still needs closing: since nothing is
    resting for it any more, the caller may submit the close directly with no
    naked-window / double-order risk (there is nothing left to race)."""

    def __init__(self, order_id) -> None:
        super().__init__(f"order {order_id!r} no longer exists — cannot replace (ORD-08b)")
        self.order_id = order_id
