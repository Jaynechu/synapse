"""B6 — /resume tri-mode + replay integration.

Layered on top of `test_resume.py` (which already covers the B1 sid path and
the bare picker). This module focuses on:

- `/resume <sid>` invokes `replay_for_sid(sid)` and pushes those bubbles via
  `send_extra_bubbles(...)` BEFORE swapping the provider so the user sees the
  `[回放]` lines arrive ahead of the "Resumed ..." ack.
- `/resume N` digit path also routes through replay + bubbles.
- `/resume` empty path does NOT trigger replay (just lists the picker).
"""

from __future__ import annotations

from synapse_core.commands.registry import CommandContext, Registry
from synapse_core.state import BridgeState


class _Hooks:
    """Capture every closure invocation for assertion."""

    def __init__(self) -> None:
        self.swap_calls: list[tuple[str | None, str | None]] = []
        self.forget_calls = 0
        self.replay_calls: list[str] = []
        self.bubble_pushes: list[list[str]] = []
        self.action_log: list[str] = []  # ordering audit: "replay" / "send" / "swap"

    def swap(self, model: str | None, sid: str | None) -> None:
        self.action_log.append("swap")
        self.swap_calls.append((model, sid))

    def close(self) -> None:
        pass

    def forget(self) -> None:
        self.forget_calls += 1

    def replay_for_sid(self, sid: str) -> list[str]:
        self.action_log.append("replay")
        self.replay_calls.append(sid)
        return [f"[回放] user: hi {sid[:4]}", f"[回放] assistant: ack {sid[:4]}"]

    def send_extra_bubbles(self, bubbles: list[str]) -> None:
        self.action_log.append("send")
        self.bubble_pushes.append(list(bubbles))


def _make(state: BridgeState, **overrides) -> tuple[Registry, _Hooks]:
    hooks = _Hooks()
    ctx = CommandContext(
        state=state,
        swap_provider=hooks.swap,
        close_provider=hooks.close,
        forget_session=hooks.forget,
        replay_for_sid=hooks.replay_for_sid,
        send_extra_bubbles=hooks.send_extra_bubbles,
        **overrides,
    )
    return Registry(ctx), hooks


def test_resume_sid_runs_replay_before_swap() -> None:
    state = BridgeState(model="claude-opus-4-7")
    reg, hooks = _make(
        state, resolve_resume_model=lambda _sid: "claude-opus-4-8[1m]"
    )
    verdict, reply = reg.dispatch("/resume abcdef123456")

    assert verdict == "handled"
    assert hooks.replay_calls == ["abcdef123456"]
    assert hooks.bubble_pushes == [
        ["[回放] user: hi abcd", "[回放] assistant: ack abcd"]
    ]
    # Order: replay read → bubbles emitted → provider swap.
    assert hooks.action_log == ["replay", "send", "swap"]
    assert hooks.swap_calls == [("claude-opus-4-8[1m]", "abcdef123456")]
    # The ack reply still mentions the resumed sid + model.
    assert "abcdef12" in (reply or "")
    assert "Opus 4.8" in (reply or "")


def test_resume_digit_picks_nth_and_replays() -> None:
    state = BridgeState()
    rows = [
        {"sid": "first-sid", "model": "claude-opus-4-6[1m]", "channel": "wx",
         "last_active": "x", "title": ""},
        {"sid": "second-sid-aaaa", "model": "claude-sonnet-4-6", "channel": "cli",
         "last_active": "x", "title": ""},
    ]
    reg, hooks = _make(
        state,
        list_recent_sessions=lambda: rows,
        resolve_resume_model=lambda sid: {"first-sid": "claude-opus-4-6[1m]",
                                          "second-sid-aaaa": "claude-sonnet-4-6"}[sid],
    )
    verdict, _ = reg.dispatch("/resume 2")
    assert verdict == "handled"
    assert hooks.replay_calls == ["second-sid-aaaa"]
    assert hooks.bubble_pushes == [
        ["[回放] user: hi seco", "[回放] assistant: ack seco"]
    ]
    assert hooks.action_log == ["replay", "send", "swap"]
    assert hooks.swap_calls == [("claude-sonnet-4-6", "second-sid-aaaa")]


def test_resume_empty_does_not_replay() -> None:
    """Empty arg renders the picker; replay only fires after a sid is chosen."""
    state = BridgeState()
    rows = [
        {"sid": "first-sid", "model": "claude-opus-4-6[1m]", "channel": "wx",
         "last_active": "2026-06-02T19:00:00Z", "title": "testuser"},
    ]
    reg, hooks = _make(state, list_recent_sessions=lambda: rows)
    verdict, reply = reg.dispatch("/resume")
    assert verdict == "handled"
    assert "Recent sessions:" in (reply or "")
    assert hooks.replay_calls == []
    assert hooks.bubble_pushes == []
    assert hooks.swap_calls == []


def test_resume_sid_with_empty_replay_skips_bubble_push() -> None:
    """No-jsonl / empty-replay path: no bubbles pushed, ack still goes out."""
    state = BridgeState()

    log: list[str] = []

    def swap(model: str | None, sid: str | None) -> None:
        log.append("swap")

    def replay(sid: str) -> list[str]:
        log.append("replay")
        return []

    def push(bubbles: list[str]) -> None:
        log.append("send")  # should never be called for empty list

    ctx = CommandContext(
        state=state,
        swap_provider=swap,
        close_provider=lambda: None,
        forget_session=lambda: None,
        replay_for_sid=replay,
        send_extra_bubbles=push,
    )
    verdict, reply = Registry(ctx).dispatch("/resume abcdef")
    assert verdict == "handled"
    # send_extra_bubbles must be skipped when the replay returned []
    assert log == ["replay", "swap"]
    assert "复活" in (reply or "") and "abcdef" in (reply or "")


def test_resume_sid_without_replay_hook_still_swaps() -> None:
    """Defaults: ctx without replay_for_sid wired — sid path must not crash."""
    state = BridgeState()
    hooks = _Hooks()
    ctx = CommandContext(
        state=state,
        swap_provider=hooks.swap,
        close_provider=hooks.close,
        forget_session=hooks.forget,
        # No replay_for_sid / send_extra_bubbles supplied → defaults kick in.
    )
    verdict, reply = Registry(ctx).dispatch("/resume zzzzzz")
    assert verdict == "handled"
    assert hooks.swap_calls == [(state.model, "zzzzzz")]
    assert "复活" in (reply or "") and "zzzzzz" in (reply or "")


def test_resume_short_sid_below_min_treated_as_sid() -> None:
    """Tokens that are not all-digits hit the sid path even if short. B6 uses
    `isdigit()` to gate the picker path — anything else flows to sid."""
    state = BridgeState()
    reg, hooks = _make(state)
    verdict, _ = reg.dispatch("/resume abc")
    assert verdict == "handled"
    # sid was passed through verbatim
    assert hooks.replay_calls == ["abc"]
    assert hooks.swap_calls == [(state.model, "abc")]
