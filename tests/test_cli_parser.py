"""Tests for the CLI parser, helpers, and pre-dispatch normalization.

These tests construct the parser via build_parser() and call parse_args
on synthetic argument lists. They never execute the pipeline.
"""

import pytest

from event_harvester.cli.parse_helpers import (
    VALID_CAP_KEYS,
    VALID_PLATFORMS,
    apply_caps_to_config,
    parse_cap_arg,
    parse_platform_csv,
    resolve_platforms,
)
from event_harvester.cli.parser import build_parser

# ── parse_cap_arg ────────────────────────────────────────────────────


class TestParseCapArg:
    def test_single_pair(self):
        assert parse_cap_arg("discord=20") == {"discord": 20}

    def test_multiple_pairs(self):
        assert parse_cap_arg("discord=20,telegram=30") == {"discord": 20, "telegram": 30}

    def test_total_key(self):
        assert parse_cap_arg("total=150") == {"total": 150}

    def test_whitespace_tolerated(self):
        assert parse_cap_arg("discord = 20 , telegram=30") == {"discord": 20, "telegram": 30}

    def test_case_insensitive_keys(self):
        assert parse_cap_arg("Discord=20") == {"discord": 20}

    def test_empty_string_returns_empty(self):
        assert parse_cap_arg("") == {}

    def test_unknown_key_raises(self):
        import argparse
        with pytest.raises(argparse.ArgumentTypeError, match="unknown cap key"):
            parse_cap_arg("facebook=10")

    def test_non_int_raises(self):
        import argparse
        with pytest.raises(argparse.ArgumentTypeError, match="must be an integer"):
            parse_cap_arg("discord=abc")

    def test_negative_raises(self):
        import argparse
        with pytest.raises(argparse.ArgumentTypeError, match="must be >= 0"):
            parse_cap_arg("discord=-5")

    def test_missing_equals_raises(self):
        import argparse
        with pytest.raises(argparse.ArgumentTypeError, match="key=value"):
            parse_cap_arg("discord20")


# ── parse_platform_csv ───────────────────────────────────────────────


class TestParsePlatformCsv:
    def test_single(self):
        assert parse_platform_csv("discord") == {"discord"}

    def test_multiple(self):
        assert parse_platform_csv("discord,telegram") == {"discord", "telegram"}

    def test_whitespace_tolerated(self):
        assert parse_platform_csv("discord , telegram") == {"discord", "telegram"}

    def test_case_insensitive(self):
        assert parse_platform_csv("Discord,TELEGRAM") == {"discord", "telegram"}

    def test_unknown_raises(self):
        import argparse
        with pytest.raises(argparse.ArgumentTypeError, match="unknown platform"):
            parse_platform_csv("facebook")

    def test_partial_unknown_raises(self):
        import argparse
        with pytest.raises(argparse.ArgumentTypeError):
            parse_platform_csv("discord,facebook")

    def test_all_valid_platforms(self):
        assert parse_platform_csv("discord,telegram,gmail,signal,web") == set(VALID_PLATFORMS)


# ── resolve_platforms ────────────────────────────────────────────────


class TestResolvePlatforms:
    def test_no_filter(self):
        result = resolve_platforms(None, None)
        assert all(v is False for v in result.values())
        assert set(result.keys()) == {f"no_{p}" for p in VALID_PLATFORMS}

    def test_only_discord(self):
        result = resolve_platforms({"discord"}, None)
        assert result["no_discord"] is False
        assert result["no_telegram"] is True
        assert result["no_gmail"] is True
        assert result["no_signal"] is True
        assert result["no_web"] is True

    def test_skip_web(self):
        result = resolve_platforms(None, {"web"})
        assert result["no_web"] is True
        assert result["no_discord"] is False
        assert result["no_telegram"] is False

    def test_skip_multiple(self):
        result = resolve_platforms(None, {"web", "signal"})
        assert result["no_web"] is True
        assert result["no_signal"] is True
        assert result["no_discord"] is False


# ── apply_caps_to_config ─────────────────────────────────────────────


