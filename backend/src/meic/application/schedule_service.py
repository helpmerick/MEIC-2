"""ScheduleService — compose, validate, version, persist the standing schedule.

UC-02: the operator composes the schedule (times + per-entry premium/width/stop
parameters), presses Arm, and the backend validates before anything is armed.
Arming an empty or illegal schedule is rejected (ENT-01a).

The panel also carries the `max_day_risk` ceiling beside the composed day-total
worst case (UI-22, v1.46), so adding a row visibly eats headroom.

The day total shown here is an ESTIMATE (v1.46, operator-ratified): no strikes
exist before selection runs, so `(wing_width - target_premium) x 100 x contracts`
is the best the panel can know. The post-selection RSK-04 gate is authoritative
and can still veto an entry the panel showed as fitting.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import time
from decimal import Decimal
from typing import Any

from meic.domain.schedule import (
    EntrySpec,
    ScheduleDefaults,
    ScheduleError,
    resolve,
    validate_schedule,
)


def worst_case_estimate(entry) -> Decimal:
    """UI-22 (v1.46): the row's worst case, ESTIMATED from row parameters.

    `(wing_width - target_premium) x 100 x contracts`. It uses the TARGET premium
    because the actual credit is unknown until the order fills — so this is an
    upper-ish bound the operator can reason about, not the number RSK-04 will
    later enforce. Always labelled ESTIMATE in the UI.
    """
    return max(Decimal("0"), entry.wing_width - entry.target_premium) * 100 * entry.contracts


def day_total_estimate(entries) -> Decimal:
    """RSK-04 shape (v1.44): the SUM of per-entry worst cases, never n x max."""
    return sum((worst_case_estimate(e) for e in entries), Decimal("0"))


def pinned_row(entry) -> dict[str, Any]:
    """A ResolvedEntry as the row we PERSIST (v1.47 pin-at-Save, doc 06 §37).

    Every parameter concrete. Globals are pre-fills for NEW rows only; they never
    retro-apply to a saved row. This extends v1.44's `contracts_per_entry`
    precedent to every field, for the same reason STP-02 makes stop changes apply
    to subsequent entries only: changing a setting must never silently change what
    a saved entry trades. What the row displays is exactly what it trades.

    Decimals go out as strings — exact, never float (the log and the order both
    depend on it).
    """
    return {
        "time": entry.time.strftime("%H:%M"),
        "contracts": entry.contracts,
        "target_premium": str(entry.target_premium),
        "wing_width": str(entry.wing_width),
        "stop_loss_pct": entry.stop_loss_pct,
        "stop_basis": entry.stop_basis,
        "stop_rebate_markup": str(entry.stop_rebate_markup),
        "min_short_premium": str(entry.min_short_premium),
        "min_total_credit": str(entry.min_total_credit),
        "probe_down_max": entry.probe_down_max,
        "strike_method": entry.strike_method,
        "short_delta_target": str(entry.short_delta_target),
    }


@dataclass(frozen=True)
class ScheduleView:
    """What the panel renders: the rows, their estimates, and the headroom."""

    rows: list[dict[str, Any]]
    day_total_estimate: Decimal
    max_day_risk: Decimal | None
    config_version: str | None

    @property
    def headroom(self) -> Decimal | None:
        if self.max_day_risk is None:
            return None
        return self.max_day_risk - self.day_total_estimate

    @property
    def exceeds_max_day_risk(self) -> bool:
        """The panel warns; RSK-04 at fire time is what actually blocks."""
        return self.max_day_risk is not None and self.day_total_estimate > self.max_day_risk

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "day_total_estimate": str(self.day_total_estimate),
            "max_day_risk": None if self.max_day_risk is None else str(self.max_day_risk),
            "headroom": None if self.headroom is None else str(self.headroom),
            "exceeds_max_day_risk": self.exceeds_max_day_risk,
            "config_version": self.config_version,
            "estimate_note": ("worst case ESTIMATED from row parameters "
                              "((width - target premium) x 100 x contracts); "
                              "RSK-04 re-prices from real strikes at fire time"),
            # RSK-04 (v1.49): the ceiling caps the BOT's placed risk only; any
            # foreign positions on the account are excluded and constrain via the
            # broker buying-power gate instead. The UI MUST disclose this scope.
            "risk_scope_note": ("max day risk caps BOT-PLACED risk only — foreign "
                                "positions on this account are excluded (they "
                                "constrain via the broker's buying-power gate)"),
        }


# A 24-hour "military" wall-clock time: HH:MM, hour 00-23, minute 00-59. Leading
# zero on the hour is optional (9:32 or 09:32) and the separator may be a colon OR
# a dot ("11:53" / "11.53" both = 11:53) — people write times both ways. There is
# NO am/pm — rejecting "1:53pm", "24:00", "11:60", "0930". Persisted times are
# canonicalised to "HH:MM" (colon) via `pinned_row`'s strftime.
_MILITARY_RE = re.compile(r"^([01]?\d|2[0-3])[.:][0-5]\d$")


def _military_time_errors(rows: list[dict[str, Any]]) -> list[ScheduleError]:
    """Every row whose `time` is not 24-hour HH:MM (military). Checked BEFORE parse
    so the operator gets a precise per-row reason, not a generic crash."""
    errors = []
    for i, row in enumerate(rows):
        raw = str(row.get("time", "")).strip()
        if not _MILITARY_RE.fullmatch(raw):
            errors.append(ScheduleError(field="time", reason="not_24h_military", index=i))
    return errors


def _parse_time(raw: str) -> time:
    h, m = re.split(r"[.:]", raw.strip())[:2]  # colon or dot separator
    return time(int(h), int(m))


def spec_from_row(row: dict[str, Any]) -> EntrySpec:
    """One UI row -> an EntrySpec. Absent keys mean "inherit the global" (doc 06
    section 37) — an empty cell is not zero."""
    def dec(key):
        v = row.get(key)
        return None if v in (None, "") else Decimal(str(v))

    def integer(key):
        v = row.get(key)
        return None if v in (None, "") else int(v)

    return EntrySpec(
        time=_parse_time(row["time"]),
        contracts=integer("contracts"),
        target_premium=dec("target_premium"),
        wing_width=dec("wing_width"),
        stop_loss_pct=integer("stop_loss_pct"),
        stop_basis=row.get("stop_basis") or None,
        stop_rebate_markup=dec("stop_rebate_markup"),
        min_short_premium=dec("min_short_premium"),
        min_total_credit=dec("min_total_credit"),
        probe_down_max=integer("probe_down_max"),
        strike_method=row.get("strike_method") or None,
        short_delta_target=dec("short_delta_target"),
    )


class ScheduleService:
    """Validate -> version -> persist. Nothing is persisted that would not arm."""

    def __init__(self, state, defaults: ScheduleDefaults | None = None, *,
                 session_open: time = time(9, 30), session_close: time = time(16, 0),
                 min_time_before_close_minutes: int = 30) -> None:
        self._state = state
        self._defaults = defaults or ScheduleDefaults()
        self._open = session_open
        self._close = session_close
        self._min_before_close = min_time_before_close_minutes

    # --- read ------------------------------------------------------------------
    def resolved(self) -> list:
        """The concrete parameters each row trades.

        Pin-at-Save (v1.47) means a SAVED row already holds concrete values, so
        `resolve` is a no-op over it. It still runs, for two cases: a row composed
        outside the panel (the paper demo's bare `{"time": ...}` rows), and any
        pre-v1.47 row already in the durable store.
        """
        rows = self._state.entry_schedule or []
        return [resolve(spec_from_row(r), self._defaults) for r in rows]

    def view(self) -> ScheduleView:
        resolved = self.resolved()
        rows = [{**pinned_row(r), "worst_case_estimate": str(worst_case_estimate(r))}
                for r in resolved]
        return ScheduleView(rows=rows, day_total_estimate=day_total_estimate(resolved),
                            max_day_risk=self.max_day_risk(),
                            config_version=self._state.config_version)

    def max_day_risk(self) -> Decimal | None:
        raw = getattr(self._state, "max_day_risk", None)
        return None if raw in (None, "") else Decimal(str(raw))

    # --- validate --------------------------------------------------------------
    def validate(self, rows: list[dict[str, Any]]) -> list[ScheduleError]:
        """Every error, not just the first — the operator fixes the form once."""
        # Military-time format first: entry times must be 24-hour HH:MM. This runs
        # before parsing so a bad time reports as `not_24h_military` on its own row
        # rather than crashing spec_from_row into a generic "unparsable".
        fmt = _military_time_errors(rows)
        if fmt:
            return fmt
        try:
            specs = [spec_from_row(r) for r in rows]
        except (KeyError, ValueError, ArithmeticError) as e:
            return [ScheduleError(field="row", reason=f"unparsable ({e})", index=None)]
        return validate_schedule(specs, self._defaults, session_open=self._open,
                                 session_close=self._close,
                                 min_time_before_close_minutes=self._min_before_close)

    # --- write -----------------------------------------------------------------
    def save(self, rows: list[dict[str, Any]], *, max_day_risk: Any = None) -> dict[str, Any]:
        """UC-02: validate, bump config_version, persist. An invalid schedule is
        never written — a half-saved schedule could arm on the next restart."""
        errors = self.validate(rows)
        if errors:
            return {"result": "invalid",
                    "errors": [{"field": e.field, "reason": e.reason, "index": e.index}
                               for e in errors]}

        version = self._next_version()
        # v1.47 pin-at-Save: persist CONCRETE values, resolved against the globals
        # in force right now. A later change to a global cannot reach back into a
        # saved row — the row is the contract.
        self._state.entry_schedule = [
            pinned_row(resolve(spec_from_row(r), self._defaults)) for r in rows]
        if max_day_risk not in (None, ""):
            self._state.max_day_risk = str(Decimal(str(max_day_risk)))
        self._state.config_version = version
        return {"result": "saved", "config_version": version, **self.view().to_dict()}

    def _next_version(self) -> str:
        current = self._state.config_version
        n = 0
        if isinstance(current, str) and current.startswith("v"):
            try:
                n = int(current[1:])
            except ValueError:
                n = 0
        return f"v{n + 1}"

    # --- arm -------------------------------------------------------------------
    def may_arm(self) -> list[ScheduleError]:
        """ENT-01a: arming requires >= 1 composed, legal entry."""
        return self.validate(self._state.entry_schedule or [])
