"""Tests for ``event_harvester.sources.instagram`` and ``web_modes.instagram``.

Three things are worth pinning here, and they are not equally interesting.

The **walker** tests use real payload shapes, including the one the old
container-keyed walker returned nothing for (posts nested inside a list).
That is the regression, so it gets a named test rather than a fixture.

The **GraphQL replay** tests drive a fake page. What they check is not "did
fetch happen" but the two properties the design rests on: that a replayed
body keeps every session parameter it was recorded with, and that a replay
which comes back empty ends the loop instead of re-asking forever.

The **discovery** tests are ranking tests. A handle cannot be validated the
way ``feed_probe`` validates a feed — logged-out, Instagram answers 200 for
handles that do not exist — so ranking is the only thing standing between a
venue's account and a promoter it happens to link to.
"""

from __future__ import annotations

import json

import pytest

from event_harvester.sources import instagram as ig
from event_harvester.sources.web_modes.instagram import _interleave, _normalize_accounts

# ── Fakes ─────────────────────────────────────────────────────────────


class FakePage:
    """Duck-typed Playwright page: records evaluate() calls, replays canned bodies."""

    def __init__(
        self, *, responses=None,
        url="https://www.instagram.com/studsf/", password_field=False,
    ):
        self._responses = list(responses or [])
        self.url = url
        self._password_field = password_field
        self.evaluated: list[list] = []
        self.waits: list[int] = []
        self.closed = False
        self.handlers: dict[str, list] = {}

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def evaluate(self, _script, arg=None):
        self.evaluated.append(arg)
        if not self._responses:
            return {"status": 200, "text": "{}"}
        return self._responses.pop(0)

    def wait_for_timeout(self, ms):
        self.waits.append(ms)

    def query_selector(self, _sel):
        return object() if self._password_field else None

    def goto(self, url, **_kw):
        self.url = url

    def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, page):
        self._page = page

    def new_page(self):
        return self._page


class FakeUC:
    def __init__(self, page):
        self._context = FakeContext(page)
        self._stealth_plugin = None
        self.timeout_ms = 30000


def _query(**overrides) -> ig.GraphQLQuery:
    body = {
        "doc_id": "9182736",
        "lsd": "AVqbxyz",
        "jazoest": "21055",
        "fb_api_req_friendly_name": "PolarisProfilePostsQuery",
        "variables": json.dumps({"first": 12, "after": None, "before": None, "id": "77"}),
    }
    body.update(overrides.pop("body", {}))
    return ig.GraphQLQuery(
        url="https://www.instagram.com/graphql/query",
        friendly_name="PolarisProfilePostsQuery",
        body=body,
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "x-csrftoken": "tok",
            "x-fb-lsd": "AVqbxyz",
            "cookie": "sessionid=secret",
            "user-agent": "Mozilla/5.0",
        },
        **overrides,
    )


def _connection(*nodes, cursor="CURSOR1", has_next=True):
    return {
        "data": {
            "xdt_api__v1__feed__user_timeline_graphql_connection": {
                "edges": [{"node": n} for n in nodes],
                "page_info": {"has_next_page": has_next, "end_cursor": cursor},
            }
        }
    }


def _post(pk="3400", text="Saturday night: doors at 9pm, $10 at the door", **extra):
    node = {
        "pk": pk,
        "code": f"C{pk}",
        "user": {"username": "studsf"},
        "caption": {"text": text},
        "taken_at": 1754400000,
    }
    node.update(extra)
    return node


# ── Walker ────────────────────────────────────────────────────────────


def test_walker_finds_posts_nested_inside_lists():
    """The regression: the old walker recursed into dicts only.

    A payload whose posts sit inside an array of sections produced zero posts
    and no error — indistinguishable from a venue that posted nothing.
    """
    payload = {"data": {"sections": [{"layout_content": {"medias": [{"media": _post()}]}}]}}
    posts = ig.normalize_posts(payload, source_name="instagram")
    assert len(posts) == 1
    assert posts[0]["id"] == "ig:3400"


