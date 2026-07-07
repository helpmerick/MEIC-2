"""Hand-written step definitions for TC-ENT-06 — the ENT-08/REC-06 entry
warm-up: proactive token renewal, stream resubscription, and the never-delay
guarantee (an unrecoverable session skips `invalid_session` on time)."""
import pytest
from pytest_bdd import given, scenarios, then, when

from meic.application.warmup import plan_warmup, warmup_runs

scenarios("../features/TC-ENT-06.feature")


@pytest.fixture
def world():
    return {}


# --- Scenario 1: near-expiry token renewed -----------------------------------

@given('the session token expires in 200 seconds at T-60')
def _(world):
    world["token_expires_in_seconds"] = 200


@when('the warm-up probe runs')
def _(world):
    world["result"] = plan_warmup(token_expires_in_seconds=world["token_expires_in_seconds"])


@then('the token is renewed before T-30')
def _(world):
    assert world["result"].renewed is True and world["result"].session_ok is True


@then('the 10:30 entry begins exactly on schedule with fresh quotes')
def _(world):
    assert world["result"].entry_delayed is False and world["result"].entry_reason is None


# --- Scenario 2: dropped stream resubscribed ---------------------------------

@given('the DXLink chain subscription is silently stale at T-60')
def _(world):
    world["result"] = plan_warmup(token_expires_in_seconds=9999, stream_stale=True)


@then('the warm-up resubscribes and quotes are fresh (STK-04) at fire time')
def _(world):
    assert world["result"].resubscribed is True
    assert world["result"].entry_reason is None and world["result"].entry_delayed is False


# --- Scenario 3: warm-up cannot restore the session --------------------------

@given('token renewal fails repeatedly from T-60')
def _(world):
    world["result"] = plan_warmup(token_expires_in_seconds=200, renewal_succeeds=False)


@then('an alert is raised at T-10')
def _(world):
    assert world["result"].alert == ("critical", "session_unrecoverable")


@then('at fire time the entry is SKIPPED with reason "invalid_session"')
def _(world):
    assert world["result"].session_ok is False
    assert world["result"].entry_reason == "invalid_session"


@then('the entry time itself was never delayed')
def _(world):
    assert world["result"].entry_delayed is False  # ENT-08: the clock never slips


# --- Scenario 4: bot starts inside the warm-up window ------------------------

@given('the bot finishes recovery at T-30')
def _(world):
    world["runs"] = warmup_runs(recovery_done_seconds_before=30)


@then('the warm-up probe runs immediately (compressed), not skipped')
def _(world):
    assert world["runs"] is True
