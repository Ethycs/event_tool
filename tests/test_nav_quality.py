"""Tests for ``event_harvester.nav_quality``."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

import pytest

from event_harvester.nav_quality import (
    CLASSIFICATIONS,
    NavQualityScorer,
    SourceQuality,
    compute_score,
    parse_classification,
    pick_key_frames,
)


# ---------------------------------------------------------------------------
# compute_score
# ---------------------------------------------------------------------------


def test_compute_score_all_classifications_zero_structural() -> None:
    empty = {"event_links": 0, "feed_items": 0, "search_hits": False}
    expected = {
        "content": 100,           # 50 + 50
        "empty": 50,              # 50 + 0
        "login_wall": 20,         # 50 - 30
        "cloudflare_challenge": 0,  # 50 - 50
        "cookie_banner": 40,      # 50 - 10
        "error": 0,               # 50 - 50
        "unknown": 50,            # 50 + 0
    }
    for label, score in expected.items():
        assert compute_score(label, dict(empty)) == score, label


def test_compute_score_event_links_capped() -> None:
    s = compute_score("empty", {"event_links": 100, "feed_items": 0, "search_hits": False})
    # 50 + 0 + min(100, 30) = 80
    assert s == 80


def test_compute_score_feed_items_capped() -> None:
    s = compute_score("empty", {"event_links": 0, "feed_items": 100, "search_hits": False})
    # 50 + 0 + min(100, 20) = 70
    assert s == 70


def test_compute_score_search_hits_bonus() -> None:
    s = compute_score("empty", {"event_links": 0, "feed_items": 0, "search_hits": True})
    assert s == 70  # 50 + 0 + 20


def test_compute_score_clamps_upper() -> None:
    s = compute_score(
        "content",
        {"event_links": 999, "feed_items": 999, "search_hits": True},
    )
    assert s == 100


def test_compute_score_clamps_lower() -> None:
    s = compute_score(
        "cloudflare_challenge",
        {"event_links": 0, "feed_items": 0, "search_hits": False},
    )
    assert s == 0


def test_compute_score_missing_keys_default_to_zero() -> None:
    assert compute_score("content", {}) == 100  # 50 + 50 + 0
    assert compute_score("empty", {}) == 50
    assert compute_score("unknown", {"event_links": 5}) == 55


def test_compute_score_docstring_examples() -> None:
    assert (
        compute_score("content", {"event_links": 27, "feed_items": 0, "search_hits": False})
        == 100
    )
    assert (
        compute_score(
            "cloudflare_challenge",
            {"event_links": 0, "feed_items": 0, "search_hits": False},
        )
        == 0
    )
    assert (
        compute_score("empty", {"event_links": 0, "feed_items": 0, "search_hits": True})
        == 70
    )
    assert (
        compute_score("login_wall", {"event_links": 0, "feed_items": 0, "search_hits": False})
        == 20
    )
    assert (
        compute_score("unknown", {"event_links": 10, "feed_items": 5, "search_hits": False})
        == 65
    )


# ---------------------------------------------------------------------------
# parse_classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ("This is clearly a login wall.", "login_wall"),
        ("Looks like content to me.", "content"),
        ("I see a Cloudflare challenge page.", "cloudflare_challenge"),
        ("It's a cookie banner blocking everything.", "cookie_banner"),
        ("Generic response with no label.", "unknown"),
        ("Classification: cloudflare_challenge", "cloudflare_challenge"),
        ("CONTENT", "content"),
        ("The page errored out badly.", "error"),
        ("Empty results page.", "empty"),
    ],
)
def test_parse_classification_labels(response: str, expected: str) -> None:
    label, full = parse_classification(response)
    assert label == expected
    assert full == response


def test_parse_classification_first_match_wins() -> None:
    label, _ = parse_classification("content, but also a cookie banner")
    assert label == "content"


def test_parse_classification_empty_response() -> None:
    label, full = parse_classification("")
    assert label == "unknown"
    assert full == ""


# ---------------------------------------------------------------------------
# pick_key_frames
# ---------------------------------------------------------------------------


def _make_trace(
    tmp_path: Path,
    source: str,
    run_id: str,
    records: list[dict],
    create_images_for_steps: list[int] | None = None,
) -> Path:
    """Build a trace dir mirroring ``data/.web_traces/<source>/<run-id>.jsonl``.

    PNG files are created (touched) for every step in
    ``create_images_for_steps`` so the existence check inside
    :func:`pick_key_frames` succeeds.
    """
    base = tmp_path / ".web_traces"
    source_dir = base / source
    source_dir.mkdir(parents=True, exist_ok=True)
    trace_path = source_dir / f"{run_id}.jsonl"

    image_dir = base / source / run_id
    image_dir.mkdir(parents=True, exist_ok=True)

    if create_images_for_steps is not None:
        for step in create_images_for_steps:
            (image_dir / f"{step}.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    with trace_path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    return trace_path


def test_pick_key_frames_missing_file_returns_empty(tmp_path: Path) -> None:
    assert pick_key_frames(tmp_path / "does_not_exist.jsonl") == []


def test_pick_key_frames_empty_jsonl(tmp_path: Path) -> None:
    trace = _make_trace(tmp_path, "luma", "r1", records=[])
    assert pick_key_frames(trace) == []


def test_pick_key_frames_picks_latest_framenavigated(tmp_path: Path) -> None:
    records = [
        {"step": 1, "kind": "open", "url": "https://lu.ma"},
        {"step": 2, "kind": "framenavigated", "url": "https://lu.ma/a", "image": "luma/r2/2.png"},
        {"step": 3, "kind": "framenavigated", "url": "https://lu.ma/b", "image": "luma/r2/3.png"},
        {"step": 4, "kind": "framenavigated", "url": "https://lu.ma/c", "image": "luma/r2/4.png"},
    ]
    trace = _make_trace(tmp_path, "luma", "r2", records, create_images_for_steps=[2, 3, 4])
    frames = pick_key_frames(trace)
    assert len(frames) == 1
    assert frames[0].name == "4.png"


def test_pick_key_frames_includes_feed_gif(tmp_path: Path) -> None:
    records = [
        {"step": 1, "kind": "framenavigated", "url": "x", "image": "luma/r3/1.png"},
        {"step": 2, "kind": "feed-gif", "url": None, "image": "luma/r3/feed.png"},
    ]
    trace = _make_trace(tmp_path, "luma", "r3", records, create_images_for_steps=[1])
    # Create the feed image manually under a fancy name.
    (tmp_path / ".web_traces" / "luma" / "r3" / "feed.png").write_bytes(b"\x89PNG")
    frames = pick_key_frames(trace)
    names = [p.name for p in frames]
    assert "feed.png" in names
    assert "1.png" in names


def test_pick_key_frames_pre_error_frame(tmp_path: Path) -> None:
    records = [
        {"step": 1, "kind": "open", "url": "x"},
        {"step": 2, "kind": "framenavigated", "url": "y", "image": "luma/r4/2.png"},
        {"step": 3, "kind": "error", "url": None},
    ]
    trace = _make_trace(tmp_path, "luma", "r4", records, create_images_for_steps=[2])
    frames = pick_key_frames(trace)
    assert len(frames) == 1
    assert frames[0].name == "2.png"


def test_pick_key_frames_pre_close_frame(tmp_path: Path) -> None:
    records = [
        {"step": 1, "kind": "framenavigated", "url": "x", "image": "luma/r4c/1.png"},
        {"step": 2, "kind": "close", "url": None},
    ]
    trace = _make_trace(tmp_path, "luma", "r4c", records, create_images_for_steps=[1])
    frames = pick_key_frames(trace)
    assert len(frames) == 1
    assert frames[0].name == "1.png"


def test_pick_key_frames_limit_drops_oldest(tmp_path: Path) -> None:
    # Many feed-gif records plus a final framenavigated: should keep the
    # last `limit` entries when over cap.
    records = [
        {"step": 1, "kind": "feed-gif", "image": "luma/r5/g1.png"},
        {"step": 2, "kind": "feed-gif", "image": "luma/r5/g2.png"},
        {"step": 3, "kind": "feed-gif", "image": "luma/r5/g3.png"},
        {"step": 4, "kind": "feed-gif", "image": "luma/r5/g4.png"},
        {"step": 5, "kind": "framenavigated", "url": "x", "image": "luma/r5/5.png"},
    ]
    img_dir = tmp_path / ".web_traces" / "luma" / "r5"
    img_dir.mkdir(parents=True, exist_ok=True)
    for name in ("g1.png", "g2.png", "g3.png", "g4.png", "5.png"):
        (img_dir / name).write_bytes(b"\x89PNG")
    trace_path = tmp_path / ".web_traces" / "luma" / "r5.jsonl"
    with trace_path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")

    frames = pick_key_frames(trace_path, limit=3)
    names = [p.name for p in frames]
    assert len(names) == 3
    # The framenavigated frame must be there (always-include rule).
    assert "5.png" in names
    # The oldest feed-gifs should have been dropped.
    assert "g1.png" not in names


def test_pick_key_frames_malformed_lines_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    img_dir = tmp_path / ".web_traces" / "luma" / "r6"
    img_dir.mkdir(parents=True, exist_ok=True)
    (img_dir / "1.png").write_bytes(b"\x89PNG")
    trace_path = tmp_path / ".web_traces" / "luma" / "r6.jsonl"
    trace_path.write_text(
        json.dumps({"step": 1, "kind": "framenavigated", "image": "luma/r6/1.png"})
        + "\n"
        + "{not valid json\n"
        + json.dumps({"step": 2, "kind": "close"})
        + "\n",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING, logger="event_harvester.nav_quality"):
        frames = pick_key_frames(trace_path)

    assert len(frames) == 1
    assert frames[0].name == "1.png"
    assert any("malformed" in rec.message for rec in caplog.records)


def test_pick_key_frames_filters_missing_images(tmp_path: Path) -> None:
    records = [
        {"step": 1, "kind": "framenavigated", "image": "luma/r7/1.png"},
        {"step": 2, "kind": "framenavigated", "image": "luma/r7/missing.png"},
    ]
    trace = _make_trace(tmp_path, "luma", "r7", records, create_images_for_steps=[1])
    frames = pick_key_frames(trace)
    # The latest framenavigated points at a missing file → drops out;
    # 1.png was overwritten as the "latest" by step 2, so the result is [].
    assert frames == []


def test_pick_key_frames_skips_records_without_image(tmp_path: Path) -> None:
    records = [
        {"step": 1, "kind": "open", "url": "x"},
        {"step": 2, "kind": "framenavigated", "url": "y"},  # no image field
    ]
    trace = _make_trace(tmp_path, "luma", "r8", records)
    assert pick_key_frames(trace) == []


# ---------------------------------------------------------------------------
# Misc surface
# ---------------------------------------------------------------------------


def test_classifications_tuple_order() -> None:
    # parse_classification's priority depends on this order.
    assert CLASSIFICATIONS == (
        "content",
        "login_wall",
        "cloudflare_challenge",
        "cookie_banner",
        "error",
        "empty",
        "unknown",
    )


def test_source_quality_dataclass() -> None:
    sq = SourceQuality(
        source="luma",
        run_id="abc",
        classification="content",
        vlm_explanation="ok",
        structural={"event_links": 5, "feed_items": 0, "search_hits": False},
        nav_score=80,
    )
    assert sq.frames_used == []
    assert sq.vlm_model == ""
    assert sq.duration_ms == 0


def test_nav_quality_scorer_instantiates() -> None:
    # Placeholder for Agent D — just must construct cleanly.
    scorer = NavQualityScorer()
    assert scorer is not None


# ---------------------------------------------------------------------------
# NavQualityScorer.score_source  (Agent D)
# ---------------------------------------------------------------------------


@dataclass
class _FakeLLMCfg:
    model: str = "gpt-4o-mini"
    api_key: str = "sk-test"
    backend: str = "litellm"
    model_path: str = ""
    tokenizer_path: str = ""
    device: str = "cpu"
    vision_model: str = "test-vlm"
    vision_max_images_per_run: int = 10


@dataclass
class _FakeAppCfg:
    llm: _FakeLLMCfg = field(default_factory=_FakeLLMCfg)


def _setup_run(
    tmp_path: Path,
    source: str = "luma",
    run_id: str = "run1",
    *,
    with_frames: bool = True,
    extra_records: list[dict] | None = None,
) -> Path:
    """Build a tmp traces_root + JSONL with one framenavigated frame.

    Returns the ``traces_root`` path (so the caller can pass it into
    ``score_source(traces_root=...)``).
    """
    traces_root = tmp_path / ".web_traces"
    source_dir = traces_root / source
    source_dir.mkdir(parents=True, exist_ok=True)
    img_dir = source_dir / run_id
    img_dir.mkdir(parents=True, exist_ok=True)

    records: list[dict] = []
    if with_frames:
        (img_dir / "1.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        records.append(
            {
                "step": 1,
                "kind": "framenavigated",
                "url": "https://lu.ma/discover",
                "image": f"{source}/{run_id}/1.png",
            }
        )
    if extra_records:
        records.extend(extra_records)

    trace_path = source_dir / f"{run_id}.jsonl"
    with trace_path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    return traces_root


@pytest.fixture(autouse=True)
def _reset_scorer_counter():
    """Wipe the class-level cost guard between tests."""
    NavQualityScorer.reset_counter()
    yield
    NavQualityScorer.reset_counter()


def test_score_source_returns_none_when_no_trace(tmp_path: Path) -> None:
    scorer = NavQualityScorer()
    traces_root = tmp_path / ".web_traces"
    traces_root.mkdir()
    result = scorer.score_source(
        "luma", "missing", _FakeAppCfg(), traces_root=traces_root
    )
    assert result is None


def test_score_source_writes_quality_json(tmp_path: Path) -> None:
    traces_root = _setup_run(tmp_path, "luma", "rA")

    with patch(
        "event_harvester.nav_quality.vision_completion",
        return_value="content\nGreat events visible.",
    ) as mock_vc:
        scorer = NavQualityScorer()
        result = scorer.score_source(
            "luma", "rA", _FakeAppCfg(), traces_root=traces_root
        )

    assert mock_vc.called
    assert result is not None
    assert result.classification == "content"
    assert result.source == "luma"
    assert result.run_id == "rA"
    assert result.vlm_model == "test-vlm"
    assert result.structural["event_links"] == 1
    assert result.nav_score > 0

    quality_path = traces_root / "luma" / "rA" / "quality.json"
    assert quality_path.exists()
    loaded = json.loads(quality_path.read_text(encoding="utf-8"))
    assert loaded["classification"] == "content"
    assert loaded["source"] == "luma"
    assert loaded["run_id"] == "rA"
    assert "frames_used" in loaded


def test_score_source_handles_llm_error(tmp_path: Path) -> None:
    traces_root = _setup_run(tmp_path, "luma", "rErr")

    with patch(
        "event_harvester.nav_quality.vision_completion",
        side_effect=RuntimeError("API down"),
    ):
        scorer = NavQualityScorer()
        result = scorer.score_source(
            "luma", "rErr", _FakeAppCfg(), traces_root=traces_root
        )

    assert result is not None
    assert result.classification == "unknown"
    assert "API down" in result.vlm_explanation
    # Structural-only score → 50 (unknown baseline) + 1 event_link = 51.
    assert result.nav_score == 51

    quality_path = traces_root / "luma" / "rErr" / "quality.json"
    assert quality_path.exists()


def test_score_source_respects_image_budget(tmp_path: Path) -> None:
    # Budget = 1; first call sends one frame and exhausts the budget,
    # second call short-circuits.
    traces_root = _setup_run(tmp_path, "luma", "rB1")
    _setup_run(tmp_path, "luma", "rB2")

    cfg = _FakeAppCfg(llm=_FakeLLMCfg(vision_max_images_per_run=1))

    with patch(
        "event_harvester.nav_quality.vision_completion",
        return_value="content\nok",
    ) as mock_vc:
        scorer = NavQualityScorer()
        first = scorer.score_source("luma", "rB1", cfg, traces_root=traces_root)
        second = scorer.score_source("luma", "rB2", cfg, traces_root=traces_root)

    assert first is not None and first.classification == "content"
    assert second is not None
    assert second.classification == "unknown"
    assert "budget" in second.vlm_explanation
    assert mock_vc.call_count == 1


def test_score_source_includes_duration_ms(tmp_path: Path) -> None:
    traces_root = _setup_run(tmp_path, "luma", "rDur")

    with patch(
        "event_harvester.nav_quality.vision_completion",
        return_value="content\nfine",
    ):
        scorer = NavQualityScorer()
        result = scorer.score_source(
            "luma", "rDur", _FakeAppCfg(), traces_root=traces_root
        )

    assert result is not None
    assert result.duration_ms >= 0


@pytest.mark.parametrize("label", list(CLASSIFICATIONS))
def test_score_source_for_each_classification(tmp_path: Path, label: str) -> None:
    traces_root = _setup_run(tmp_path, "luma", f"rC_{label}")

    with patch(
        "event_harvester.nav_quality.vision_completion",
        return_value=f"{label}\nExplanation.",
    ):
        scorer = NavQualityScorer()
        result = scorer.score_source(
            "luma", f"rC_{label}", _FakeAppCfg(), traces_root=traces_root
        )

    assert result is not None
    assert result.classification == label


def test_reset_counter_clears_budget(tmp_path: Path) -> None:
    traces_root = _setup_run(tmp_path, "luma", "rR1")
    _setup_run(tmp_path, "luma", "rR2")

    cfg = _FakeAppCfg(llm=_FakeLLMCfg(vision_max_images_per_run=1))

    with patch(
        "event_harvester.nav_quality.vision_completion",
        return_value="content\nok",
    ) as mock_vc:
        scorer = NavQualityScorer()
        scorer.score_source("luma", "rR1", cfg, traces_root=traces_root)
        # Budget is now exhausted — without reset, this would short-circuit.
        NavQualityScorer.reset_counter()
        result = scorer.score_source("luma", "rR2", cfg, traces_root=traces_root)

    assert result is not None
    assert result.classification == "content"
    assert mock_vc.call_count == 2
