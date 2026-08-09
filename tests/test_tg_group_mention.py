"""Group chat mention gate: config parsing, gate logic, reply routing."""

from __future__ import annotations

import asyncio
import time
import types

from synapse_tg.__main__ import _whitelist_filter
from synapse_tg.config import TgConfig, load_config
from synapse_tg.loop import TgLoop, _chat_meta, _passes_mention_gate


# ---------------------------------------------------------------------------
# Helpers: minimal fakes for Telegram objects
# ---------------------------------------------------------------------------

def _chat(type_="private", title=None, cid=-100123):
    return types.SimpleNamespace(type=type_, title=title, id=cid)


def _user(first_name="Alice", uid=42):
    return types.SimpleNamespace(first_name=first_name, id=uid)


def _msg(
    text=None,
    caption=None,
    chat_type="private",
    chat_id=-100123,
    from_uid=42,
    reply_from_uid=None,
    chat_title="TestGroup",
    forward_origin=None,
):
    chat = types.SimpleNamespace(type=chat_type, title=chat_title, id=chat_id)
    from_user = _user(uid=from_uid)
    reply_to = None
    if reply_from_uid is not None:
        reply_to = types.SimpleNamespace(
            from_user=types.SimpleNamespace(id=reply_from_uid)
        )
    return types.SimpleNamespace(
        text=text,
        caption=caption,
        chat=chat,
        chat_id=chat_id,
        from_user=from_user,
        reply_to_message=reply_to,
        forward_origin=forward_origin,
        message_id=1,
        date=None,
        sticker=None,
        photo=None,
        animation=None,
        document=None,
        video=None,
    )


class _FakeBot:
    def __init__(self, username="mybot", bot_id=999):
        self.username = username
        self.id = bot_id
        self.sent: list[dict] = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return types.SimpleNamespace(message_id=len(self.sent))

    async def send_chat_action(self, **_):
        pass


class _FakeContext:
    def __init__(self, bot):
        self.bot = bot


def _loop(tmp_path):
    cfg = TgConfig(
        data_dir=tmp_path / "tg-data",
        group_ids=[-100123],
        group_mention_keywords=["hey bot", "!ask"],
    )
    return TgLoop(cfg)


# ---------------------------------------------------------------------------
# 1. Config parsing
# ---------------------------------------------------------------------------

