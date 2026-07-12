"""stop_limit unfilled-escalation — STP-03 / EC-STP-08 (pure decision).

stop_market is the default and needs none of this. But if the operator selects
stop_limit, an unfilled-stop watchdog is MANDATORY: a stop that triggered but
has not filled after config.stop_limit_escalation_seconds is cancelled and
replaced with a market order. (This is distinct from STP-03b, the secondary
trigger-source watchdog on resting stops.)
"""
from __future__ import annotations


def requires_unfilled_watchdog(stop_order_type: str) -> bool:
    """STP-03: only stop_limit needs the unfilled-escalation watchdog."""
    return stop_order_type == "stop_limit"


def should_escalate_to_market(*, stop_order_type: str, triggered: bool, filled: bool,
                              seconds_since_trigger: float,
                              escalation_seconds: float) -> bool:
    """EC-STP-08: a triggered-but-unfilled stop_limit past its escalation window
    is cancelled and replaced with a market order. stop_market never escalates
    here (it either fills or gaps through, EC-STP-03)."""
    if not requires_unfilled_watchdog(stop_order_type):
        return False
    return triggered and not filled and seconds_since_trigger >= escalation_seconds
