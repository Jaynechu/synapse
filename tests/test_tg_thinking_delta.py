"""Plaintext thinking via cc --include-partial-messages, tg side.

Under OAuth the final assistant `thinking` block is empty (signature-only).
The plaintext only arrives as live `stream_event` -> `content_block_delta` ->
`thinking_delta` chunks. _collect_turn must accumulate those into the
returned thinking string, and must not double-append from the (possibly
populated, under non-OAuth auth) final `thinking` block.

Mock at the provider boundary (recv yields dict events); never spawn claude.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from synapse_tg.config import TgConfig
from synapse_tg.loop import TgLoop


class FakeBot:
    async def send_message(self, **kwargs):
        return type("M", (), {"message_id": 1})()

    async def send_chat_action(self, **_):
        return None


class ScriptedProvider:
    """recv() yields the events of ONE turn per call."""

    def __init__(self, turns: list[list[dict]]) -> None:
        self._turns = list(turns)
        self.alive = True
        self.session_id = None
        self.turn_output_capped = False

    def recv(self, first_line=None):
        if not self._turns:
            return
        for ev in self._turns.pop(0):
            yield ev

    def send(self, msg):
        return None

    def is_alive(self):
        return True


class _NoTyping:
    running = True

    def start(self):
        pass

    def stop(self):
        pass


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    async def fake_sleep(_s):
        return None
    monkeypatch.setattr("synapse_tg.loop.asyncio.sleep", fake_sleep)


def _loop(tmp_path):
    cfg = TgConfig(data_dir=tmp_path / "tg-data")
    loop = TgLoop(cfg)
    loop._pending_chat_id = 123
    return loop


def _stream(loop, bot, provider, monkeypatch):
    loop._provider = provider
    monkeypatch.setattr(
        "synapse_tg.loop.split_for_tg_typed",
        lambda text: [{"kind": "text", "text": text}],
    )
    monkeypatch.setattr("synapse_tg.loop.gfm_to_tg_html", lambda t: t)
    return asyncio.run(loop._stream_response(bot, 123, _NoTyping()))


def _turn_with_thinking(deltas: list[str], *, final_thinking: str = "", sid="sid-x"):
    """One turn: init -> thinking_delta stream_events -> assistant frame
    (with a possibly-empty final thinking block, OAuth-shaped) -> result."""
    evs = [{"type": "system", "subtype": "init", "session_id": sid}]
    for d in deltas:
        evs.append({
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": d},
            },
        })
    evs.append({"type": "assistant", "message": {
        "content": [
            {"type": "thinking", "thinking": final_thinking, "signature": "sig=="},
            {"type": "text", "text": "reply text"},
        ],
        "usage": {"output_tokens": 1},
    }})
    evs.append({"type": "result", "result": "reply text"})
    return evs


def test_thinking_delta_collected_once_oauth_empty_final(tmp_path, monkeypatch):
    """OAuth shape: final thinking block is empty; deltas are the only source."""
    loop = _loop(tmp_path)
    bot = FakeBot()
    provider = ScriptedProvider([
        _turn_with_thinking(["let me think—", "ok, decided."], final_thinking=""),
    ])
    text, thinking = _stream(loop, bot, provider, monkeypatch)
    assert text == "reply text"
    assert thinking == "let me think—ok, decided."


def test_thinking_not_duplicated_when_final_block_populated(tmp_path, monkeypatch):
    """Non-OAuth shape: final thinking block carries the same plaintext as the
    deltas. The final block must be skipped, not appended a second time."""
    loop = _loop(tmp_path)
    bot = FakeBot()
    provider = ScriptedProvider([
        _turn_with_thinking(
            ["let me think—", "ok, decided."],
            final_thinking="let me think—ok, decided.",
        ),
    ])
    text, thinking = _stream(loop, bot, provider, monkeypatch)
    assert text == "reply text"
    assert thinking == "let me think—ok, decided."


def test_no_thinking_deltas_yields_empty_thinking(tmp_path, monkeypatch):
    loop = _loop(tmp_path)
    bot = FakeBot()
    provider = ScriptedProvider([_turn_with_thinking([], final_thinking="")])
    text, thinking = _stream(loop, bot, provider, monkeypatch)
    assert text == "reply text"
    assert thinking == ""
