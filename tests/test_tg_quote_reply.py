"""TG quote-tag delivery: every <quote>FRAGMENT</quote> is stripped and turns
into a real Telegram reply (reply_to_message_id) on the first bubble of the
text that follows it. Multiple tags per turn are position-aware.

Mock at the bot boundary — never spawn a real bot or provider.
"""

from __future__ import annotations

import asyncio

import pytest

from synapse_tg.config import TgConfig
from synapse_tg.loop import TgLoop


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return type("M", (), {"message_id": 900 + len(self.sent)})()

    async def send_chat_action(self, **_):
        return None


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    async def fake_sleep(_s):
        return None
    monkeypatch.setattr("synapse_tg.loop.asyncio.sleep", fake_sleep)


@pytest.fixture(autouse=True)
def passthrough_render(monkeypatch):
    monkeypatch.setattr("synapse_tg.loop.gfm_to_tg_html", lambda t: t)


def _loop(tmp_path, cache: dict[int, str] | None = None) -> TgLoop:
    (tmp_path / "config.toml").write_text('[cortex]\nshells = ["cli", "tg"]\n')
    cfg = TgConfig(data_dir=tmp_path / "tg-data",
                   marrow_db=str(tmp_path / "marrow.db"))
    loop = TgLoop(cfg)
    for msg_id, text in (cache or {}).items():
        loop._msg_id_cache[msg_id] = text
    return loop


def _deliver(loop, bot, response: str) -> None:
    asyncio.run(loop._deliver_reply(bot, 123, response, ""))


def _texts(bot) -> list[str]:
    return [m["text"] for m in bot.sent]


def _replies(bot) -> list[int | None]:
    return [m.get("reply_to_message_id") for m in bot.sent]


def _no_literal_tags(bot) -> bool:
    return all(
        "<quote>" not in m["text"] and "</quote>" not in m["text"]
        for m in bot.sent
    )


# ── single quote (regression) ────────────────────────────────────────

def test_single_quote_attaches_to_first_bubble(tmp_path):
    loop = _loop(tmp_path, {11: "how are you"})
    bot = FakeBot()
    _deliver(loop, bot, "<quote>how are you</quote>doing great\n\nyou?")
    assert _texts(bot) == ["doing great", "you?"]
    assert _replies(bot) == [11, None]
    assert _no_literal_tags(bot)


def test_single_quote_unresolved_strips_without_reply(tmp_path):
    loop = _loop(tmp_path, {11: "something else"})
    bot = FakeBot()
    _deliver(loop, bot, "<quote>never said this</quote>body")
    assert _texts(bot) == ["body"]
    assert _replies(bot) == [None]
    assert _no_literal_tags(bot)


def test_quote_newest_match_wins(tmp_path):
    loop = _loop(tmp_path, {11: "ping", 12: "ping again"})
    bot = FakeBot()
    _deliver(loop, bot, "<quote>ping</quote>pong")
    assert _replies(bot) == [12]


def test_quote_match_is_case_insensitive_on_fragment(tmp_path):
    loop = _loop(tmp_path, {11: "Hello There"})
    bot = FakeBot()
    _deliver(loop, bot, "<quote>hello there</quote>hi")
    assert _replies(bot) == [11]


def test_uppercase_tag_is_stripped(tmp_path):
    loop = _loop(tmp_path, {11: "ref"})
    bot = FakeBot()
    _deliver(loop, bot, "<QUOTE>ref</QUOTE>body")
    assert _texts(bot) == ["body"]
    assert _replies(bot) == [11]


def test_no_quote_tag_unchanged(tmp_path):
    loop = _loop(tmp_path, {11: "hi"})
    bot = FakeBot()
    _deliver(loop, bot, "plain one\n\nplain two")
    assert _texts(bot) == ["plain one", "plain two"]
    assert _replies(bot) == [None, None]


# ── multi quote ──────────────────────────────────────────────────────

def test_two_quotes_each_attach_to_their_own_segment(tmp_path):
    loop = _loop(tmp_path, {11: "first question", 12: "second question"})
    bot = FakeBot()
    _deliver(
        loop, bot,
        "<quote>first question</quote>answer one\n\n"
        "<quote>second question</quote>answer two",
    )
    assert _texts(bot) == ["answer one", "answer two"]
    assert _replies(bot) == [11, 12]
    assert _no_literal_tags(bot)


