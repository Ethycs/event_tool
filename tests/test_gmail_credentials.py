"""Gmail credential resolution.

The bug these were written for: a stale ``GMAIL_TOKEN_JSON`` in ``.env`` and a
working ``token.json`` on disk. The env token parsed, which was enough to claim
the only credential slot, so the file was never consulted -- and when the env
token's refresh came back ``invalid_grant``, the whole source went dark while a
usable credential sat one line away. Gmail returned zero messages and logged a
warning that read like a network hiccup.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from event_harvester.config import GmailConfig, load_config
from event_harvester.sources import gmail


def _b64(payload: dict) -> str:
    return base64.b64encode(json.dumps(payload).encode()).decode()


class _FakeCreds:
    """Stand-in for google.oauth2.credentials.Credentials."""

    def __init__(self, *, valid=False, expired=True, refresh_token="r", refreshes=True, name="?"):
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token
        self._refreshes = refreshes
        self.name = name
        self.refresh_calls = 0

    def refresh(self, _request):
        self.refresh_calls += 1
        if not self._refreshes:
            raise RuntimeError("invalid_grant: Bad Request")
        self.valid = True
        self.expired = False

    def to_json(self):
        return json.dumps({"token": self.name})


@pytest.fixture
def cfg(tmp_path: Path) -> GmailConfig:
    return GmailConfig(
        credentials_file=str(tmp_path / "credentials.json"),
        token_file=str(tmp_path / "token.json"),
    )


class TestCredentialPrecedence:
    """A dead candidate must not mask a live one."""

    def test_dead_env_token_falls_through_to_a_working_file_token(self, cfg, monkeypatch):
        """The exact production failure: env token is stale, file token is fine."""
        monkeypatch.setenv("GMAIL_TOKEN_JSON", _b64({"token": "stale"}))
        Path(cfg.token_file).write_text(json.dumps({"token": "good"}))

        dead = _FakeCreds(refreshes=False, name="env")
        live = _FakeCreds(refreshes=True, name="file")

        with patch.object(gmail.Credentials, "from_authorized_user_info", return_value=dead), \
             patch.object(gmail.Credentials, "from_authorized_user_file", return_value=live), \
             patch.object(gmail, "InstalledAppFlow") as flow:
            got = gmail._get_credentials(cfg)

        assert got is live, "the working file token must be used"
        assert dead.refresh_calls == 1, "the env token should be tried first, and once"
        flow.assert_not_called(), "no browser flow while a stored token still works"

    def test_a_valid_env_token_wins_and_the_file_token_is_left_alone(self, cfg, monkeypatch):
        """Order still matters when both work.

        Both candidates are *parsed* up front — the file is read either way — but
        only the first usable one is returned, and the loser is never refreshed
        or written back.
        """
        monkeypatch.setenv("GMAIL_TOKEN_JSON", _b64({"token": "fresh"}))
        Path(cfg.token_file).write_text(json.dumps({"token": "also-fine"}))
        live = _FakeCreds(valid=True, expired=False, name="env")
        unused = _FakeCreds(valid=True, expired=False, name="file")

        with patch.object(gmail.Credentials, "from_authorized_user_info", return_value=live), \
             patch.object(gmail.Credentials, "from_authorized_user_file", return_value=unused):
            got = gmail._get_credentials(cfg)

        assert got is live
        assert unused.refresh_calls == 0
        assert json.loads(Path(cfg.token_file).read_text()) == {"token": "also-fine"}

    def test_a_refreshed_token_is_persisted(self, cfg, monkeypatch):
        monkeypatch.delenv("GMAIL_TOKEN_JSON", raising=False)
        Path(cfg.token_file).write_text(json.dumps({"token": "old"}))
        creds = _FakeCreds(refreshes=True, name="refreshed")

        with patch.object(gmail.Credentials, "from_authorized_user_file", return_value=creds):
            got = gmail._get_credentials(cfg)

        assert got is creds
        assert json.loads(Path(cfg.token_file).read_text()) == {"token": "refreshed"}

    def test_all_tokens_dead_and_no_tty_returns_none_instead_of_hanging(self, cfg, monkeypatch):
        """A browser consent prompt in a headless run is a hang, not a recovery."""
        monkeypatch.delenv("GMAIL_TOKEN_JSON", raising=False)
        monkeypatch.delenv("GMAIL_OAUTH_INTERACTIVE", raising=False)
        Path(cfg.token_file).write_text(json.dumps({"token": "dead"}))
        Path(cfg.credentials_file).write_text(json.dumps({"installed": {}}))
        dead = _FakeCreds(refreshes=False)

        with patch.object(gmail.Credentials, "from_authorized_user_file", return_value=dead), \
             patch.object(gmail.sys.stdin, "isatty", return_value=False), \
             patch.object(gmail, "InstalledAppFlow") as flow:
            got = gmail._get_credentials(cfg)

        assert got is None
        flow.from_client_secrets_file.assert_not_called()

    def test_the_override_re_enables_the_flow(self, cfg, monkeypatch):
        monkeypatch.delenv("GMAIL_TOKEN_JSON", raising=False)
        monkeypatch.setenv("GMAIL_OAUTH_INTERACTIVE", "1")
        Path(cfg.credentials_file).write_text(json.dumps({"installed": {}}))

        with patch.object(gmail.sys.stdin, "isatty", return_value=False), \
             patch.object(gmail, "InstalledAppFlow") as flow:
            flow.from_client_secrets_file.return_value.run_local_server.return_value = _FakeCreds(
                valid=True, expired=False, name="fresh",
            )
            got = gmail._get_credentials(cfg)

        assert got is not None
        flow.from_client_secrets_file.assert_called_once()


class TestQueryWindow:
    """`--days-back` must actually reach Gmail."""

    def test_the_default_query_no_longer_caps_the_window_at_seven_days(self):
        """`newer_than:7d` ANDed with `after:` won, so --days-back 30 saw 7."""
        assert "newer_than" not in GmailConfig().query

    def test_query_and_max_results_are_populated_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("GMAIL_QUERY", "label:events")
        monkeypatch.setenv("GMAIL_MAX_RESULTS", "42")
        cfg = load_config()
        assert cfg.gmail.query == "label:events"
        assert cfg.gmail.max_results == 42