def test_walker_handles_the_modern_connection_name():
    posts = ig.normalize_posts(_connection(_post()), source_name="instagram")
    assert [p["id"] for p in posts] == ["ig:3400"]


def test_walker_preserves_document_order():
    """A timeline arrives newest-first, and every cap downstream trusts that."""
    payload = _connection(_post(pk="1"), _post(pk="2"), _post(pk="3"))
    posts = ig.normalize_posts(payload, source_name="instagram")
    assert [p["id"] for p in posts] == ["ig:1", "ig:2", "ig:3"]


def test_walker_handles_legacy_edge_media_to_caption():
    payload = {"items": [{
        "id": "p1",
        "shortcode": "XYZ",
        "owner": {"username": "elriosf"},
        "edge_media_to_caption": {"edges": [{"node": {"text": "Sunday: Salsa on the patio, 3pm"}}]},
    }]}
    posts = ig.normalize_posts(payload, source_name="instagram")
    assert posts[0]["author"] == "@elriosf"
    assert "Salsa" in posts[0]["content"]


def test_content_leads_with_the_permalink():
    """build_prompt truncates content to 400 chars — a trailing link is a lost link."""
    posts = ig.normalize_posts(_connection(_post()), source_name="instagram")
    assert posts[0]["content"].startswith("https://www.instagram.com/p/C3400/")


def test_reels_get_a_reel_permalink():
    posts = ig.normalize_posts(
        _connection(_post(product_type="clips")), source_name="instagram",
    )
    assert posts[0]["content"].startswith("https://www.instagram.com/reel/C3400/")


def test_injected_ads_are_dropped():
    payload = _connection(
        _post(), _post(pk="3401", text="Buy this mattress today, free shipping", ad_id="99"),
    )
    assert [p["id"] for p in ig.normalize_posts(payload, source_name="instagram")] == ["ig:3400"]


def test_seen_set_dedupes_across_payloads():
    seen: set[str] = set()
    first = ig.normalize_posts(_connection(_post()), source_name="ig", seen=seen)
    second = ig.normalize_posts(_connection(_post()), source_name="ig", seen=seen)
    assert len(first) == 1
    assert second == []


def test_short_captions_are_dropped():
    assert ig.normalize_posts(_connection(_post(text="🔥🔥")), source_name="ig") == []


def test_timestamp_falls_back_to_caption_created_at():
    node = _post()
    del node["taken_at"]
    node["caption"] = {"text": node["caption"]["text"], "created_at": 1754400000}
    posts = ig.normalize_posts(_connection(node), source_name="ig")
    assert posts[0]["timestamp"].startswith("2025-08-05")


def test_location_is_carried_into_content():
    node = _post(location={"name": "The Stud", "city": "San Francisco"})
    posts = ig.normalize_posts(_connection(node), source_name="ig")
    assert "at: The Stud, San Francisco" in posts[0]["content"]


def test_alt_text_only_supplements_a_thin_caption():
    thin = _post(text="tonight!! 🌈", accessibility_caption="Photo of a flyer reading DISCO 10PM")
    fat = _post(
        pk="3402",
        text="A long caption about the party that says everything already, "
             "with the door price and the start time and the lineup in full.",
        accessibility_caption="Photo of a flyer reading DISCO 10PM",
    )
    posts = ig.normalize_posts(_connection(thin, fat), source_name="ig")
    by_id = {p["id"]: p["content"] for p in posts}
    assert "alt: Photo of a flyer" in by_id["ig:3400"]
    assert "alt:" not in by_id["ig:3402"]


def test_generic_image_alt_text_is_not_worth_carrying():
    node = _post(text="tonight!! 🌈", accessibility_caption="May be an image of one person")
    posts = ig.normalize_posts(_connection(node), source_name="ig")
    assert "alt:" not in posts[0]["content"]


def test_message_shape_matches_the_message_standard():
    post = ig.normalize_posts(
        _connection(_post()), source_name="instagram", channel="instagram/@studsf",
    )[0]
    assert post["platform"] == "web"  # EH-STD-MSG-v0: never the source name
    assert post["channel"] == "instagram/@studsf"
    assert set(post) == {"platform", "id", "timestamp", "author", "channel", "content"}


