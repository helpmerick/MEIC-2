"""adapters/calendar_sources -- CAL-09 (v1.77) read-only official-source
fetch, doc 11. See `common.py` for the shared domain-allowlist enforcement
every source routes through, and each source module's own docstring for
its page format and fixture provenance."""
from meic.adapters.calendar_sources.bea import BeaSource
from meic.adapters.calendar_sources.bls import BlsSource
from meic.adapters.calendar_sources.fomc import FomcSource

__all__ = ["BeaSource", "BlsSource", "FomcSource"]
