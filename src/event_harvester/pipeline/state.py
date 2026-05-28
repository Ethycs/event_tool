"""Explicit state machine for the Flet UI's pipeline lifecycle.

Before this module, ``EventsApp._state`` was a string scattered through
the Flet button handlers (``"idle"`` / ``"running"`` / ``"paused"`` /
``"done"``), with informal invariants. This module formalizes the
transitions so illegal moves raise instead of silently corrupting UI
state.

Transitions:

    IDLE    ──start──▶ RUNNING
    RUNNING ──pause──▶ PAUSED ──resume──▶ RUNNING
    RUNNING ──stop───▶ IDLE      PAUSED ──stop──▶ IDLE
    RUNNING ──done───▶ DONE      PAUSED ──done──▶ DONE  (finished while paused)
    DONE    ──start──▶ RUNNING   (re-run from the events view)

``reset()`` is a hard fallback for cancellation / exception handlers
that need to land on IDLE regardless of the current state.
"""

from __future__ import annotations

from enum import Enum


class PipelineState(str, Enum):
    """Lifecycle states for the Flet pipeline runner."""

    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    DONE = "done"


# Source state → set of legal targets. Self-loops are treated as no-ops
# by ``transition()`` so callers don't need to guard before calling.
_TRANSITIONS: dict[PipelineState, frozenset[PipelineState]] = {
    PipelineState.IDLE: frozenset({PipelineState.RUNNING}),
    PipelineState.RUNNING: frozenset({
        PipelineState.PAUSED, PipelineState.IDLE, PipelineState.DONE,
    }),
    # PAUSED → DONE is reachable when the pipeline finishes its last
    # stage between the moment Pause was clicked and the next checkpoint
    # the runner would have honored.
    PipelineState.PAUSED: frozenset({
        PipelineState.RUNNING, PipelineState.IDLE, PipelineState.DONE,
    }),
    PipelineState.DONE: frozenset({PipelineState.RUNNING}),
}


class IllegalTransitionError(RuntimeError):
    """Raised when ``transition()`` is asked to make a move the table forbids."""


class PipelineStateMachine:
    """A small, explicit FSM around ``PipelineState``.

    Wraps the bare enum with transition validation and a few convenience
    checks (``is_active``, ``is_startable``) used by the Flet action bar.
    """

    def __init__(self, initial: PipelineState = PipelineState.IDLE):
        self._state = initial

    # ── State access ────────────────────────────────────────────────

    @property
    def state(self) -> PipelineState:
        return self._state

    @property
    def is_idle(self) -> bool:
        return self._state is PipelineState.IDLE

    @property
    def is_running(self) -> bool:
        return self._state is PipelineState.RUNNING

    @property
    def is_paused(self) -> bool:
        return self._state is PipelineState.PAUSED

    @property
    def is_done(self) -> bool:
        return self._state is PipelineState.DONE

    @property
    def is_active(self) -> bool:
        """RUNNING or PAUSED — i.e. there is a live pipeline task."""
        return self._state in (PipelineState.RUNNING, PipelineState.PAUSED)

    @property
    def is_startable(self) -> bool:
        """IDLE or DONE — the Start button is enabled in these states."""
        return self._state in (PipelineState.IDLE, PipelineState.DONE)

    # ── Transitions ─────────────────────────────────────────────────

    def can_transition(self, target: PipelineState) -> bool:
        """True if ``target`` is reachable from the current state (incl. self-loop)."""
        return target is self._state or target in _TRANSITIONS[self._state]

    def transition(self, target: PipelineState) -> None:
        """Move to ``target``.

        Self-loops are silently no-ops so call sites don't need to guard
        before every assignment. Illegal transitions raise so bugs in the
        button handlers fail loudly instead of corrupting UI state.
        """
        if target is self._state:
            return
        if target not in _TRANSITIONS[self._state]:
            raise IllegalTransitionError(
                f"Cannot transition from {self._state.value} to {target.value}"
            )
        self._state = target

    def reset(self) -> None:
        """Force the state to IDLE without checking transitions.

        For cancellation and exception handlers in the runner that need
        a hard reset from any state.
        """
        self._state = PipelineState.IDLE
