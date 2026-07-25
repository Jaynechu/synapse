"""Inbound sender whitelist: config fallback + filter construction.

filters.User (not filters.Chat) so an allowed sender is recognised anywhere
(private chat or group), never a real Bot/Application boot.
"""

from __future__ import annotations

from synapse_tg.__main__ import _whitelist_filter
from synapse_tg.config import TgConfig, load_config


def test_effective_ids_prefers_explicit_allowed_user_ids():
    cfg = TgConfig(allowed_user_ids=[111, 222], chat_id=999)
    assert cfg.effective_allowed_user_ids() == [111, 222]


def test_effective_ids_falls_back_to_chat_id():
    cfg = TgConfig(chat_id=999)
    assert cfg.effective_allowed_user_ids() == [999]


def test_effective_ids_empty_when_neither_set():
    cfg = TgConfig()
    assert cfg.effective_allowed_user_ids() == []


def test_config_parses_allowed_user_ids(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[tg]\nchat_id = 1\nallowed_user_ids = [1, 2, 3]\n")
    cfg = load_config(p)
    assert cfg.allowed_user_ids == [1, 2, 3]
    assert cfg.effective_allowed_user_ids() == [1, 2, 3]


def test_whitelist_filter_builds_user_filter_from_allowed_ids():
    cfg = TgConfig(allowed_user_ids=[42, 7])
    f = _whitelist_filter(cfg)
    assert f is not None
    assert f.user_ids == frozenset({42, 7})


def test_whitelist_filter_builds_from_chat_id_fallback():
    cfg = TgConfig(chat_id=555)
    f = _whitelist_filter(cfg)
    assert f is not None
    assert f.user_ids == frozenset({555})


def test_whitelist_filter_none_when_no_whitelist_configured():
    cfg = TgConfig()
    assert _whitelist_filter(cfg) is None
