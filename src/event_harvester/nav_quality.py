"""Navigation quality scoring for web source runs.

Pure scoring + classification-parsing layer. Phase 3 Agent B:

* :func:`compute_score` — pure formula mapping a VLM classification +
  structural counts to a 0-100 navigation quality score.
* :func:`pick_key_frames` — select the most informative PNG frames from
  a navigation trace JSONL so the VLM only has to look at a few images.
* :func:`parse_classification` — extract a canonical classification
  label from free-form VLM text.
* :class:`SourceQuality` — typed result struct that Agent D will fill
  in after wiring this module to ``vision_completion``.
* :class:`NavQualityScorer` — placeholder; Agent D adds the
  ``score_source`` method that combines a VLM call with this module's
  helpers.

No LLM calls, no ``litellm`` imports. The only I/O is reading the
trace JSONL and checking whether referenced PNG files exist.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


_CLASSIFY_PROMPT = """You are inspecting screenshots of a web-scraping pipeline's navigation.
Classify what the page shows into EXACTLY ONE of these labels:

- content: actual events, posts, listings, or messages are visible
- login_wall: the page is asking for sign-in / authentication
- cloudflare_challenge: a bot-check page (Cloudflare, hCaptcha, etc)
- cookie_banner: a modal/banner is blocking the main content
- error: 404, 500, DNS failure, or connection error message
- empty: page loaded but no content (empty search result, rate-limited feed)
- unknown: none of the above