# ── JSON / cursor plumbing ────────────────────────────────────────────


def test_loads_multi_reads_newline_delimited_chunks():
    """Deferred GraphQL arrives as several objects; json.loads drops all of them."""
    assert ig.loads_multi('{"a": 1}\n{"b": 2}') == [{"a": 1}, {"b": 2}]


def test_loads_multi_survives_garbage():
    assert ig.loads_multi("for(;;);") == []
    assert ig.loads_multi("") == []


def test_find_page_info_prefers_a_page_that_has_a_next():
    payload = {
        "a": {"page_info": {"has_next_page": False, "end_cursor": "done"}},
        "b": {"page_info": {"has_next_page": True, "end_cursor": "more"}},
    }
    assert ig.find_page_info(payload)["end_cursor"] == "more"


def test_find_page_info_reads_the_rest_flavoured_spelling():
    payload = {"items": [], "has_next_page": True, "next_max_id": "QVFB"}
    assert ig.find_page_info(payload) == {"end_cursor": "QVFB", "has_next_page": True}


# ── Query replay ──────────────────────────────────────────────────────


def test_with_cursor_keeps_every_session_parameter():
    """The whole point of recording: doc_id/lsd/jazoest ride along untouched."""
    body = ig.GraphQLQuery.with_cursor(_query(), "CURSOR9")
    from urllib.parse import parse_qs

    parsed = parse_qs(body)
    assert parsed["doc_id"] == ["9182736"]
    assert parsed["lsd"] == ["AVqbxyz"]
    assert parsed["jazoest"] == ["21055"]
    variables = json.loads(parsed["variables"][0])
    assert variables["after"] == "CURSOR9"
    assert variables["before"] is None
    assert variables["id"] == "77"  # untouched


def test_with_cursor_patches_a_nested_data_cursor_when_one_exists():
    from urllib.parse import parse_qs

    q = _query(body={"variables": json.dumps({"data": {"count": 12, "after": None}})})
    variables = json.loads(parse_qs(q.with_cursor("C2"))["variables"][0])
    assert variables["data"]["after"] == "C2"


def test_fetch_headers_drops_cookie_and_keeps_the_x_headers():
    headers = _query().fetch_headers()
    assert "cookie" not in {k.lower() for k in headers}
    assert headers["x-csrftoken"] == "tok"
    assert headers["content-type"] == "application/x-www-form-urlencoded"


def test_replay_returns_nothing_on_a_non_200():
    page = FakePage(responses=[{"status": 429, "text": "rate limited"}])
    assert ig.replay_query(page, _query(), "C1") == []


def test_recorder_tracks_cursors_and_forgets_exhausted_ones():
    rec = ig.QueryRecorder()
    rec.queries["Q"] = _query()
    rec.absorb("Q", _connection(_post(), cursor="C1"))
    assert rec.next_target()[1] == "C1"
    rec.absorb("Q", _connection(_post(pk="2"), cursor="C2", has_next=False))
    assert rec.next_target() is None


def test_paginate_walks_until_the_cursors_run_out():
    page = FakePage(responses=[
        {"status": 200, "text": json.dumps(_connection(_post(pk="2"), cursor="C2"))},
        {"status": 200, "text": json.dumps(_connection(_post(pk="3"), has_next=False))},
    ])
    rec = ig.QueryRecorder()
    rec.queries["PolarisProfilePostsQuery"] = _query()
    rec.absorb("PolarisProfilePostsQuery", _connection(_post(), cursor="C1"))

    assert ig.paginate(page, rec, max_pages=5, delay_ms=1) == 2
    assert len(page.evaluated) == 2
    posts = []
    for _name, payload in rec.payloads:
        posts.extend(ig.normalize_posts(payload, source_name="ig"))
    assert {p["id"] for p in posts} == {"ig:3400", "ig:2", "ig:3"}


