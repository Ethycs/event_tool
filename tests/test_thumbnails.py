"""Tests for embedded event thumbnails.

The design constraint these protect is that the report is **one self-contained
file that gets forwarded**. A remote `<img src>` would fail
`test_no_external_resources` and, worse, would phone home to the image host
every time anyone opened a forwarded copy. So images are downscaled and inlined
as `data:` URIs, and nothing here may reintroduce a network dependency at
render time.
"""

from __future__ import annotations

import base64
import io

import pytest

from event_harvester.outputs import thumbnails as th


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(th, "CACHE_DIR", tmp_path / "thumbs")


def _png(w=800, h=400, colour=(200, 40, 90)):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), colour).save(buf, format="PNG")
    return buf.getvalue()


class TestFindImageUrl:
    def test_og_image(self):
        html = '<meta property="og:image" content="https://cdn.x/a.jpg">'
        assert th.find_image_url(html) == "https://cdn.x/a.jpg"

    def test_attribute_order_does_not_matter(self):
        html = '<meta content="https://cdn.x/a.jpg" property="og:image">'
        assert th.find_image_url(html) == "https://cdn.x/a.jpg"

    def test_name_instead_of_property(self):
        html = '<meta name="og:image" content="https://cdn.x/a.jpg">'
        assert th.find_image_url(html) == "https://cdn.x/a.jpg"

    def test_twitter_image_is_the_fallback(self):
        html = '<meta name="twitter:image" content="https://cdn.x/t.jpg">'
        assert th.find_image_url(html) == "https://cdn.x/t.jpg"

    def test_json_ld_is_the_last_resort(self):
        html = ('<script type="application/ld+json">'
                '{"@type":"Event","name":"E","startDate":"2026-09-01T18:00",'
                '"image":"https://cdn.x/ld.jpg"}</script>')
        assert th.find_image_url(html) == "https://cdn.x/ld.jpg"

    def test_a_relative_url_is_resolved_against_the_page(self):
        html = '<meta property="og:image" content="/img/a.jpg">'
        assert th.find_image_url(html, "https://venue.com/events/x") \
            == "https://venue.com/img/a.jpg"

    def test_a_protocol_relative_url_gets_https(self):
        html = '<meta property="og:image" content="//cdn.x/a.jpg">'
        assert th.find_image_url(html) == "https://cdn.x/a.jpg"

    def test_an_inline_data_image_is_ignored(self):
        """Already inline; re-encoding it would be pointless work."""
        html = '<meta property="og:image" content="data:image/png;base64,AAAA">'
        assert th.find_image_url(html) == ""

    def test_a_page_with_no_image_yields_nothing(self):
        assert th.find_image_url("<html><body>no pictures</body></html>") == ""

    def test_empty_input(self):
        assert th.find_image_url("") == ""


class TestHtmlEscapedUrls:
    """A `content="..."` attribute is HTML, so `&` is written `&amp;`.

    Taken literally that renames every query parameter after the first —
    `oh` becomes `amp;oh` — and on a signed CDN URL the two renamed ones are
    the signature and its expiry, so the request arrives unsigned and is
    correctly refused. This was diagnosed as an Instagram access restriction
    twice before it was diagnosed as our own bug.
    """

    def test_ampersands_are_decoded(self):
        html = ('<meta property="og:image" '
                'content="https://cdn.x/a.jpg?stp=c400&amp;oh=SIG&amp;oe=6A81F015">')
        got = th.find_image_url(html)
        assert "&amp;" not in got
        assert got.endswith("?stp=c400&oh=SIG&oe=6A81F015")

    def test_the_signature_parameters_keep_their_names(self):
        from urllib.parse import parse_qs, urlparse

        html = ('<meta property="og:image" '
                'content="https://cdn.x/a.jpg?a=1&amp;oh=SIG&amp;oe=EXP">')
        q = parse_qs(urlparse(th.find_image_url(html)).query)
        assert set(q) == {"a", "oh", "oe"}, "not amp;oh / amp;oe"

    def test_other_entities_are_decoded_too(self):
        html = '<meta property="og:image" content="https://cdn.x/a.jpg?q=1&#38;b=2">'
        assert th.find_image_url(html) == "https://cdn.x/a.jpg?q=1&b=2"


