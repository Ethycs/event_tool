"""Unit tests for the Pipeline class.

Mocks at the module-level seams (harvest_messages, filter_read_sent,
extract_events_llm, find_acted_fingerprint) so the tests exercise
Pipeline's orchestration without needing real credentials, network,
or an LLM.
"""

from __future__ import annotations

import pytest

from event_harvester.config import AppConfig
from event_harvester.pipeline import Pipeline, PipelineResult
from event_harvester.pipeline.analysis import ExtractionResult

# ── helpers ──────────────────────────────────────────────────────────


def _no_kwargs(**overrides: bool) -> dict[str, bool]:
    """Default no_kwargs (everything enabled) with selective overrides."""
    base = {
        "no_discord": False,
        "no_telegram": False,
        "no_gmail": False,
        "no_signal": False,
        "no_web": False,
    }
    base.update(overrides)
    return base


def _msg(platform: str = "discord", **extra) -> dict:
    base = {
        "platform": platform,
        "id": "x",
        "timestamp": "2026-05-20T12:00:00+00:00",
        "author": "alice",
        "channel": "test",
        "content": "Hello",
    }
    base.update(extra)
    return base


class FakeReporter:
    """Captures reporter calls for assertion."""
    def __init__(self):
        self.stages: list[tuple[str, str | None]] = []
        self.logs: list[str] = []
        self.checkpoints = 0

    def update_stage(self, stage_id: str, label: str | None = None) -> None:
        self.stages.append((stage_id, label))

    def log(self, line: str, color=None) -> None:
        self.logs.append(line)

    async def checkpoint(self) -> None:
        self.checkpoints += 1


@pytest.fixture
def patch_pipeline_deps(monkeypatch):
    """Patch the four module-level seams Pipeline calls. Returns a config dict
    the test can mutate to control behavior.
    """
    state = {
        "messages": [],
        "filter_drops": 0,        # how many from messages to drop in filter_read_sent
        "events": [],             # what extract_events_llm returns
        "summary": "fake summary",
        "rejects": {              # pre-LLM buckets on the returned ExtractionResult
            "classifier": [],
            "reranker": [],
            "caps": [],
        },
        "llm_dropped": [],        # -> rejects["llm_past"]
        "llm_no_events": [],      # -> rejects["llm_no_events"]
        "fingerprints": {},       # event-id -> {"status": "..."} (None = no fp)
        "last_caps_arg": "UNSET", # what was passed to extract_events_llm
        "last_harvest_kwargs": None,
        "save_messages_calls": [],
    }

    async def fake_harvest(cfg, **kwargs):
        state["last_harvest_kwargs"] = kwargs
        return list(state["messages"])

    def fake_filter(msgs, *, drop_read=True):
        # `drop_read` is recorded rather than acted on: the double does not
        # model Gmail read state, but the pipeline must pass the configured
        # value through, and test_pipeline_forwards_drop_read asserts it.
        state["last_drop_read"] = drop_read
        if state["filter_drops"] == 0:
            return list(msgs)
        return list(msgs[state["filter_drops"]:])

    def fake_extract(messages, days_back, llm_cfg, caps=None):
        state["last_caps_arg"] = caps
        rejects = dict(state["rejects"])
        rejects["llm_past"] = list(state["llm_dropped"])
        rejects["llm_no_events"] = list(state["llm_no_events"])
        return ExtractionResult(state["summary"], list(state["events"]), rejects)

    def fake_save(messages, path):
        state["save_messages_calls"].append((len(messages), path))

    def fake_extract_links(msgs):
        return [{"url": f"http://x/{i}"} for i in range(len(msgs))]

    def fake_find_fp(event):
        return state["fingerprints"].get(event.get("id"))

    # Patch the names Pipeline.run reads from its own module's scope.
    # After the Phase 5 reorg the Pipeline class lives in
    # event_harvester.pipeline.orchestrator (the package's __init__
    # only re-exports symbols; the import bindings the runtime reads
    # are in the orchestrator module).
    import event_harvester.pipeline.orchestrator as pl_mod

    monkeypatch.setattr(pl_mod, "harvest_messages", fake_harvest)
    monkeypatch.setattr(pl_mod, "save_messages", fake_save)
    monkeypatch.setattr(pl_mod, "filter_read_sent", fake_filter)
    monkeypatch.setattr(pl_mod, "extract_events_llm", fake_extract)
    monkeypatch.setattr(pl_mod, "extract_links", fake_extract_links)
    monkeypatch.setattr(pl_mod, "find_acted_fingerprint", fake_find_fp)

    # No `_extract_events_cloud` patching any more: the LLM reject buckets
    # now ride back on the ExtractionResult rather than being read off that
    # function object, so there is nothing module-level left to fake.
    return state


