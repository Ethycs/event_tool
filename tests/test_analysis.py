"""Tests for LLM analysis - prompt building and response parsing."""

from unittest.mock import patch

from event_harvester.config import LLMConfig
from event_harvester.pipeline.analysis import (
    REJECT_BUCKETS,
    build_prompt,
    empty_rejects,
    extract_events_llm,
)


class TestBuildPrompt:
    def test_groups_by_platform_and_channel(self, sample_messages):
        prompt = build_prompt(sample_messages, 7)
        assert "Discord / 123456" in prompt
        assert "Discord / 789012" in prompt
        assert "Telegram / Project Updates" in prompt

    def test_includes_message_content(self, sample_messages):
        prompt = build_prompt(sample_messages, 7)
        assert "review the PR" in prompt
        assert "smoke tests" in prompt

    def test_truncates_long_content(self):
        messages = [
            {
                "platform": "discord",
                "id": "1",
                "timestamp": "2026-03-15T10:00:00+00:00",
                "author": "user",
                "channel": "ch",
                "content": "x" * 500,
            }
        ]
        prompt = build_prompt(messages, 1)
        lines = [line for line in prompt.split("\n") if "user:" in line]
        assert len(lines) == 1
        content_part = lines[0].split("user: ")[1]
        assert len(content_part) <= 400

    def test_limits_messages_per_chat(self):
        messages = [
            {
                "platform": "discord",
                "id": str(i),
                "timestamp": f"2026-03-15T{10 + i // 60:02d}:{i % 60:02d}:00+00:00",
                "author": "user",
                "channel": "ch",
                "content": f"msg {i}",
            }
            for i in range(100)
        ]
        prompt = build_prompt(messages, 1)
        assert "msg 99" in prompt
        assert "msg 40" in prompt
        assert "msg 39" not in prompt

    def test_shows_message_count_header(self, sample_messages):
        prompt = build_prompt(sample_messages, 3)
        assert "5 messages" in prompt
        assert "3 day(s)" in prompt
        assert "3 chat(s)" in prompt

    def test_empty_messages(self):
        prompt = build_prompt([], 7)
        assert "0 messages" in prompt


class TestAnalyseAndExtractTasks:
    def test_returns_empty_when_not_configured(self):
        cfg = LLMConfig(model="")
        result = extract_events_llm([], 7, cfg)
        assert result.summary == ""
        assert result.events == []
        # Even the early-out path returns a full reject shape, so callers
        # never have to guard on a missing bucket.
        assert result.rejects == empty_rejects()

    def test_parses_valid_response(self, sample_messages):
        llm_response = (
            "[Event.1]\n"
            "title = Team Standup\n"
            "date = 2099-04-01\n"
            "time = 10:00\n"
            "location = online\n"
            "link = TBD\n"
            "source = @alice in #123456\n"
            "details = Daily standup meeting"
        )

        cfg = LLMConfig(model="openrouter/test-model")

        with patch("event_harvester.pipeline.classifier.has_trained_models", return_value=False):
            with patch(
                "event_harvester.pipeline.analysis.chat_completion",
                return_value=llm_response,
            ):
                result = extract_events_llm(sample_messages, 7, cfg)

        events = result.events
        assert len(events) == 1
        assert events[0]["title"] == "Team Standup"
        assert events[0]["date"] == "2099-04-01"
        assert set(result.rejects) == set(REJECT_BUCKETS)

    def test_handles_no_events(self, sample_messages):
        cfg = LLMConfig(model="openrouter/test-model")

        with patch("event_harvester.pipeline.classifier.has_trained_models", return_value=False):
            with patch(
                "event_harvester.pipeline.analysis.chat_completion",
                return_value="No events found in the messages.",
            ):
                result = extract_events_llm(sample_messages, 7, cfg)

        assert result.events == []
        # A batch that yields nothing lands in llm_no_events, not nowhere.
        assert result.rejects["llm_no_events"]


def _bulk_messages(n: int) -> list[dict]:
    """n distinct discord messages, enough to exceed the default total cap."""
    return [
        {
            "platform": "discord",
            "id": str(9000 + i),
            "timestamp": "2026-03-15T10:00:00+00:00",
            "author": "alice",
            "channel": "123456",
            "content": f"Standup number {i} is on 2099-04-01 at 10:00",
        }
        for i in range(n)
    ]