class TestUnwrapProxy:
    """Eventbrite's og:image is a Next.js proxy that 400s for anyone else."""

    EB = ("https://www.eventbrite.com/e/_next/image?url="
          "https%3A%2F%2Fimg.evbuc.com%2Fhttps%253A%252F%252Fcdn.evbuc.com"
          "%252Fimages%252F1%252F2%252Foriginal.png&w=940&q=75")

    def test_the_real_cdn_url_is_recovered(self):
        assert th.unwrap_proxy(self.EB).startswith("https://img.evbuc.com/")

    def test_the_inner_encoding_is_left_alone(self):
        """Decoding twice was the bug: `img.evbuc.com` expects the CDN URL as
        an *encoded* path segment, and a bare `https://` there is a 400."""
        got = th.unwrap_proxy(self.EB)
        assert "https%3A%2F%2Fcdn.evbuc.com" in got
        assert "https://cdn.evbuc.com" not in got

    def test_an_ordinary_url_is_untouched(self):
        assert th.unwrap_proxy("https://cdn.x/a.jpg") == "https://cdn.x/a.jpg"

    def test_a_proxy_with_no_url_param_is_untouched(self):
        u = "https://x.com/_next/image?w=100"
        assert th.unwrap_proxy(u) == u

    def test_a_non_http_inner_value_is_refused(self):
        u = "https://x.com/_next/image?url=javascript%3Aalert(1)"
        assert th.unwrap_proxy(u) == u

    def test_unwrapping_happens_during_discovery(self):
        html = f'<meta property="og:image" content="{self.EB}">'
        assert th.find_image_url(html).startswith("https://img.evbuc.com/")


class TestEncoding:
    def test_it_produces_a_fixed_size_jpeg(self):
        from PIL import Image

        out = th.encode_thumbnail(_png(1600, 400))
        img = Image.open(io.BytesIO(out))
        assert img.format == "JPEG"
        assert img.size == (th.THUMB_W, th.THUMB_H), "cover-cropped, not letterboxed"

    def test_a_tall_image_is_also_normalised(self):
        from PIL import Image

        img = Image.open(io.BytesIO(th.encode_thumbnail(_png(300, 2000))))
        assert img.size == (th.THUMB_W, th.THUMB_H)

    def test_it_is_much_smaller_than_the_original(self):
        big = _png(2000, 2000)
        assert len(th.encode_thumbnail(big)) < len(big) / 4

    def test_transparency_does_not_become_a_black_box(self):
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGBA", (400, 300), (255, 255, 255, 0)).save(buf, format="PNG")
        img = Image.open(io.BytesIO(th.encode_thumbnail(buf.getvalue())))
        assert img.getpixel((10, 10))[0] > 200, "flattened onto white"

    def test_junk_bytes_are_not_an_exception(self):
        assert th.encode_thumbnail(b"this is not an image") == b""

    def test_empty_bytes(self):
        assert th.encode_thumbnail(b"") == b""


class TestDataUri:
    def test_it_is_a_jpeg_data_uri(self):
        uri = th.data_uri(b"\xff\xd8\xff")
        assert uri.startswith("data:image/jpeg;base64,")
        assert base64.b64decode(uri.split(",", 1)[1]) == b"\xff\xd8\xff"

    def test_it_contains_no_scheme_the_report_test_would_reject(self):
        """`test_no_external_resources` fails the build on `src="http..."`."""
        import re
        uri = th.data_uri(_png(10, 10))
        assert not re.search(r'^https?:', uri)


