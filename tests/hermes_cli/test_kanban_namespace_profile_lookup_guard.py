"""Guard: no profile lookup may be fed a raw (namespace-qualified) Kanban assignee.

R33 stores the install-namespace qualifier in ``tasks.assignee`` ("em:default") on shared
boards, and ``resolve_profile_env`` rejects a colon. Every site that turns an assignee into a
profile home therefore has to strip it with ``assignee_profile`` first — but nothing detected a
NEW such site, so the same class landed twice after upstream merges (2026-10-16 board-metadata
move, 2026-10-17 gateway claim admission). This test is that detector: it fails on any new
``resolve_profile_env(<something with 'assignee' in it>)`` that does not go through
``assignee_profile``, and it fails if a blessed site loses its strip.
"""
import ast
from pathlib import Path

import pytest

# The claim-admission module pulls the dispatcher, which reads install metadata from the real
# home at import time; that read is the module's, not this suite's test data.
pytestmark = pytest.mark.allow_real_home_io

ROOT = Path(__file__).resolve().parents[2]

# Vendored trees, virtualenvs and tests themselves are not shipped code.
_SKIP_PARTS = {'node_modules', 'venv', '.git', 'build', 'dist', '.pytest_cache', '__pycache__',
               'state-snapshots', 'installs', 'tests'}

# Sites where an assignee legitimately becomes a profile home. Each must keep assignee_profile().
_BLESSED = {
    'gateway/session_kanban.py': 'claim admission resolves the owner profile',
    'hermes_cli/kanban_pr_acceptance.py': 'the completion-contract gate resolves the gh login',
    'hermes_cli/kanban_db_dispatch.py': 'the dispatcher resolves the spawn profile',
}


def _lookup_calls(path: Path):
    """(lineno, argument source) for every resolve_profile_env(...) call in *path*."""
    source = path.read_text(encoding='utf-8', errors='replace')
    try:
        tree = ast.parse(source)
    except SyntaxError:  # a fixture or template that is not importable Python
        return []
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, 'id', None) or getattr(node.func, 'attr', None)
        if name != 'resolve_profile_env' or not node.args:
            continue
        found.append((node.lineno, ast.get_source_segment(source, node.args[0]) or ''))
    return found


def test_no_profile_lookup_is_fed_a_raw_assignee():
    offenders, blessed = [], set()
    for path in ROOT.rglob('*.py'):
        if _SKIP_PARTS & set(path.relative_to(ROOT).parts):
            continue
        rel = path.relative_to(ROOT).as_posix()
        for lineno, argument in _lookup_calls(path):
            if 'assignee' not in argument:
                continue
            if 'assignee_profile(' in argument:
                blessed.add(rel)
                continue
            offenders.append(f'{rel}:{lineno}: resolve_profile_env({argument})')
    assert not offenders, (
        'These profile lookups are fed a raw assignee, so any namespace-qualified assignee '
        '("em:default") raises ValueError here. Wrap the argument in assignee_profile(...) from '
        'hermes_cli.kanban_db_dispatch:\n  ' + '\n  '.join(offenders))


def test_blessed_namespace_sites_still_strip_the_qualifier():
    """A merge that drops one of these sites is as bad as adding a new unguarded one."""
    missing = []
    for rel, why in _BLESSED.items():
        path = ROOT / rel
        if not path.exists():
            missing.append(f'{rel} (file is gone; {why})')
            continue
        source = path.read_text(encoding='utf-8', errors='replace')
        if 'assignee_profile(' not in source:
            missing.append(f'{rel} no longer strips the namespace ({why})')
    assert not missing, 'Namespace-qualified assignees are unhandled again:\n  ' + '\n  '.join(missing)


@pytest.mark.parametrize('assignee, expected', [
    ('em:default', 'default'),
    ('EM:Default', 'Default'),
    ('yummi:coder', 'coder'),
    ('default', 'default'),
    (None, ''),
])
def test_assignee_profile_strips_only_the_namespace(assignee, expected):
    """The helper the guard blesses: namespace dropped, profile half untouched."""
    from hermes_cli.kanban_db_dispatch import assignee_profile

    assert assignee_profile(assignee) == expected
