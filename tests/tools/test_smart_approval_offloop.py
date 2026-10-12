"""The guardian call must survive running on an event-loop thread.

The relay transport refuses a sync LLM call made from a thread with a running loop
(``agent/relay_llm.py::_run_awaitable`` raises "Synchronous Relay LLM execution cannot run on an
event-loop thread"), but it explicitly supports the loop-free path (``asyncio.run(value)``). The
tui_gateway/desktop approval path can execute inside a loop, which is what made every flagged command
degrade to a manual prompt there (observed on our fleet: errors.log 2026-10-12 09:45 / 12:44 / 13:01).

These tests pin both halves: from a loop thread the call must hop off it; from a plain thread it must
stay inline (no gratuitous thread hop on the hot path).
"""

import asyncio
import threading

from tools import approval_smart

OFF_LOOP_THREAD = "smart-approval-offloop"


class _FakeChoice:
    finish_reason = "stop"

    def __init__(self, text):
        self.message = type("M", (), {"content": text})()


class _FakeResponse:
    def __init__(self, text):
        self.choices = [_FakeChoice(text)]


def _relay_like_call_llm(seen):
    """Mimic the real relay refusal: a call on a loop thread raises, loop-free succeeds."""

    def _call(**kwargs):
        seen["thread"] = threading.current_thread().name
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            seen["loop"] = False
            return _FakeResponse("APPROVE")
        seen["loop"] = True
        raise RuntimeError(
            "Synchronous Relay LLM execution cannot run on an event-loop thread"
        )

    return _call


def test_guardian_hops_off_a_running_loop(monkeypatch):
    seen = {}
    monkeypatch.setattr("agent.auxiliary_client.call_llm", _relay_like_call_llm(seen))

    async def _on_a_loop():
        return approval_smart._smart_approve("echo hi", "test")

    verdict = asyncio.run(_on_a_loop())

    assert verdict == "approve", "a loop-thread call must yield a verdict, not escalate"
    assert seen["loop"] is False, "the call must run on a thread with no running loop"
    assert seen["thread"] == OFF_LOOP_THREAD


def test_guardian_stays_inline_off_the_loop(monkeypatch):
    seen = {}
    monkeypatch.setattr("agent.auxiliary_client.call_llm", _relay_like_call_llm(seen))

    caller = threading.current_thread().name
    assert approval_smart._smart_approve("echo hi", "test") == "approve"

    assert seen["loop"] is False
    assert seen["thread"] == caller, "no thread hop when the caller has no loop"


def test_escalate_is_still_not_a_verdict(monkeypatch):
    """A genuine ESCALATE keeps escalating — the hop must not change verdict semantics."""

    def _escalate(**kwargs):
        return _FakeResponse("ESCALATE")

    monkeypatch.setattr("agent.auxiliary_client.call_llm", _escalate)
    assert approval_smart._smart_approve("echo hi", "test") == "escalate"


def test_failures_still_escalate_and_annotate(monkeypatch):
    def _boom(**kwargs):
        raise ValueError("provider exploded")

    monkeypatch.setattr("agent.auxiliary_client.call_llm", _boom)
    assert approval_smart._smart_approve("echo hi", "test") == "escalate"
    assert "provider exploded" in (approval_smart.unavailable_reason() or "")
