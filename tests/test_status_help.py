"""Tests for B10: /status snapshot formatting + /help render."""

from __future__ import annotations

from pathlib import Path

import pytest

from synapse_core.commands.registry import (
    CommandContext,
    Registry,
    _fmt_uptime,
)
from synapse_core.state import BridgeState


def _noop(*_a, **_k) -> None:
    return None


def _make_ctx(status: dict, commands_doc_path: Path | None = None) -> CommandContext:
    return CommandContext(
        state=BridgeState(model="claude-opus-4-6", session_id="abcdef1234"),
        swap_provider=_noop,
        close_provider=_noop,
        forget_session=_noop,
        get_status=lambda: status,
        commands_doc_path=commands_doc_path,
    )


def test_fmt_uptime_branches() -> None:
    assert _fmt_uptime(None) == "?"
    assert _fmt_uptime(-1) == "?"
    assert _fmt_uptime(0) == "0s"
    assert _fmt_uptime(59) == "59s"
    assert _fmt_uptime(60) == "1m00s"
    assert _fmt_uptime(125) == "2m05s"
    assert _fmt_uptime(3600) == "1h00m"
    assert _fmt_uptime(3661) == "1h01m"
    assert _fmt_uptime(86_400) == "1d00h"
    assert _fmt_uptime(90_061) == "1d01h"


def test_status_full_snapshot() -> None:
    ctx = _make_ctx(
        {
            "cc_pid": 4242,
            "cwd": "/Users/Gabrielle/CC-Lab/marrow",
            "ilink_ok": True,
            "last_active_sid": "abcdef1234567890",
            "session_age_sec": 125.5,
        }
    )
    out = Registry(ctx).dispatch("/status")
    assert out[0] == "handled"
    assert out[1] == (
        "Opus 4.6[high] | /Users/Gabrielle/CC-Lab/marrow | Health:ok | "
        "abcdef12 | 2m05s | ?(5h) ?(7d) | 0.0k"
    )


def test_status_empty_snapshot_renders_question_marks() -> None:
    ctx = _make_ctx({})
    out = Registry(ctx).dispatch("/status")
    # Empty snap = no pid + no ilink → Health:down. cwd "?" + uptime "?".
    assert out[1] == (
        "Opus 4.6[high] | ? | Health:down | "
        "abcdef12 | ? | ?(5h) ?(7d) | 0.0k"
    )


def test_status_cc_dead_with_polling_ok() -> None:
    ctx = _make_ctx({"cc_pid": None, "cwd": "/x", "ilink_ok": True, "session_age_sec": 10})
    assert Registry(ctx).dispatch("/status")[1] == (
        "Opus 4.6[high] | /x | Health:cc-dead | "
        "abcdef12 | 10s | ?(5h) ?(7d) | 0.0k"
    )


def test_status_no_poll_with_cc_alive() -> None:
    ctx = _make_ctx({"cc_pid": 99, "cwd": "/x", "ilink_ok": False, "session_age_sec": 10})
    assert Registry(ctx).dispatch("/status")[1] == (
        "Opus 4.6[high] | /x | Health:no-poll | "
        "abcdef12 | 10s | ?(5h) ?(7d) | 0.0k"
    )


def test_status_prefers_real_running_model() -> None:
    """state.model holds the floating alias; /info shows what cc resolved."""
    ctx = _make_ctx({"cwd": "/x", "ilink_ok": True, "cc_pid": 1,
                     "model_actual": "claude-opus-5"})
    ctx.state.model = "opus"
    assert Registry(ctx).dispatch("/status")[1].startswith("Opus 5[high] | /x")


def test_status_real_model_shown_when_state_model_unset() -> None:
    """Fresh boot with no configured model — no more bare '?'."""
    ctx = _make_ctx({"cwd": "/x", "ilink_ok": True, "cc_pid": 1,
                     "model_actual": "claude-opus-5"})
    ctx.state.model = None
    assert Registry(ctx).dispatch("/status")[1].startswith("Opus 5[high]")


def test_status_falls_back_to_state_model_before_first_init() -> None:
    ctx = _make_ctx({"cwd": "/x", "ilink_ok": True, "cc_pid": 1})
    ctx.state.model = "opus"
    assert Registry(ctx).dispatch("/status")[1].startswith("Opus[high]")


def test_help_renders_doc_body(tmp_path: Path) -> None:
    doc = tmp_path / "COMMANDS.md"
    doc.write_text("# Demo\n- /status\n- /help\n")
    ctx = _make_ctx({}, commands_doc_path=doc)
    out = Registry(ctx).dispatch("/help")
    assert out[0] == "handled"
    assert out[1] is not None
    assert "/status" in out[1]
    assert "/help" in out[1]


def test_help_missing_doc_returns_friendly_string(tmp_path: Path) -> None:
    ctx = _make_ctx({}, commands_doc_path=tmp_path / "missing.md")
    out = Registry(ctx).dispatch("/help")
    assert out[1] == "😭小抄找不到了！！"


def test_status_handler_swallows_exception() -> None:
    def bad_status() -> dict:
        raise RuntimeError("status sample failed")

    ctx = CommandContext(
        state=BridgeState(model="claude-opus-4-6"),
        swap_provider=_noop,
        close_provider=_noop,
        forget_session=_noop,
        get_status=bad_status,
    )
    out = Registry(ctx).dispatch("/status")
    # Falls back to empty snapshot, never crashes — health renders as down.
    assert out[0] == "handled"
    reply = out[1] or ""
    assert "Health:down" in reply
    assert reply.startswith("Opus 4.6[high] | ? | Health:down")


@pytest.mark.parametrize("cmd", ["/status", "/help"])
def test_new_commands_route_as_handled(cmd: str) -> None:
    ctx = _make_ctx({"cc_pid": 1, "cwd": "/", "ilink_ok": True, "session_age_sec": 1})
    verdict, _ = Registry(ctx).dispatch(cmd)
    assert verdict == "handled"
