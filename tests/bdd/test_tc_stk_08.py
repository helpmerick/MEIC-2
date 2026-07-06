"""TC-STK-08 — probe-walk vectors (v1.39): BLOCKED on a proposed spec amendment.

The extracted TC-STK-08.feature is invalid Gherkin: the final scenario's Then
step wraps across an indented continuation line ('T-0.20, T-0.25 ...'), which
the Gherkin parser rejects (TokenError) — the same defect class the v1.37
amendment fixed in TC-ENT-07/TC-FLT-01/TC-NFR-04. pytest-bdd cannot bind ANY
scenario in a file that fails to parse, so this module carries no scenarios()
call until the operator ratifies the line-join.

The eight vectors themselves are NOT waiting: they are pinned green at unit
level in tests/domain/test_chain_and_walk.py (TestProbeWalkVectors), against
the rebuilt probe walk in meic/domain/walk.py. Once the amendment lands,
regenerate features and replace this module with real step definitions.
"""


def test_tc_stk_08_blocked_on_spec_amendment():
    raise NotImplementedError(
        "TC-STK-08: feature file is invalid Gherkin (wrapped step line in the "
        "'Probe order is deterministic and logged' scenario) — blocked on the "
        "proposed spec amendment; the eight probe vectors are meanwhile pinned "
        "green at unit level (tests/domain/test_chain_and_walk.py)"
    )