class TestCollection:
    @staticmethod
    def _patch(monkeypatch, per_link):
        monkeypatch.setattr(th, "thumbnail_for", lambda link: per_link.get(link, b""))

    def test_only_links_with_images_appear(self, monkeypatch):
        self._patch(monkeypatch, {"a": _png(40, 40)})
        out = th.thumbnails_for(["a", "b"])
        assert set(out) == {"a"}

    def test_a_link_is_fetched_once_however_many_events_share_it(self, monkeypatch):
        calls = []
        monkeypatch.setattr(th, "thumbnail_for",
                            lambda link: calls.append(link) or _png(20, 20))
        th.thumbnails_for(["a", "a", "a"])
        assert calls == ["a"]

    def test_the_link_ceiling_is_honoured(self, monkeypatch):
        calls = []
        monkeypatch.setattr(th, "thumbnail_for",
                            lambda link: calls.append(link) or _png(20, 20))
        th.thumbnails_for([str(i) for i in range(50)], max_links=5)
        assert len(calls) == 5

    def test_the_byte_budget_stops_the_page_ballooning(self, monkeypatch):
        self._patch(monkeypatch, {str(i): _png(600, 600, (i * 7 % 255, 90, 20))
                                  for i in range(20)})
        out = th.thumbnails_for([str(i) for i in range(20)], budget_bytes=30_000)
        assert 0 < len(out) < 20
        assert sum(len(v) for v in out.values()) <= 30_000

    def test_no_links_makes_no_calls(self, monkeypatch):
        def boom(link):
            raise AssertionError("must not fetch with nothing to do")

        monkeypatch.setattr(th, "thumbnail_for", boom)
        assert th.thumbnails_for([]) == {}
        assert th.thumbnails_for(["", None]) == {}


class TestFetchIsFailSafe:
    def test_a_dead_link_yields_nothing(self, monkeypatch):
        def boom(*a, **k):
            raise OSError("connection reset")

        monkeypatch.setattr(th, "_fetch", boom)
        assert th.thumbnail_for("https://x/gone") == b""

    def test_a_page_without_an_image_yields_nothing(self, monkeypatch):
        monkeypatch.setattr(th, "_fetch", lambda u, a: b"<html>nothing</html>")
        assert th.thumbnail_for("https://x/plain") == b""

    def test_an_empty_link_makes_no_request(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("must not fetch an empty link")

        monkeypatch.setattr(th, "_fetch", boom)
        assert th.thumbnail_for("") == b""

    def test_the_cache_serves_the_second_call(self, monkeypatch, tmp_path):
        calls = []

        def fake(url, accept, referer=""):
            calls.append(url)
            if "html" in accept:
                return b'<meta property="og:image" content="https://cdn.x/a.png">'
            # Sent as a courtesy, not because any measured host requires it.
            assert referer, "image requests carry the page as Referer"
            return _png(50, 50)

        monkeypatch.setattr(th, "_fetch", fake)
        first = th.thumbnail_for("https://x/e1")
        assert first
        calls.clear()
        assert th.thumbnail_for("https://x/e1") == first
        assert calls == [], "second call must be served from disk"


class TestReportIntegration:
    def test_the_report_embeds_no_external_image(self, tmp_path, monkeypatch):
        import re

        from event_harvester.outputs.html_report import generate_html_report

        monkeypatch.setattr(
            "event_harvester.outputs.thumbnails.thumbnails_for",
            lambda links, **k: {links[0]: th.data_uri(_png(60, 40))},
        )
        p = tmp_path / "r.html"
        generate_html_report(
            [{"title": "E", "date": "2026-09-01", "link": "https://lu.ma/x"}],
            output_path=str(p), thumbnails=True,
        )
        html = p.read_text(encoding="utf-8")
        assert "data:image/jpeg;base64," in html
        assert not re.search(r'src\s*=\s*["\']https?:', html, re.I)

    def test_thumbnails_are_off_by_default(self, tmp_path, monkeypatch):
        """Report generation must not silently depend on the network."""
        from event_harvester.outputs.html_report import generate_html_report

        def boom(*a, **k):
            raise AssertionError("thumbnails must be opt-in")

        monkeypatch.setattr(
            "event_harvester.outputs.thumbnails.thumbnails_for", boom)
        generate_html_report(
            [{"title": "E", "link": "https://lu.ma/x"}],
            output_path=str(tmp_path / "r.html"),
        )

    def test_a_thumbnail_failure_never_fails_the_report(self, tmp_path, monkeypatch):
        from event_harvester.outputs.html_report import generate_html_report

        def boom(*a, **k):
            raise RuntimeError("network down")

        monkeypatch.setattr(
            "event_harvester.outputs.thumbnails.thumbnails_for", boom)
        p = tmp_path / "r.html"
        generate_html_report(
            [{"title": "E", "link": "https://lu.ma/x"}],
            output_path=str(p), thumbnails=True,
        )
        assert "E" in p.read_text(encoding="utf-8")
