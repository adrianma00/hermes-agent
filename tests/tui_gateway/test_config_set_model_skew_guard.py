"""`config.set model` must notice a backend that is serving stale code.

The Desktop's bottom-bar model (and effort) picker dispatches here. Three other
model-switch entry points already refuse while the process runs code older than
the checkout — the dashboard's picker, the typed `/model` command, and the
`model.options` / `model.save_key` RPCs — but this one had no guard at all, so a
pick resolved a post-update model string against stale cached modules. The user
sees the switch appear to do nothing and then revert.

The shape is a CONFIRM, not a hard refusal: the user may take the restart or
continue anyway. That mirrors the selection guards (`_expensive_model_confirm`)
rather than `_model_skew_err`, because the point is to offer the recovery, not
to block the user from their own backend.

`config.set` is registered through `method_ctx.bind_module`, which REBINDS the
handler's globals onto `server` — so the switch it calls is
`server._apply_model_switch`, and `_set_model` itself must be driven with its
explicit `(rid, params, key, value, session)` signature (the registered handler
looks the session up in `server._sessions`).
"""

import contextlib

import pytest

from tui_gateway import server

SKEW = ("113624af7a", "c46ebe1b9a")
MODEL = "deepseek-v4-flash --provider deepseek"


@pytest.fixture
def skewed(monkeypatch):
    """Report a skewed backend, the way the gateway's real detector would."""
    import gateway.code_skew as code_skew

    monkeypatch.setattr(code_skew, "detect_code_skew", lambda: SKEW)


def _switch(monkeypatch, session=None, **params):
    """Drive the REAL `_set_model` (server-rebound) with an agent stub."""
    called = []

    def fake_apply(sid, sess, value, **kwargs):
        called.append((sid, value, kwargs))

        return {"value": value, "scope": "session", "warning": ""}

    monkeypatch.setattr(server, "_apply_model_switch", fake_apply)
    monkeypatch.setattr(
        "hermes_cli.model_switch.parse_model_switch_args",
        lambda value: type("P", (), {"explicit_provider": "", "is_once": False})(),
    )
    monkeypatch.setattr(server, "_cfgset_await_agent", lambda *a, **k: None)
    monkeypatch.setattr(server, "_session_profile_runtime_scope", lambda _s: contextlib.nullcontext())

    body = {"key": "model", "value": MODEL, **params}
    answer = server._set_model(1, body, "model", MODEL, session)

    return answer, called


def test_a_skewed_switch_asks_before_applying_and_does_not_apply(monkeypatch, skewed):
    answer, called = _switch(monkeypatch, session={"agent": None, "running": False})

    result = answer["result"]

    assert result["confirm_required"] is True
    # The refusal must NAME the skew, or the prompt reads as a generic error.
    assert "113624af7a" in result["confirm_message"]
    assert "c46ebe1b9a" in result["confirm_message"]
    # Nothing was applied: applying is exactly the hazard.
    assert called == []


def test_confirming_applies_the_switch_anyway(monkeypatch, skewed):
    # "Continue anyway" resends with the confirm flag — the same round-trip the
    # expensive-model prompt uses, so there is no new client contract.
    answer, called = _switch(
        monkeypatch,
        session={"agent": None, "running": False},
        confirm_expensive_model=True,
    )

    assert "error" not in answer
    assert len(called) == 1


def test_a_current_backend_is_not_asked_about_skew(monkeypatch):
    import gateway.code_skew as code_skew

    monkeypatch.setattr(code_skew, "detect_code_skew", lambda: None)

    answer, called = _switch(monkeypatch, session={"agent": None, "running": False})

    assert answer["result"].get("confirm_required") is False
    assert len(called) == 1


def test_a_mid_turn_pick_is_guarded_before_it_is_stashed(monkeypatch, skewed):
    # A pick made while a turn streams is stashed for the next turn start. The
    # guard runs BEFORE the stash, so the prompt is still answerable — a stashed
    # pick has no client left to ask.
    session = {"agent": object(), "running": True}
    answer, called = _switch(monkeypatch, session=session)

    assert answer["result"]["confirm_required"] is True
    assert "pending_model_switch" not in session
    assert called == []


def test_a_skewed_switch_with_no_session_still_asks(monkeypatch, skewed):
    # The sessionless branch answers 4001 for a profile default, so the skew
    # prompt must come first or the user sees the wrong reason entirely.
    answer, _called = _switch(monkeypatch, session=None)

    assert answer["result"]["confirm_required"] is True
