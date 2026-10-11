"""A user-pinned reasoning level must survive a route change.

Regression for the fallback (and switch_model) bug where ``_reresolve_fallback_reasoning_config``
re-derived ``agent.reasoning_config`` from config.yaml only, discarding an explicit pin. The setting
then appeared to turn itself back on: a session pinned to "thinking off" came back at the config
default the moment the primary provider rate-limited.

Precedence under test: per-model override > explicit pin > global ``agent.reasoning_effort``.
"""
import types

import pytest

from agent.chat_completion_helpers import _reresolve_fallback_reasoning_config
from hermes_constants import resolve_reasoning_config


CFG = {"agent": {"reasoning_effort": "high"}}


def _agent(model="m-fallback", explicit=None):
    """Minimal agent stub: the function only reads .model and ._reasoning_config_explicit."""
    a = types.SimpleNamespace(model=model, reasoning_config=None)
    if explicit is not None:
        a._reasoning_config_explicit = explicit
    return a


# ── the resolver's new rung ──────────────────────────────────────────────────────

def test_explicit_pin_wins_over_global():
    assert resolve_reasoning_config(CFG, "m", explicit={"enabled": False}) == {"enabled": False}


def test_explicit_pin_is_not_shared_by_reference():
    pin = {"enabled": True, "effort": "low"}
    out = resolve_reasoning_config(CFG, "m", explicit=pin)
    assert out == pin and out is not pin


def test_per_model_override_still_wins_over_explicit_pin():
    """#21256 must keep working: a level declared for THIS model is more specific than a pin."""
    cfg = {"agent": {"reasoning_effort": "high", "reasoning_overrides": {"m-other": "low"}}}
    assert resolve_reasoning_config(cfg, "m-other", explicit={"enabled": False}) == {
        "enabled": True, "effort": "low"}


def test_no_explicit_falls_through_to_global():
    """Unchanged behaviour for every caller that pins nothing."""
    assert resolve_reasoning_config(CFG, "m") == {"enabled": True, "effort": "high"}


# ── fallback activation ──────────────────────────────────────────────────────────

@pytest.fixture
def cfg_high(monkeypatch):
    """Fallback re-resolve reads config.yaml through hermes_cli.config.load_config.

    A fresh deep copy per test: a test that adds a per-model override must not leak it into the
    next one through the shared module-level CFG.
    """
    import copy
    import hermes_cli.config as hcfg
    cfg = copy.deepcopy(CFG)
    monkeypatch.setattr(hcfg, "load_config", lambda *a, **k: cfg)
    return cfg


def test_fallback_preserves_explicit_pin(cfg_high):
    """The reported bug: pinned OFF, config says high — the pin must survive the swap."""
    agent = _agent(explicit={"enabled": False})
    _reresolve_fallback_reasoning_config(agent)
    assert agent.reasoning_config == {"enabled": False}, (
        "an explicit pin was discarded on fallback — the setting turns itself back on")


def test_fallback_preserves_explicit_low_pin(cfg_high):
    agent = _agent(explicit={"enabled": True, "effort": "minimal"})
    _reresolve_fallback_reasoning_config(agent)
    assert agent.reasoning_config == {"enabled": True, "effort": "minimal"}


def test_fallback_still_applies_per_model_override_for_new_model(cfg_high, monkeypatch):
    cfg_high["agent"]["reasoning_overrides"] = {"m-fallback": "low"}
    agent = _agent(explicit={"enabled": False})
    _reresolve_fallback_reasoning_config(agent)
    assert agent.reasoning_config == {"enabled": True, "effort": "low"}


def test_fallback_unchanged_when_nothing_was_pinned(cfg_high):
    """#21256 regression guard: no pin → config.yaml still drives the fallback level."""
    agent = _agent(explicit=None)
    _reresolve_fallback_reasoning_config(agent)
    assert agent.reasoning_config == {"enabled": True, "effort": "high"}


def test_fallback_handles_config_load_failure(monkeypatch):
    """A config load failure must not kill the swap (keep current value)."""
    import hermes_cli.config as hcfg

    def boom(*a, **k):
        raise RuntimeError("unreadable config")

    monkeypatch.setattr(hcfg, "load_config", boom)
    agent = _agent(explicit={"enabled": False})
    agent.reasoning_config = {"enabled": False}
    _reresolve_fallback_reasoning_config(agent)
    assert agent.reasoning_config == {"enabled": False}
