"""Server logging from boot (2026-07-14, operator ruling).

Server logs were NEVER WRITTEN AT ALL until 2026-07-13 -- the only prior logs
existed because an operator hand-typed PowerShell's `-RedirectStandardOutput`
on the uvicorn launch line, so anyone starting the app any other way got
NOTHING and the cert-day logs are permanently lost. These tests pin
`adapters/logging_setup.py`'s fix: logging is configured IN THE APPLICATION,
one timestamped file PER BOOT (never truncating a prior run), file + stdout,
env-configurable, and no secret ever reaches a log line.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from meic.adapters.logging_setup import configure_logging, reset_for_testing


def setup_function(_fn) -> None:
    reset_for_testing()


def teardown_function(_fn) -> None:
    reset_for_testing()


def test_boot_writes_a_new_timestamped_file_under_the_log_dir(tmp_path):
    path = configure_logging({}, root=tmp_path, now=datetime(2026, 7, 14, 18, 30, 0, tzinfo=timezone.utc))

    assert path is not None
    assert path.parent == tmp_path / "logs"
    assert path.name == "meic-20260714T183000Z.log"
    assert path.exists()


def test_a_restart_never_truncates_the_previous_boots_file(tmp_path):
    """MUST FAIL before the fix: no logging was configured in the app at
    all, so there was no file, let alone one immune to truncation."""
    first = datetime(2026, 7, 14, 18, 30, 0, tzinfo=timezone.utc)
    path1 = configure_logging({}, root=tmp_path, now=first)
    logging.getLogger("meic.test").info("first boot's own line")

    reset_for_testing()  # simulate a fresh process boot

    second = datetime(2026, 7, 14, 18, 45, 0, tzinfo=timezone.utc)
    path2 = configure_logging({}, root=tmp_path, now=second)
    logging.getLogger("meic.test").info("second boot's own line")

    assert path1 != path2, "a restart must get its OWN file, never reuse the prior boot's name"
    assert path1.exists() and path2.exists()
    # The first boot's line must still be there -- never truncated by the second.
    assert "first boot's own line" in path1.read_text(encoding="utf-8")
    assert "second boot's own line" in path2.read_text(encoding="utf-8")
    assert "second boot's own line" not in path1.read_text(encoding="utf-8")


def test_two_boots_within_the_same_second_still_get_distinct_files(tmp_path):
    """The filename resolution is to the second; a same-second collision (a
    fast test harness, or two factories booting back to back) must still
    never silently truncate a sibling file."""
    same_instant = datetime(2026, 7, 14, 18, 30, 0, tzinfo=timezone.utc)
    path1 = configure_logging({}, root=tmp_path, now=same_instant)
    reset_for_testing()
    path2 = configure_logging({}, root=tmp_path, now=same_instant)

    assert path1 != path2
    assert path1.exists() and path2.exists()


def test_configure_logging_is_idempotent_within_one_process(tmp_path):
    """A test harness (or the app itself) calling configure_logging twice in
    the SAME process must never open a second file or duplicate handlers --
    only a restart (a fresh process) gets a new one."""
    path1 = configure_logging({}, root=tmp_path)
    path2 = configure_logging({}, root=tmp_path)  # same process, no reset

    assert path1 is not None
    assert path2 is None, "a second call in the same process must be a no-op"


def test_logs_to_both_file_and_stdout(tmp_path, capsys):
    path = configure_logging({}, root=tmp_path, now=datetime(2026, 7, 14, 18, 30, 0, tzinfo=timezone.utc))

    logging.getLogger("meic.test").warning("both destinations must see this")

    assert "both destinations must see this" in path.read_text(encoding="utf-8")
    assert "both destinations must see this" in capsys.readouterr().out


def test_log_dir_and_level_are_env_configurable(tmp_path):
    custom_dir = tmp_path / "custom-logs"
    path = configure_logging({"MEIC_LOG_DIR": str(custom_dir), "MEIC_LOG_LEVEL": "WARNING"},
                             root=tmp_path, now=datetime(2026, 7, 14, 18, 30, 0, tzinfo=timezone.utc))

    assert path.parent == custom_dir
    logging.getLogger("meic.test").info("suppressed below WARNING")
    logging.getLogger("meic.test").warning("kept at WARNING")
    content = path.read_text(encoding="utf-8")
    assert "suppressed below WARNING" not in content
    assert "kept at WARNING" in content


def test_zero_config_boot_still_logs_with_sane_defaults(tmp_path):
    """No MEIC_LOG_DIR/MEIC_LOG_LEVEL set at all -- must still work."""
    path = configure_logging({}, root=tmp_path, now=datetime(2026, 7, 14, 18, 30, 0, tzinfo=timezone.utc))

    assert path is not None and path.exists()
    assert path.parent == tmp_path / "logs"


def test_log_format_carries_timestamp_level_and_logger_name(tmp_path):
    """Forensically useful (per the operator's ask): enough context to
    reconstruct WHEN, HOW SEVERE, and FROM WHERE without external context."""
    path = configure_logging({}, root=tmp_path, now=datetime(2026, 7, 14, 18, 30, 0, tzinfo=timezone.utc))

    logging.getLogger("meic.server").error("a forensic test line")

    line = [l for l in path.read_text(encoding="utf-8").splitlines() if "a forensic test line" in l][0]
    assert "ERROR" in line
    assert "meic.server" in line
    assert "2026" in line  # a real timestamp is present


def test_no_secret_ever_reaches_the_log_from_a_boot_announcement(tmp_path, monkeypatch):
    """Drives the REAL live_app() boot path (not just logging_setup directly)
    and asserts none of the operator's actual secrets appear anywhere in the
    resulting file -- the concrete pin for 'NEVER log the contents of .env,
    tokens, the user password, or broker credentials'."""
    import base64
    import json

    from meic.adapters.api.server import live_app

    def _jwt(iss: str) -> str:
        seg = base64.urlsafe_b64encode(json.dumps({"iss": iss}).encode()).decode().rstrip("=")
        return f"header.{seg}.sig"

    secret_password = "sekrit-panel-password-xyz"
    secret_provider_secret = "sekrit-provider-secret-abc"
    secret_refresh_token = _jwt("https://api.sandbox.tastyworks.com")

    monkeypatch.setenv("TT_CERT_PROVIDER_SECRET", secret_provider_secret)
    monkeypatch.setenv("TT_CERT_REFRESH_TOKEN", secret_refresh_token)
    monkeypatch.setenv("TT_CERT_ACCOUNT", "5WZ00000")
    monkeypatch.setenv("MEIC_LIVE_IS_TEST", "true")
    monkeypatch.setenv("MEIC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MEIC_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("MEIC_USER_PASSWORD", secret_password)

    live_app()

    log_files = list((tmp_path / "logs").glob("meic-*.log"))
    assert log_files, "live_app() must have configured logging and written a file"
    content = log_files[0].read_text(encoding="utf-8")

    assert secret_password not in content
    assert secret_provider_secret not in content
    assert secret_refresh_token not in content
