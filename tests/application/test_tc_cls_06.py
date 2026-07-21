"""TC-CLS-06 — close-boundary result taxonomy & the partial-truth invariant
(spec v1.85, CLS-06; PROPOSAL-CLOSE-500-2026-07-21.md). Ports the proposal's
Appendix B reproduction harness into permanent regression tests, driving the
REAL `/close/{entry_id}` endpoint (create_app + PanelCommands +
PaperComposition) so the whole boundary — not just PanelCommands.close_as in
isolation — is exercised, exactly as the incident was."""
import asyncio
from datetime import datetime
from decimal import Decimal as D

from fastapi.testclient import TestClient

from meic.adapters.api.app import create_app
from meic.application.cancel_taxonomy import ReplaceFilled
from meic.composition.paper import PaperComposition
from meic.composition.panel_commands import PanelCommands
from meic.domain.events import CloseIncomplete, CondorFilled, EntryClosed, FilledLeg, ShortStopped, SideClosed
from meic.domain.projection import fold
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_clock import ET, FakeClock
from tests.harness.intents import stop_intent

PANEL = "http://127.0.0.1"
SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
ENTRY_ID = "2026-07-20#7"

_TASTYTRADE_REJECTION = (
    "tif_no_after_hours_market_orders: Market orders with a time in force of "
    "Day cannot be placed when the market is closed.")


def _legs(prefix="SPXW  260720"):
    """ORD-09: the broker-reported legs a real fill would have recorded --
    matches the incident card's shape (two shorts, two longs, both sides
    Protected)."""
    return (FilledLeg(f"{prefix}P07385000", "P", "long", 1),
            FilledLeg(f"{prefix}P07435000", "P", "short", 1),
            FilledLeg(f"{prefix}C07505000", "C", "short", 1),
            FilledLeg(f"{prefix}C07555000", "C", "long", 1))


def _seed_protected_entry():
    """A FILLED, PROTECTED condor -- matches the incident card (shield badge,
    two resting stops), dated 2026-07-20 like the tester's entries."""
    comp = PaperComposition(clock=FakeClock(datetime(2026, 7, 20, 10, 29, tzinfo=ET)), ticks=SPX)
    comp.events.append(CondorFilled(entry_id=ENTRY_ID, net_credit=D("3.60"), legs=_legs()))
    asyncio.run(comp.broker.submit(stop_intent("PUT", "3.80", entry_id=ENTRY_ID)))
    asyncio.run(comp.broker.submit(stop_intent("CALL", "3.80", entry_id=ENTRY_ID)))
    return comp


class _BrokerRaisesOnMethod:
    """CLS-06 scenario 1 (Appendix B, verbatim): simulate the LIVE
    TastytradeAdapter raising on ONE named method call (its documented
    behaviour on a rejected/after-hours order). Everything else delegates to
    the real simulated broker."""
    def __init__(self, real, failing_method):
        self._real = real
        self._failing = failing_method

    def __getattr__(self, name):
        if name == self._failing:
            async def _boom(*a, **k):
                raise RuntimeError(_TASTYTRADE_REJECTION)
            return _boom
        return getattr(self._real, name)


class _BrokerRaisesOnCallLongSell:
    """CLS-06 scenario 2: `submit()` raises ONLY for the CALL long's
    sell_to_close (the mid-sequence failure site, close_entry.py's longs
    loop) -- both short replaces and the PUT long's sell delegate straight
    through to the real SimulatedBroker.

    Note: `SimulatedBroker.replace()` calls `self.submit(...)` on ITSELF
    (the real broker instance), not through this wrapper -- so wrapping only
    `submit` here does not intercept the shorts' replace-based exit at all,
    exactly matching the deterministic CLS-01 order the plan calls for: PUT
    short (replace, real), CALL short (replace, real), PUT long (sell, real),
    CALL long (sell, RAISES)."""
    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        if name != "submit":
            return getattr(self._real, name)

        async def _submit(intent, *a, **k):
            leg0 = intent.legs[0]
            if leg0.action == "sell_to_close" and leg0.right == "C":
                raise RuntimeError(_TASTYTRADE_REJECTION)
            return await self._real.submit(intent, *a, **k)
        return _submit


