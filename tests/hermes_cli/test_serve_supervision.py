"""Serve supervision: when is it safe for a backend to exit and expect a respawn?

The code-skew watchdog retires a backend whose checkout moved. Doing that on a guess
turns a stale dashboard into a dead one, so the gate needs evidence, not hope. These
pin the two sources of it: a supervisor's own environment marker, and proof accumulated
when a service comes back on the same port shortly after a graceful exit.
"""

from __future__ import annotations

import json

import pytest

from hermes_cli import serve_supervision


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    # HERMES_HOME drives get_hermes_home() on the path this module resolves when a test
    # does not pass `home` explicitly.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in ("INVOCATION_ID", "SYSTEMD_EXEC_PID", "SUPERVISOR_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    yield tmp_path


class TestSupervisionMarker:
    def test_none_without_a_supervisor(self):
        assert serve_supervision.supervision_marker() is None

    def test_systemd_needs_both_stamps(self, monkeypatch):
        # INVOCATION_ID alone is also set for transient units (`systemd-run`), which do
        # not restart anything; the pair is the service-shape signal.
        monkeypatch.setenv("INVOCATION_ID", "abc")
        assert serve_supervision.supervision_marker() is None

        monkeypatch.setenv("SYSTEMD_EXEC_PID", "4242")
        assert serve_supervision.supervision_marker() == "systemd"

    def test_supervisord(self, monkeypatch):
        monkeypatch.setenv("SUPERVISOR_ENABLED", "1")
        assert serve_supervision.supervision_marker() == "supervisord"


class TestRespawnProof:
    def test_a_first_boot_proves_nothing(self, tmp_path):
        serve_supervision.note_boot(port=9120, pid=100, home=tmp_path, now=1_000.0)

        assert serve_supervision.respawn_expected(port=9120, home=tmp_path) == (False, "unknown")

    def test_a_fast_return_on_the_same_port_is_proof(self, tmp_path):
        # The supervisor restart shape: graceful exit, new process on the same port
        # seconds later. This is what a `systemctl restart` or an update looks like.
        serve_supervision.note_boot(port=9120, pid=100, home=tmp_path, now=1_000.0)
        serve_supervision.note_exit(port=9120, pid=100, home=tmp_path, now=1_100.0)
        serve_supervision.note_boot(port=9120, pid=200, home=tmp_path, now=1_105.0)

        expected, evidence = serve_supervision.respawn_expected(port=9120, home=tmp_path)

        assert expected is True
        assert evidence == "proven:5.0s"

    def test_a_late_return_is_not_proof(self, tmp_path):
        # Hours later is a box that was off, or a human starting it by hand — neither
        # promises to restart the process the next time it exits.
        serve_supervision.note_boot(port=9120, pid=100, home=tmp_path, now=1_000.0)
        serve_supervision.note_exit(port=9120, pid=100, home=tmp_path, now=1_000.0)
        serve_supervision.note_boot(port=9120, pid=200, home=tmp_path, now=1_000.0 + 3_600)

        assert serve_supervision.respawn_expected(port=9120, home=tmp_path) == (False, "unknown")

    def test_a_return_on_a_different_port_is_not_proof(self, tmp_path):
        # Port identifies the service; a different one is a different service, and
        # nothing says the old port would be restored.
        serve_supervision.note_boot(port=9120, pid=100, home=tmp_path, now=1_000.0)
        serve_supervision.note_exit(port=9120, pid=100, home=tmp_path, now=1_100.0)
        serve_supervision.note_boot(port=9200, pid=200, home=tmp_path, now=1_105.0)

        assert serve_supervision.respawn_expected(port=9120, home=tmp_path) == (False, "unknown")
        assert serve_supervision.respawn_expected(port=9200, home=tmp_path) == (False, "unknown")

    def test_a_crash_leaves_no_proof(self, tmp_path):
        # No exit stamp means no tight gap to measure; the boot record alone is not
        # evidence (the process may have been down for hours).
        serve_supervision.note_boot(port=9120, pid=100, home=tmp_path, now=1_000.0)
        serve_supervision.note_boot(port=9120, pid=200, home=tmp_path, now=1_005.0)

        assert serve_supervision.respawn_expected(port=9120, home=tmp_path) == (False, "unknown")

    def test_proof_survives_later_boots(self, tmp_path):
        serve_supervision.note_boot(port=9120, pid=100, home=tmp_path, now=1_000.0)
        serve_supervision.note_exit(port=9120, pid=100, home=tmp_path, now=1_100.0)
        serve_supervision.note_boot(port=9120, pid=200, home=tmp_path, now=1_105.0)
        serve_supervision.note_boot(port=9120, pid=300, home=tmp_path, now=9_000.0)

        assert serve_supervision.respawn_expected(port=9120, home=tmp_path)[0] is True

    def test_an_ephemeral_bind_records_nothing(self, tmp_path):
        assert serve_supervision.note_boot(port=0, pid=100, home=tmp_path, now=1_000.0) == {}
        assert serve_supervision.respawn_expected(port=0, home=tmp_path) == (False, "unknown")

    def test_a_corrupt_record_is_not_evidence(self, tmp_path):
        path = serve_supervision._state_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")

        assert serve_supervision.respawn_expected(port=9120, home=tmp_path) == (False, "unknown")
        # And a boot on top of it still works rather than raising.
        serve_supervision.note_boot(port=9120, pid=100, home=tmp_path, now=1_000.0)
        assert serve_supervision.respawn_expected(port=9120, home=tmp_path) == (False, "unknown")


class TestExitStamp:
    def test_a_foreign_pid_does_not_stamp_this_record(self, tmp_path):
        serve_supervision.note_boot(port=9120, pid=100, home=tmp_path, now=1_000.0)
        serve_supervision.note_exit(port=9120, pid=999, home=tmp_path, now=1_100.0)

        record = json.loads(serve_supervision._state_path(tmp_path).read_text(encoding="utf-8"))
        assert record["exited_at"] is None

    def test_marker_wins_without_any_proof(self, tmp_path, monkeypatch):
        monkeypatch.setenv("INVOCATION_ID", "abc")
        monkeypatch.setenv("SYSTEMD_EXEC_PID", "1")

        assert serve_supervision.respawn_expected(port=9120, home=tmp_path) == (True, "marker:systemd")