class TestApplyCapsToConfig:
    def test_empty_no_change(self):
        from event_harvester.config import CapConfig

        class _Cfg:
            caps = CapConfig()
        cfg = _Cfg()
        original = cfg.caps.discord
        apply_caps_to_config(cfg, [])
        assert cfg.caps.discord == original

    def test_single_dict(self):
        from event_harvester.config import CapConfig

        class _Cfg:
            caps = CapConfig()
        cfg = _Cfg()
        apply_caps_to_config(cfg, [{"discord": 10, "telegram": 20}])
        assert cfg.caps.discord == 10
        assert cfg.caps.telegram == 20

    def test_multiple_dicts_later_overrides(self):
        from event_harvester.config import CapConfig

        class _Cfg:
            caps = CapConfig()
        cfg = _Cfg()
        apply_caps_to_config(cfg, [{"discord": 10}, {"discord": 25, "total": 100}])
        assert cfg.caps.discord == 25
        assert cfg.caps.total == 100


# ── Parser routing ───────────────────────────────────────────────────


class TestParserRouting:
    def setup_method(self):
        self.parser = build_parser()

    def test_harvest_explicit(self):
        args = self.parser.parse_args(["harvest"])
        assert args.command == "harvest"

    def test_harvest_with_days(self):
        args = self.parser.parse_args(["harvest", "--days", "14"])
        assert args.command == "harvest"
        assert args.days == 14

    def test_watch(self):
        args = self.parser.parse_args(["watch", "--interval", "60"])
        assert args.command == "watch"
        assert args.interval == 60

    def test_web_list(self):
        args = self.parser.parse_args(["web", "list"])
        assert args.command == "web"
        assert args.web_command == "list"

    def test_web_add_with_url(self):
        args = self.parser.parse_args(["web", "add", "https://example.com"])
        assert args.command == "web"
        assert args.web_command == "add"
        assert args.url == "https://example.com"

    def test_web_test(self):
        args = self.parser.parse_args(["web", "test", "https://lu.ma/discover"])
        assert args.web_command == "test"
        assert args.url == "https://lu.ma/discover"

    def test_web_login(self):
        args = self.parser.parse_args(["web", "login"])
        assert args.web_command == "login"

    def test_recruiters_grade(self):
        args = self.parser.parse_args(["recruiters", "grade", "--auto-trash"])
        assert args.command == "recruiters"
        assert args.recruiters_command == "grade"
        assert args.auto_trash is True

    def test_recruiters_reparse(self):
        args = self.parser.parse_args(["recruiters", "reparse", "report.md"])
        assert args.recruiters_command == "reparse"
        assert args.file == "report.md"

    def test_classifier_train(self):
        args = self.parser.parse_args(["classifier", "train", "--out-labels", "labels.json"])
        assert args.command == "classifier"
        assert args.classifier_command == "train"
        assert args.out_labels == "labels.json"

    def test_classifier_eval_requires_labels(self):
        with pytest.raises(SystemExit):
            self.parser.parse_args(["classifier", "eval"])

    def test_classifier_eval(self):
        args = self.parser.parse_args(
            ["classifier", "eval", "--labels", "L.json", "--out-samples", "samples/"],
        )
        assert args.classifier_command == "eval"
        assert args.labels == "L.json"
        assert args.out_samples == "samples/"

    def test_serve(self):
        args = self.parser.parse_args(["serve"])
        assert args.command == "serve"

    def test_no_command_required(self):
        # Without normalization, bare invocation must fail
        with pytest.raises(SystemExit):
            self.parser.parse_args([])


# ── Cap parsing through argparse ─────────────────────────────────────


class TestParserCapFlag:
    def setup_method(self):
        self.parser = build_parser()

    def test_single_cap(self):
        args = self.parser.parse_args(["harvest", "--cap", "discord=20,telegram=30"])
        assert args.cap == [{"discord": 20, "telegram": 30}]

    def test_repeated_cap(self):
        args = self.parser.parse_args(
            ["harvest", "--cap", "total=100", "--cap", "web=10"],
        )
        assert args.cap == [{"total": 100}, {"web": 10}]

    def test_invalid_cap_key_exits(self):
        with pytest.raises(SystemExit):
            self.parser.parse_args(["harvest", "--cap", "bogus=5"])

    def test_non_int_cap_exits(self):
        with pytest.raises(SystemExit):
            self.parser.parse_args(["harvest", "--cap", "discord=abc"])

    def test_negative_cap_exits(self):
        with pytest.raises(SystemExit):
            self.parser.parse_args(["harvest", "--cap", "discord=-1"])


