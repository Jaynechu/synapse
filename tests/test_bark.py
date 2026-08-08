"""Tests for Bark push notification helper and config parsing."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from synapse_tg.bark import push
from synapse_tg.config import TgConfig, load_config


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


def test_bark_config_defaults():
    cfg = TgConfig()
    assert cfg.bark_push_url == ""
    assert cfg.bark_icon == ""
    assert cfg.bark_max_chars == 150


def test_bark_config_parsed(tmp_path: Path):
    p = tmp_path / "config.toml"
    p.write_text(
        "[bark]\n"
        'push_url = "https://api.day.app/TESTKEY"\n'
        'icon = "https://example.com/icon.jpg"\n'
        "max_chars = 80\n"
    )
    cfg = load_config(p)
    assert cfg.bark_push_url == "https://api.day.app/TESTKEY"
    assert cfg.bark_icon == "https://example.com/icon.jpg"
    assert cfg.bark_max_chars == 80


def test_bark_config_absent(tmp_path: Path):
    p = tmp_path / "config.toml"
    p.write_text("[bot]\ntoken = 'x'\n")
    cfg = load_config(p)
    assert cfg.bark_push_url == ""
    assert cfg.bark_icon == ""
    assert cfg.bark_max_chars == 150


def test_bark_config_partial(tmp_path: Path):
    p = tmp_path / "config.toml"
    p.write_text('[bark]\npush_url = "https://api.day.app/XYZ"\n')
    cfg = load_config(p)
    assert cfg.bark_push_url == "https://api.day.app/XYZ"
    assert cfg.bark_icon == ""
    assert cfg.bark_max_chars == 150


# ---------------------------------------------------------------------------
# push() behaviour
# ---------------------------------------------------------------------------


def test_push_noop_when_unconfigured():
    cfg = TgConfig()
    # Should return immediately without making any HTTP call.
    with patch("synapse_tg.bark.httpx") as mock_httpx:
        asyncio.run(push(cfg, "title", "body"))
    mock_httpx.AsyncClient.assert_not_called()


def test_push_truncates_body():
    cfg = TgConfig(
        bark_push_url="https://api.day.app/KEY",
        bark_max_chars=10,
    )
    captured: list[dict] = []

    async def fake_post(url, *, json):
        captured.append(json)
        resp = MagicMock()
        resp.status_code = 200
        return resp

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = fake_post

    with patch("synapse_tg.bark.httpx.AsyncClient", return_value=mock_client):
        asyncio.run(push(cfg, "t", "a" * 20))

    assert len(captured) == 1
    body = captured[0]["body"]
    assert body.endswith("…")
    assert len(body) == 11  # 10 chars + ellipsis


def test_push_payload_shape_with_icon():
    cfg = TgConfig(
        bark_push_url="https://api.day.app/KEY",
        bark_icon="https://example.com/icon.png",
        bark_max_chars=150,
    )
    captured: list[dict] = []

    async def fake_post(url, *, json):
        captured.append(json)
        resp = MagicMock()
        resp.status_code = 200
        return resp

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = fake_post

    with patch("synapse_tg.bark.httpx.AsyncClient", return_value=mock_client):
        asyncio.run(push(cfg, "hello", "world"))

    assert captured == [{"title": "hello", "body": "world", "icon": "https://example.com/icon.png"}]


def test_push_payload_omits_icon_when_empty():
    cfg = TgConfig(
        bark_push_url="https://api.day.app/KEY",
        bark_icon="",
    )
    captured: list[dict] = []

    async def fake_post(url, *, json):
        captured.append(json)
        resp = MagicMock()
        resp.status_code = 200
        return resp

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = fake_post

    with patch("synapse_tg.bark.httpx.AsyncClient", return_value=mock_client):
        asyncio.run(push(cfg, "t", "b"))

    assert "icon" not in captured[0]


def test_push_never_raises_on_http_error():
    cfg = TgConfig(bark_push_url="https://api.day.app/KEY")

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(side_effect=RuntimeError("network down"))

    with patch("synapse_tg.bark.httpx.AsyncClient", return_value=mock_client):
        # Must not raise.
        asyncio.run(push(cfg, "t", "b"))


def test_push_never_raises_on_non_200():
    cfg = TgConfig(bark_push_url="https://api.day.app/KEY")

    resp = MagicMock()
    resp.status_code = 503
    resp.text = "service unavailable"

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=resp)

    with patch("synapse_tg.bark.httpx.AsyncClient", return_value=mock_client):
        asyncio.run(push(cfg, "t", "b"))