def test_paginate_stops_when_a_replay_comes_back_empty():
    """An error page must end the walk, not restart it — the cursor is cleared first."""
    page = FakePage(responses=[{"status": 401, "text": "nope"}])
    rec = ig.QueryRecorder()
    rec.queries["PolarisProfilePostsQuery"] = _query()
    rec.absorb("PolarisProfilePostsQuery", _connection(_post(), cursor="C1"))

    assert ig.paginate(page, rec, max_pages=5, delay_ms=1) == 0
    assert len(page.evaluated) == 1


def test_paginate_respects_the_hard_cap():
    page = FakePage(responses=[
        {"status": 200, "text": json.dumps(_connection(_post(pk=str(i)), cursor=f"C{i}"))}
        for i in range(50)
    ])
    rec = ig.QueryRecorder()
    rec.queries["PolarisProfilePostsQuery"] = _query()
    rec.absorb("PolarisProfilePostsQuery", _connection(_post(), cursor="C0"))

    assert ig.paginate(page, rec, max_pages=99, delay_ms=1) == ig.MAX_PAGES_HARD_CAP


def test_recorder_captures_a_graphql_post_from_a_request():
    rec = ig.QueryRecorder()

    class FakeRequest:
        method = "POST"
        url = "https://www.instagram.com/graphql/query"
        headers = {"x-fb-friendly-name": "PolarisProfilePostsQuery"}
        post_data = "doc_id=123&variables=%7B%22first%22%3A12%7D&lsd=abc"

    rec._on_request(FakeRequest())
    assert rec.queries["PolarisProfilePostsQuery"].doc_id == "123"
    assert rec.queries["PolarisProfilePostsQuery"].variables() == {"first": 12}


def test_recorder_ignores_non_graphql_posts():
    rec = ig.QueryRecorder()

    class FakeRequest:
        method = "POST"
        url = "https://www.instagram.com/api/v1/web/telemetry/"
        headers: dict = {}
        post_data = "doc_id=1"

    rec._on_request(FakeRequest())
    assert rec.queries == {}


# ── Account harvesting ────────────────────────────────────────────────


def test_harvest_account_raises_on_a_login_wall():
    """Empty and logged-out must not look the same — only one is fixable by the user."""
    page = FakePage(password_field=True)
    with pytest.raises(ig.InstagramLoginRequired):
        ig.harvest_account(FakeUC(page), "studsf", wait_ms=0)
    assert page.closed


def test_harvest_account_closes_its_page_on_success():
    page = FakePage()
    posts = ig.harvest_account(FakeUC(page), "@studsf/", max_pages=0, wait_ms=0)
    assert posts == []
    assert page.closed


# ── Mode config ───────────────────────────────────────────────────────


def test_normalize_accounts_accepts_every_spelling():
    src = {"accounts": [
        "studsf",
        "@elriosf",
        "https://www.instagram.com/theoasissf/",
        {"handle": "@sfeagle", "name": "sf-eagle"},
        "STUDSF",  # duplicate, different case
    ]}
    assert _normalize_accounts(src) == [
        {"handle": "studsf", "name": "studsf"},
        {"handle": "elriosf", "name": "elriosf"},
        {"handle": "theoasissf", "name": "theoasissf"},
        {"handle": "sfeagle", "name": "sf-eagle"},
    ]


def test_normalize_accounts_accepts_a_bare_string():
    assert _normalize_accounts({"accounts": "studsf"}) == [{"handle": "studsf", "name": "studsf"}]


def test_normalize_accounts_of_nothing_is_nothing():
    assert _normalize_accounts({}) == []


def _msg(handle, ts, n):
    return {"id": f"ig:{handle}{n}", "channel": f"instagram/@{handle}", "timestamp": ts}


def test_interleave_gives_every_venue_its_newest_post_first():
    """max_events=30 against 20 venues: truncation would only ever show two."""
    per_account = [
        [_msg("a", "2026-08-05T10:00:00", 1), _msg("a", "2026-08-04T10:00:00", 2)],
        [_msg("b", "2026-08-03T10:00:00", 1), _msg("b", "2026-08-02T10:00:00", 2)],
        [_msg("c", "2026-08-01T10:00:00", 1)],
    ]
    got = [m["id"] for m in _interleave(per_account, limit=4)]
    assert got == ["ig:a1", "ig:b1", "ig:c1", "ig:a2"]