class TestCapEffect:
    """Pin how many messages actually reach the LLM, not which args were passed.

    The `--only` path reaches `extract_events_llm` as `caps=None`. Before the
    2026-07-29 fix that fell through to a hardcoded `cap_total = 150`, so a run
    that advertised "no caps" still dropped everything past the 150th message.
    """

    def _count_messages_sent(self, messages, caps):
        cfg = LLMConfig(model="openrouter/test-model")
        seen: list[int] = []

        def _fake_cloud(msgs, days_back, cfg_, batch_size=10):
            seen.append(len(msgs))
            return "", [], {"llm_past": [], "llm_no_events": []}

        with patch("event_harvester.pipeline.classifier.has_trained_models", return_value=False):
            with patch(
                "event_harvester.pipeline.analysis._extract_events_cloud",
                side_effect=_fake_cloud,
            ):
                extract_events_llm(messages, 7, cfg, caps=caps)

        assert len(seen) == 1
        return seen[0]

    def test_caps_none_sends_everything(self):
        msgs = _bulk_messages(200)
        assert self._count_messages_sent(msgs, None) == 200

    def test_explicit_total_is_honoured(self):
        from event_harvester.config import CapConfig

        msgs = _bulk_messages(200)
        assert self._count_messages_sent(msgs, CapConfig(total=40)) <= 40

    def test_negative_total_sentinel_sends_everything(self):
        from event_harvester.config import CapConfig

        msgs = _bulk_messages(200)
        caps = CapConfig(discord=-1, telegram=-1, gmail=-1, signal=-1, web=-1, total=-1)
        assert self._count_messages_sent(msgs, caps) == 200


class TestRejectsComeFromTheBackendThatRan:
    """Regression for the reject buckets being read off the wrong backend.

    The orchestrator used to read `llm_past`/`llm_no_events` from
    `_extract_events_cloud`'s function attributes on *every* run. On a
    `backend="local"` run that function never executes, so those buckets held
    whatever a previous cloud run left behind — or nothing.
    """

    def _run(self, backend, cloud_ret, local_ret):
        # A local-backend config is only `is_configured` with a model_path,
        # so set one — otherwise extract_events_llm short-circuits before
        # either backend runs and the test proves nothing.
        if backend == "local":
            cfg = LLMConfig(backend="local", model_path="/fake/model.onnx")
        else:
            cfg = LLMConfig(model="openrouter/test-model", backend=backend)
        with patch("event_harvester.pipeline.classifier.has_trained_models", return_value=False):
            with patch(
                "event_harvester.pipeline.analysis._extract_events_cloud",
                return_value=cloud_ret,
            ), patch(
                "event_harvester.pipeline.analysis._extract_events_local",
                return_value=local_ret,
            ):
                return extract_events_llm(_bulk_messages(3), 7, cfg)

    def test_local_backend_gets_local_rejects(self):
        result = self._run(
            "local",
            ("", [], {"llm_past": [{"title": "CLOUD"}], "llm_no_events": []}),
            ("", [], {"llm_past": [{"title": "LOCAL"}], "llm_no_events": []}),
        )
        assert [e["title"] for e in result.rejects["llm_past"]] == ["LOCAL"]

    def test_cloud_backend_gets_cloud_rejects(self):
        result = self._run(
            "litellm",
            ("", [], {"llm_past": [{"title": "CLOUD"}], "llm_no_events": []}),
            ("", [], {"llm_past": [{"title": "LOCAL"}], "llm_no_events": []}),
        )
        assert [e["title"] for e in result.rejects["llm_past"]] == ["CLOUD"]

    def test_all_buckets_always_present(self):
        result = self._run("local", ("", [], {}), ("", [], {}))
        assert set(result.rejects) == set(REJECT_BUCKETS)


class TestParseEventIniIsThreadSafe:
    """`_parse_event_ini` returns its dropped list instead of stashing it.

    It runs on up to 5 pool threads concurrently; the old
    `_parse_event_ini._last_dropped` write-then-read pair meant one batch
    could read a sibling's dropped events.
    """

    def test_dropped_is_returned_per_call(self):
        from event_harvester.pipeline.analysis import _parse_event_ini

        past = (
            "[Event.1]\ntitle = Old Thing\ndate = 2001-01-01\ntime = 10:00\n"
            "location = x\nlink = TBD\nsource = @a in #1\ndetails = d"
        )
        future = (
            "[Event.1]\ntitle = New Thing\ndate = 2099-04-01\ntime = 10:00\n"
            "location = x\nlink = TBD\nsource = @a in #1\ndetails = d"
        )

        _, events_past, dropped_past = _parse_event_ini(past)
        _, events_future, dropped_future = _parse_event_ini(future)

        assert events_past == [] and len(dropped_past) == 1
        assert len(events_future) == 1 and dropped_future == []

    def test_concurrent_calls_do_not_share_dropped(self):
        import concurrent.futures

        from event_harvester.pipeline.analysis import _parse_event_ini

        def make(i, year):
            return (
                f"[Event.1]\ntitle = E{i}\ndate = {year}-04-01\ntime = 10:00\n"
                f"location = x\nlink = TBD\nsource = @a in #1\ndetails = d"
            )

        # Half resolve to the past (dropped), half to the future (kept).
        raws = [make(i, 2001 if i % 2 else 2099) for i in range(20)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(_parse_event_ini, raws))

        for i, (_, events, dropped) in enumerate(results):
            if i % 2:
                assert events == [] and len(dropped) == 1, f"call {i} leaked"
            else:
                assert len(events) == 1 and dropped == [], f"call {i} leaked"
