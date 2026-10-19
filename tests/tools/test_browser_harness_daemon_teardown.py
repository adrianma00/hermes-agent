"""kanban t_85a12f98 — the exit cleanup must reap the harness daemon ``browser_exec`` drove.

``browser_exec`` starts browser-harness's own daemon through the CLI subprocess. That daemon
is spawned detached (``start_new_session``) precisely so it outlives the call that started
it, and this process records it in ``browser_use_cli._driven_daemons`` — never in
``browser_tool._active_sessions`` (that dict belongs to the built-in agent-browser backend).

``_emergency_cleanup_all_sessions`` called ``cleanup_all_browsers()`` — the only caller of
``stop_harness_daemons()`` — behind ``if _bt._active_sessions:``, so in a process whose only
browser use was ``browser_exec`` the daemon was never stopped. It then survived the process
*inside that process's systemd cgroup*, keeping a transient ``hermes-worker-kanban-*`` scope
non-empty so ``--collect`` never reaped it (four leaked scopes, all browser_harness.daemon).
"""

from contextlib import contextmanager
from unittest.mock import patch

from tools import browser_tool as bt
from tools import browser_tool_lifecycle as lc
from tools import browser_use_cli as buc


@contextmanager
def _isolated_browser_state(driven_daemons):
    """Pin the process-global browser state (+ neutralise the unrelated, destructive sweeps)."""
    saved_sessions = bt._active_sessions.copy()
    saved_activity = bt._session_last_activity.copy()
    saved_owner_homes = bt._session_owner_homes.copy()
    saved_failures = bt._cleanup_failures.copy()
    saved_recordings = bt._recording_sessions.copy()
    saved_cleanup_done = bt._cleanup_done
    saved_driven = buc._driven_daemons.copy()
    try:
        bt._active_sessions.clear()
        bt._session_last_activity.clear()
        bt._session_owner_homes.clear()
        bt._cleanup_failures.clear()
        bt._recording_sessions.clear()
        bt._cleanup_done = False
        buc._driven_daemons.clear()
        buc._driven_daemons.update(driven_daemons)
        with (
            patch.object(lc._real_profile, "_terminate_real_profile_chrome"),
            patch.object(lc, "_stop_all_lightpanda"),
            patch.object(lc, "_reap_orphaned_browser_sessions"),
        ):
            yield
    finally:
        bt._active_sessions.clear()
        bt._active_sessions.update(saved_sessions)
        bt._session_last_activity.clear()
        bt._session_last_activity.update(saved_activity)
        bt._session_owner_homes.clear()
        bt._session_owner_homes.update(saved_owner_homes)
        bt._cleanup_failures.clear()
        bt._cleanup_failures.update(saved_failures)
        bt._recording_sessions.clear()
        bt._recording_sessions.update(saved_recordings)
        bt._cleanup_done = saved_cleanup_done
        buc._driven_daemons.clear()
        buc._driven_daemons.update(saved_driven)


def test_exit_cleanup_stops_harness_daemon_without_active_sessions():
    """browser_exec-only process: no _active_sessions entry, but the daemon must still be stopped."""
    with _isolated_browser_state({"marketing-prospect"}):
        assert bt._active_sessions == {}
        with patch.object(buc, "stop_harness_daemons") as mock_stop:
            lc._emergency_cleanup_all_sessions()
        assert mock_stop.called, "harness daemon survived the exit cleanup (scope leak)"


def test_exit_cleanup_stops_harness_daemon_alongside_active_sessions():
    """Built-in sessions AND a driven harness daemon: both teardowns run, the stop is idempotent."""
    with _isolated_browser_state({"default"}):
        bt._active_sessions["task-1"] = {"session_name": "sess-1"}
        with (
            patch.object(lc, "cleanup_all_browsers") as mock_cleanup_all,
            patch.object(buc, "stop_harness_daemons") as mock_stop,
        ):
            lc._emergency_cleanup_all_sessions()
        mock_cleanup_all.assert_called_once_with()
        assert mock_stop.called


def test_exit_cleanup_with_no_driven_daemon_is_a_noop():
    """The unconditional stop must stay harmless for a process that never used browser_exec."""
    with _isolated_browser_state(set()):
        with patch.object(buc, "_find_cli") as mock_find_cli:
            lc._emergency_cleanup_all_sessions()
        assert not mock_find_cli.called, "no daemon was driven, yet the harness CLI was invoked"