def test_interleave_sorts_newest_first_within_an_account():
    per_account = [[_msg("a", "2026-07-01T00:00:00", 1), _msg("a", "2026-08-01T00:00:00", 2)]]
    assert [m["id"] for m in _interleave(per_account, limit=2)] == ["ig:a2", "ig:a1"]


def test_interleave_of_nothing_is_nothing():
    assert _interleave([], limit=10) == []
    assert _interleave([[], []], limit=10) == []


# ── Mode, end to end (no browser) ─────────────────────────────────────


class ResponsePage(FakePage):
    """A page that fires the recorder's response handler when it navigates.

    Enough of Playwright's event surface to drive the whole chain — recorder
    attach → response capture → normalise → interleave — without a browser.
    """

    def __init__(self, payload_by_path: dict):
        super().__init__()
        self._payload_by_path = payload_by_path

    def goto(self, url, **_kw):
        self.url = url
        handle = url.rstrip("/").rsplit("/", 1)[-1]
        payload = self._payload_by_path.get(handle)
        if payload is None:
            return
        body = json.dumps(payload)

        class FakeRequest:
            method = "POST"
            headers = {"x-fb-friendly-name": "PolarisProfilePostsQuery"}
            post_data = "doc_id=1&variables=%7B%7D"

            def __init__(self, u):
                self.url = u

        class FakeResponse:
            headers = {"content-type": "application/json; charset=utf-8"}

            def __init__(self, u):
                self.url = u
                self.request = FakeRequest(u)

            def text(self):
                return body

        for handler in self.handlers.get("response", []):
            handler(FakeResponse("https://www.instagram.com/graphql/query"))


def test_do_instagram_walks_every_account_and_interleaves(monkeypatch):
    from event_harvester.sources.web_modes import instagram as mode

    monkeypatch.setattr(mode.time, "sleep", lambda _s: None)
    page = ResponsePage({
        "studsf": _connection(
            _post(pk="s1", text="Stud: Saturday drag, doors 9pm"),
            _post(pk="s2", text="Stud: Sunday tea dance, 4pm to 9pm"),
            has_next=False,
        ),
        "elriosf": _connection(
            _post(pk="e1", text="El Rio: Salsa Sunday on the patio, 3pm"),
            has_next=False,
        ),
    })
    src = {
        "url": "https://www.instagram.com/", "name": "instagram",
        "accounts": ["studsf", "elriosf"], "max_pages": 0,
    }
    msgs = mode._do_instagram_inner(FakeUC(page), src, max_events=10)

    assert [m["id"] for m in msgs] == ["ig:s1", "ig:e1", "ig:s2"]  # round-robin
    assert {m["channel"] for m in msgs} == {"instagram/@studsf", "instagram/@elriosf"}


def test_do_instagram_stops_the_whole_source_at_a_login_wall(monkeypatch):
    """One dead session should cost one profile load, not twenty."""
    from event_harvester.sources.web_modes import instagram as mode

    monkeypatch.setattr(mode.time, "sleep", lambda _s: None)
    opened: list[str] = []

    class WalledPage(FakePage):
        def __init__(self):
            super().__init__(password_field=True)

        def goto(self, url, **_kw):
            opened.append(url)
            self.url = url

    page = WalledPage()
    src = {"url": "https://www.instagram.com/", "accounts": ["a1", "a2", "a3"]}
    assert mode._do_instagram_inner(FakeUC(page), src, max_events=10) == []
    assert len(opened) == 1