def test_config_parses_group_ids(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[tg]\ngroup_ids = [-100123, -100456]\n")
    cfg = load_config(p)
    assert cfg.group_ids == [-100123, -100456]


def test_config_parses_group_mention_keywords(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[tg]\ngroup_mention_keywords = ["hey bot", "!ask"]\n')
    cfg = load_config(p)
    assert cfg.group_mention_keywords == ["hey bot", "!ask"]


def test_config_group_ids_default_empty():
    cfg = TgConfig()
    assert cfg.group_ids == []
    assert cfg.group_mention_keywords == []


# ---------------------------------------------------------------------------
# 2. Outer filter: group_ids extends the gate
# ---------------------------------------------------------------------------

def test_whitelist_filter_includes_group_chat_filter():
    cfg = TgConfig(allowed_user_ids=[1], group_ids=[-100123])
    f = _whitelist_filter(cfg)
    assert f is not None


def test_whitelist_filter_group_only_no_user_whitelist():
    """group_ids set but no allowed_user_ids — filter admits group + private.
    When group_ids set but no user whitelist, returning the group filter alone
    silently blocks all private chats, so we OR in ChatType.PRIVATE."""
    cfg = TgConfig(group_ids=[-100123])
    f = _whitelist_filter(cfg)
    assert f is not None
    # Result is a combined OR filter (group | PRIVATE), not a bare Chat filter.
    assert not hasattr(f, "chat_ids"), (
        "filter should not be a bare Chat filter — private chats would be blocked"
    )


def test_whitelist_filter_none_when_neither_set():
    cfg = TgConfig()
    assert _whitelist_filter(cfg) is None


# ---------------------------------------------------------------------------
# 3. _passes_mention_gate: keyword / @mention / reply-to-bot / private passthrough
# ---------------------------------------------------------------------------

def test_gate_passes_private_message():
    msg = _msg(text="hello", chat_type="private")
    assert _passes_mention_gate(msg, "mybot", 999, ["hey bot"]) is True


def test_gate_passes_keyword_hit_text():
    msg = _msg(text="hey bot, what time is it?", chat_type="group")
    assert _passes_mention_gate(msg, "mybot", 999, ["hey bot"]) is True


def test_gate_passes_keyword_hit_caption():
    msg = _msg(caption="!ask something", chat_type="supergroup")
    assert _passes_mention_gate(msg, "mybot", 999, ["!ask"]) is True


def test_gate_passes_keyword_case_insensitive():
    msg = _msg(text="HEY BOT please help", chat_type="group")
    assert _passes_mention_gate(msg, "mybot", 999, ["hey bot"]) is True


def test_gate_blocks_keyword_miss():
    msg = _msg(text="just chatting here", chat_type="group")
    assert _passes_mention_gate(msg, "mybot", 999, ["hey bot", "!ask"]) is False


def test_gate_passes_at_mention():
    msg = _msg(text="@mybot please help", chat_type="group")
    assert _passes_mention_gate(msg, "mybot", 999, []) is True


def test_gate_passes_at_mention_case_insensitive():
    msg = _msg(text="@MyBot please help", chat_type="group")
    assert _passes_mention_gate(msg, "MYBOT", 999, []) is True


def test_gate_passes_reply_to_bot():
    msg = _msg(text="yes exactly", chat_type="group", reply_from_uid=999)
    assert _passes_mention_gate(msg, "mybot", 999, []) is True


def test_gate_blocks_reply_to_non_bot():
    msg = _msg(text="yes exactly", chat_type="group", reply_from_uid=42)
    assert _passes_mention_gate(msg, "mybot", 999, []) is False


def test_gate_blocks_no_keywords_no_mention_no_reply():
    msg = _msg(text="hey everyone", chat_type="group")
    assert _passes_mention_gate(msg, "mybot", 999, ["hey bot"]) is False


def test_gate_no_bot_username_still_checks_keywords():
    msg = _msg(text="!ask something", chat_type="group")
    assert _passes_mention_gate(msg, None, None, ["!ask"]) is True


# ---------------------------------------------------------------------------
# 4. Owner in group also goes through the gate (not exempted)
# ---------------------------------------------------------------------------

def test_owner_in_group_blocked_without_mention(tmp_path):
    """Even a whitelisted user's group messages are dropped if no mention hit."""
    loop = _loop(tmp_path)
    bot = _FakeBot()
    loop._bot = bot

    msg = _msg(
        text="just chatting in group",
        chat_type="group",
        chat_id=-100123,
        from_uid=1,  # owner / whitelisted
    )
    result = loop._check_group_gate(msg)
    assert result is False
    # No buffer created for this chat.
    assert -100123 not in loop._group_buffers


def test_owner_in_group_passes_with_keyword(tmp_path):
    loop = _loop(tmp_path)
    bot = _FakeBot()
    loop._bot = bot

    msg = _msg(
        text="!ask something",
        chat_type="group",
        chat_id=-100123,
        from_uid=1,
    )
    result = loop._check_group_gate(msg)
    assert result is True


# ---------------------------------------------------------------------------
# 5. _chat_meta includes numeric user id
# ---------------------------------------------------------------------------

def test_chat_meta_private_empty():
    msg = _msg(chat_type="private")
    assert _chat_meta(msg) == ""


def test_chat_meta_group_includes_uid():
    msg = _msg(chat_type="group", chat_title="MyGroup", from_uid=12345678, chat_id=-100123)
    meta = _chat_meta(msg)
    assert "12345678" in meta
    assert "MyGroup" in meta
    assert "群:" in meta


# ---------------------------------------------------------------------------
# 6. Group message does not rebind _pending_chat_id / state.chat_id
# ---------------------------------------------------------------------------

def test_group_message_does_not_rebind_pending_chat_id(tmp_path):
    loop = _loop(tmp_path)
    bot = _FakeBot()
    loop._pending_chat_id = 777  # private chat already bound

    loop._track(bot, -100123, is_group=True)

    assert loop._pending_chat_id == 777  # unchanged


def test_private_message_rebinds_pending_chat_id(tmp_path):
    loop = _loop(tmp_path)
    bot = _FakeBot()
    loop._pending_chat_id = 777

    loop._track(bot, 888, is_group=False)

    assert loop._pending_chat_id == 888


# ---------------------------------------------------------------------------
# 7. _check_group_gate returns correct verdict
# ---------------------------------------------------------------------------

def test_check_group_gate_passes_keyword(tmp_path):
    """Gate returns True for keyword hit."""
    loop = _loop(tmp_path)
    loop._bot = _FakeBot()
    msg = _msg(text="hey bot question", chat_type="group", chat_id=-100123)
    assert loop._check_group_gate(msg) is True


def test_check_group_gate_blocks_noise(tmp_path):
    """Gate returns False for unmatched group message; no buffer created."""
    loop = _loop(tmp_path)
    loop._bot = _FakeBot()
    msg = _msg(text="just noise", chat_type="group", chat_id=-100123)
    assert loop._check_group_gate(msg) is False
    assert -100123 not in loop._group_buffers


# ---------------------------------------------------------------------------
# 8. on_message: group message handling
# ---------------------------------------------------------------------------

def test_on_message_group_gated_out_does_not_buffer(tmp_path):
    loop = _loop(tmp_path)
    bot = _FakeBot()
    loop._bot = bot
    loop._pending_chat_id = 777

    msg = _msg(
        text="just noise in group",
        chat_type="group",
        chat_id=-100123,
    )
    update = types.SimpleNamespace(message=msg)
    ctx = _FakeContext(bot)

    asyncio.run(loop.on_message(update, ctx))

    assert len(loop._private_buffer) == 0
    assert not loop._group_buffers
    assert loop._pending_chat_id == 777  # unchanged


def test_on_message_group_keyword_hit_buffers_into_group_buffer(tmp_path):
    loop = _loop(tmp_path)
    bot = _FakeBot()
    loop._bot = bot
    loop._pending_chat_id = 777

    msg = _msg(
        text="!ask what is the weather?",
        chat_type="group",
        chat_id=-100123,
    )
    update = types.SimpleNamespace(message=msg)
    ctx = _FakeContext(bot)

    asyncio.run(loop.on_message(update, ctx))

    # message went into the group buffer, not the private buffer
    assert -100123 in loop._group_buffers
    assert len(loop._group_buffers[-100123]) > 0
    assert len(loop._private_buffer) == 0
    # private target unchanged
    assert loop._pending_chat_id == 777


def test_on_message_private_unaffected_by_group_gate(tmp_path):
    loop = _loop(tmp_path)
    bot = _FakeBot()
    loop._bot = bot

    msg = _msg(
        text="hello there",
        chat_type="private",
        chat_id=42,
    )
    update = types.SimpleNamespace(message=msg)
    ctx = _FakeContext(bot)

    asyncio.run(loop.on_message(update, ctx))

    assert len(loop._private_buffer) > 0
    assert loop._pending_chat_id == 42
    assert not loop._group_buffers  # no group buffer created


# ---------------------------------------------------------------------------
# 9. check_flush: group buffer routes to group chat_id
# ---------------------------------------------------------------------------

class _FakeSendable:
    """Minimal async bot that records send_message calls."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return types.SimpleNamespace(message_id=len(self.sent))

    async def send_chat_action(self, **_):
        pass


def test_check_flush_routes_group_buffer_to_group_chat(tmp_path):
    """Group messages buffered in _group_buffers must flush to the group chat_id,
    not the private chat. Per-origin buffers survive early ticks until ready."""
    from synapse_core.debounce import InboundBuffer

    tick = [0.0]
    clock = lambda: tick[0]  # noqa: E731

    loop = _loop(tmp_path)
    bot = _FakeSendable()
    loop._bot = bot
    loop._pending_chat_id = 777  # private chat — must NOT be used for group reply

    # Simulate a group message landed in the group buffer.
    gbuf = InboundBuffer(clock=clock)
    gbuf.add("!ask something")  # ts = 0.0
    loop._group_buffers[-100123] = gbuf

    ctx = _FakeContext(bot)

    # Tick 1: quiet window hasn't elapsed (time still 0.0, need >=5.0).
    asyncio.run(loop.check_flush(ctx))
    # Group buffer should still have content — not flushed yet.
    assert len(loop._group_buffers[-100123]) > 0, (
        "group buffer was flushed before quiet window elapsed"
    )
    assert len(bot.sent) == 0

    # Tick 2: advance clock past the quiet window so buffer.ready() is True.
    tick[0] = 6.0
    asyncio.run(loop.check_flush(ctx))

    # The reply must have gone to the group, not the private chat.
    assert len(bot.sent) > 0, "No message was sent on flush"
    for call in bot.sent:
        assert call.get("chat_id") == -100123, (
            f"Message delivered to {call.get('chat_id')!r} instead of group -100123"
        )


# ---------------------------------------------------------------------------
# 10. Afterglow window
# ---------------------------------------------------------------------------

def _fwd_msg(from_uid=42, chat_type="group", chat_id=-100123, forward_origin=None):
    """Minimal fake for a forwarded group message."""
    return types.SimpleNamespace(
        text="forwarded content",
        caption=None,
        chat=types.SimpleNamespace(type=chat_type, title="TestGroup", id=chat_id),
        chat_id=chat_id,
        from_user=types.SimpleNamespace(id=from_uid),
        reply_to_message=None,
        forward_origin=forward_origin,
        message_id=2,
        date=None,
        sticker=None,
        photo=None,
        animation=None,
        document=None,
        video=None,
    )


def test_afterglow_disabled_by_default():
    """group_afterglow_sec=0 must not pass any extra message."""
    msg = _msg(text="just noise", chat_type="group", chat_id=-100123)
    last_outbound = {-100123: time.monotonic()}  # just sent
    assert _passes_mention_gate(
        msg, "mybot", 999, [],
        afterglow_sec=0.0,
        last_outbound=last_outbound,
    ) is False


def test_afterglow_passes_within_window():
    """Message arrives while the afterglow window is still open."""
    msg = _msg(text="just noise", chat_type="group", chat_id=-100123)
    last_outbound = {-100123: time.monotonic() - 5.0}  # 5 s ago
    assert _passes_mention_gate(
        msg, "mybot", 999, [],
        afterglow_sec=60.0,
        last_outbound=last_outbound,
    ) is True


def test_afterglow_blocks_after_expiry():
    """Message arrives after the afterglow window has closed."""
    msg = _msg(text="just noise", chat_type="group", chat_id=-100123)
    last_outbound = {-100123: time.monotonic() - 120.0}  # 2 min ago
    assert _passes_mention_gate(
        msg, "mybot", 999, [],
        afterglow_sec=60.0,
        last_outbound=last_outbound,
    ) is False


def test_afterglow_no_prior_send():
    """Chat not in last_outbound at all — must not pass."""
    msg = _msg(text="just noise", chat_type="group", chat_id=-100123)
    assert _passes_mention_gate(
        msg, "mybot", 999, [],
        afterglow_sec=60.0,
        last_outbound={},
    ) is False


# ---------------------------------------------------------------------------
# 11. Forwarded-message allowlist
# ---------------------------------------------------------------------------

def test_forward_from_allowed_id_passes():
    """Forward from a user in the allowlist passes the gate."""
    sentinel = types.SimpleNamespace()  # truthy forward_origin
    msg = _fwd_msg(from_uid=111, forward_origin=sentinel)
    assert _passes_mention_gate(
        msg, "mybot", 999, [],
        forward_allow_ids=[111, 222],
    ) is True


def test_forward_from_non_allowed_id_blocked():
    """Forward from a user NOT in the allowlist is blocked."""
    sentinel = types.SimpleNamespace()
    msg = _fwd_msg(from_uid=999, forward_origin=sentinel)
    assert _passes_mention_gate(
        msg, "mybot", 999, [],
        forward_allow_ids=[111, 222],
    ) is False


def test_non_forward_from_allowed_id_blocked():
    """Regular (non-forwarded) message from an allowlisted user is NOT exempt."""
    msg = _fwd_msg(from_uid=111, forward_origin=None)
    assert _passes_mention_gate(
        msg, "mybot", 999, [],
        forward_allow_ids=[111, 222],
    ) is False


def test_forward_empty_allowlist_blocked():
    """Forward with an empty allowlist must not pass."""
    sentinel = types.SimpleNamespace()
    msg = _fwd_msg(from_uid=111, forward_origin=sentinel)
    assert _passes_mention_gate(
        msg, "mybot", 999, [],
        forward_allow_ids=[],
    ) is False


# ---------------------------------------------------------------------------
# 12. Config parsing for new keys
# ---------------------------------------------------------------------------

def test_config_parses_afterglow_sec(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[tg]\ngroup_afterglow_sec = 120\n")
    cfg = load_config(p)
    assert cfg.group_afterglow_sec == 120.0


def test_config_afterglow_default_zero():
    assert TgConfig().group_afterglow_sec == 0.0


def test_config_parses_forward_allow_ids(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[tg]\ngroup_forward_allow_ids = [111, 222]\n")
    cfg = load_config(p)
    assert cfg.group_forward_allow_ids == [111, 222]


def test_config_forward_allow_ids_default_empty():
    assert TgConfig().group_forward_allow_ids == []