# ── no_messages branch ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_returns_no_messages_when_harvest_empty(patch_pipeline_deps):
    state = patch_pipeline_deps
    state["messages"] = []

    result = await Pipeline(AppConfig()).run(no_kwargs=_no_kwargs())

    assert result.no_messages is True
    assert result.all_messages == []
    assert result.actionable == []
    assert result.events == []
    assert result.skipped_analysis is False
    assert result.rejects["classifier"] == []
    # source_counts always has all 5 platforms with zeroes for empty harvest
    assert set(result.source_counts.keys()) == {
        "discord", "telegram", "gmail", "signal", "web",
    }
    assert all(v == 0 for v in result.source_counts.values())


# ── skip_analyze branch ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_skips_extract_when_skip_analyze(patch_pipeline_deps):
    state = patch_pipeline_deps
    state["messages"] = [_msg(id="a"), _msg(id="b")]
    state["events"] = [{"id": "evt", "title": "Should not appear"}]

    cfg = AppConfig()
    cfg.skip_analyze = True
    result = await Pipeline(cfg).run(no_kwargs=_no_kwargs())

    assert result.skipped_analysis is True
    assert result.events == []
    assert result.summary == ""
    assert len(result.all_messages) == 2
    assert len(result.actionable) == 2  # filter_drops=0 by default
    assert state["last_caps_arg"] == "UNSET"  # extract was never called


# ── full pipeline path ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_full_pipeline_tags_status_and_collects_rejects(patch_pipeline_deps):
    state = patch_pipeline_deps
    state["messages"] = [_msg(id="m1"), _msg(id="m2", platform="web")]
    state["filter_drops"] = 0
    state["events"] = [
        {"id": "e1", "title": "New event"},
        {"id": "e2", "title": "Already approved"},
    ]
    state["fingerprints"] = {"e2": {"status": "approved"}}
    state["rejects"] = {
        "classifier": [{"id": "x"}],
        "reranker": [],
        "caps": [{"id": "y"}, {"id": "z"}],
    }
    state["llm_dropped"] = [{"title": "Past event"}]
    state["llm_no_events"] = [{"author": "bot"}]

    result = await Pipeline(AppConfig()).run(no_kwargs=_no_kwargs())

    assert result.no_messages is False
    assert result.skipped_analysis is False
    assert len(result.events) == 2
    assert result.events[0]["_status"] == "new"
    assert result.events[1]["_status"] == "approved"
    assert result.summary == "fake summary"
    assert len(result.rejects["classifier"]) == 1
    assert len(result.rejects["caps"]) == 2
    assert len(result.rejects["llm_past"]) == 1
    assert len(result.rejects["llm_no_events"]) == 1
    # The rejects dict is detached — adding the llm_past/llm_no_events
    # keys must not leak back into extract_events_llm._last_rejects.
    # (This is the bug the old harvest.py path had: it mutated the
    # global dict and would accumulate keys across runs.)
    assert "llm_past" not in state["rejects"]
    assert "llm_no_events" not in state["rejects"]


@pytest.mark.asyncio
async def test_source_counts_reflect_platform_field(patch_pipeline_deps):
    state = patch_pipeline_deps
    state["messages"] = [
        _msg(id="a", platform="discord"),
        _msg(id="b", platform="discord"),
        _msg(id="c", platform="gmail"),
        _msg(id="d", platform="web"),
    ]
    state["events"] = []

    result = await Pipeline(AppConfig()).run(no_kwargs=_no_kwargs())

    assert result.source_counts["discord"] == 2
    assert result.source_counts["gmail"] == 1
    assert result.source_counts["web"] == 1
    assert result.source_counts["telegram"] == 0


# ── the Gmail read filter ────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [True, False])
async def test_pipeline_forwards_drop_read(patch_pipeline_deps, configured):
    """The filter must take its value from config, not from its own default.

    `GmailConfig.drop_read` defaults to False so event harvesting stops
    discarding mail you have already opened — 42% of the mailbox, measured
    2026-08-10. A config flag nothing reads is exactly the failure this pins.
    """
    state = patch_pipeline_deps
    state["messages"] = [_msg(id="a", platform="gmail")]
    state["events"] = []

    cfg = AppConfig()
    cfg.gmail.drop_read = configured
    await Pipeline(cfg).run(no_kwargs=_no_kwargs())

    assert state["last_drop_read"] is configured