def test_second_quote_unresolvable_strips_without_leak(tmp_path):
    loop = _loop(tmp_path, {11: "first question"})
    bot = FakeBot()
    _deliver(
        loop, bot,
        "<quote>first question</quote>answer one\n\n"
        "<quote>ghost fragment</quote>answer two",
    )
    assert _texts(bot) == ["answer one", "answer two"]
    assert _replies(bot) == [11, None]
    assert _no_literal_tags(bot)


def test_leading_text_before_first_quote_gets_no_reply(tmp_path):
    loop = _loop(tmp_path, {11: "the ref"})
    bot = FakeBot()
    _deliver(loop, bot, "intro line\n\n<quote>the ref</quote>tail line")
    assert _texts(bot) == ["intro line", "tail line"]
    assert _replies(bot) == [None, 11]


def test_multi_bubble_segment_only_first_bubble_replies(tmp_path):
    loop = _loop(tmp_path, {11: "ref one", 12: "ref two"})
    bot = FakeBot()
    _deliver(
        loop, bot,
        "<quote>ref one</quote>a1\n\na2\n\n<quote>ref two</quote>b1\n\nb2",
    )
    assert _texts(bot) == ["a1", "a2", "b1", "b2"]
    assert _replies(bot) == [11, None, 12, None]


def test_three_quotes_positionally_mapped(tmp_path):
    loop = _loop(tmp_path, {11: "one", 12: "two", 13: "three"})
    bot = FakeBot()
    _deliver(
        loop, bot,
        "<quote>one</quote>A\n\n<quote>two</quote>B\n\n<quote>three</quote>C",
    )
    assert _texts(bot) == ["A", "B", "C"]
    assert _replies(bot) == [11, 12, 13]


def test_multiline_quote_body_matches(tmp_path):
    loop = _loop(tmp_path, {11: "long inbound message here"})
    bot = FakeBot()
    _deliver(loop, bot, "<quote>long inbound\nmessage here\n</quote>ok")
    assert _texts(bot) == ["ok"]
    assert _no_literal_tags(bot)


# ── degenerate placements ────────────────────────────────────────────

def test_quote_at_end_of_response_is_just_stripped(tmp_path):
    loop = _loop(tmp_path, {11: "the ref"})
    bot = FakeBot()
    _deliver(loop, bot, "body text\n<quote>the ref</quote>")
    assert _texts(bot) == ["body text"]
    assert _replies(bot) == [None]
    assert _no_literal_tags(bot)


def test_empty_fragment_strips_and_attaches_nothing(tmp_path):
    loop = _loop(tmp_path, {11: "hi"})
    bot = FakeBot()
    _deliver(loop, bot, "<quote></quote>body")
    assert _texts(bot) == ["body"]
    assert _replies(bot) == [None]
    assert _no_literal_tags(bot)


def test_response_that_is_only_a_quote_sends_nothing(tmp_path):
    loop = _loop(tmp_path, {11: "the ref"})
    bot = FakeBot()
    _deliver(loop, bot, "<quote>the ref</quote>")
    assert bot.sent == []


def test_back_to_back_quotes_last_one_wins_the_bubble(tmp_path):
    loop = _loop(tmp_path, {11: "one", 12: "two"})
    bot = FakeBot()
    _deliver(loop, bot, "<quote>one</quote><quote>two</quote>body")
    assert _texts(bot) == ["body"]
    assert _replies(bot) == [12]
    assert _no_literal_tags(bot)


# ── media bubbles keep carrying reply_to ─────────────────────────────

def test_quote_before_media_bubble_passes_reply_to(tmp_path, monkeypatch):
    calls: list[dict] = []

    async def fake_send_media(bot, chat_id, kind, path, *, reply_to=None, **kw):
        calls.append({"kind": kind, "path": path, "reply_to": reply_to})
        return True

    monkeypatch.setattr("synapse_tg.loop.send_media", fake_send_media)
    loop = _loop(tmp_path, {11: "show me"})
    bot = FakeBot()
    _deliver(loop, bot, '<quote>show me</quote><image path="/tmp/a.png"/>')
    assert calls == [{"kind": "image", "path": "/tmp/a.png", "reply_to": 11}]
    assert bot.sent == []
