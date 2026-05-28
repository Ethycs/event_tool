"""Tests for PipelineStateMachine.

Exhaustively covers the transition table (legal moves succeed, illegal
ones raise) plus the convenience predicates the Flet action bar reads.
"""

from __future__ import annotations

import pytest

from event_harvester.pipeline.state import (
    IllegalTransitionError,
    PipelineState,
    PipelineStateMachine,
)


# ── Initial state ────────────────────────────────────────────────────


def test_default_initial_state_is_idle():
    sm = PipelineStateMachine()
    assert sm.state is PipelineState.IDLE
    assert sm.is_idle
    assert not sm.is_active


def test_initial_state_override():
    sm = PipelineStateMachine(initial=PipelineState.DONE)
    assert sm.state is PipelineState.DONE
    assert sm.is_done
    assert sm.is_startable


# ── Convenience predicates ───────────────────────────────────────────


@pytest.mark.parametrize("state,active,startable", [
    (PipelineState.IDLE,    False, True),
    (PipelineState.RUNNING, True,  False),
    (PipelineState.PAUSED,  True,  False),
    (PipelineState.DONE,    False, True),
])
def test_is_active_and_is_startable(state, active, startable):
    sm = PipelineStateMachine(initial=state)
    assert sm.is_active is active
    assert sm.is_startable is startable


# ── Legal transitions ───────────────────────────────────────────────


LEGAL_MOVES = [
    (PipelineState.IDLE,    PipelineState.RUNNING),
    (PipelineState.RUNNING, PipelineState.PAUSED),
    (PipelineState.RUNNING, PipelineState.IDLE),
    (PipelineState.RUNNING, PipelineState.DONE),
    (PipelineState.PAUSED,  PipelineState.RUNNING),
    (PipelineState.PAUSED,  PipelineState.IDLE),
    (PipelineState.PAUSED,  PipelineState.DONE),   # finished while paused
    (PipelineState.DONE,    PipelineState.RUNNING),  # re-run
]


@pytest.mark.parametrize("src,dst", LEGAL_MOVES)
def test_legal_transitions(src, dst):
    sm = PipelineStateMachine(initial=src)
    sm.transition(dst)
    assert sm.state is dst


@pytest.mark.parametrize("src,dst", LEGAL_MOVES)
def test_can_transition_reports_legal_moves(src, dst):
    sm = PipelineStateMachine(initial=src)
    assert sm.can_transition(dst) is True


# ── Illegal transitions ─────────────────────────────────────────────


ILLEGAL_MOVES = [
    # IDLE: only → RUNNING is legal
    (PipelineState.IDLE, PipelineState.PAUSED),
    (PipelineState.IDLE, PipelineState.DONE),
    # DONE: only → RUNNING is legal
    (PipelineState.DONE, PipelineState.PAUSED),
    (PipelineState.DONE, PipelineState.IDLE),
]


@pytest.mark.parametrize("src,dst", ILLEGAL_MOVES)
def test_illegal_transitions_raise(src, dst):
    sm = PipelineStateMachine(initial=src)
    with pytest.raises(IllegalTransitionError, match=f"from {src.value} to {dst.value}"):
        sm.transition(dst)
    # State is unchanged on raise
    assert sm.state is src


@pytest.mark.parametrize("src,dst", ILLEGAL_MOVES)
def test_can_transition_reports_illegal_moves(src, dst):
    sm = PipelineStateMachine(initial=src)
    assert sm.can_transition(dst) is False


# ── Self-loops are no-ops ───────────────────────────────────────────


@pytest.mark.parametrize("state", list(PipelineState))
def test_self_loop_is_noop(state):
    sm = PipelineStateMachine(initial=state)
    sm.transition(state)  # must not raise
    assert sm.state is state


@pytest.mark.parametrize("state", list(PipelineState))
def test_can_transition_to_self_is_true(state):
    sm = PipelineStateMachine(initial=state)
    assert sm.can_transition(state) is True


# ── reset() ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("state", list(PipelineState))
def test_reset_forces_idle_from_any_state(state):
    sm = PipelineStateMachine(initial=state)
    sm.reset()
    assert sm.state is PipelineState.IDLE


def test_reset_does_not_raise_on_illegal_transition():
    """reset() must succeed from DONE even though DONE → IDLE isn't a legal
    transition() target. This is exactly why reset() exists — exception /
    cancellation handlers need a hard fallback.
    """
    sm = PipelineStateMachine(initial=PipelineState.DONE)
    sm.reset()  # would raise if it went through transition()
    assert sm.is_idle


# ── End-to-end happy-path cycle ─────────────────────────────────────


def test_full_lifecycle_idle_to_done_to_rerun():
    """idle → running → paused → running → done → running (re-run)."""
    sm = PipelineStateMachine()
    sm.transition(PipelineState.RUNNING)
    assert sm.is_running
    sm.transition(PipelineState.PAUSED)
    assert sm.is_paused
    sm.transition(PipelineState.RUNNING)
    assert sm.is_running
    sm.transition(PipelineState.DONE)
    assert sm.is_done
    sm.transition(PipelineState.RUNNING)  # re-run
    assert sm.is_running


def test_stop_during_running_then_restart():
    sm = PipelineStateMachine()
    sm.transition(PipelineState.RUNNING)
    sm.transition(PipelineState.IDLE)  # stop
    assert sm.is_idle
    sm.transition(PipelineState.RUNNING)  # restart
    assert sm.is_running


def test_pipeline_finishes_while_paused():
    """Edge case: pause clicked, then last stage completes before checkpoint."""
    sm = PipelineStateMachine()
    sm.transition(PipelineState.RUNNING)
    sm.transition(PipelineState.PAUSED)
    sm.transition(PipelineState.DONE)  # the case that motivates PAUSED → DONE
    assert sm.is_done


# ── Enum surface ────────────────────────────────────────────────────


def test_pipeline_state_values_are_stable_strings():
    """The Flet pipeline log + telemetry rely on these exact lowercase strings."""
    assert PipelineState.IDLE.value == "idle"
    assert PipelineState.RUNNING.value == "running"
    assert PipelineState.PAUSED.value == "paused"
    assert PipelineState.DONE.value == "done"
