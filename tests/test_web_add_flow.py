"""The `web add` / `web test` flow: auto-diagnose, log the attempt, and save
only when it is worth saving.

These pin the decision table, not the browser: `probe_block`, the browser
diagnostic, and the config write are all mocked, so the test is about *what the
flow decides and records* — the part that regressed into "add a dead source and
print to a terminal nobody kept".
"""

from __future__ import annotations

from unittest.mock import patch

import event_harvester.sources.onboard as onboard
from event_harvester.cli.commands import web


def _run(url, *, add=True, force=False, mode_override=None, no_probe=True,
         block=None, diag=None):
    block = block or {"status": 200, "server": "", "blocked": False, "reason": None}
    diag = diag or {"mode": "calendar", "event_links": 5, "feed_items": 0}
    rec: dict = {}
    added: dict = {}
    with patch.object(onboard, "probe_block", return_value=block), \
         patch.object(onboard, "record_attempt",
                      side_effect=lambda e: rec.update(e) or [e]), \
         patch.object(web, "_run_url_diagnostics", return_value=diag), \
         patch.object(web, "_probe_and_report", return_value=None), \
         patch.object(web, "_add_source_to_config",
                      side_effect=lambda *a, **k: added.update(args=a, kw=k)):
        web._diagnose_and_log(url, add=add, force=force,
                              mode_override=mode_override, no_probe=no_probe)
    return rec, added


def test_a_good_source_is_saved_and_logged():
    rec, added = _run("https://venue.test/cal")
    assert rec["saved"] is True and rec["reason"] is None
    assert added["args"][0] == "https://venue.test/cal"


def test_datadome_is_logged_and_not_added():
    rec, added = _run("https://tixr.test/", block={
        "status": 403, "server": "nginx", "blocked": True, "reason": "datadome"})
    assert rec["reason"] == "datadome" and rec["saved"] is False
    assert added == {}            # never written to config


def test_force_adds_even_a_walled_source():
    rec, added = _run("https://tixr.test/", force=True, block={
        "status": 403, "server": "nginx", "blocked": True, "reason": "datadome"})
    assert rec["saved"] is True and "args" in added


def test_cloudflare_is_added_with_native_chrome():
    """A Cloudflare challenge is recoverable — the erobay case — so it is saved,
    with native_chrome set, not refused like DataDome."""
    rec, added = _run("https://erobay.test/", block={
        "status": 403, "server": "cloudflare", "blocked": True,
        "reason": "cloudflare-challenge"})
    assert rec["saved"] is True
    assert added["kw"].get("native_chrome") is True


def test_an_empty_raw_page_is_logged_and_not_added():
    rec, added = _run("https://thin.test/", diag={
        "mode": "raw", "event_links": 0, "feed_items": 0})
    assert rec["reason"] == "empty" and rec["saved"] is False and added == {}


def test_test_action_diagnoses_and_logs_but_never_saves():
    rec, added = _run("https://venue.test/cal", add=False)
    assert rec["action"] == "test" and rec["saved"] is False and added == {}
