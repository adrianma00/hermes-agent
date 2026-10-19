"""`hermes pm lock` with no arguments relocks uv.lock through PM and nothing else."""
from __future__ import annotations

from argparse import Namespace

import pm
from pm import cli
from pm.package import InstallError

PYPROJECT = """\
[project]
name = "demo"
version = "0"

[project.optional-dependencies]
alpha = ["six"]
beta = ["idna"]
all = ["demo[alpha]"]
"""
LOCK = """\
version = 1

[[package]]
name = "demo"
version = "0"
source = { editable = "." }

[package.metadata]
provides-extras = ["alpha", "all"]
"""


def test_relock_writes_nothing_when_current_and_names_new_opt_in_extras(tmp_path, monkeypatch, capsys):
    (tmp_path / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    (tmp_path / "uv.lock").write_text(LOCK, encoding="utf-8")
    monkeypatch.setattr(cli, "repo_root", lambda: tmp_path)
    stale = []
    relocks = []

    def check(source, **kw):
        assert (source, kw.get("explicit")) == (tmp_path, True)
        # A stale lock is this command's expected case: no red failure report.
        assert kw.get("quiet") is True
        if stale:
            raise InstallError("venv", "The lockfile at `uv.lock` needs to be updated")

    monkeypatch.setattr(pm, "check_project_lock", check)
    monkeypatch.setattr(pm, "lock_project", lambda source, **kw: relocks.append((source, kw)))
    monkeypatch.setattr(pm, "sync_venv", lambda *a, **kw: relocks.append("sync"))

    before = (tmp_path / "uv.lock").read_bytes()
    assert cli.cmd_lock(Namespace(name=None, version=None)) == 0
    assert relocks == []
    assert (tmp_path / "uv.lock").read_bytes() == before
    assert "already current" in capsys.readouterr().out

    stale.append(True)
    assert cli.cmd_lock(Namespace(name=None, version=None)) == 0
    # Relocking never syncs an environment; activation owns that.
    assert relocks == [(tmp_path, {"explicit": True})]
    out = capsys.readouterr().out
    assert "out of date" in out and "✗" not in out
    assert "activate" in out
    # beta is new and outside [all]; alpha was already locked and is in [all].
    assert "all,beta" in out and "alpha" not in out


def _project(path, lock_text=LOCK):
    """A minimal project tree: pyproject.toml + uv.lock."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    (path / "uv.lock").write_text(lock_text, encoding="utf-8")
    return path


def _stale(monkeypatch):
    """Record every tree that is checked or relocked; every lock is stale."""
    inspected, relocked = [], []

    def check(source, **kw):
        inspected.append(source)
        raise InstallError("venv", "The lockfile at `uv.lock` needs to be updated")

    monkeypatch.setattr(pm, "check_project_lock", check)
    monkeypatch.setattr(pm, "lock_project", lambda source, **kw: relocked.append(source))
    monkeypatch.setattr(pm, "sync_venv", lambda *a, **kw: relocked.append("sync"))
    return inspected, relocked


def test_lock_source_picks_the_named_tree_not_the_loaded_one(tmp_path, monkeypatch, capsys):
    """The subject is WHICH TREE, and --source decides it.

    `repo_root()` is the tree the running code was loaded from, so an install's
    `hermes pm lock` inspects the install even when the caller stands in a dev
    worktree. That is the defect: the checked tree must be the named one.
    """
    loaded = _project(tmp_path / "loaded")
    worktree = _project(tmp_path / "worktree")
    monkeypatch.setattr(cli, "repo_root", lambda: loaded)
    inspected, relocked = _stale(monkeypatch)

    assert cli.cmd_lock(Namespace(name=None, version=None, source=str(worktree))) == 0
    assert inspected == [worktree]
    assert relocked == [worktree]
    assert loaded not in inspected


def test_lock_cli_accepts_a_source_so_a_worktree_can_be_the_subject(tmp_path, monkeypatch, capsys):
    """The subject must be nameable from the command line, not just in theory.

    `pm lock --help` offered only --bump, so a caller in a worktree had no way
    to name their tree and no way to learn which tree had been named for them.
    """
    import pm.runtime

    loaded = _project(tmp_path / "loaded")
    worktree = _project(tmp_path / "worktree")
    monkeypatch.setattr(cli, "repo_root", lambda: loaded)
    monkeypatch.setattr(pm.runtime, "is_runtime", lambda: True)  # run in-process, not via the runtime
    inspected, _ = _stale(monkeypatch)

    assert cli.main(["lock", "--source", str(worktree)]) == 0
    assert inspected == [worktree]


def test_lock_verdict_names_the_tree_it_inspected(tmp_path, monkeypatch, capsys):
    """The verdict line carries the subject, because that is the line quoted.

    A verdict about a tree the caller did not name reads exactly like a verdict
    about the tree they are standing in — which is how the trap fired.
    """
    loaded = _project(tmp_path / "loaded")
    monkeypatch.setattr(cli, "repo_root", lambda: loaded)
    monkeypatch.setattr(pm, "check_project_lock", lambda source, **kw: None)
    monkeypatch.setattr(pm, "lock_project", lambda source, **kw: None)

    assert cli.cmd_lock(Namespace(name=None, version=None)) == 0
    out = capsys.readouterr().out
    assert "already current" in out
    assert str(loaded) in out  # the relationship: verdict <-> inspected tree


def test_lock_says_so_when_the_caller_stands_in_another_tree(tmp_path, monkeypatch, capsys):
    """Loaded tree consistent, caller's cwd a different checkout: name both.

    This is the reported trap in its exact shape: the relock verdict was true,
    but about a tree the caller never named, and nothing said so.
    """
    loaded = _project(tmp_path / "loaded")
    cwd_tree = _project(tmp_path / "worktree")
    monkeypatch.setattr(cli, "repo_root", lambda: loaded)
    monkeypatch.setattr(pm, "check_project_lock", lambda source, **kw: None)
    monkeypatch.setattr(pm, "lock_project", lambda source, **kw: None)
    monkeypatch.chdir(cwd_tree)

    assert cli.cmd_lock(Namespace(name=None, version=None)) == 0
    out = capsys.readouterr().out
    assert str(loaded) in out  # the tree actually inspected
    assert str(cwd_tree) in out  # and the tree the caller is standing in


def test_lock_source_rejects_a_tree_that_is_not_a_project(tmp_path, monkeypatch, capsys):
    """A named subject that holds no project is refused, not silently swapped."""
    loaded = _project(tmp_path / "loaded")
    monkeypatch.setattr(cli, "repo_root", lambda: loaded)
    inspected, relocked = _stale(monkeypatch)
    empty = tmp_path / "not-a-project"
    empty.mkdir()

    assert cli.cmd_lock(Namespace(name=None, version=None, source=str(empty))) == 1
    assert inspected == [] and relocked == []
    assert str(empty) in capsys.readouterr().out
