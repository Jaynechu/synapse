"""Tests for /resume sid / N / empty (B1 + B6 picker)."""

from __future__ import annotations

from synapse_core.commands.registry import CommandContext, Registry
from synapse_core.state import BridgeState


class _Hooks:
    def __init__(self) -> None:
        self.swap_calls: list[tuple[str | None, str | None]] = []
        self.forget_calls = 0

    def swap(self, model: str | None, sid: str | None) -> None:
        self.swap_calls.append((model, sid))

    def close(self) -> None:
        pass

    def forget(self) -> None:
        self.forget_calls += 1


def _make(state: BridgeState, **overrides) -> tuple[Registry, _Hooks]:
    hooks = _Hooks()
    ctx = CommandContext(
        state=state,
        swap_provider=hooks.swap,
        close_provider=hooks.close,
        forget_session=hooks.forget,
        **overrides,
    )
    return Registry(ctx), hooks


def test_resume_sid_calls_resolver_and_swaps() -> None:
    state = BridgeState(model="claude-opus-4-7")
    seen_sids: list[str] = []

    def resolver(sid: str) -> str | None:
        seen_sids.append(sid)
        return "claude-opus-4-8[1m]"

    reg, hooks = _make(state, resolve_resume_model=resolver)
    verdict, reply = reg.dispatch("/resume abcdef123456")
    assert verdict == "handled"
    assert seen_sids == ["abcdef123456"]
    assert hooks.swap_calls == [("claude-opus-4-8[1m]", "abcdef123456")]
    assert state.session_id == "abcdef123456"
    assert state.model == "claude-opus-4-8[1m]"
    assert "abcdef12" in (reply or "")
    assert "Opus 4.8" in (reply or "")


def test_resume_sid_resolver_miss_falls_back_to_default() -> None:
    state = BridgeState(model=None)
    reg, hooks = _make(
        state,
        resolve_resume_model=lambda _sid: None,
        clear_default_model="claude-opus-4-6[1m]",
    )
    verdict, _ = reg.dispatch("/resume some-sid")
    assert verdict == "handled"
    assert hooks.swap_calls == [("claude-opus-4-6[1m]", "some-sid")]


def test_resume_empty_with_no_history_message() -> None:
    state = BridgeState()
    reg, _ = _make(state, list_recent_sessions=lambda: [])
    verdict, reply = reg.dispatch("/resume")
    assert verdict == "handled"
    assert reply == "😤最近没找我吧？"


def test_resume_empty_lists_picker() -> None:
    state = BridgeState()
    rows = [
        {"sid": "abcdef12-aaaa", "model": "claude-opus-4-6[1m]", "channel": "wx",
         "last_active": "2026-06-02T20:00:00Z", "title": "lumi-wx"},
        {"sid": "12345678-bbbb", "model": "sonnet", "channel": "cli",
         "last_active": "2026-06-02T19:00:00Z", "title": ""},
    ]
    reg, _ = _make(state, list_recent_sessions=lambda: rows)
    verdict, reply = reg.dispatch("/resume")
    assert verdict == "handled"
    body = reply or ""
    assert "Recent sessions:" in body
    # New layout: [ch] title (sid8) model HH:MM
    # `last_active` is UTC; HH:MM is rendered in the local zone so we only
    # assert the structural prefix and let the clock segment be free-form.
    assert "1. [wx] lumi-wx (abcdef12) Opus 4.6 [1M] " in body
    # Empty title falls back to a placeholder so the row still has shape.
    assert "2. [cli] (untitled) (12345678) Sonnet " in body
    assert "Reply with the number" in body


def test_resume_digit_picks_nth() -> None:
    state = BridgeState()
    rows = [
        {"sid": "first-sid", "model": "claude-opus-4-6[1m]", "channel": "wx",
         "last_active": "x", "title": ""},
        {"sid": "second-sid", "model": "claude-sonnet-4-6", "channel": "cli",
         "last_active": "x", "title": ""},
    ]

    def resolver(sid: str) -> str | None:
        return {"first-sid": "claude-opus-4-6[1m]",
                "second-sid": "claude-sonnet-4-6"}[sid]

    reg, hooks = _make(
        state,
        list_recent_sessions=lambda: rows,
        resolve_resume_model=resolver,
    )
    verdict, _ = reg.dispatch("/resume 2")
    assert verdict == "handled"
    assert hooks.swap_calls == [("claude-sonnet-4-6", "second-sid")]


def test_resume_digit_out_of_range_message() -> None:
    state = BridgeState()
    rows = [{"sid": "first-sid", "model": "m", "channel": "wx",
             "last_active": "x", "title": ""}]
    reg, _ = _make(state, list_recent_sessions=lambda: rows)
    verdict, reply = reg.dispatch("/resume 5")
    assert verdict == "handled"
    assert reply == "🙂‍↔️你要的太多了"


