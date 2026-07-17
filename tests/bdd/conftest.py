"""Shared fixtures for tests/bdd.

`vitest_result` is used by TC-UI-05 and TC-UI-06: both scenarios pin frontend
TypeScript behaviour (frontend/src/time.ts's etToZone, and
frontend/src/components/NextEntryCountdown.tsx's display logic) that Python
cannot execute directly. Each binds its clause by shelling out to the REAL
vitest suite via `npx vitest run` and asserting on the actual pass/fail
result -- never a tautology, since a regression in either frontend file
fails the corresponding vitest test and therefore this fixture's assertions.

Session-scoped so the (~2-3s) vitest/esbuild startup cost is paid once for
the whole tests/bdd run rather than once per scenario.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture(scope="session")
def vitest_result():
    """Runs the two vitest files TC-UI-05/TC-UI-06 (and TC-DAY-07's frontend
    halves) depend on, once, verbose (so individual test names appear in the
    output for scenarios to assert against, not just an aggregate pass
    count)."""
    proc = subprocess.run(
        ["npx", "vitest", "run", "src/time.test.ts",
         "src/components/NextEntryCountdown.test.tsx", "--reporter=verbose"],
        cwd=str(FRONTEND_DIR), capture_output=True, encoding="utf-8",
        shell=(sys.platform == "win32"), timeout=120,
    )
    output = _ANSI.sub("", proc.stdout + proc.stderr)
    return proc.returncode, output


@pytest.fixture(scope="session")
def vitest_cal_result():
    """TC-CAL-01/TC-CAL-02's frontend halves (slice 2, v1.71) -- tier-2 visual
    distinction (CAL-01), the year-grid's tagged-day marking, and the CAL-06
    manual-fire warn-and-acknowledge dialog (OK disabled until checked,
    request carries blackout_ack) -- the same real-vitest binding strategy as
    `vitest_result`/`vitest_ui07_result` above, over the three suites those
    behaviours live in. Session-scoped for the same startup-cost reason."""
    proc = subprocess.run(
        ["npx", "vitest", "run", "src/components/CalendarPage.test.tsx",
         "src/components/ManualTradeCard.test.tsx",
         "src/components/SchedulePanel.test.tsx", "--reporter=verbose"],
        cwd=str(FRONTEND_DIR), capture_output=True, encoding="utf-8",
        shell=(sys.platform == "win32"), timeout=180,
    )
    output = _ANSI.sub("", proc.stdout + proc.stderr)
    return proc.returncode, output


@pytest.fixture(scope="session")
def vitest_ui07_result():
    """TC-UI-07's frontend halves (UI-28 contract-dollar display, UI-18a
    markup disclosure, UI-26a heatmap honesty, UI-28 slippage columns) — the
    same real-vitest binding strategy as `vitest_result` above, over the four
    suites those behaviours live in. Session-scoped for the same startup-cost
    reason."""
    proc = subprocess.run(
        ["npx", "vitest", "run", "src/money.test.ts",
         "src/components/SchedulePanel.test.tsx",
         "src/components/results/CalendarHeatmap.test.tsx",
         "src/components/results/SlippagePanels.test.tsx", "--reporter=verbose"],
        cwd=str(FRONTEND_DIR), capture_output=True, encoding="utf-8",
        shell=(sys.platform == "win32"), timeout=180,
    )
    output = _ANSI.sub("", proc.stdout + proc.stderr)
    return proc.returncode, output


@pytest.fixture(scope="session")
def vitest_ui09_result():
    """TC-UI-09's frontend halves (UI-31, v1.73 queue slice 5): the activity
    feed's sticky ET date separators, per-row ET wall-clock times, and hover/
    focus/tap tooltips explaining every event (ActivityFeed.test.tsx), plus
    the app.py-authoritative completeness gate (activityVocabulary.test.ts) --
    same real-vitest binding strategy as `vitest_result` above. Session-scoped
    for the same startup-cost reason."""
    proc = subprocess.run(
        ["npx", "vitest", "run", "src/components/ActivityFeed.test.tsx",
         "src/activityVocabulary.test.ts", "--reporter=verbose"],
        cwd=str(FRONTEND_DIR), capture_output=True, encoding="utf-8",
        shell=(sys.platform == "win32"), timeout=180,
    )
    output = _ANSI.sub("", proc.stdout + proc.stderr)
    return proc.returncode, output


@pytest.fixture(scope="session")
def vitest_rpt23_result():
    """TC-RPT-23's frontend half (RPT-17/UI-33, v1.82): the day-trades table's
    per-side badges/credits/realized-P&L rendering, the open row's live P&L
    badged "unrealized" updating in place on the next poll, and the Timing &
    Unmanaged report's honest "no data (not sampled)" state -- same real-
    vitest binding strategy as `vitest_result` above. Session-scoped for the
    same startup-cost reason."""
    proc = subprocess.run(
        ["npx", "vitest", "run", "src/components/DayTradesTable.test.tsx", "--reporter=verbose"],
        cwd=str(FRONTEND_DIR), capture_output=True, encoding="utf-8",
        shell=(sys.platform == "win32"), timeout=180,
    )
    output = _ANSI.sub("", proc.stdout + proc.stderr)
    return proc.returncode, output


@pytest.fixture(scope="session")
def vitest_doc_result():
    """TC-DOC-01's frontend halves (doc 12, slices 4 and 6): the how-it-works
    tab's DOC-05 single-source rendering (version stamp, mismatch banner,
    DOC-03 chapter completeness, no-trading-controls) in HowItWorksPage.test.tsx,
    the DOC-06/UI-32 Getting-started tab (its own section stamp, the no-secret-
    leak contract, DOC-06 five-section completeness) in
    GettingStartedPage.test.tsx, plus the five-tab nav (App.test.tsx) -- same
    real-vitest binding strategy as `vitest_result`/`vitest_cal_result` above.
    Session-scoped for the same startup-cost reason."""
    proc = subprocess.run(
        ["npx", "vitest", "run", "src/components/HowItWorksPage.test.tsx",
         "src/components/GettingStartedPage.test.tsx",
         "src/App.test.tsx", "--reporter=verbose"],
        cwd=str(FRONTEND_DIR), capture_output=True, encoding="utf-8",
        shell=(sys.platform == "win32"), timeout=180,
    )
    output = _ANSI.sub("", proc.stdout + proc.stderr)
    return proc.returncode, output