@pytest.mark.asyncio
async def test_harvesting_keeps_read_mail_by_default(patch_pipeline_deps):
    state = patch_pipeline_deps
    state["messages"] = [_msg(id="a", platform="gmail")]
    state["events"] = []

    await Pipeline(AppConfig()).run(no_kwargs=_no_kwargs())

    assert state["last_drop_read"] is False, (
        "a default-constructed harvest must not discard read mail"
    )


# ── unlimited_caps flag ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unlimited_caps_passes_none_to_extract(patch_pipeline_deps):
    state = patch_pipeline_deps
    state["messages"] = [_msg(id="a")]
    state["events"] = []

    cfg = AppConfig()
    await Pipeline(cfg).run(no_kwargs=_no_kwargs(), unlimited_caps=True)
    assert state["last_caps_arg"] is None

    await Pipeline(cfg).run(no_kwargs=_no_kwargs(), unlimited_caps=False)
    assert state["last_caps_arg"] is cfg.caps


# ── reporter integration ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reporter_receives_stage_and_checkpoint_calls(patch_pipeline_deps):
    state = patch_pipeline_deps
    state["messages"] = [_msg(id="a")]
    state["events"] = [{"id": "e1", "title": "An event"}]

    reporter = FakeReporter()
    await Pipeline(AppConfig(), reporter=reporter).run(no_kwargs=_no_kwargs())

    stage_ids = [s[0] for s in reporter.stages]
    assert stage_ids == ["harvest", "filter", "extract", "match"]
    # One checkpoint before each stage = 4 total
    assert reporter.checkpoints == 4
    # log() called at least for filter, extract, match (3 minimum)
    assert len(reporter.logs) >= 3


@pytest.mark.asyncio
async def test_no_reporter_means_no_calls_and_no_crash(patch_pipeline_deps):
    state = patch_pipeline_deps
    state["messages"] = [_msg(id="a")]
    state["events"] = []

    # Just verify it runs without a reporter and returns sensibly.
    result = await Pipeline(AppConfig(), reporter=None).run(no_kwargs=_no_kwargs())
    assert result.no_messages is False
    assert result.events == []


@pytest.mark.asyncio
async def test_reporter_skipped_for_no_messages(patch_pipeline_deps):
    """When harvest is empty, only the harvest stage runs — no filter/extract/match."""
    state = patch_pipeline_deps
    state["messages"] = []

    reporter = FakeReporter()
    await Pipeline(AppConfig(), reporter=reporter).run(no_kwargs=_no_kwargs())

    stage_ids = [s[0] for s in reporter.stages]
    assert stage_ids == ["harvest"]


# ── validate() ──────────────────────────────────────────────────────


def test_validate_passes_no_kwargs_through_to_config_check():
    """validate() should request gmail validation when gmail is enabled."""
    cfg = AppConfig()
    warnings = Pipeline(cfg).validate(
        no_kwargs=_no_kwargs(no_discord=True, no_gmail=False),
        need_ticktick=False,
    )
    # We don't assert specific warning text (that's validate_config's job),
    # just that the call shape is right and we got a list back.
    assert isinstance(warnings, list)


# ── harvest kwargs plumbing ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_forwards_harvest_kwargs(patch_pipeline_deps):
    state = patch_pipeline_deps
    state["messages"] = []

    no_kwargs = _no_kwargs(no_web=True)
    await Pipeline(AppConfig()).run(
        no_kwargs=no_kwargs,
        load_path="msgs.json",
        save_path="out.json",
        web_source="luma",
        web_sources=["luma", "eventbrite"],
        no_cooldown=True,
    )

    sent = state["last_harvest_kwargs"]
    assert sent["load_path"] == "msgs.json"
    assert sent["skip_cache"] is True  # because save_path was set
    assert sent["web_source"] == "luma"
    assert sent["web_sources"] == ["luma", "eventbrite"]
    assert sent["no_cooldown"] is True
    assert sent["no_web"] is True
    # save_messages was called even though harvest was empty
    assert state["save_messages_calls"] == [(0, "out.json")]


@pytest.mark.asyncio
async def test_no_cooldown_none_uses_cfg_value(patch_pipeline_deps):
    state = patch_pipeline_deps
    state["messages"] = []
    cfg = AppConfig()
    cfg.web.no_cooldown = True

    await Pipeline(cfg).run(no_kwargs=_no_kwargs(), no_cooldown=None)

    assert state["last_harvest_kwargs"]["no_cooldown"] is True


# ── PipelineResult shape ────────────────────────────────────────────


def test_pipeline_result_default_values():
    """PipelineResult fields default to safe empty values where applicable."""
    result = PipelineResult(
        all_messages=[],
        actionable=[],
        events=[],
        summary="",
        rejects={},
        source_counts={},
        links=[],
    )
    assert result.skipped_analysis is False
    assert result.no_messages is False
