"""Hand-written step definitions for TC-ORD-02 — a submit timeout must not
produce a duplicate order (ORD-04/REC-05 idempotency)."""
import pytest
from pytest_bdd import given, scenarios, then, when

from meic.application.idempotency import resolve_submit_after_timeout

scenarios("../features/TC-ORD-02.feature")

KEY = "entry:2026-07-06:1"


@pytest.fixture
def world():
    return {}


@given('the broker accepts the order but the submit response times out')
def _(world):
    world["key"] = KEY
    # the order actually landed at the broker (its key is present) even though
    # the bot never saw the response
    world["broker_keys"] = {KEY, "stop:2026-07-06:1:PUT"}


@when('the bot queries by idempotency key')
def _(world):
    world["decision"] = resolve_submit_after_timeout(world["key"], world["broker_keys"])


@then('it discovers the existing order and does NOT resubmit')
def _(world):
    assert world["decision"]["exists"] is True
    assert world["decision"]["resubmit"] is False

    # sanity: a key the broker does NOT hold would resubmit
    missing = resolve_submit_after_timeout("entry:2026-07-06:2", world["broker_keys"])
    assert missing == {"exists": False, "resubmit": True}