Respond with the label on the first line, then a one-sentence explanation.
Example:
content
Calendar page showing approximately 30 upcoming event cards.
"""


CLASSIFICATIONS: tuple[str, ...] = (
    "content",
    "login_wall",
    "cloudflare_challenge",
    "cookie_banner",
    "error",
    "empty",
    "unknown",
)


# Per-classification base delta applied on top of the 50 baseline.
_BASE_DELTAS: dict[str, int] = {
    "content": 50,
    "empty": 0,
    "login_wall": -30,
    "cloudflare_challenge": -50,
    "cookie_banner": -10,
    "error": -50,
    "unknown": 0,
}


@dataclass
class SourceQuality:
    """Result of scoring a single web source run.

    Agent D produces this from a VLM call + this module's helpers.
    """

    source: str
    run_id: str
    classification: str
    vlm_explanation: str
    structural: dict
    nav_score: int
    frames_used: list[str] = field(default_factory=list)
    vlm_model: str = ""
    duration_ms: int = 0


def compute_score(classification: str, structural: dict) -> int:
    """Pure scoring formula. Returns int clamped to ``[0, 100]``.

    Base by classification:
      content +50, empty +0, login_wall -30, cloudflare_challenge -50,
      cookie_banner -10, error -50, unknown 0.

    Structural bonus (additive on top of the 50 baseline):
      +1 per event_link, capped at +30;
      +1 per feed_item, capped at +20;
      +20 if search_hits is True.

    Score = 50 + base_delta + structural_bonus, clamped to ``[0, 100]``.
    Missing keys in ``structural`` are treated as 0 / False.
    """
    base_delta = _BASE_DELTAS.get(classification, 0)

    event_links = int(structural.get("event_links", 0) or 0)
    feed_items = int(structural.get("feed_items", 0) or 0)
    search_hits = bool(structural.get("search_hits", False))

    bonus = min(event_links, 30) + min(feed_items, 20)
    if search_hits:
        bonus += 20

    raw = 50 + base_delta + bonus
    return max(0, min(100, raw))


def parse_classification(vlm_response: str) -> tuple[str, str]:
    """Pull a classification label from a free-form VLM response.

    Case-insensitive substring search over :data:`CLASSIFICATIONS` in
    declaration order. For each label, also matches the human variant
    (``"login wall"``) and the first underscore-separated token
    (``"login"``) so the VLM doesn't have to echo the exact identifier.

    Returns ``(label, full_response)``. Falls back to
    ``("unknown", full_response)`` if nothing matches.
    """
    if not vlm_response:
        return ("unknown", vlm_response)

    haystack = vlm_response.lower()

    # ``unknown`` is the fallback — never match it positively, since any
    # non-empty response would otherwise trip on the word "unknown" only
    # by accident. We do still let it match if the VLM literally said
    # "unknown", which is intentional.
    for label in CLASSIFICATIONS:
        candidates = {
            label.lower(),
            label.replace("_", " ").lower(),
            label.split("_")[0].lower(),
        }
        for candidate in candidates:
            if candidate and candidate in haystack:
                return (label, vlm_response)

    return ("unknown", vlm_response)


def _load_trace_records(trace_jsonl_path: Path) -> list[dict]:
    """Read a trace JSONL into a list of dicts. Tolerates malformed lines."""
    try:
        text = trace_jsonl_path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("nav_quality: could not read trace %s: %s", trace_jsonl_path, e)
        return []

    records: list[dict] = []
    for idx, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(
                "nav_quality: skipping malformed trace line %d in %s: %s",
                idx,
                trace_jsonl_path,
                e,
            )
            continue
    return records


def pick_key_frames(trace_jsonl_path: Path, limit: int = 3) -> list[Path]:
    """Pick the most informative PNGs from a navigation trace.

    Selection rules (categories collected then merged in walk order):

    * Always include the most recent ``framenavigated`` record's image.
    * Always include any ``feed-gif`` record's image (the recorded GIF).
    * Include the image of the record immediately preceding any
      ``close`` or ``error`` record.

    After collecting, dedupe (first-seen order preserved), keep only
    paths that actually exist on disk, and trim to ``limit`` entries by
    dropping the oldest first.

    The trace JSONL is expected at
    ``data/.web_traces/<source>/<run-id>.jsonl``; image paths inside
    are stored relative to ``data/.web_traces/`` (e.g.
    ``"luma/<run-id>/3.png"``), so they resolve via
    ``trace_jsonl_path.parent.parent / image``.

    Returns ``[]`` if the trace is missing, unreadable, or yields no
    valid images.
    """
    trace_jsonl_path = Path(trace_jsonl_path)
    if not trace_jsonl_path.exists():
        return []

    records = _load_trace_records(trace_jsonl_path)
    if not records:
        return []

    base_dir = trace_jsonl_path.parent.parent

    def _resolve_image(rec: dict) -> Path | None:
        image = rec.get("image")
        if not image:
            return None
        try:
            return (base_dir / image).resolve()
        except OSError:
            return None

    latest_framenavigated: Path | None = None
    feed_gif_frames: list[Path] = []
    pre_terminal_frames: list[Path] = []

    prev_image: Path | None = None
    for rec in records:
        kind = rec.get("kind")
        img = _resolve_image(rec)

        if kind == "framenavigated" and img is not None:
            latest_framenavigated = img
        elif kind == "feed-gif" and img is not None:
            feed_gif_frames.append(img)
        elif kind in ("close", "error") and prev_image is not None:
            pre_terminal_frames.append(prev_image)

        if img is not None:
            prev_image = img

    # Merge in walk order: feed-gifs and pre-terminal frames appear in
    # the order they were encountered; the latest framenavigated is
    # appended last (it's "the most recent" by definition).
    candidates: list[Path] = []
    for frames in (feed_gif_frames, pre_terminal_frames):
        candidates.extend(frames)
    if latest_framenavigated is not None:
        candidates.append(latest_framenavigated)

    # Dedupe preserving first-seen order, and require the file to exist.
    seen: set[Path] = set()
    deduped: list[Path] = []
    for p in candidates:
        if p in seen:
            continue
        if not p.exists():
            continue
        seen.add(p)
        deduped.append(p)

    if len(deduped) > limit:
        # Drop oldest first.
        deduped = deduped[-limit:]

    return deduped


def _structural_signals_from_records(records: list[dict]) -> dict:
    """Derive ``compute_score``'s structural-signals dict from trace records.

    * ``event_links``: number of ``framenavigated`` records (a rough proxy
      for "did we visit event pages?").
    * ``feed_items``: number of ``xhr`` records labelled ``feed-scroll``.
    * ``search_hits``: True if any ``framenavigated`` URL contains
      ``search`` (case-insensitive).
    """
    event_links = 0
    feed_items = 0
    search_hits = False
    for rec in records:
        kind = rec.get("kind")
        if kind == "framenavigated":
            event_links += 1
            url = (rec.get("url") or "")
            if "search" in url.lower():
                search_hits = True
        elif kind == "xhr" and rec.get("label") == "feed-scroll":
            feed_items += 1
    return {
        "event_links": event_links,
        "feed_items": feed_items,
        "search_hits": search_hits,
    }


def _resolve_vlm_model_name(cfg_llm) -> str:
    """Mirror of ``llm._resolve_vision_model``. Duplicated locally so we can
    record the model string in ``SourceQuality`` without importing the
    private helper. Keep in sync with ``llm._resolve_vision_model``.
    """
    explicit = getattr(cfg_llm, "vision_model", "") or ""
    if explicit:
        return explicit
    api_key = getattr(cfg_llm, "api_key", "") or ""
    if api_key.startswith("sk-ant-"):
        return "claude-sonnet-4-7"
    return "gpt-4o-mini"


class NavQualityScorer:
    """VLM-powered navigation quality scorer.

    Combines :func:`pick_key_frames` (cheap, deterministic frame
    selection) with one ``vision_completion`` call per source/run.
    Results are persisted as ``quality.json`` under the run's trace
    directory and also returned as a :class:`SourceQuality`.

    The class-level ``_images_sent_this_run`` counter is a soft cost
    guard for the whole pipeline run. Call :meth:`reset_counter` at the
    start of each run (or instantiate a fresh scorer process-wide).
    """

    # Class-level cost guard. Reset by reset_counter().
    _images_sent_this_run: int = 0

    @classmethod
    def reset_counter(cls) -> None:
        cls._images_sent_this_run = 0

    def __init__(self) -> None:
        pass

    def score_source(
        self,
        source: str,
        run_id: str,
        cfg,  # AppConfig — needs cfg.llm
        traces_root: Path = Path("data/.web_traces"),
    ) -> SourceQuality | None:
        """Score a single source's most recent (or specified) run.

        Returns ``None`` if no trace JSONL exists for ``source``/``run_id``.
        Otherwise returns a :class:`SourceQuality` and writes
        ``<traces_root>/<source>/<run_id>/quality.json``.

        On LLM errors: returns a ``SourceQuality`` with
        ``classification='unknown'`` and ``vlm_explanation=str(e)``, with
        ``nav_score`` computed from structural signals alone.

        On image-budget exceeded: short-circuits to
        ``classification='unknown'``, ``vlm_explanation='image budget
        exceeded ...'``, without calling the LLM.
        """
        cls = type(self)
        traces_root = Path(traces_root)
        trace_jsonl = traces_root / source / f"{run_id}.jsonl"
        if not trace_jsonl.exists():
            return None

        frames = pick_key_frames(trace_jsonl, limit=3)
        records = _load_trace_records(trace_jsonl)
        structural = _structural_signals_from_records(records)

        # Same logic as llm._resolve_vision_model — kept duplicated so we
        # can fill in ``vlm_model`` without a private import.
        vlm_model = _resolve_vlm_model_name(cfg.llm)

        budget = int(getattr(cfg.llm, "vision_max_images_per_run", 10) or 0)
        sent = cls._images_sent_this_run

        classification: str
        explanation: str
        duration_ms = 0

        if frames and (sent + len(frames) > budget):
            classification = "unknown"
            explanation = (
                f"image budget exceeded for this run (sent {sent}, "
                f"limit {budget})"
            )
            logger.warning(
                "nav_quality: %s/%s — %s; skipping VLM call",
                source,
                run_id,
                explanation,
            )
        elif not frames:
            # Nothing visual to classify; structural-only score with an
            # explicit explanation. No LLM call.
            classification = "unknown"
            explanation = "no key frames available"
        else:
            # Lazy-import keeps ``nav_quality.py`` cheap to import (and
            # makes test-time monkey-patching of vision_completion work
            # via the module-level binding we set up below).
            t0 = time.monotonic()
            try:
                response = vision_completion(
                    _CLASSIFY_PROMPT, frames, cfg.llm, max_tokens=512
                )
            except Exception as e:  # broad on purpose; pipeline must continue
                duration_ms = int((time.monotonic() - t0) * 1000)
                logger.warning(
                    "nav_quality: VLM call failed for %s/%s: %s",
                    source,
                    run_id,
                    e,
                )
                classification = "unknown"
                explanation = str(e)
            else:
                duration_ms = int((time.monotonic() - t0) * 1000)
                # Only successful calls burn budget.
                cls._images_sent_this_run += len(frames)
                classification, explanation = parse_classification(response)

        nav_score = compute_score(classification, structural)

        result = SourceQuality(
            source=source,
            run_id=run_id,
            classification=classification,
            vlm_explanation=explanation,
            structural=structural,
            nav_score=nav_score,
            frames_used=[str(p) for p in frames],
            vlm_model=vlm_model,
            duration_ms=duration_ms,
        )

        run_dir = traces_root / source / run_id
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "quality.json").write_text(
                json.dumps(asdict(result), indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning(
                "nav_quality: could not write quality.json for %s/%s: %s",
                source,
                run_id,
                e,
            )

        return result


# Lazy import target: ``vision_completion`` is rebound at call time so
# tests can patch ``event_harvester.nav_quality.vision_completion``
# without paying the litellm import cost on module load.
def vision_completion(*args, **kwargs):  # pragma: no cover - thin shim
    from event_harvester.llm import vision_completion as _vc
    return _vc(*args, **kwargs)