class _PutStopRacesFilledBroker:
    """CLS-06 + ORD-08a: `replace()` on the PUT stop discovers the stop
    already FILLED (raises ReplaceFilled -- the classified outcome both
    SimulatedBroker.replace and the live adapter's taxonomy produce for a
    filled target), while the CALL long's sell raises like scenario 2.
    Everything else delegates to the real simulated broker."""
    def __init__(self, real, put_stop_id):
        self._real = real
        self._put_stop_id = put_stop_id

    def __getattr__(self, name):
        if name == "replace":
            async def _replace(stop_id, intent):
                if stop_id == self._put_stop_id:
                    raise ReplaceFilled(stop_id, fill_price=D("3.90"))
                return await self._real.replace(stop_id, intent)
            return _replace
        if name == "submit":
            async def _submit(intent, *a, **k):
                leg0 = intent.legs[0]
                if leg0.action == "sell_to_close" and leg0.right == "C":
                    raise RuntimeError(_TASTYTRADE_REJECTION)
                return await self._real.submit(intent, *a, **k)
            return _submit
        return getattr(self._real, name)


class _RecordingAlerts:
    """A spy installed as `comp.alerts` BEFORE the close -- PanelCommands
    reads `self._comp.alerts` live at call time, not at construction."""
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []

    def alert(self, level: str, message: str, **context) -> None:
        self.calls.append((level, message, context))


def _http_close(comp, entry_id=ENTRY_ID):
    cmd = PanelCommands(comp)
    app = create_app(comp.state, comp.events, commands=cmd, panel_origin=PANEL)
    client = TestClient(app, raise_server_exceptions=False)  # behave like a real server
    return client.post(f"/close/{entry_id.replace('#', '%23')}", headers={"origin": PANEL})


def test_tc_cls_06_pre_action_failure_reports_close_failed_position_untouched():
    """CLS-06 scenario 1 (pre-action): `working_orders()` raises inside
    `assemble_close_inputs` (close_assembly.py) before any order is sent.
    Must come back 200 with a structured `close_failed/pre_submit` body --
    never the raw 500 `Internal Server Error` the 2026-07-20 incident
    produced -- and the position must be untouched: no per-side event of any
    kind lands in the journal."""
    comp = _seed_protected_entry()
    comp.broker = _BrokerRaisesOnMethod(comp.broker, "working_orders")

    resp = _http_close(comp)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")  # never text/plain
    body = resp.json()
    assert body["result"] == "close_failed"
    assert body["stage"] == "pre_submit"
    assert isinstance(body["reason"], str) and body["reason"]

    journaled_types = {type(e) for e in comp.events}
    assert not journaled_types & {SideClosed, ShortStopped, EntryClosed, CloseIncomplete}


def test_tc_cls_06_mid_sequence_failure_reports_close_partial_naming_sides():
    """CLS-06 scenario 2 (mid-sequence): the CALL long's sell_to_close raises
    AFTER both shorts already replaced clean (deterministic CLS-01 order:
    shorts PUT-then-CALL, then longs PUT-then-CALL). This must surface as
    `close_partial` naming which side closed and which remains -- never as a
    generic failure, which would present a half-closed entry as an untouched
    one (the money-risk this rule exists to fix)."""
    comp = _seed_protected_entry()
    wrapped = _BrokerRaisesOnCallLongSell(comp.broker)
    comp.broker = wrapped
    comp.close._broker = wrapped  # CloseEntry captured its own ref at construction (paper.py:74)
    alerts = _RecordingAlerts()
    comp.alerts = alerts  # installed BEFORE the call (PanelCommands reads it live)

    resp = _http_close(comp)

    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == "close_partial"
    assert body["stage"] == "in_flight"
    assert body["sides_closed"] == ["PUT"]
    assert body["sides_remaining"] == ["CALL"]
    assert isinstance(body["reason"], str) and body["reason"]

    events = list(comp.events)
    assert not any(isinstance(e, EntryClosed) for e in events)
    assert sum(isinstance(e, CloseIncomplete) for e in events) == 1

    # The per-side events for the CLOSED sides are journaled: PUT's short
    # (replace) and long (sell), and the CALL short's replace-exit.
    put_events = [e for e in events if isinstance(e, (SideClosed, ShortStopped)) and e.side == "PUT"]
    call_events = [e for e in events if isinstance(e, (SideClosed, ShortStopped)) and e.side == "CALL"]
    assert len(put_events) >= 2   # PUT short's replace-exit + PUT long's sell
    assert len(call_events) >= 1  # CALL short's replace-exit landed before the long raised

    assert any(level == "critical" for level, _msg, _ctx in alerts.calls)  # RSK-06


