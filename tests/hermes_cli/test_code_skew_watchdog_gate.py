"""The code-skew watchdog gate: arm only when a respawn is expected.

Historically the gate listed two cases by hand (Desktop-owned, SSH-isolated). A
supervised serve was a third case it never covered, so a systemd dashboard served stale
code for two days with the updater unaware the unit existed. These pin the precedence
and, more importantly, that "unknown" is never permission — a manual dashboard in a
terminal must not exit into nothing.
"""

from __future__ import annotations

import types

import pytest

from hermes_cli import serve_supervision, web_server


def _app(*, ssh_isolated: bool) -> types.SimpleNamespace:
    state = types.SimpleNamespace()
    if ssh_isolated:
        state.ssh_isolated_clients = set()
    return types.SimpleNamespace(state=state)


@pytest.fixture(autouse=True)
def _no_marker(monkeypatch):
    for name in ("INVOCATION_ID", "SYSTEMD_EXEC_PID", "SUPERVISOR_ENABLED"):
        monkeypatch.delenv(name, raising=False)


def test_a_desktop_owned_backend_is_armed_without_any_supervision_evidence(monkeypatch):
    monkeypatch.setattr(web_server, "is_desktop_owned_backend", lambda: True)
    monkeypatch.setattr(
        serve_supervision, "respawn_expected", lambda *, port: pytest.fail("ownership decides first")
    )

    assert web_server._code_skew_watchdog_decision(_app(ssh_isolated=False), 9120) == (
        True,
        "client-owned:desktop",
    )


def test_an_ssh_isolated_backend_is_armed_for_its_owning_client(monkeypatch):
    monkeypatch.setattr(web_server, "is_desktop_owned_backend", lambda: False)

    assert web_server._code_skew_watchdog_decision(_app(ssh_isolated=True), 9120) == (
        True,
        "client-owned:ssh-isolated",
    )


def test_a_supervised_serve_is_armed_on_a_supervisor_marker(monkeypatch):
    # The case that used to fall through to "no watchdog".
    monkeypatch.setattr(web_server, "is_desktop_owned_backend", lambda: False)
    monkeypatch.setenv("INVOCATION_ID", "abc")
    monkeypatch.setenv("SYSTEMD_EXEC_PID", "1")

    assert web_server._code_skew_watchdog_decision(_app(ssh_isolated=False), 9120) == (
        True,
        "marker:systemd",
    )


def test_a_bare_dashboard_is_not_armed_and_says_so(monkeypatch):
    # A manual `hermes dashboard` in a terminal: nothing promises to start it again, so
    # retiring it would trade a stale dashboard for a dead one.
    monkeypatch.setattr(web_server, "is_desktop_owned_backend", lambda: False)

    assert web_server._code_skew_watchdog_decision(_app(ssh_isolated=False), 9120) == (
        False,
        "unknown",
    )
