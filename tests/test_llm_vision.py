"""Tests for ``vision_completion`` in ``event_harvester.llm``."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from event_harvester.config import LLMConfig
from event_harvester.llm import vision_completion


def _mock_resp(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
    )


def _write_png(path: Path) -> Path:
    Image.new("RGB", (1, 1), color=(255, 0, 0)).save(path, "PNG")
    return path


def test_vision_completion_builds_content_list(tmp_path: Path) -> None:
    img1 = _write_png(tmp_path / "a.png")
    img2 = _write_png(tmp_path / "b.png")
    cfg = LLMConfig(api_key="sk-xxx")

    with patch("litellm.completion", return_value=_mock_resp("ok")) as mock_call:
        vision_completion("describe", [img1, img2], cfg)

    kwargs = mock_call.call_args.kwargs
    assert kwargs["messages"][0]["role"] == "user"
    content = kwargs["messages"][0]["content"]
    assert len(content) == 3
    assert content[0]["type"] == "image_url"
    assert content[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert content[2] == {"type": "text", "text": "describe"}


def test_vision_completion_skips_missing_files(
    tmp_path: Path, caplog
) -> None:
    img1 = _write_png(tmp_path / "real.png")
    missing = tmp_path / "ghost.png"
    cfg = LLMConfig(api_key="sk-xxx")

    with caplog.at_level(logging.WARNING, logger="event_harvester.llm"):
        with patch("litellm.completion", return_value=_mock_resp("ok")) as mock_call:
            vision_completion("p", [img1, missing], cfg)

    content = mock_call.call_args.kwargs["messages"][0]["content"]
    # 1 image (real) + 1 text item — missing image dropped
    assert len(content) == 2
    assert content[0]["type"] == "image_url"
    assert content[1]["type"] == "text"
    assert any("missing" in rec.message for rec in caplog.records)


def test_vision_completion_uses_vision_model_when_set(tmp_path: Path) -> None:
    img = _write_png(tmp_path / "a.png")
    cfg = LLMConfig(api_key="sk-xxx")
    # vision_model isn't on LLMConfig yet — set it dynamically to exercise
    # the getattr path.
    cfg.vision_model = "gpt-4o"  # type: ignore[attr-defined]

    with patch("litellm.completion", return_value=_mock_resp("ok")) as mock_call:
        vision_completion("p", [img], cfg)

    assert mock_call.call_args.kwargs["model"] == "gpt-4o"


def test_vision_completion_falls_back_to_claude_when_anthropic_key(
    tmp_path: Path,
) -> None:
    img = _write_png(tmp_path / "a.png")
    cfg = LLMConfig(api_key="sk-ant-xxxxx")
    cfg.vision_model = ""  # type: ignore[attr-defined]

    with patch("litellm.completion", return_value=_mock_resp("ok")) as mock_call:
        vision_completion("p", [img], cfg)

    assert mock_call.call_args.kwargs["model"] == "claude-sonnet-4-7"


def test_vision_completion_falls_back_to_openai_otherwise(tmp_path: Path) -> None:
    img = _write_png(tmp_path / "a.png")
    cfg = LLMConfig(api_key="sk-xxx")
    cfg.vision_model = ""  # type: ignore[attr-defined]

    with patch("litellm.completion", return_value=_mock_resp("ok")) as mock_call:
        vision_completion("p", [img], cfg)

    assert mock_call.call_args.kwargs["model"] == "gpt-4o-mini"


def test_vision_completion_returns_response_text(tmp_path: Path) -> None:
    img = _write_png(tmp_path / "a.png")
    cfg = LLMConfig(api_key="sk-xxx")

    with patch("litellm.completion", return_value=_mock_resp("hello")):
        result = vision_completion("p", [img], cfg)

    assert result == "hello"


def test_vision_completion_warns_on_large_batch(
    tmp_path: Path, caplog
) -> None:
    imgs = [_write_png(tmp_path / f"img{i}.png") for i in range(6)]
    cfg = LLMConfig(api_key="sk-xxx")

    with caplog.at_level(logging.WARNING, logger="event_harvester.llm"):
        with patch("litellm.completion", return_value=_mock_resp("ok")):
            vision_completion("p", imgs, cfg)

    assert any(">5" in rec.message or "6 images" in rec.message for rec in caplog.records)
