"""All data paths must resolve to the same <repo>/data directory.

Regression coverage. The codebase computes the data directory in four
different places, and pipeline.harvest used `.parent` three times instead of
four -- landing on <repo>/src/data. Nothing pointed there, so the message
cache was written to a directory no other component read, the
"Delete data/.message_cache.json" hint pointed at a file that did not exist,
and `harvest_events(web_source=X)` happily returned source Y's stale cache.

The same off-by-one had already disabled the classifier (pipeline/classifier.py).
Pinning them together here so a third instance fails loudly.
"""

from pathlib import Path

from event_harvester.pipeline.harvest import CACHE_FILE, _DATA_DIR as HARVEST_DATA_DIR
from event_harvester.sources.web_utils import _DATA_DIR as WEB_UTILS_DATA_DIR
from event_harvester.sources.web_session import _DATA_DIR as WEB_SESSION_DATA_DIR


def _repo_root() -> Path:
    # tests/ lives directly under the repo root.
    return Path(__file__).resolve().parents[1]


def test_all_data_dirs_agree():
    expected = _repo_root() / "data"
    assert HARVEST_DATA_DIR == expected, (
        f"harvest._DATA_DIR is {HARVEST_DATA_DIR}, expected {expected}"
    )
    assert WEB_UTILS_DATA_DIR == expected
    assert WEB_SESSION_DATA_DIR == expected


def test_data_dir_is_not_under_src():
    """The specific failure: a data dir nested inside the package tree."""
    for name, d in (
        ("harvest", HARVEST_DATA_DIR),
        ("web_utils", WEB_UTILS_DATA_DIR),
        ("web_session", WEB_SESSION_DATA_DIR),
    ):
        assert "src" not in d.parts, f"{name} data dir is inside src/: {d}"


def test_cache_file_lives_in_the_data_dir():
    assert CACHE_FILE.parent == HARVEST_DATA_DIR
    assert CACHE_FILE.name == ".message_cache.json"
