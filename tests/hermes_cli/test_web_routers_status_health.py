"""``GET /api/health`` ``commit`` contract (review G4).

The public health probe reports the backend's BOOT ``commit`` so an attaching client can refuse a
backend whose boot commit differs from its checkout: a ``serve`` that outlived ``hermes update``
is never re-adopted. The field must always be present
(``null`` off git) and must come from ``get_version_info().commit``.
"""

from __future__ import annotations

import pytest

from hermes_cli.version_info import VersionInfo


def _info(commit: str | None) -> VersionInfo:
    return VersionInfo(base_version="2026.10.1", derived_version="2026.10.1", distance=0,
                       commit=commit, branch="main", source="git")


@pytest.mark.parametrize("commit", ["0fbe50a9d88aa7e1c0b5f3d2e4a6b8c9d0e1f2a3", None])
def test_health_reports_the_boot_commit_from_version_info(monkeypatch, commit):
    from starlette.testclient import TestClient

    from hermes_cli.web_routers import status
    import hermes_cli.web_server as ws

    monkeypatch.setattr(status, "get_version_info", lambda: _info(commit))

    response = TestClient(ws.app).get("/api/health")  # public: no session token

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert "commit" in body, "attaching clients key the boot-identity decision on this field"
    assert body["commit"] == commit


@pytest.mark.parametrize("disk_commit", ["1729888f93cba0a30ccbfc4c67146edeaf4c6071", None])
def test_health_publishes_the_disk_commit_beside_the_boot_commit(monkeypatch, disk_commit):
    """A client decides "stale" by comparing these two strings.

    ``code_skew`` cannot carry that decision on its own: the process that is stale is
    precisely the one that may have booted from code predating the field, so it answers
    200 with no verdict and a client reading "no field" as "clean" calls it healthy. The
    revision pair is computable from any build that has ``commit`` at all.
    """
    from starlette.testclient import TestClient

    from gateway import code_skew
    from hermes_cli.web_routers import status
    import hermes_cli.web_server as ws

    boot = "6e4d10829c8db1b4d7d19889eb724bf4480f20ad"
    monkeypatch.setattr(status, "get_version_info", lambda: _info(boot))
    monkeypatch.setattr(code_skew, "current_code_sha", lambda: disk_commit)

    body = TestClient(ws.app).get("/api/health").json()

    assert body["commit"] == boot, "the existing key keeps its meaning: the BOOT revision"
    assert "disk_commit" in body, "present even when unresolved, so 'unknown' is not 'matches'"
    assert body["disk_commit"] == disk_commit


def test_health_publishes_whether_something_would_respawn_this_process(monkeypatch):
    """The client needs this to tell "wait" from "go restart it on the host"."""
    from starlette.testclient import TestClient

    import hermes_cli.web_server as ws

    monkeypatch.setattr(ws.app.state, "respawn_evidence", "marker:supervised-child", raising=False)

    body = TestClient(ws.app).get("/api/health").json()

    assert body["respawn"] == "marker:supervised-child"


def test_health_says_unknown_when_no_supervision_evidence_exists(monkeypatch):
    from starlette.testclient import TestClient

    import hermes_cli.web_server as ws

    # Startup on a bare `hermes dashboard` sets no evidence; absence must read as unknown,
    # never as "something will restart me".
    monkeypatch.delattr(ws.app.state, "respawn_evidence", raising=False)

    body = TestClient(ws.app).get("/api/health").json()

    assert body["respawn"] == "unknown"


def test_health_keeps_the_boot_commit_when_the_disk_revision_cannot_be_read(monkeypatch):
    """A broken git read must neither drop `commit` nor pretend the disk matches."""
    from starlette.testclient import TestClient

    from gateway import code_skew
    from hermes_cli.web_routers import status
    import hermes_cli.web_server as ws

    def _boom():
        raise OSError("git exploded")

    monkeypatch.setattr(status, "get_version_info", lambda: _info("0fbe50a9d88aa7e1c0b5f3d2e4a6b8c9d0e1f2a3"))
    monkeypatch.setattr(code_skew, "current_code_sha", _boom)

    body = TestClient(ws.app).get("/api/health").json()

    assert body["commit"] == "0fbe50a9d88aa7e1c0b5f3d2e4a6b8c9d0e1f2a3"
    assert body["disk_commit"] is None, "unresolved reads as unknown, never as 'matches the boot revision'"
