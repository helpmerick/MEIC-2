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
