"""A deliberate gate hold is not a stuck dispatcher.

Regression guard for the health line the embedded gateway dispatcher emits:

    kanban dispatcher stuck: ready queue non-empty for N consecutive ticks but 0 workers
    spawned.

`ready_nonempty()` asks "is there spawnable ready work?", which is TRUE while a card is held by
a deliberate gate — the provider cost window (`DispatchResult.cost_deferred`) or the shared-quota
pause (`DispatchResult.quota_paused`). With no spawn that read as a stuck queue and fired on every
tick through every peak window, with an EMPTY reason line, because `describe_suppression` knew
neither field. Observed live 2026-09-30: 56 consecutive ticks on a card the cost gate was correctly
deferring to 18:00 SGT.

The properties that must hold:
  1. a gate hold is named by `deliberate_gate_holds` (so the dispatcher can stop counting it), and
  2. every OTHER suppression reason is NOT — a genuine stall must still raise the alarm. That
     second property is the point of the test: a suppressor that swallows too much is worse than
     the noise it removes.
"""

from hermes_cli.kanban_db_dispatch import (
    DispatchResult,
    deliberate_gate_holds,
    describe_suppression,
)


def _cost_held() -> DispatchResult:
    res = DispatchResult()
    res.cost_deferred = ["t_cost"]
    return res


def _quota_held() -> DispatchResult:
    res = DispatchResult()
    res.quota_paused = ["t_quota_a", "t_quota_b"]
    return res


def _guard_held() -> DispatchResult:
    res = DispatchResult()
    res.respawn_guarded = [("t_guard", "active_pr")]
    return res


def test_cost_and_quota_holds_are_named():
    assert deliberate_gate_holds([_cost_held()]) == "cost_deferred=1"
    assert deliberate_gate_holds([_quota_held()]) == "quota_paused=2"
    assert deliberate_gate_holds([_cost_held(), _quota_held()]) == "cost_deferred=1, quota_paused=2"


def test_non_gate_suppression_is_not_a_gate_hold():
    """The negative control: a respawn guard must keep counting as a bad tick."""
    assert deliberate_gate_holds([_guard_held()]) == ""


def test_empty_and_none_inputs():
    assert deliberate_gate_holds([]) == ""
    assert deliberate_gate_holds([None]) == ""


def test_describe_suppression_names_the_gates_too():
    """So a warning that DOES fire can never be reason-less about a gate."""
    described = describe_suppression([_cost_held(), _guard_held()])
    assert "cost_deferred=1" in described
    assert "active_pr=1" in described
