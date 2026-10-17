"""A cross-fleet assignee ("em:default") must resolve to the authority profile, not be refused.

R33 stores the install-namespace qualifier in ``tasks.assignee`` on shared boards, so every
site that turns an assignee into a profile home has to strip it first. ``resolve_profile_env``
rejects a colon outright; before this guard the dispatcher stripped it before spawning
(``kanban_db_dispatch._default_spawn``) but the gateway's claim admission did not, so a
namespaced card was claimed, spawned, and then refused as ``invalid_kanban_claim``.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

# The claim-admission module pulls the dispatcher, which reads install metadata from the real
# home at import time; that read is the module's, not this suite's test data.
pytestmark = pytest.mark.allow_real_home_io


def _claimed_card(tmp_path, assignee):
    """A board + running claim for *assignee*, and the owner connection that may admit it."""
    from contextlib import closing
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    from gateway.session_contract import Principal

    workspace = tmp_path / 'workspace'
    workspace.mkdir(exist_ok=True)
    kb.create_board('fleet')
    with closing(connect(board='fleet')) as conn:
        task_id = kb.create_task(conn, title='Namespaced card', body='Goal criteria',
                                 assignee=assignee, workspace_kind='dir',
                                 workspace_path=str(workspace))
        kb.recompute_ready(conn)
        task = kb.claim_task(conn, task_id)
    actor = Principal('owner', str(tmp_path), frozenset({'session:create'}), 'transport')
    connection = SimpleNamespace(authority=SimpleNamespace(profile_id=str(tmp_path)),
                                 actor=actor, native_owner=True)
    params = dict(board='fleet', task_id=task.id, run_id=task.current_run_id,
                  claim_lock=task.claim_lock)
    config = {'model': {'default': 'loop-model', 'provider': 'custom'},
              'platform_toolsets': {'cli': ['terminal', 'file']}}
    return connection, params, config, task


@pytest.fixture
def fleet_home(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    monkeypatch.delenv('HERMES_DELEGATED_CHILD', raising=False)
    # Bare names map into the fixture home; a colon-containing name would raise here, which is
    # exactly the failure this suite pins.
    monkeypatch.setattr('hermes_cli.profiles.resolve_profile_env',
                        lambda name: str(tmp_path) if name == 'default' else str(tmp_path / name))
    return tmp_path


@pytest.mark.parametrize('assignee', ['em:default', 'EM:default'])
def test_qualified_assignee_is_admitted(fleet_home, assignee):
    """The claim is admitted, and the task keeps its qualified assignee in the worker context."""
    from gateway.session_kanban import build_kanban_policy

    connection, params, config, task = _claimed_card(fleet_home, assignee)
    policy, _secrets = build_kanban_policy(connection, params, config)
    context = json.loads(policy.kanban_json)
    # The card keeps its qualified assignee (normalized to lowercase at creation); the policy
    # resolves it to this install's profile.
    assert context['profile'] == assignee.lower()
    assert context['task_id'] == task.id
    assert policy.source == 'kanban'


def test_pr_acceptance_resolves_a_qualified_assignee(fleet_home):
    """The completion-contract gate reads the assignee's own gh login; it must resolve too."""
    from hermes_cli.kanban_pr_acceptance import _assignee_profile_home

    assert _assignee_profile_home('em:default') == str(fleet_home)
    # An unassigned card keeps using the ambient login.
    assert _assignee_profile_home(None) is None
    # The qualifier is stripped exactly as the dispatcher strips it before spawning, so the
    # profile half — not the namespace — decides which login is used.
    assert _assignee_profile_home('yummi:coder') == str(fleet_home / 'coder')
