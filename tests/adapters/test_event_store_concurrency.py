"""REC-01 sqlite shared-connection race (2026-07-14 production incident,
logs/panel-2026-07-13-own01.err.log): FastAPI's sync endpoints run on the
ASGI threadpool while the trading loops (supervisor, watchers, attempt
tasks) run on the event-loop thread. Both sides can touch the SAME
`sqlite3.Connection` (opened with `check_same_thread=False`) at the SAME
time. Without synchronization this corrupts the connection's own internal
statement/cursor state -- `sqlite3.InterfaceError: bad parameter or other
API misuse` -- observed twice in one production afternoon, once via
`SqliteStateStore.get` (the `/state`-family reads) and once via
`EventJournal`-backed `schedule_rows -> ScheduleService.resolved()`.

The fix (event_store.py) is a per-instance `threading.RLock` around every
method that touches `self._conn`. These tests pin BOTH directions:
  * WITH the lock: hammering the real classes from many threads must be
    perfectly clean, deterministically, every run -- this is what CI pins.
  * WITHOUT the lock: the exact same hammer against the exact same classes,
    with the lock swapped for a no-op, is the fail-first evidence the race
    is real. It is inherently timing-dependent (GIL/C-extension scheduling
    dependent), so it is a BOUNDED best-effort demonstration, not a hard
    CI gate -- it never asserts failure and never turns red on its own; if
    it can't reproduce on a given machine within its attempt budget it
    skips with an honest message instead of flaking red or green.
"""
import threading

import pytest

from meic.adapters.persistence.event_store import EventJournal, SqliteStateStore
from meic.domain.events import DayArmed

N_THREADS = 8
OPS_PER_THREAD = 150


class _NullLock:
    """A no-op stand-in for the real per-instance lock. Swapped onto an
    otherwise-real store/journal instance so the WITHOUT-the-fix
    demonstration below exercises the SAME production code path (only the
    synchronization is removed), rather than a reimplementation that could
    silently drift from what actually ships."""

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _hammer_state_store(store, n_threads: int, ops_per_thread: int) -> list[BaseException]:
    """N threads x M operations, mixed reads and writes, on ONE shared
    SqliteStateStore -- the `/state`-family read + a background writer
    shape from the incident."""
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def worker(tid: int) -> None:
        for i in range(ops_per_thread):
            try:
                store.set(f"k{tid}", str(i))
                store.get(f"k{tid}")
                store.all()
            except BaseException as exc:  # noqa: BLE001 — capturing the race itself
                with errors_lock:
                    errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    return errors


def _hammer_journal(journal, n_threads: int, ops_per_thread: int) -> list[BaseException]:
    """N threads x M operations, mixed append + load, on ONE shared
    EventJournal -- the `schedule_rows -> ScheduleService.resolved()` read
    racing attempt-task/watcher writes shape from the incident."""
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def worker(tid: int) -> None:
        for i in range(ops_per_thread):
            try:
                journal.append(DayArmed(date=f"2026-07-{(tid % 28) + 1:02d}", entry_count=i))
                journal.load()
            except BaseException as exc:  # noqa: BLE001
                with errors_lock:
                    errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    return errors


# --- WITH the fix: deterministic, always green --------------------------------

def test_sqlite_state_store_survives_concurrent_hammering_with_lock(tmp_path):
    store = SqliteStateStore(tmp_path / "state.db")

    errors = _hammer_state_store(store, N_THREADS, OPS_PER_THREAD)

    assert errors == []
    # Sanity: every writer's last value actually landed -- the lock
    # serializes, it does not silently drop writes.
    for tid in range(N_THREADS):
        assert store.get(f"k{tid}") == str(OPS_PER_THREAD - 1)


def test_event_journal_survives_concurrent_hammering_with_lock(tmp_path):
    journal = EventJournal(tmp_path / "state.db")

    errors = _hammer_journal(journal, N_THREADS, OPS_PER_THREAD)

    assert errors == []
    assert len(journal.load()) == N_THREADS * OPS_PER_THREAD


# --- WITHOUT the fix: bounded, best-effort fail-first evidence ----------------
# Never a flaky red (or a falsely-green) CI gate: bounded attempts, skip with
# an honest message if the race doesn't reproduce on this machine/run. The
# LOCKED tests above are what CI actually pins.

def test_without_the_lock_the_state_store_race_can_reproduce(tmp_path):
    reproduced_error = None
    attempts = 5
    for attempt in range(attempts):
        store = SqliteStateStore(tmp_path / f"unlocked_state_{attempt}.db")
        store._lock = _NullLock()  # simulate the pre-fix code: no synchronization
        errors = _hammer_state_store(store, N_THREADS, OPS_PER_THREAD)
        if errors:
            reproduced_error = errors[0]
            break
    if reproduced_error is None:
        pytest.skip(f"the shared-connection race did not reproduce within "
                    f"{attempts} bounded attempts on this machine/timing -- it is "
                    "inherently non-deterministic (GIL/C-extension scheduling). "
                    "The locked version's determinism (see the paired test above) "
                    "is what this suite actually pins.")
    assert isinstance(reproduced_error, Exception)


def test_without_the_lock_the_journal_race_can_reproduce(tmp_path):
    reproduced_error = None
    attempts = 5
    for attempt in range(attempts):
        journal = EventJournal(tmp_path / f"unlocked_journal_{attempt}.db")
        journal._lock = _NullLock()  # simulate the pre-fix code: no synchronization
        errors = _hammer_journal(journal, N_THREADS, OPS_PER_THREAD)
        if errors:
            reproduced_error = errors[0]
            break
    if reproduced_error is None:
        pytest.skip(f"the shared-connection race did not reproduce within "
                    f"{attempts} bounded attempts on this machine/timing -- it is "
                    "inherently non-deterministic (GIL/C-extension scheduling). "
                    "The locked version's determinism (see the paired test above) "
                    "is what this suite actually pins.")
    assert isinstance(reproduced_error, Exception)
