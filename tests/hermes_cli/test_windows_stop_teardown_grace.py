"""Windows ``hermes gateway stop`` must let an accepted teardown finish.

A drained gateway is not an exited one: adapters, MCP servers, the session-DB close and the
clean-exit receipt all come after the "drain done" log line. Killing in that window is what made a
fully drained Windows planned stop read as ``exited UNCLEANLY (no exit path ran)`` in the lifecycle
ledger (Windows peer EJ-Laptop, two occurrences in one day).

``_drain_gateway_pid`` therefore grants a second, bounded window once the gateway has stamped the
ledger ``stopping`` — it accepted the stop and is tearing down. Everything here runs with fake
clocks and no Windows dependency, so the budget arithmetic is testable on any host.
"""
from __future__ import annotations

import pytest


class _Clock:
    """Deterministic monotonic clock; ``sleep`` advances it."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def drain_env(monkeypatch):
    """Patch the Windows helpers' clock, PID probe, marker writer and ledger reader."""
    from hermes_cli import gateway_windows
    from gateway import status

    clock = _Clock()
    state = {"alive": True, "phase": "stopping", "wrote_marker": []}

    monkeypatch.setattr(gateway_windows.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(gateway_windows.time, "sleep", clock.sleep)
    monkeypatch.setattr(status, "_pid_exists", lambda pid: state["alive"])
    monkeypatch.setattr(status, "write_planned_stop_marker", lambda pid: state["wrote_marker"].append(pid))
    monkeypatch.setattr(gateway_windows, "_lifecycle_phase", lambda: state["phase"])
    return gateway_windows, clock, state


def test_teardown_grace_lets_a_draining_gateway_exit(drain_env) -> None:
    """Drain window elapses at t=2 while the gateway is still tearing down; it exits at t=10, well
    inside the teardown grace — the stop must report success instead of escalating to taskkill."""
    gateway_windows, clock, state = drain_env

    original_sleep = clock.sleep

    def sleep_and_maybe_exit(seconds: float) -> None:
        original_sleep(seconds)
        if clock.now >= 10.0:
            state["alive"] = False

    gateway_windows.time.sleep = sleep_and_maybe_exit

    assert gateway_windows._drain_gateway_pid(4242, drain_timeout=2.0, teardown_grace=45.0) is True
    assert clock.now >= 10.0
    assert state["wrote_marker"] == [4242]


def test_no_grace_without_a_stopping_sentinel(drain_env) -> None:
    """A gateway that never stamped ``stopping`` gets no extra window: a wedged process must still be
    bounded, and the call must report the failure so the caller can escalate."""
    gateway_windows, clock, state = drain_env
    state["phase"] = "running"

    assert gateway_windows._drain_gateway_pid(4242, drain_timeout=2.0, teardown_grace=45.0) is False
    assert clock.now < 5.0


def test_teardown_grace_is_bounded(drain_env) -> None:
    """A gateway that accepts the stop and then wedges hard still gets force-stopped: the extra
    window is one bounded span, not an unbounded wait."""
    gateway_windows, clock, _state = drain_env

    assert gateway_windows._drain_gateway_pid(4242, drain_timeout=2.0, teardown_grace=5.0) is False
    assert clock.now <= 2.0 + 5.0 + 1.0


def test_immediate_exit_returns_at_once(drain_env) -> None:
    gateway_windows, clock, state = drain_env
    state["alive"] = False

    assert gateway_windows._drain_gateway_pid(4242, drain_timeout=30.0) is True
    assert clock.now == 0.0


# ---------------------------------------------------------------------------
# The already-gone branches of the force kill, and the stop line that names the
# path that actually ran. Both were invisible to the suite: a regression here
# silently restores "⚠ Permission denied" / "✓ Stopped hermes-gateway service"
# on a box where no service exists.
# ---------------------------------------------------------------------------


class _StatusProbe:
    """Fake ``gateway.status`` pair: ``_pid_exists`` walks a scripted sequence (its last value
    repeats) and ``terminate_pid`` raises the caller's exception."""

    def __init__(self, monkeypatch, exists, raises):
        from gateway import status

        sequence = list(exists)
        self.calls = 0

        def fake_exists(pid: int) -> bool:
            self.calls += 1
            return sequence[min(self.calls - 1, len(sequence) - 1)]

        def fake_terminate(pid, *, force=False, expected_start_time=None):
            raise raises

        monkeypatch.setattr(status, "_pid_exists", fake_exists)
        monkeypatch.setattr(status, "terminate_pid", fake_terminate)


def test_permission_error_on_an_exiting_pid_is_not_a_failure(monkeypatch, capsys) -> None:
    """taskkill/POSIX both report a permission error for a process that is already terminating. The
    gateway this stop asked to drain is often exactly that, so the kill is a no-op — not a warning
    the operator has to explain away."""
    from hermes_cli import gateway_windows

    _StatusProbe(monkeypatch, exists=[True, False], raises=PermissionError("Access denied"))

    assert gateway_windows._force_terminate_known_gateway_pids({4242: 1000.0}) == 0
    assert capsys.readouterr().out == ""


def test_oserror_access_denied_on_an_exiting_pid_is_not_a_failure(monkeypatch, capsys) -> None:
    """The Windows shape of the same thing: ``taskkill`` raises OSError("Access denied") for a
    process already on its way out. Only a SURVIVOR is a failure."""
    from hermes_cli import gateway_windows

    _StatusProbe(monkeypatch, exists=[True, False], raises=OSError("Access denied"))

    assert gateway_windows._force_terminate_known_gateway_pids({4242: 1000.0}) == 0
    # Not just "no 'Could not kill' line": a no-op kill must print NOTHING. The base printed
    # "Failed to kill PID 4242: Access denied", which is the shape that needs to be red.
    assert capsys.readouterr().out == ""


def test_permission_denied_on_a_survivor_still_warns(monkeypatch, capsys) -> None:
    """The other direction: a gateway that is STILL RUNNING after a refused kill is a real failure
    and must stay visible — the already-gone check must not swallow it."""
    from hermes_cli import gateway_windows

    _StatusProbe(monkeypatch, exists=[True], raises=PermissionError("Access denied"))

    assert gateway_windows._force_terminate_known_gateway_pids({4242: 1000.0}) == 0
    out = capsys.readouterr().out
    # Structural, not a wording check: a survivor must be surfaced (pid + reason) exactly once.
    assert "4242" in out and "denied" in out.lower() and out.count("4242") == 1


def test_access_denied_on_a_survivor_still_warns(monkeypatch, capsys) -> None:
    from hermes_cli import gateway_windows

    _StatusProbe(monkeypatch, exists=[True], raises=OSError("Access denied"))

    assert gateway_windows._force_terminate_known_gateway_pids({4242: 1000.0}) == 0
    out = capsys.readouterr().out
    assert "4242" in out and "denied" in out.lower() and out.count("4242") == 1


def _stop_env(monkeypatch, *, service_kind, task_registered):
    """Wire `_cmd_stop` down to the one branch under test: a profile-scoped stop whose gateway PIDs
    were found and whose installed service stop reported success."""
    import types

    from hermes_cli import gateway as gateway_cli
    from hermes_cli import gateway_profile_lifecycle

    monkeypatch.setattr(gateway_cli, "_refuse_from_inside_gateway", lambda *a, **k: None)
    monkeypatch.setattr(gateway_profile_lifecycle, "profile_lifecycle", lambda verb, args: False)
    monkeypatch.setattr(gateway_cli, "find_gateway_pids", lambda *a, **k: [4242])
    monkeypatch.setattr(gateway_cli, "_served_by_another_host_gateway", lambda: None)
    monkeypatch.setattr(gateway_cli, "named_profile_served_by_running_multiplexer", lambda: None)
    monkeypatch.setattr(gateway_cli, "_dispatch_all_via_service_manager_if_s6", lambda verb: False)
    monkeypatch.setattr(gateway_cli, "_dispatch_via_service_manager_if_s6", lambda verb: False)
    monkeypatch.setattr(gateway_cli, "_installed_service_kind", lambda: service_kind)
    monkeypatch.setattr(gateway_cli, "_stop_installed_service", lambda system: True)
    monkeypatch.setattr(gateway_cli, "get_service_name", lambda: "hermes-gateway")
    monkeypatch.setattr(gateway_cli, "stop_profile_gateway", lambda: False)
    monkeypatch.setattr(
        gateway_cli, "_gw_windows",
        lambda: types.SimpleNamespace(is_task_registered=lambda: task_registered),
    )
    return gateway_cli, types.SimpleNamespace(all=False, system=False)


def test_stop_names_the_startup_folder_launcher_when_no_service_exists(monkeypatch, capsys) -> None:
    """A standard-user Windows account has only the Startup-folder VBS, and ``is_installed()`` counts
    it. Claiming "Stopped hermes-gateway service" there is fiction (`sc query` → 1060) and it hid
    which stop path actually ran — the same line, in the same breath, as "✗ No gateway was running"."""
    gateway_cli, args = _stop_env(monkeypatch, service_kind="windows", task_registered=False)

    gateway_cli._cmd_stop(args)

    out = capsys.readouterr().out
    assert "Startup-folder launcher; no service or Scheduled Task installed" in out
    assert "Stopped hermes-gateway service" not in out


def test_stop_still_names_the_service_when_a_task_is_registered(monkeypatch, capsys) -> None:
    """The fix must not over-correct: a real Scheduled Task IS a service, and saying so is right."""
    gateway_cli, args = _stop_env(monkeypatch, service_kind="windows", task_registered=True)

    gateway_cli._cmd_stop(args)

    out = capsys.readouterr().out
    assert "✓ Stopped hermes-gateway service" in out
    assert "Startup-folder launcher" not in out


def test_stop_on_systemd_keeps_the_service_line(monkeypatch, capsys) -> None:
    """The Windows branch must not leak onto hosts that really do have a unit."""
    gateway_cli, args = _stop_env(monkeypatch, service_kind="systemd", task_registered=False)

    gateway_cli._cmd_stop(args)

    out = capsys.readouterr().out
    assert "✓ Stopped hermes-gateway service" in out
    assert "Startup-folder launcher" not in out


def test_a_real_kill_is_still_counted(monkeypatch, capsys) -> None:
    """Counterweight to the already-gone branches above: the early ``continue`` paths must not
    swallow a kill that really happened, nor the start-time identity the kill is checked against."""
    from gateway import status
    from hermes_cli import gateway_windows

    state = {"alive": True, "kills": []}

    def fake_terminate(pid, *, force=False, expected_start_time=None):
        state["kills"].append((pid, force, expected_start_time))
        state["alive"] = False

    monkeypatch.setattr(status, "_pid_exists", lambda pid: state["alive"])
    monkeypatch.setattr(status, "terminate_pid", fake_terminate)

    assert gateway_windows._force_terminate_known_gateway_pids({4242: 100}) == 1
    assert state["kills"] == [(4242, True, 100)]
    assert capsys.readouterr().out == ""
