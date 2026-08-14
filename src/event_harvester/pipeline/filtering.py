"""The free half of extraction: classifier → caps → reranker, and its arithmetic.

Split out of ``analysis.extract_events_llm`` on 2026-08-14. Two reasons, and
the second is the one that matters.

**It is free, and it was locked behind the stage that costs money.** These three
stages need no LLM. On the harvest of 2026-08-13 they answered 96% of the
question on their own — 19,646 messages in, 775 out — and "does this page look
like it has events?" is exactly what they compute. Reaching them required
calling the extractor, so the source-onboarding funnel in
``10 - Blueprints/01 - Adding a Web Source.md`` could not use them.

**The funnel did not add up, and nothing checked.** Same run: 775 messages
reached the model, 244 events came back, and ``llm_no_events`` reported 0 — the
difference attributed to nothing. That is the second incident of the family in
three days; the first filed 814 unreachable messages as "the model read these
and found nothing", reported ``ok`` with 0 events, and overwrote a cache holding
251. Axiom 3 says every drop is attributable, and a count that reads as a
measurement while being structurally fixed is worse than a missing count,
because a missing count prompts a question.

So every stage here reports ``in``, ``out`` and a **reason for every dropped
item**, and :meth:`FilterResult.check` asserts they balance. Design and the
reasoning behind the shape: ``01 - Design/04 - Decomposing the Pipeline.md``.

Two rules from that design are load-bearing here:

* **No stage self-reports its drop count.** Every number below is derived at the
  boundary from what went in and what came out. A counter a stage has to
  remember to increment is a counter that reads zero on the path nobody
  exercised — which is how the pattern this borrows from ended up reporting
  ``skip_count: 0`` on every run it had ever done.
* **The accounting unit is the message**, the same unit as the pipeline
  element. The bug in the extract stage is that its bucket is written per
  *batch*, and a batch of 5 yielding one event cannot represent its other four.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger("event_harvester.pipeline.filtering")

# The six reject buckets every caller expects, in pipeline order. `llm_errors`
# was added 2026-08-12: it is not a rejection but an outage, and reading it as
# "no events" destroyed a harvest.
REJECT_BUCKETS = ("classifier", "caps", "reranker", "llm_past", "llm_no_events",
                  "llm_errors")

#: Which buckets this module is responsible for. The rest belong to the extract
#: stage and are merged in by `analysis.extract_events_llm`.
FILTER_BUCKETS = ("classifier", "caps", "reranker")

#: Why a message was dropped, and — the distinction that cost 251 events —
#: whether anything actually looked at it.
#:
#: ``policy``    the stage examined the message and decided against it.
#: ``unreached`` the stage never got to look. An outage, not a finding.
#:
#: Every bucket this module produces is ``policy`` by construction: a classifier
#: that cannot load its model does not error, it passes everything through
#: (`classifier.predict`). ``unreached`` exists here so the vocabulary is
#: complete for the extract stage, which is where it is actually needed.
DROP_KINDS = {
    "classifier": "policy",
    "caps": "policy",
    "reranker": "policy",
    "llm_past": "policy",
    "llm_no_events": "policy",
    "llm_errors": "unreached",
}


def empty_rejects() -> dict[str, list[dict]]:
    """A full reject dict with every bucket present and empty."""
    return {bucket: [] for bucket in REJECT_BUCKETS}


class FunnelImbalance(AssertionError):
    """A stage's inputs do not equal its outputs plus its attributed drops."""


def _strict_default() -> bool:
    """Raise on an imbalance under test; record it in production.

    Raising mid-harvest would turn an accounting bug into a lost run, which is
    the failure mode this machinery exists to prevent — a 24-minute harvest
    killed by a counter is not an improvement on a miscounted one. Under
    pytest the opposite is true: a silent imbalance is exactly what a test
    suite is for. `EH_STRICT_FUNNEL` forces either.
    """
    raw = os.getenv("EH_STRICT_FUNNEL")
    if raw is not None and raw.strip():
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return "PYTEST_CURRENT_TEST" in os.environ


@dataclass(frozen=True)
class StageCount:
    """What one stage received, emitted, and can account for.

    ``dropped`` maps a reason to a count. The invariant is
    ``n_in == n_out + sum(dropped.values())`` and it is checked, not assumed.
    """

    name: str
    n_in: int
    n_out: int
    dropped: dict[str, int] = field(default_factory=dict)

    @property
    def unattributed(self) -> int:
        """Items that went in, did not come out, and have no reason. Zero."""
        return self.n_in - self.n_out - sum(self.dropped.values())

    def check(self, *, strict: bool) -> str | None:
        """Return an error string when this stage does not balance."""
        if self.unattributed == 0:
            return None
        msg = (
            f"funnel imbalance in stage {self.name!r}: {self.n_in} in, "
            f"{self.n_out} out, {sum(self.dropped.values())} attributed "
            f"({self.dropped}) — {self.unattributed} message(s) unaccounted for"
        )
        if strict:
            raise FunnelImbalance(msg)
        logger.error("%s", msg)
        return msg


