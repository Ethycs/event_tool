"""Tests for the chunking helper used by parallel web fetch."""

from event_harvester.sources.web_fetch import _chunk_sources


def _s(name: str) -> dict:
    return {"name": name, "url": f"https://example.com/{name}"}


class TestChunkSources:
    def test_empty(self):
        assert _chunk_sources([], 3) == []

    def test_n_one_returns_single_chunk(self):
        srcs = [_s("a"), _s("b"), _s("c")]
        assert _chunk_sources(srcs, 1) == [srcs]

    def test_round_robin_split(self):
        srcs = [_s(c) for c in "abcdef"]
        chunks = _chunk_sources(srcs, 3)
        assert len(chunks) == 3
        # Round-robin so each chunk has 2 items
        assert [s["name"] for s in chunks[0]] == ["a", "d"]
        assert [s["name"] for s in chunks[1]] == ["b", "e"]
        assert [s["name"] for s in chunks[2]] == ["c", "f"]

    def test_n_larger_than_sources(self):
        srcs = [_s("a"), _s("b")]
        chunks = _chunk_sources(srcs, 5)
        # Empty chunks are dropped; only as many chunks as we have sources
        assert len(chunks) == 2
        assert [s["name"] for s in chunks[0]] == ["a"]
        assert [s["name"] for s in chunks[1]] == ["b"]

    def test_uneven_split(self):
        srcs = [_s(c) for c in "abcde"]
        chunks = _chunk_sources(srcs, 3)
        assert len(chunks) == 3
        assert [s["name"] for s in chunks[0]] == ["a", "d"]
        assert [s["name"] for s in chunks[1]] == ["b", "e"]
        assert [s["name"] for s in chunks[2]] == ["c"]

    def test_n_zero_treated_as_one(self):
        srcs = [_s("a"), _s("b")]
        assert _chunk_sources(srcs, 0) == [srcs]
