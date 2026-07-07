"""NLE calibration records + view — NLE-06 (informational, never trigger math).

Every short-stop event writes a calibration record (estimate vs realized). The
view reports "insufficient data" below a sample threshold, else per-side
realized net-loss ratio and mean estimate error — the empirical basis for
tuning stop_loss_pct by hand (NLE-07 defers any automatic adjustment).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

MIN_SAMPLES = 25  # < 25 ⇒ insufficient data


@dataclass(frozen=True)
class CalibrationRecord:
    side: str            # "PUT" | "CALL"
    estimated_net_loss: Decimal
    realized_net_loss: Decimal
    markup: Decimal = Decimal("0")

    @property
    def error(self) -> Decimal:
        return self.realized_net_loss - self.estimated_net_loss


@dataclass
class CalibrationView:
    records: list[CalibrationRecord] = field(default_factory=list)

    def add(self, record: CalibrationRecord) -> None:
        self.records.append(record)

    def sufficient(self) -> bool:
        return len(self.records) >= MIN_SAMPLES

    def summary(self) -> dict:
        if not self.sufficient():
            return {"status": "insufficient_data", "samples": len(self.records)}
        out: dict = {"status": "ok", "samples": len(self.records)}
        for side in ("PUT", "CALL"):
            rs = [r for r in self.records if r.side == side]
            if not rs:
                continue
            mean_err = sum((r.error for r in rs), Decimal("0")) / len(rs)
            realized = sum((r.realized_net_loss for r in rs), Decimal("0"))
            estimated = sum((r.estimated_net_loss for r in rs), Decimal("0"))
            out[side] = {
                "mean_estimate_error": mean_err,
                "realized_over_estimated": (realized / estimated) if estimated else None,
                "n": len(rs),
            }
        return out
