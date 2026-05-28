"""Pipeline package: harvest → filter → extract → match → state lifecycle.

Re-exports the public surface so callers keep short imports:

    from event_harvester.pipeline import Pipeline, PipelineResult
    from event_harvester.pipeline import PipelineState, PipelineStateMachine

Submodules (``harvest``, ``analysis``, ``classifier``, ``weights`` etc.)
are also importable directly, e.g. ``from event_harvester.pipeline.harvest
import harvest_messages``, when callers need a specific helper.
"""

from event_harvester.pipeline.orchestrator import Pipeline, PipelineResult
from event_harvester.pipeline.state import (
    IllegalTransitionError,
    PipelineState,
    PipelineStateMachine,
)

__all__ = [
    "IllegalTransitionError",
    "Pipeline",
    "PipelineResult",
    "PipelineState",
    "PipelineStateMachine",
]