@dataclass
class FilterResult:
    """What survived the free stages, why the rest did not, and the arithmetic."""

    messages: list[dict]
    rejects: dict[str, list[dict]]
    stages: tuple[StageCount, ...]
    n_in: int

    @property
    def n_out(self) -> int:
        return len(self.messages)

    @property
    def dropped(self) -> dict[str, int]:
        return {b: len(self.rejects[b]) for b in FILTER_BUCKETS}

    def check(self, *, strict: bool | None = None) -> list[str]:
        """Verify every stage, then the section as a whole.

        The per-stage checks are near-tautological today, because each reject
        list is computed as a set difference and therefore balances by
        construction. They are here so that a later stage which builds its
        rejects some other way cannot quietly stop balancing — which is
        precisely what happened one layer down, in the extract stage.

        The section total is the one with teeth: it is the check that would
        have caught a bucket being written per batch instead of per message.
        """
        if strict is None:
            strict = _strict_default()
        problems = [p for s in self.stages if (p := s.check(strict=strict))]

        attributed = sum(self.dropped.values())
        if self.n_in != self.n_out + attributed:
            msg = (
                f"funnel imbalance across filtering: {self.n_in} in, "
                f"{self.n_out} out, {attributed} attributed "
                f"({self.dropped}) — "
                f"{self.n_in - self.n_out - attributed} message(s) unaccounted for"
            )
            if strict:
                raise FunnelImbalance(msg)
            logger.error("%s", msg)
            problems.append(msg)
        return problems


def filter_for_extraction(
    messages: list[dict],
    caps=None,
    *,
    label: str = "LLM",
    check: bool = True,
) -> FilterResult:
    """Run classifier → caps → reranker. No LLM, no network, no cost.

    Order is load-bearing and unchanged from when this lived inside
    ``extract_events_llm``: caps run **before** the reranker so each source
    gets its quota of candidates before global ranking trims to ``top_k``.
    Reranking first would let a noisy source monopolise the top-k and starve
    every other source regardless of its per-source cap.

    ``label`` names the downstream consumer in the funnel log line; it exists
    so that log stays byte-identical to the one this replaced. ``check=False``
    skips the arithmetic, for callers deliberately running a partial funnel.
    """
    from event_harvester.pipeline.analysis import (
        prioritize,
        prioritize_with_caps,
        score_message,
    )
    from event_harvester.pipeline.classifier import filter_actionable as classifier_filter
    from event_harvester.pipeline.classifier import has_trained_models

    total = len(messages)
    if caps is None or getattr(caps, "no_cap", False) or caps.total < 0:
        # Three ways a caller says "don't cap this run", all equivalent here:
        #   caps=None      -- the orchestrator's unlimited_caps path, which is
        #                     what CLI `--only` takes. This used to fall through
        #                     to a literal 150, so `--only` silently trimmed to
        #                     the default total after advertising no caps.
        #   caps.no_cap    -- the UI checkbox / MCP no_cap param / profile flag.
        #   caps.total < 0 -- the -1 "unlimited" sentinel (e.g. CAP_TOTAL=-1).
        # Use the message count itself as the rerank top_k so nothing is dropped.
        cap_total = max(total, 1)
    else:
        cap_total = caps.total

    rejects = empty_rejects()
    stages: list[StageCount] = []

    if has_trained_models():
        after_clf = classifier_filter(messages)
        n_after_clf = len(after_clf)
        clf_ids = {id(m) for m in after_clf}
        rejects["classifier"] = [m for m in messages if id(m) not in clf_ids]
        stages.append(StageCount(
            "classifier", total, n_after_clf,
            {"classifier": len(rejects["classifier"])},
        ))

        if caps:
            after_caps = prioritize_with_caps(after_clf, caps)
        else:
            after_caps = sorted(after_clf, key=score_message, reverse=True)[:cap_total]

        caps_ids = {id(m) for m in after_caps}
        rejects["caps"] = [m for m in after_clf if id(m) not in caps_ids]
        stages.append(StageCount(
            "caps", n_after_clf, len(after_caps), {"caps": len(rejects["caps"])},
        ))

        try:
            from event_harvester.pipeline.reranker import rerank_messages
            filtered = rerank_messages(after_caps, top_k=cap_total)
        except ImportError:
            logger.warning("sentence-transformers not installed, falling back to regex priority.")
            filtered = sorted(after_caps, key=score_message, reverse=True)

        filtered_ids = {id(m) for m in filtered}
        rejects["reranker"] = [m for m in after_caps if id(m) not in filtered_ids]
        stages.append(StageCount(
            "reranker", len(after_caps), len(filtered),
            {"reranker": len(rejects["reranker"])},
        ))

        logger.info(
            "Event extraction: %d -> %d (classifier) -> %d (caps) -> %d (rerank/score) -> LLM (%s)",
            total, n_after_clf, len(after_caps), len(filtered), label,
        )
    else:
        # No classifier — score, then per-source cap (or global cap).
        #
        # `classifier` and `reranker` stay empty here, and that is a true zero
        # with a false reading: no classifier ran, so it dropped nothing, but
        # the number is identical to one that examined every message and kept
        # them all. See §7 of the design doc — this is the same shape as
        # `caps: 0` on the service path, and the reason a stage should
        # eventually declare whether it was applicable at all.
        if caps:
            filtered = prioritize_with_caps(messages, caps)
        else:
            filtered = prioritize(messages, max_messages=cap_total)

        filtered_ids = {id(m) for m in filtered}
        rejects["caps"] = [m for m in messages if id(m) not in filtered_ids]
        stages.append(StageCount(
            "caps", total, len(filtered), {"caps": len(rejects["caps"])},
        ))

        logger.info(
            "Event extraction: %d -> %d messages (priority) -> LLM (%s)",
            total, len(filtered), label,
        )

    result = FilterResult(
        messages=filtered, rejects=rejects, stages=tuple(stages), n_in=total,
    )
    if check:
        result.check()
    return result
