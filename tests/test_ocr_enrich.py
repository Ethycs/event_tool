"""OCR enrichment: images on a message become text in its content.

The cache is repo-anchored (`data/.ocr_cache.json` via `data_path`), so every
test redirects it — a `monkeypatch.chdir` is not isolation here, the same trap
that overwrote a real store on 2026-08-12.

The load-bearing properties: it does nothing when disabled, it never raises, it
only ever *adds* to content, and it caches by image content so a re-harvest
doesn't re-OCR.
"""
from __future__ import annotations

import pytest

from event_harvester.config import OCRConfig
from event_harvester.pipeline import ocr_enrich


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr_enrich, "CACHE_FILE", tmp_path / "ocr_cache.json")


@pytest.fixture
def _stub_ocr(monkeypatch):
    """Reachable endpoint; each image OCRs to a marker derived from its bytes."""
    monkeypatch.setattr(ocr_enrich.glm_ocr, "health", lambda *a, **k: True)
    calls = {"n": 0}

    def _fetch(ref, timeout):
        return ref.encode()          # the ref itself stands in for image bytes

    def _ocr(data, **k):
        calls["n"] += 1
        return f"TEXT<{data.decode()}>"

    monkeypatch.setattr(ocr_enrich, "_fetch_image", _fetch)
    monkeypatch.setattr(ocr_enrich.glm_ocr, "ocr_image_bytes", _ocr)
    return calls


def _msg(mid, images=None, content="body"):
    m = {"platform": "discord", "id": mid, "timestamp": "t", "author": "a",
         "channel": "c", "content": content}
    if images is not None:
        m["images"] = images
    return m


class TestDisabledIsANoOp:
    def test_disabled_does_nothing(self, _stub_ocr):
        msgs = [_msg("1", images=["a.png"])]
        assert ocr_enrich.enrich_messages(msgs, OCRConfig(enabled=False)) == 0
        assert "flyer text" not in msgs[0]["content"]

    def test_no_images_no_work(self, _stub_ocr):
        msgs = [_msg("1"), _msg("2")]
        assert ocr_enrich.enrich_messages(msgs, OCRConfig(enabled=True)) == 0


class TestEnrichment:
    def test_image_text_is_appended_to_content(self, _stub_ocr):
        msgs = [_msg("1", images=["flyer.png"], content="see this")]
        assert ocr_enrich.enrich_messages(msgs, OCRConfig(enabled=True)) == 1
        assert "see this" in msgs[0]["content"]
        assert "[flyer text]" in msgs[0]["content"]
        assert "TEXT<flyer.png>" in msgs[0]["content"]

    def test_messages_without_images_are_untouched(self, _stub_ocr):
        msgs = [_msg("1", images=["f.png"]), _msg("2", content="plain")]
        ocr_enrich.enrich_messages(msgs, OCRConfig(enabled=True))
        assert msgs[1]["content"] == "plain"

    def test_max_images_caps_per_message(self, _stub_ocr):
        msgs = [_msg("1", images=[f"{i}.png" for i in range(10)])]
        ocr_enrich.enrich_messages(msgs, OCRConfig(enabled=True, max_images=3))
        assert _stub_ocr["n"] == 3

    def test_it_records_ocr_char_count(self, _stub_ocr):
        msgs = [_msg("1", images=["f.png"])]
        ocr_enrich.enrich_messages(msgs, OCRConfig(enabled=True))
        assert msgs[0]["_ocr_chars"] > 0


class TestCaching:
    def test_the_same_image_is_ocred_once_across_messages(self, _stub_ocr):
        msgs = [_msg("1", images=["same.png"]), _msg("2", images=["same.png"])]
        ocr_enrich.enrich_messages(msgs, OCRConfig(enabled=True))
        assert _stub_ocr["n"] == 1          # second hit the cache

    def test_cache_persists_across_runs(self, _stub_ocr):
        ocr_enrich.enrich_messages([_msg("1", images=["x.png"])], OCRConfig(enabled=True))
        first = _stub_ocr["n"]
        ocr_enrich.enrich_messages([_msg("2", images=["x.png"])], OCRConfig(enabled=True))
        assert _stub_ocr["n"] == first      # loaded from disk, not re-OCR'd


class TestNonFatal:
    def test_an_unreachable_endpoint_is_a_clean_noop(self, monkeypatch):
        monkeypatch.setattr(ocr_enrich.glm_ocr, "health", lambda *a, **k: False)
        msgs = [_msg("1", images=["f.png"])]
        assert ocr_enrich.enrich_messages(msgs, OCRConfig(enabled=True)) == 0
        assert msgs[0]["content"] == "body"

    def test_an_image_that_ocrs_to_nothing_does_not_touch_content(self, monkeypatch):
        monkeypatch.setattr(ocr_enrich.glm_ocr, "health", lambda *a, **k: True)
        monkeypatch.setattr(ocr_enrich, "_fetch_image", lambda r, t: b"bytes")
        monkeypatch.setattr(ocr_enrich.glm_ocr, "ocr_image_bytes", lambda d, **k: "  ")
        msgs = [_msg("1", images=["blank.png"])]
        assert ocr_enrich.enrich_messages(msgs, OCRConfig(enabled=True)) == 0
        assert msgs[0]["content"] == "body"
