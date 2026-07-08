"""UC-02 arm pre-flight checklist.

"backend validates (>= 1 entry; times legal) and runs the sequence: reconcile
(REC-02) -> verify clock (DAY-03) -> load config -> subscribe market data ->
ARMED. UI shows a pre-flight checklist with pass/fail per item."

Order matters and is the spec's. Each check runs only if the ones before it
passed: there is no point subscribing market data for a schedule that will not
validate, and no point arming on top of an unresolved reconciliation mismatch.

Nothing here arms. It returns the checklist; the caller arms iff `passed`.
A check that raises is a FAIL with its error, never an exception that leaves the
operator staring at a spinner.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class Check:
    name: str
    rule: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "rule": self.rule, "passed": self.passed,
                "detail": self.detail}


@dataclass(frozen=True)
class Preflight:
    checks: list[Check]

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def first_failure(self) -> Check | None:
        return next((c for c in self.checks if not c.passed), None)

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed,
                "checks": [c.to_dict() for c in self.checks],
                "blocked_by": None if self.passed else self.first_failure.name}


def _run(name: str, rule: str, fn: Callable[[], tuple[bool, str]]) -> Check:
    try:
        ok, detail = fn()
    except Exception as e:  # noqa: BLE001 — a check that explodes is a FAIL, loudly
        return Check(name, rule, False, f"check raised: {e!r}")
    return Check(name, rule, ok, detail)


def run_preflight(
    *,
    schedule_service,
    reconcile_clear: Callable[[], tuple[bool, str]],
    clock_ok: Callable[[], tuple[bool, str]],
    config_ok: Callable[[], tuple[bool, str]],
    market_data_ok: Callable[[], tuple[bool, str]],
    require_max_day_risk: bool = False,
) -> Preflight:
    """The UC-02 sequence, short-circuited at the first failure.

    `require_max_day_risk` is the live-mode rule (doc 06 section 169): max_day_risk
    is mandatory before live can be enabled. In paper it may be unset — which means
    "no RSK-04 ceiling configured", never "unlimited".
    """
    checks: list[Check] = []

    def schedule_check() -> tuple[bool, str]:
        errors = schedule_service.may_arm()
        if not schedule_service._state.entry_schedule:
            return False, "arming an empty schedule is rejected (ENT-01a)"
        if errors:
            return False, "; ".join(f"row {e.index}: {e.field} {e.reason}" if e.index is not None
                                    else f"{e.field} {e.reason}" for e in errors)
        return True, f"{len(schedule_service._state.entry_schedule)} entries composed"

    checks.append(_run("schedule", "ENT-01a", schedule_check))
    if not checks[-1].passed:
        return Preflight(checks)

    if require_max_day_risk:
        def risk_ceiling() -> tuple[bool, str]:
            ceiling = schedule_service.max_day_risk()
            if ceiling is None:
                return False, "max_day_risk is mandatory before live mode (doc 06 s169)"
            view = schedule_service.view()
            if view.exceeds_max_day_risk:
                return False, (f"composed day total (est.) {view.day_total_estimate} "
                               f"exceeds max_day_risk {ceiling}")
            return True, f"headroom (est.) {view.headroom} of {ceiling}"

        checks.append(_run("max_day_risk", "RSK-04", risk_ceiling))
        if not checks[-1].passed:
            return Preflight(checks)

    for name, rule, fn in (
        ("reconcile", "REC-02", reconcile_clear),
        ("clock", "DAY-03", clock_ok),
        ("config", "UC-02", config_ok),
        ("market_data", "DAT-02", market_data_ok),
    ):
        checks.append(_run(name, rule, fn))
        if not checks[-1].passed:
            break                    # the sequence is ordered; do not run past a failure

    return Preflight(checks)
