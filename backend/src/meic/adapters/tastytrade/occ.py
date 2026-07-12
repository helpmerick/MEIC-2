"""Re-export: OCC symbology is not tastytrade-specific.

It moved to `meic.adapters.occ` when the SimulatedBroker and the test FakeBroker
needed it too — ORD-09 requires EVERY broker to report leg symbols, so every
broker adapter needs symbology. Kept here so existing imports keep working.
"""
from meic.adapters.occ import occ_symbol  # noqa: F401
