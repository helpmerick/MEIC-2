"""Paper vs live compositions — EC-RSK-04 structural separation."""
import asyncio
import base64
import json
from datetime import datetime
from decimal import Decimal as D

from meic.adapters.sim.simulated_broker import SimulatedBroker
from meic.adapters.tastytrade.adapter import TastytradeAdapter
from meic.application.execute_entry import Condor
from meic.composition.live import LiveComposition
from meic.composition.paper import PaperComposition
from meic.domain.events import CondorFilled, FilledLeg
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_clock import ET, FakeClock

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
CLOCK = FakeClock(datetime(2026, 7, 6, 9, 30, tzinfo=ET))


def _filled_legs():
    """Today's incident shape: 1.80/1.95 sold, 0.08/0.07 bought -> 3.60 actual."""
    return (
        FilledLeg(symbol="SPXW260706P07535000", right="P", role="short", qty=1, price=D("1.80")),
        FilledLeg(symbol="SPXW260706P07510000", right="P", role="long", qty=1, price=D("0.08")),
        FilledLeg(symbol="SPXW260706C07540000", right="C", role="short", qty=1, price=D("1.95")),
        FilledLeg(symbol="SPXW260706C07565000", right="C", role="long", qty=1, price=D("0.07")),
    )


def _condor():
    return Condor(entry_number=1, put_short=D("7535"), call_short=D("7540"),
                  put_short_mid=D("1.85"), call_short_mid=D("2.00"),
                  mid_credit=D("3.50"), min_total_credit=D("2.00"),
                  put_long=D("7510"), call_long=D("7565"))


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


# --- BUG 2 (STP-02, 2026-07-09 incident): stops off the ACTUAL fill credit -----

def test_paper_on_filled_passes_the_actual_fill_credit_to_protect():
    """The stop trigger is pct x net credit (STP-02) — the day the rung read
    3.50 but the broker filled 3.60, a stop wired to `condor.mid_credit` rests
    at pct x 3.50 instead of pct x the real 3.60. `_on_filled`'s `fill_credit`
    kwarg must reach `protect.protect`'s `total_net_credit` unchanged."""
    comp = PaperComposition(clock=CLOCK, ticks=SPX)
    entry_id = "2026-07-06#1"
    comp.events.append(CondorFilled(entry_id=entry_id, net_credit=D("3.60"), legs=_filled_legs()))

    captured = {}

    async def fake_protect(**kw):
        captured.update(kw)

    comp.protect.protect = fake_protect
    asyncio.run(comp._on_filled(entry_id, _condor(), None, fill_credit=D("3.60")))

    assert captured["total_net_credit"] == D("3.60")


def test_paper_on_filled_falls_back_to_mid_when_fill_credit_is_none():
    """Older call sites that don't yet know the fill (fill_credit omitted) must
    keep the pre-existing behaviour: protect off the mid estimate."""
    comp = PaperComposition(clock=CLOCK, ticks=SPX)
    entry_id = "2026-07-06#1"
    comp.events.append(CondorFilled(entry_id=entry_id, net_credit=D("3.60"), legs=_filled_legs()))

    captured = {}

    async def fake_protect(**kw):
        captured.update(kw)

    comp.protect.protect = fake_protect
    asyncio.run(comp._on_filled(entry_id, _condor(), None))

    assert captured["total_net_credit"] == D("3.50")   # condor.mid_credit


def test_live_on_filled_passes_the_actual_fill_credit_to_protect():
    """Same fix, live composition."""
    comp = LiveComposition(clock=CLOCK, ticks=SPX, provider_secret="s", refresh_token=_cert_jwt())
    entry_id = "2026-07-06#1"
    comp.events.append(CondorFilled(entry_id=entry_id, net_credit=D("3.60"), legs=_filled_legs()))

    captured = {}

    async def fake_protect(**kw):
        captured.update(kw)

    comp.protect.protect = fake_protect
    asyncio.run(comp._on_filled(entry_id, _condor(), None, fill_credit=D("3.60")))

    assert captured["total_net_credit"] == D("3.60")
