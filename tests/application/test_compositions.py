"""Paper vs live compositions — EC-RSK-04 structural separation."""
import base64
import json
from datetime import datetime
from decimal import Decimal as D

from meic.adapters.sim.simulated_broker import SimulatedBroker
from meic.adapters.tastytrade.adapter import TastytradeAdapter
from meic.composition.live import LiveComposition
from meic.composition.paper import PaperComposition
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_clock import ET, FakeClock

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
CLOCK = FakeClock(datetime(2026, 7, 6, 9, 30, tzinfo=ET))


def _jwt(iss: str) -> str:
    def seg(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{seg({'alg': 'EdDSA'})}.{seg({'iss': iss})}.sig"


def _cert_jwt() -> str:
    return _jwt("https://api.sandbox.tastyworks.com")


def test_paper_binds_simulated_broker_only():
    comp = PaperComposition(clock=CLOCK, ticks=SPX)
    assert isinstance(comp.broker, SimulatedBroker)
    assert comp.state.trading_mode == "paper"


def test_live_binds_tastytrade_adapter_never_simulated():
    """EC-RSK-04: the live wiring constructs the live adapter; the
    SimulatedBroker is never constructed in the live path."""
    comp = LiveComposition(clock=CLOCK, ticks=SPX, provider_secret="s",
                           refresh_token=_cert_jwt(), is_test=True)
    assert isinstance(comp.broker, TastytradeAdapter)
    assert not isinstance(comp.broker, SimulatedBroker)
    assert comp.state.trading_mode == "live"
    # same service surface as paper (structurally identical pipeline)
    assert comp.execute and comp.protect and comp.recover and comp.close and comp.day


def test_live_refuses_non_cert_token_at_construction():
    """The issuer guard fires in the adapter constructor (before any network)."""
    from meic.adapters.tastytrade.adapter import NonCertTokenRefused
    prod = _jwt("https://api.tastytrade.com")  # production issuer
    try:
        LiveComposition(clock=CLOCK, ticks=SPX, provider_secret="s", refresh_token=prod, is_test=True)
        assert False, "should have refused a non-cert token"
    except NonCertTokenRefused:
        pass
