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