def test_fetch_web_sources_routes_instagram_before_the_page_open(monkeypatch):
    """`instagram` mode drives its own pages; the shared open would be waste."""
    from event_harvester.sources import uc_browser as uc_module
    from event_harvester.sources import web_fetch

    class FakeUCBrowser:
        def __init__(self, **_kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def open(self, *_a, **_kw):
            raise AssertionError("instagram mode must not use the shared page-open")

    seen: dict = {}

    def fake_do_instagram(uc, src, max_events, **kwargs):
        seen["src"] = src
        seen["kwargs"] = kwargs
        return [{"id": "ig:1"}]

    monkeypatch.setattr(uc_module, "UCBrowser", FakeUCBrowser)
    monkeypatch.setattr(web_fetch, "_do_instagram", fake_do_instagram)
    monkeypatch.setattr(web_fetch, "_save_cooldown", lambda url: seen.setdefault("cooldown", url))

    src = {"url": "https://www.instagram.com/", "name": "instagram", "mode": "instagram"}
    out = web_fetch.fetch_web_sources([src], parallel=1)

    assert out == [{"id": "ig:1"}]
    assert seen["cooldown"] == "https://www.instagram.com/"
    # The debug-trace pair has to reach the mode, or --debug-web-trace would
    # produce an empty directory for this source and look like a dead run.
    assert set(seen["kwargs"]) == {"tracer", "capture"}


# ── Handle discovery ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "handle,names,expected_confident",
    [
        ("theoasissf", ("oasis", "sfoasis.com"), True),      # the guess would have been @sfoasis
        ("sfeagle", ("sf-eagle", "sf-eagle.com"), True),     # punctuation differs, brand doesn't
        ("driftwoodsf", ("driftwood", "driftwoodbarsf.com"), True),
        ("beauxdragbrunch", ("beaux", "beauxsf.com"), True),  # exactly at the threshold
        ("queencurveball", ("stud", "studsf.com"), False),    # a promoter the venue links to
        ("nudienubiessf", ("stud", "studsf.com"), False),
        ("pandulce_events", ("beaux", "beauxsf.com"), False),
    ],
)
def test_affinity_separates_the_venue_from_who_it_links_to(handle, names, expected_confident):
    cand = ig.HandleCandidate(handle=handle, affinity=ig.handle_affinity(handle, *names))
    assert cand.confident is expected_confident


def test_extract_handles_ignores_instagram_own_paths():
    html = """
      <a href="https://www.instagram.com/p/DQ12/">a post</a>
      <a href="https://instagram.com/explore/tags/sf/">tag</a>
      <img src="https://scontent.cdninstagram.com/t51.2885-15/x.jpg">
      <a href="https://instagram.com/studsf">us</a>
    """
    handles = [c.handle for c in ig.extract_handles(html, page_url="https://studsf.com")]
    assert handles == ["studsf"]


def test_extract_handles_ranks_by_affinity_not_by_count():
    """The venue's own account is often linked once; a promoter's, ten times."""
    html = ("<a href='https://instagram.com/beauxdragbrunch'>x</a>" * 18) + \
           "<a href='https://instagram.com/beauxsf'>us</a>"
    ranked = ig.extract_handles(html, page_url="https://beauxsf.com", names=("beaux",))
    assert ranked[0].handle == "beauxsf"
    assert ranked[0].count == 1


def test_best_handle_declines_rather_than_guesses(monkeypatch):
    monkeypatch.setattr(
        "event_harvester.sources.feed_probe._fetch_page",
        lambda url, timeout_s: "<a href='https://instagram.com/someotherparty'>x</a>",
    )
    assert ig.best_handle("https://hitopsbar.com", names=("hi-tops",)) is None


def test_best_handle_adopts_a_confident_match(monkeypatch):
    monkeypatch.setattr(
        "event_harvester.sources.feed_probe._fetch_page",
        lambda url, timeout_s: "<a href='https://instagram.com/rikkisbarsf'>us</a>",
    )
    best = ig.best_handle("https://www.rikkisbarsf.com", names=("rikkis",))
    assert best is not None and best.handle == "rikkisbarsf"


def test_discover_handles_returns_nothing_when_the_fetch_fails(monkeypatch):
    monkeypatch.setattr(
        "event_harvester.sources.feed_probe._fetch_page", lambda url, timeout_s: None,
    )
    assert ig.discover_handles("https://down.example") == []
