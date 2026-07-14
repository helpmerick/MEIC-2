"""Boot-time logging configuration (2026-07-14, operator ruling).

Server logs were NEVER WRITTEN AT ALL until 2026-07-13 -- the only reason any
log existed before that date is an operator hand-typing PowerShell's
`-RedirectStandardOutput`/`-RedirectStandardError` on the uvicorn launch line.
Anyone starting the app any other way (a different shell, a supervisor, a
container) got NOTHING, and the cert-day (2026-07-09/07-10) logs are
permanently lost as a direct result -- several live bugs (the 07-10 C7565
missed stop, the unjournaled OWN-09/10 standdown) took DAYS to diagnose partly
because there was no durable record of what the process actually did.

This module configures Python's own `logging` so the behaviour lives IN THE
APPLICATION, not the launch command: every entrypoint (`paper_app`, `live_app`
-- adapters/api/server.py) calls `configure_logging(env, root=ROOT)` once at
boot, so a boot logs identically whether started via
`uvicorn ...:live_app --factory`, a supervisor, or a bare `python -c`.

Design:
  * ONE timestamped file PER BOOT under `MEIC_LOG_DIR` (default `<repo>/logs`,
    already gitignored) -- named with the boot's own UTC instant to the
    second, so a restart can NEVER truncate the previous run's file (the
    exact failure mode of a fixed filename opened in truncate mode, or of
    relying on an operator to remember a fresh `-RedirectStandardOutput`
    path every time).
  * BOTH the file AND stdout -- console/dev use keeps working unchanged.
  * Level and directory are env-configurable (`MEIC_LOG_DIR` / `MEIC_LOG_
    LEVEL`), read the SAME way every other dial in adapters/api/server.py
    is (`_read_env()`'s merged dict), with sane defaults so a zero-config
    boot still logs.
  * Idempotent per PROCESS: a test harness or a factory called more than
    once in the same process (every existing `paper_app()`/`live_app()`
    test fixture) must never accumulate duplicate handlers or open a
    second file -- see `_CONFIGURED`. Tests that need a FRESH file (to
    assert on boot-file behaviour) call `reset_for_testing()` first.
  * NEVER logs secrets by construction: this module emits no line that
    embeds `.env` contents, a token, the operator password, or broker
    credentials -- it only attaches handlers/formatters. Whether a
    CALLER's log line could carry one is a property of what that caller
    logs, not of this module; see the operator report for the one
    pre-existing spot flagged (a broker connect failure's `repr(exc)`,
    already exposed via the `/broker/connect` JSON response today, is now
    ALSO durably logged -- unchanged risk, wider durability).
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

_CONFIGURED = False  # process-wide idempotency guard -- see module docstring


def reset_for_testing() -> None:
    """Test-only: clear the idempotency guard AND detach any handlers this
    module previously attached to the root logger, so a test can observe a
    fresh `configure_logging()` call actually create a new file. Production
    code never calls this."""
    global _CONFIGURED
    _CONFIGURED = False
    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        if getattr(handler, "_meic_boot_logging", False):
            root_logger.removeHandler(handler)
            handler.close()


def _log_level(env: dict[str, str]) -> int:
    """`MEIC_LOG_LEVEL` (default INFO). An unrecognised name falls back to
    INFO rather than crashing boot over a typo -- the same reject-the-dial
    convention every dial in adapters/api/server.py follows."""
    name = env.get("MEIC_LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, name, None)
    return level if isinstance(level, int) else logging.INFO


def _log_dir(env: dict[str, str], root: Path) -> Path:
    """`MEIC_LOG_DIR` (default `<repo root>/logs`, already gitignored)."""
    raw = env.get("MEIC_LOG_DIR")
    return Path(raw) if raw else (root / "logs")


def configure_logging(env: dict[str, str], *, root: Path, now: datetime | None = None) -> Path | None:
    """Attach a per-boot file handler + a stdout handler to the root logger.
    Returns the log file path, or None if already configured this process
    (idempotent -- see module docstring; call `reset_for_testing()` first if
    a test needs to observe a second real boot).

    `now` is injectable (tests only) for a deterministic filename; live
    callers never pass it and get the real boot instant (UTC, so the
    filename never depends on the operator's local timezone/DST)."""
    global _CONFIGURED
    if _CONFIGURED:
        return None

    log_dir = _log_dir(env, root)
    log_dir.mkdir(parents=True, exist_ok=True)

    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    path = log_dir / f"meic-{stamp}.log"
    # A second boot within the same UTC second (or a test harness booting
    # several factories back to back) must still never truncate a sibling
    # file -- never open in 'w' mode, never reuse a path that already exists.
    suffix = 1
    while path.exists():
        path = log_dir / f"meic-{stamp}-{suffix}.log"
        suffix += 1

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    file_handler = logging.FileHandler(path, mode="a", encoding="utf-8")
    file_handler.setFormatter(fmt)
    file_handler._meic_boot_logging = True  # tag for reset_for_testing()

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    stream_handler._meic_boot_logging = True

    root_logger = logging.getLogger()
    root_logger.setLevel(_log_level(env))
    root_logger.addHandler(file_handler)
    root_logger.addHandler(stream_handler)
    # Route `warnings.warn(...)` (any supervised loop, any library) through
    # the SAME logging pipeline -- forensically useful, never a secret by
    # itself (a warning's own message, same as any other log line here).
    logging.captureWarnings(True)

    _CONFIGURED = True
    return path
