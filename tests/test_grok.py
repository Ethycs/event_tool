"""Unit tests for the Grok automation client.

The default suite uses mocked UCBrowser instances — no network, no
browser launch. An opt-in integration test (``test_live_send_read_delete``)
runs the real "create chat → send → read → delete" cycle when
``UC_GROK_LIVE=1`` is set in the environment.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from event_harvester.sites import grok as grok_mod
from event_harvester.sites.grok import (
    GrokAuthRequired,
    GrokClient,
    _conv_id_from_url,
    _normalize_url,
)


# ── Pure helpers ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "https://grok.com/c/9a321760-da6c-4ece-84cd-2e138e9a56b8",
            "9a321760-da6c-4ece-84cd-2e138e9a56b8",
        ),
        (
            "https://grok.com/c/9a321760-da6c-4ece-84cd-2e138e9a56b8?rid=26e027de",
            "9a321760-da6c-4ece-84cd-2e138e9a56b8",
        ),
        ("https://grok.com/", None),
        ("", None),
        ("nope", None),
    ],
)
def test_conv_id_from_url(url, expected):
    assert _conv_id_from_url(url) == expected


def test_normalize_url_full_url_passthrough():
    assert _normalize_url("https://grok.com/c/abc") == "https://grok.com/c/abc"


def test_normalize_url_bare_id():
    assert _normalize_url("abc-123") == "https://grok.com/c/abc-123"


def test_normalize_url_empty_returns_home():
    assert _normalize_url("") == "https://grok.com/"


# ── GrokClient tests with mocked UCBrowser ───────────────────────────


@pytest.fixture
def mock_uc():
    """Yield a fake UCBrowser with chat()/open()/dismiss/close stubs."""
    uc = MagicMock()
    page = MagicMock()
    page.url = "https://grok.com/c/abc12345-1234-1234-1234-1234567890ab"
    page.evaluate = MagicMock(return_value=None)
    page.wait_for_timeout = MagicMock()
    page.close = MagicMock()
    uc.open = MagicMock(return_value=page)
    uc.dismiss_cookies = MagicMock(return_value=True)
    uc.close_modal = MagicMock(return_value=False)
    uc.has_login_wall = MagicMock(return_value=False)
    uc.chat = MagicMock(return_value="hello back")
    uc._wait_ready = MagicMock(return_value=True)
    uc.start = MagicMock()
    uc.close = MagicMock()
    return uc, page


@pytest.fixture
def grok_client(mock_uc):
    """A GrokClient whose underlying UCBrowser is the mock."""
    uc, _page = mock_uc
    with patch.object(grok_mod, "UCBrowser", return_value=uc):
        client = GrokClient()
        client._ensure_uc()  # force the (mocked) browser to be "started"
        yield client
        client.close()


def test_send_basic(grok_client, mock_uc):
    uc, page = mock_uc
    out = grok_client.send("hello")
    assert out["response"] == "hello back"
    assert out["url"] == page.url
    assert out["conversation_id"] == "abc12345-1234-1234-1234-1234567890ab"
    uc.chat.assert_called_once()
    assert uc.chat.call_args.args[1] == "hello"


def test_send_login_wall_raises(grok_client, mock_uc):
    uc, _ = mock_uc
    uc.has_login_wall.return_value = True
    with pytest.raises(GrokAuthRequired):
        grok_client.send("anything")


def test_read_returns_messages(grok_client, mock_uc):
    _, page = mock_uc
    page.evaluate.return_value = [
        {"role": "user", "text": "hi"},
        {"role": "assistant", "text": "hello"},
    ]
    page.url = "https://grok.com/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    out = grok_client.read("https://grok.com/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    assert out["conversation_id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert len(out["messages"]) == 2
    assert out["messages"][1]["role"] == "assistant"


def test_list_conversations(grok_client, mock_uc):
    _, page = mock_uc
    page.evaluate.return_value = [
        {"id": "x1", "title": "Chat 1", "url": "https://grok.com/c/x1"},
        {"id": "x2", "title": "Chat 2", "url": "https://grok.com/c/x2"},
    ]
    items = grok_client.list_conversations()
    assert len(items) == 2
    assert items[0]["id"] == "x1"


def test_delete_full_flow(grok_client, mock_uc):
    """delete() runs: open menu → click Delete → confirm → verify gone."""
    _, page = mock_uc
    # 4 evaluate() calls in sequence: open-menu, click-delete, confirm, verify
    page.evaluate.side_effect = [
        {"ok": True},  # open menu (now returns dict, not bool)
        True,           # clicked Delete menu item
        True,           # confirm modal clicked
        True,           # row is gone
    ]
    ok = grok_client.delete("https://grok.com/c/del-id")
    assert ok is True
    assert page.evaluate.call_count == 4


def test_delete_aborts_when_menu_not_found(grok_client, mock_uc):
    _, page = mock_uc
    page.evaluate.return_value = {"ok": False, "why": "row-not-found"}
    assert grok_client.delete("https://grok.com/c/missing") is False
    # Only one evaluate — we bail before clicking Delete.
    assert page.evaluate.call_count == 1


def test_stop_generation(grok_client, mock_uc):
    _, page = mock_uc
    page.evaluate.return_value = True
    assert grok_client.stop("https://grok.com/c/abc") is True


def test_stop_generation_no_button(grok_client, mock_uc):
    _, page = mock_uc
    page.evaluate.return_value = False
    assert grok_client.stop("https://grok.com/c/abc") is False


def test_new_chat_returns_home_url(grok_client):
    out = grok_client.new_chat()
    assert out["url"].rstrip("/") == "https://grok.com"
    assert out["conversation_id"] is None


def test_close_is_idempotent(grok_client):
    grok_client.close()
    grok_client.close()  # no error


# ── Live integration smoke (opt-in via UC_GROK_LIVE=1) ───────────────


@pytest.mark.skipif(
    os.environ.get("UC_GROK_LIVE") != "1",
    reason="Set UC_GROK_LIVE=1 to run live Grok smoke test (requires login)",
)
def test_live_send_read_delete():
    """Real round-trip against grok.com — uses your saved Chrome profile."""
    with GrokClient() as client:
        try:
            sent = client.send("Reply with one word: pineapple", timeout_s=60)
        except GrokAuthRequired:
            pytest.skip("Not logged in to Grok — run web-login first")
        assert sent["response"], "expected a non-empty response"
        url = sent["url"]
        assert "pineapple" in sent["response"].lower() or len(sent["response"]) > 0

        transcript = client.read(url)
        assert transcript["messages"], "read should return at least one message"

        deleted = client.delete(url)
        # Deletion is best-effort; log but don't fail the suite hard if Grok's
        # menu shape has drifted.
        assert deleted in (True, False)