def test_tc_cls_06_filled_race_side_is_not_misreported_as_remaining():
    """CLS-06 + CLS-01(3)/LEX-01: a PUT stop that FILLED at the broker just
    before the close's replace (the ORD-08a race) makes CloseEntry journal
    ShortStopped and structurally hand the PUT long to LEX -- that long was
    never this close's to attempt and must NOT be counted as `remaining`.
    When the CALL long's sell then raises, the partial report must say
    sides_closed==["PUT"] (its short genuinely closed) and
    sides_remaining==["CALL"], and CloseIncomplete.remaining must not name
    the PUT long's symbol."""
    comp = _seed_protected_entry()
    # The ORD-08a race window, injected at the PORT seam: the stop is still
    # listed WORKING when `assemble_close_inputs` correlates it (so the PUT
    # side routes through the replace path, not the no-stop direct submit),
    # but by the time CloseEntry's `replace()` reaches the broker the stop
    # has filled -- the broker classifies that as ReplaceFilled (ORD-08a),
    # exactly what SimulatedBroker.replace/the live adapter's taxonomy
    # raise for a FILLED target. Nothing is pre-journaled, so the
    # projection still showed PUT open at click time.
    put_stop = next(o for o in asyncio.run(comp.broker.working_orders())
                    if o.intent.order_type == "stop_market" and o.intent.legs[0].right == "P")
    wrapped = _PutStopRacesFilledBroker(comp.broker, put_stop.order_id)
    comp.broker = wrapped
    comp.close._broker = wrapped

    resp = _http_close(comp)

    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == "close_partial"
    assert body["stage"] == "in_flight"
    assert body["sides_closed"] == ["PUT"]
    assert body["sides_remaining"] == ["CALL"]

    # The raced PUT is a genuine stop-out: ShortStopped journaled, its long
    # is LEX's job -- the journaled remainder holds ONLY the CALL long.
    assert any(isinstance(e, ShortStopped) and e.side == "PUT" for e in comp.events)
    incompletes = [e for e in comp.events if isinstance(e, CloseIncomplete)]
    assert len(incompletes) == 1
    remaining_symbols = {r[0] for r in incompletes[0].remaining}
    assert remaining_symbols == {"SPXW  260720C07555000"}  # the CALL long only
    assert "SPXW  260720P07385000" not in remaining_symbols  # PUT long is LEX's, not CLS's


def test_tc_cls_06_remainder_closes_idempotently_on_second_click():
    """CLS-06 scenario 3: continuing from the close_partial above, a second
    Close click against a HEALTHY broker must close ONLY the remaining side
    (the CALL long) -- the already-closed CALL short must NOT be re-bought
    (that would open an unintended long, the CLS-01 double-fill risk this
    rule exists to prevent) -- and exactly one EntryClosed lands in the whole
    journal (idempotency, ORD-04)."""
    comp = _seed_protected_entry()
    wrapped = _BrokerRaisesOnCallLongSell(comp.broker)
    healthy_broker = comp.broker
    comp.broker = wrapped
    comp.close._broker = wrapped

    first = _http_close(comp)
    assert first.json()["result"] == "close_partial"

    comp.broker = healthy_broker
    comp.close._broker = healthy_broker

    submitted = []
    real_submit = healthy_broker.submit

    async def _spying_submit(intent, *a, **k):
        submitted.append(intent)
        return await real_submit(intent, *a, **k)
    healthy_broker.submit = _spying_submit

    second = _http_close(comp)

    assert second.status_code == 200
    body = second.json()
    assert body["result"] == "closed"

    assert len(submitted) == 1, f"expected exactly one new broker order, got {len(submitted)}"
    only_order = submitted[0]
    assert only_order.legs[0].action == "sell_to_close"
    assert only_order.legs[0].right == "C"  # the CALL long -- never a buy_to_close on the short

    events = list(comp.events)
    assert sum(isinstance(e, EntryClosed) for e in events) == 1

    entry = fold(comp.events).entries[ENTRY_ID]
    assert entry.close_initiator == "manual"
    assert entry.incomplete_close_legs == ()


def test_tc_cls_06_baseline_and_existing_results_byte_identical():
    """CLS-06 scenario 4: a healthy close, a second click (already_closed) and
    an unknown entry id all stay byte-identical to the pre-CLS-06 contract."""
    comp = _seed_protected_entry()

    resp = _http_close(comp)
    assert resp.status_code == 200
    assert resp.json() == {"result": "closed", "initiator": "manual"}

    resp2 = _http_close(comp)
    assert resp2.status_code == 200
    assert resp2.json() == {"result": "already_closed"}

    resp3 = _http_close(comp, entry_id="nope")
    assert resp3.status_code == 200
    assert resp3.json() == {"result": "unknown_entry"}