# ── Platform filter through argparse ─────────────────────────────────


class TestParserPlatformFilter:
    def setup_method(self):
        self.parser = build_parser()

    def test_only(self):
        args = self.parser.parse_args(["harvest", "--only", "discord,telegram"])
        assert args.only == {"discord", "telegram"}
        assert args.skip is None

    def test_skip(self):
        args = self.parser.parse_args(["harvest", "--skip", "web"])
        assert args.skip == {"web"}
        assert args.only is None

    def test_only_and_skip_mutex(self):
        with pytest.raises(SystemExit):
            self.parser.parse_args(["harvest", "--only", "discord", "--skip", "web"])

    def test_unknown_platform_exits(self):
        with pytest.raises(SystemExit):
            self.parser.parse_args(["harvest", "--only", "facebook"])


# ── Pre-dispatch normalization ───────────────────────────────────────


class TestNormalizeArgv:
    def setup_method(self):
        from event_harvester.cli.dispatch import _normalize_argv
        self.normalize = _normalize_argv

    def test_empty_to_harvest(self):
        assert self.normalize([]) == ["harvest"]

    def test_flags_only_inserts_harvest(self):
        assert self.normalize(["--days", "14"]) == ["harvest", "--days", "14"]

    def test_explicit_harvest_unchanged(self):
        assert self.normalize(["harvest", "--days", "14"]) == ["harvest", "--days", "14"]

    def test_watch_unchanged(self):
        assert self.normalize(["watch"]) == ["watch"]

    def test_help_unchanged(self):
        assert self.normalize(["--help"]) == ["--help"]
        assert self.normalize(["-h"]) == ["-h"]

    def test_version_unchanged(self):
        assert self.normalize(["--version"]) == ["--version"]

    def test_web_subcommand_unchanged(self):
        assert self.normalize(["web", "list"]) == ["web", "list"]


# ── Backward compat (post-normalization) ─────────────────────────────


class TestBackwardCompat:
    def setup_method(self):
        from event_harvester.cli.dispatch import _normalize_argv
        self.parser = build_parser()
        self.normalize = _normalize_argv

    def test_bare_with_days_works(self):
        argv = self.normalize(["--days", "14"])
        args = self.parser.parse_args(argv)
        assert args.command == "harvest"
        assert args.days == 14

    def test_bare_with_skip_works(self):
        argv = self.normalize(["--skip", "web"])
        args = self.parser.parse_args(argv)
        assert args.command == "harvest"
        assert args.skip == {"web"}

    def test_bare_and_explicit_equivalent(self):
        bare = self.parser.parse_args(self.normalize(["--days", "14", "--no-sync"]))
        explicit = self.parser.parse_args(["harvest", "--days", "14", "--no-sync"])
        assert bare.command == explicit.command
        assert bare.days == explicit.days
        assert bare.no_sync == explicit.no_sync


# ── Verbosity and version ────────────────────────────────────────────


class TestVerbosityAndVersion:
    def setup_method(self):
        self.parser = build_parser()

    def test_verbose(self):
        args = self.parser.parse_args(["harvest", "-v"])
        assert args.verbose is True
        assert args.quiet is False

    def test_quiet(self):
        args = self.parser.parse_args(["harvest", "-q"])
        assert args.verbose is False
        assert args.quiet is True

    def test_verbose_and_quiet_mutex(self):
        with pytest.raises(SystemExit):
            self.parser.parse_args(["harvest", "-v", "-q"])

    def test_version_exits(self):
        with pytest.raises(SystemExit):
            self.parser.parse_args(["--version"])


# ── Constants exported for use by command modules ────────────────────


class TestConstants:
    def test_valid_platforms_are_strings(self):
        assert all(isinstance(p, str) for p in VALID_PLATFORMS)
        assert "discord" in VALID_PLATFORMS
        assert "web" in VALID_PLATFORMS

    def test_valid_cap_keys_includes_total(self):
        assert "total" in VALID_CAP_KEYS
        assert VALID_PLATFORMS <= VALID_CAP_KEYS  # platforms ⊂ cap keys