def test_resume_empty_arms_pending_picker() -> None:
    """After /resume with rows, the bare-digit reply should land on the picker.

    Without arming pending_picker, dispatch("5") would forward "5" to cc as
    prose — that's the bug caught in the field (cc replied "好，5分钟。").
    """
    state = BridgeState()
    rows = [
        {"sid": "first-sid", "model": "claude-opus-4-6[1m]", "channel": "wx",
         "last_active": "x", "title": ""},
        {"sid": "second-sid", "model": "claude-sonnet-4-6", "channel": "cli",
         "last_active": "x", "title": ""},
    ]

    def resolver(sid: str) -> str | None:
        return {"first-sid": "claude-opus-4-6[1m]",
                "second-sid": "claude-sonnet-4-6"}[sid]

    reg, hooks = _make(
        state,
        list_recent_sessions=lambda: rows,
        resolve_resume_model=resolver,
    )
    reg.dispatch("/resume")
    assert state.pending_picker == "resume"
    verdict, _ = reg.dispatch("2")
    assert verdict == "handled"
    assert hooks.swap_calls == [("claude-sonnet-4-6", "second-sid")]
    # Picker consumed: a second bare digit should NOT route again.
    assert state.pending_picker is None


def test_resume_empty_no_rows_does_not_arm_picker() -> None:
    state = BridgeState()
    reg, _ = _make(state, list_recent_sessions=lambda: [])
    reg.dispatch("/resume")
    assert state.pending_picker is None


def test_pending_picker_cleared_by_non_digit_text() -> None:
    """A non-digit, non-slash message after the picker forwards (and clears)."""
    state = BridgeState()
    rows = [{"sid": "first-sid", "model": "m", "channel": "wx",
             "last_active": "x", "title": ""}]
    reg, _ = _make(state, list_recent_sessions=lambda: rows)
    reg.dispatch("/resume")
    assert state.pending_picker == "resume"
    verdict, reply = reg.dispatch("hello there")
    assert verdict == "forward"
    assert reply is None
    assert state.pending_picker is None


def test_pending_picker_cleared_by_slash_command() -> None:
    """Slash commands win over the picker; pending clears at dispatch entry."""
    state = BridgeState()
    rows = [{"sid": "first-sid", "model": "m", "channel": "wx",
             "last_active": "x", "title": ""}]
    reg, _ = _make(state, list_recent_sessions=lambda: rows)
    reg.dispatch("/resume")
    assert state.pending_picker == "resume"
    reg.dispatch("/help")
    assert state.pending_picker is None


# ── /resume cwd switching ─────────────────────────────────────────────────


def test_resume_switches_cwd_when_target_differs(tmp_path) -> None:
    """resolve_session_cwd returns a real dir different from state.cc_cwd
    → state.cc_cwd updated, ack contains cwd_switched text."""
    import os
    state = BridgeState(model="claude-opus-4-7", cc_cwd="/old/path")
    target = str(tmp_path)

    reg, hooks = _make(
        state,
        resolve_resume_model=lambda _sid: "claude-opus-4-7",
        resolve_session_cwd=lambda _sid: target,
    )
    verdict, reply = reg.dispatch("/resume sid-abc")
    assert verdict == "handled"
    assert state.cc_cwd == target
    assert os.path.basename(target) in (reply or "")
    # swap was called with updated sid
    assert hooks.swap_calls == [("claude-opus-4-7", "sid-abc")]


def test_resume_cwd_unchanged_when_none(tmp_path) -> None:
    """resolve_session_cwd returns None → cc_cwd unchanged, no extra ack line."""
    state = BridgeState(model="claude-opus-4-7", cc_cwd="/original")
    reg, _ = _make(
        state,
        resolve_resume_model=lambda _sid: "claude-opus-4-7",
        resolve_session_cwd=lambda _sid: None,
    )
    _, reply = reg.dispatch("/resume sid-xyz")
    assert state.cc_cwd == "/original"
    # Only the standard resume.ok line — no cwd_switched suffix
    assert "\n" not in (reply or "")


def test_resume_cwd_unchanged_when_same(tmp_path) -> None:
    """resolve_session_cwd returns the same path → no switch, no extra line."""
    state = BridgeState(model="claude-opus-4-7", cc_cwd=str(tmp_path))
    reg, _ = _make(
        state,
        resolve_resume_model=lambda _sid: "claude-opus-4-7",
        resolve_session_cwd=lambda _sid: str(tmp_path),
    )
    _, reply = reg.dispatch("/resume sid-xyz")
    assert state.cc_cwd == str(tmp_path)
    assert "\n" not in (reply or "")


def test_resume_cwd_unchanged_when_not_a_dir(tmp_path) -> None:
    """resolve_session_cwd returns a non-existent path → no switch."""
    state = BridgeState(model="claude-opus-4-7", cc_cwd="/original")
    reg, _ = _make(
        state,
        resolve_resume_model=lambda _sid: "claude-opus-4-7",
        resolve_session_cwd=lambda _sid: "/does/not/exist/at/all",
    )
    _, reply = reg.dispatch("/resume sid-xyz")
    assert state.cc_cwd == "/original"
    assert "\n" not in (reply or "")
