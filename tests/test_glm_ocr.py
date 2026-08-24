"""The GLM-OCR client: the Triton v2 contract, parsed defensively.

GLM-OCR is a bespoke Triton service, not OpenAI-shaped (wiki
`inference/inference-plane.md`), so it can't reuse `llm.vision_completion`. The
live contract was captured 2026-08-23: `outputs[0].data[0]` is a JSON list of
`{page, bbox:[x0,y0,x1,y1], kind, text}`. These pin the parsing and the
reading-order join without needing the endpoint.
"""
from __future__ import annotations

from event_harvester.sources import glm_ocr


def _envelope(regions_json: str) -> dict:
    return {"model_name": "glm-ocr", "outputs": [
        {"name": "regions", "datatype": "BYTES", "shape": [1], "data": [regions_json]}]}


class TestParseRegions:
    def test_the_live_response_shape(self):
        env = _envelope('[{"page":1,"bbox":[0,0,10,10],"kind":"text","text":"hi"}]')
        assert glm_ocr.parse_regions(env) == [
            {"page": 1, "bbox": [0, 0, 10, 10], "kind": "text", "text": "hi"}]

    def test_a_malformed_regions_string_is_empty_not_a_crash(self):
        assert glm_ocr.parse_regions(_envelope("{not json")) == []

    def test_a_missing_regions_output_is_empty(self):
        assert glm_ocr.parse_regions({"outputs": [{"name": "other", "data": ["x"]}]}) == []

    def test_an_empty_envelope_is_empty(self):
        assert glm_ocr.parse_regions({}) == []


class TestReadingOrder:
    def test_regions_join_top_to_bottom_then_left(self):
        env = _envelope(
            '[{"bbox":[0,100,9,110],"text":"second"},'
            ' {"bbox":[0,0,9,10],"text":"first"},'
            ' {"bbox":[50,0,60,10],"text":"first-right"}]')
        assert glm_ocr._regions_to_text(glm_ocr.parse_regions(env)) == \
            "first\nfirst-right\nsecond"

    def test_blank_texts_are_dropped(self):
        env = _envelope('[{"bbox":[0,0,1,1],"text":"  "},{"bbox":[0,5,1,6],"text":"real"}]')
        assert glm_ocr._regions_to_text(glm_ocr.parse_regions(env)) == "real"


class TestOcrImageBytes:
    def test_empty_bytes_short_circuits(self):
        assert glm_ocr.ocr_image_bytes(b"") == ""

    def test_a_transport_failure_returns_empty_not_raises(self, monkeypatch):
        """Additive feature: a dead endpoint means 'no extra text', never a
        broken harvest."""
        def _boom(*a, **k):
            raise RuntimeError("connection refused")
        monkeypatch.setattr(glm_ocr.httpx, "post", _boom)
        assert glm_ocr.ocr_image_bytes(b"\x89PNG...") == ""
