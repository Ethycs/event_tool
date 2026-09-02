"""Tests for the browser-level MHTML checkpoint (record/replay)."""

from __future__ import annotations

import event_harvester.sources.web_checkpoint as wc


def _restore():
    wc.set_mode(None)


def test_mode_off_by_default(monkeypatch):
    monkeypatch.delenv("EH_WEB_CHECKPOINT", raising=False)
    wc.set_mode(None)
    assert wc.mode() == "off"
    assert not wc.recording()
    assert not wc.replaying()


def test_env_selects_mode(monkeypatch):
    monkeypatch.setenv("EH_WEB_CHECKPOINT", "record")
    wc.set_mode(None)
    assert wc.recording() and not wc.replaying()
    monkeypatch.setenv("EH_WEB_CHECKPOINT", "replay")
    assert wc.replaying() and not wc.recording()
    _restore()


def test_override_beats_env(monkeypatch):
    monkeypatch.setenv("EH_WEB_CHECKPOINT", "off")
    wc.set_mode("replay")
    assert wc.replaying()
    _restore()


def test_stem_is_stable_readable_and_unique():
    u = "https://erobay.com/calendar/Calcium40.pl?Op=ShowIt&CalendarName=Community"
    assert wc._stem(u) == wc._stem(u)          # stable
    assert "erobay" in wc._stem(u)             # readable
    assert wc._stem("https://a.com") != wc._stem("https://b.com")  # unique


def test_record_is_noop_unless_recording(tmp_path, monkeypatch):
    monkeypatch.setattr(wc, "_DIR", tmp_path)
    wc.set_mode("off")
    assert wc.record(object(), "https://x.com") is None
    assert not wc.exists("https://x.com")
    _restore()


class _FakeCDP:
    def send(self, method, params):
        assert method == "Page.captureSnapshot"
        assert params == {"format": "mhtml"}
        return {"data": "From: <Saved by Blink>\n\nMHTML-ARCHIVE"}


class _FakeCtx:
    def new_cdp_session(self, page):
        return _FakeCDP()


class _FakePage:
    context = _FakeCtx()

    def content(self):
        return "<html><noscript>RAW-DOM</noscript></html>"


def test_record_writes_html_primary_plus_mhtml_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(wc, "_DIR", tmp_path)
    url = "https://erobay.com/x"

    wc.set_mode("record")
    out = wc.record(_FakePage(), url)
    assert out is not None and out.suffix == ".html"        # raw DOM is primary
    assert (tmp_path / f"{wc._stem(url)}.mhtml").exists()    # archive alongside
    assert wc.exists(url)

    wc.set_mode("replay")
    res = wc.replay(uc=None, url=url)     # replay needs no browser
    assert res is not None
    page, html = res
    assert "RAW-DOM" in html             # the raw DOM the parser consumes, not the MHTML
    assert page.content() == html        # the stand-in serves the raw HTML
    page.close()                         # incidental page calls are safe no-ops
    _restore()


def test_replay_falls_through_when_no_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(wc, "_DIR", tmp_path)
    wc.set_mode("replay")
    assert wc.replay(object(), "https://never-recorded.com") is None
    _restore()


def test_record_without_cdp_still_writes_html(tmp_path, monkeypatch):
    monkeypatch.setattr(wc, "_DIR", tmp_path)

    class _NoCDPPage:
        context = None  # new_cdp_session lookup will raise → MHTML archive skipped

        def content(self):
            return "<html>content-only</html>"

    wc.set_mode("record")
    out = wc.record(_NoCDPPage(), "https://y.com")
    assert out is not None and out.suffix == ".html"
    assert not (tmp_path / f"{wc._stem('https://y.com')}.mhtml").exists()
    assert wc.exists("https://y.com")
    _restore()
