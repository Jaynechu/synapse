"""Tests for synapse_wx.commands.registry."""

from __future__ import annotations

import pytest

from synapse_core.commands.registry import CommandContext, Registry
from synapse_core.state import BridgeState
from synapse_core.usage import Usage


class FakeHooks:
    def __init__(self) -> None:
        self.swap_calls: list[tuple[str | None, str | None]] = []
        self.close_calls: int = 0
        self.forget_calls: int = 0
        self.fire_sessionend_calls: list[str] = []

    def swap(self, model: str | None, sid: str | None) -> None:
        self.swap_calls.append((model, sid))

    def close(self) -> None:
        self.close_calls += 1

    def forget(self) -> None:
        self.forget_calls += 1

    def fire_sessionend(self, sid: str) -> None:
        self.fire_sessionend_calls.append(sid)


def _make(
    state: BridgeState | None = None,
    *,
    usage_client=None,
) -> tuple[Registry, FakeHooks, BridgeState]:
    s = state if state is not None else BridgeState()
    hooks = FakeHooks()
    kwargs: dict = {}
    if usage_client is not None:
        kwargs["usage_client"] = usage_client
    ctx = CommandContext(
        state=s,
        swap_provider=hooks.swap,
        close_provider=hooks.close,
        forget_session=hooks.forget,
        fire_sessionend=hooks.fire_sessionend,
        **kwargs,
    )
    return Registry(ctx), hooks, s


# ── /info ─────────────────────────────────────────────────────


def test_info_empty_state() -> None:
    reg, _, _ = _make()
    verdict, reply = reg.dispatch("/info")
    assert verdict == "handled"
    # Empty state: no model, no sid, no snap, no usage_client.
    assert reply == "?[high] | ? | Health:down | SID | ? | ?(5h) ?(7d) | 0.0k"


def test_info_populated_state() -> None:
    s = BridgeState(
        model="claude-opus-4-7",
        session_id="70c32ba1-28b6-400a-a0cf-c3e2f8fc0869",
        last_assistant_usage={
            "input_tokens": 600,
            "cache_read_input_tokens": 30000,
            "cache_creation_input_tokens": 4400,
            "output_tokens": 1000,  # output excluded from ctx total
        },
    )
    reg, _, _ = _make(s)
    verdict, reply = reg.dispatch("/info")
    assert verdict == "handled"
    assert reply is not None
    assert "Opus 4.7[high] | ? | Health:down" in reply
    assert "70c32ba1" in reply
    assert reply.endswith("35.0k")
    assert "?(7d)" in reply


def test_info_with_usage_client_renders_pct() -> None:
    s = BridgeState(
        model="claude-opus-4-7",
        session_id="70c32ba1-28b6-400a-a0cf-c3e2f8fc0869",
        last_assistant_usage={"input_tokens": 1000},
    )
    usage = Usage(five_hour_pct=42.0, seven_day_pct=17.0)
    reg, _, _ = _make(s, usage_client=lambda: usage)
    verdict, reply = reg.dispatch("/info")
    assert verdict == "handled"
    assert reply is not None
    assert "42%(5h)" in reply
    assert "17%(7d)" in reply
    assert "?(5h)" not in reply
    assert "?(7d)" not in reply


def test_info_usage_client_partial_falls_back_per_window() -> None:
    s = BridgeState(model="claude-opus-4-7")
    usage = Usage(five_hour_pct=88.0, seven_day_pct=None)
    reg, _, _ = _make(s, usage_client=lambda: usage)
    _, reply = reg.dispatch("/info")
    assert reply is not None
    assert "88%(5h)" in reply
    assert "?(7d)" in reply


def test_info_usage_client_returns_none_legacy_placeholders() -> None:
    s = BridgeState(model="claude-opus-4-7")
    reg, _, _ = _make(s, usage_client=lambda: None)
    _, reply = reg.dispatch("/info")
    assert reply is not None
    assert "?(5h)" in reply
    assert "?(7d)" in reply


def test_info_usage_client_raises_falls_back() -> None:
    s = BridgeState(model="claude-opus-4-7")

    def boom() -> Usage | None:
        raise RuntimeError("boom")

    reg, _, _ = _make(s, usage_client=boom)
    _, reply = reg.dispatch("/info")
    assert reply is not None
    assert "?(5h)" in reply
    assert "?(7d)" in reply


@pytest.mark.parametrize("cmd", ["/info", "/status", "/usage"])
def test_info_status_usage_render_identical(cmd: str) -> None:
    s = BridgeState(model="claude-opus-4-7", session_id="abcd1234efgh")
    usage = Usage(five_hour_pct=12.0, seven_day_pct=3.0)
    reg, _, _ = _make(s, usage_client=lambda: usage)
    verdict, reply = reg.dispatch(cmd)
    assert verdict == "handled"
    assert reply is not None
    assert reply.startswith("Opus 4.7[high] | ? | Health:down | abcd1234 | ")
    assert "12%(5h)" in reply
    assert "3%(7d)" in reply


# ── /model ────────────────────────────────────────────────────


def test_model_alias_switch() -> None:
    s = BridgeState(model="claude-opus-4-8[1m]", session_id="sid-abc")
    reg, hooks, _ = _make(s)
    verdict, reply = reg.dispatch("/model 4.7")
    assert verdict == "handled"
    assert reply == "🤖(Opus 4.7 [1M])上线中..."
    assert hooks.swap_calls == [("claude-opus-4-7[1m]", "sid-abc")]
    assert s.model == "claude-opus-4-7[1m]"


def test_model_already_current_no_swap() -> None:
    s = BridgeState(model="claude-opus-4-7[1m]", session_id="sid-x")
    reg, hooks, _ = _make(s)
    verdict, reply = reg.dispatch("/model 4.7")
    assert verdict == "handled"
    assert reply == "🤖是我是我还是我！"
    assert hooks.swap_calls == []


def test_model_no_arg_returns_usage() -> None:
    reg, hooks, _ = _make()
    verdict, reply = reg.dispatch("/model")
    assert verdict == "handled"
    assert reply is not None
    assert reply.startswith("查无此机")
    assert hooks.swap_calls == []


def test_model_raw_canonical_id_passes_through() -> None:
    s = BridgeState(model=None)
    reg, hooks, _ = _make(s)
    verdict, reply = reg.dispatch("/model claude-future-9")
    assert verdict == "handled"
    # display fallback uses the id itself.
    assert reply == "🤖(claude-future-9)上线中..."
    assert hooks.swap_calls == [("claude-future-9", None)]


def test_natural_alias_routes_as_model() -> None:
    s = BridgeState(model=None, session_id="sid-xyz")
    reg, hooks, _ = _make(s)
    verdict, reply = reg.dispatch("4.7")
    assert verdict == "handled"
    assert reply == "🤖(Opus 4.7 [1M])上线中..."
    assert hooks.swap_calls == [("claude-opus-4-7[1m]", "sid-xyz")]
    assert s.model == "claude-opus-4-7[1m]"


def test_natural_alias_case_insensitive() -> None:
    s = BridgeState(model=None)
    reg, hooks, _ = _make(s)
    verdict, _ = reg.dispatch("SONNET")
    assert verdict == "handled"
    assert hooks.swap_calls == [("claude-sonnet-4-6", None)]


# ── /clear ────────────────────────────────────────────────────


def test_clear_resets_session() -> None:
    s = BridgeState(model="claude-opus-4-7", session_id="sid-old")
    reg, hooks, _ = _make(s)
    verdict, reply = reg.dispatch("/clear")
    assert verdict == "handled"
    # B1: /clear lands on the configured default (opus-4.6[1m]).
    assert (reply or "").startswith("新鸭上桌")
    assert "Opus 4.6 [1M]" in (reply or "")
    assert hooks.swap_calls == [("claude-opus-4-6[1m]", None)]
    assert hooks.forget_calls == 1
    assert s.session_id is None
    assert s.model == "claude-opus-4-6[1m]"


def test_new_is_alias_for_clear() -> None:
    s = BridgeState(model="claude-opus-4-7", session_id="sid-old")
    reg, hooks, _ = _make(s)
    verdict, reply = reg.dispatch("/new")
    assert verdict == "handled"
    assert (reply or "").startswith("新鸭上桌")
    assert hooks.swap_calls == [("claude-opus-4-6[1m]", None)]
    assert s.session_id is None


def test_clear_keeps_effort_and_thinking() -> None:
    """/clear preserves both effort_level and thinking_on (0614)."""
    s = BridgeState(model="claude-opus-4-7", session_id="sid-old")
    s.effort_level = "low"
    s.thinking_on = True
    reg, _, _ = _make(s)
    reg.dispatch("/clear")
    assert s.effort_level == "low"
    assert s.thinking_on is True


def test_clear_fires_sessionend_for_old_sid() -> None:
    """/clear must popen sessionend_async for the old sid BEFORE swap so the
    sid actually runs through marrow's LLM pipeline. Without this the wx sid
    stays orphaned (no lifecycle:end, no affect, no digest)."""
    s = BridgeState(model="claude-opus-4-7", session_id="sid-old-123")
    reg, hooks, _ = _make(s)
    reg.dispatch("/clear")
    assert hooks.fire_sessionend_calls == ["sid-old-123"]


def test_clear_no_fire_when_no_old_sid() -> None:
    """First /clear (or back-to-back /clear) has no sid to retire — skip."""
    s = BridgeState(model="claude-opus-4-7", session_id=None)
    reg, hooks, _ = _make(s)
    reg.dispatch("/clear")
    assert hooks.fire_sessionend_calls == []


def test_clear_fire_failure_does_not_block() -> None:
    """fire_sessionend exception must not prevent the rest of /clear."""
    s = BridgeState(model="claude-opus-4-7", session_id="sid-boom")
    hooks = FakeHooks()

    def boom(_sid: str) -> None:
        raise RuntimeError("popen failed")

    ctx = CommandContext(
        state=s,
        swap_provider=hooks.swap,
        close_provider=hooks.close,
        forget_session=hooks.forget,
        fire_sessionend=boom,
    )
    verdict, _ = Registry(ctx).dispatch("/clear")
    assert verdict == "handled"
    assert hooks.swap_calls == [("claude-opus-4-6[1m]", None)]
    assert s.session_id is None


def test_resume_preserves_effort() -> None:
    """/resume <sid> must NOT touch state.effort_level — persisted level
    survives across resume swaps."""
    s = BridgeState(model="claude-opus-4-7", session_id="sid-old")
    s.effort_level = "max"
    hooks = FakeHooks()
    ctx = CommandContext(
        state=s,
        swap_provider=hooks.swap,
        close_provider=hooks.close,
        forget_session=hooks.forget,
        resolve_resume_model=lambda _sid: "claude-opus-4-6[1m]",
    )
    Registry(ctx).dispatch("/resume abcdef1234567890")
    assert s.effort_level == "max"


# ── /stop ─────────────────────────────────────────────────────


def test_stop_keeps_session() -> None:
    s = BridgeState(model="claude-opus-4-7", session_id="sid-keep")
    reg, hooks, _ = _make(s)
    verdict, reply = reg.dispatch("/stop")
    assert verdict == "handled"
    assert reply == "🛑施法已打断"
    assert hooks.swap_calls == [("claude-opus-4-7", "sid-keep")]
    assert s.session_id == "sid-keep"


# ── /rewind + /regen ──────────────────────────────────────────


def test_rewind_respawns_without_session_block(monkeypatch) -> None:
    import synapse_core.commands.registry as _reg_mod

    sid = "rewind-sid"
    s = BridgeState(model="claude-opus-4-7", session_id=sid)
    calls: list[tuple] = []

    def audit(kind: str, call_sid: str, status: str) -> None:
        calls.append(("audit", kind, call_sid, status))

    def respawn(call_sid: str, model: str | None) -> None:
        calls.append(("respawn", call_sid, model))

    monkeypatch.setattr(
        _reg_mod.jsonl_edit,
        "drop_last_n_replies",
        lambda *_args, **_kwargs: [
            {"type": "assistant", "message": {"role": "assistant", "content": "stale"}}
        ],
    )
    reg, _, _ = _make(s)
    reg._ctx.audit_writer = audit
    reg._ctx.respawn_with_resume = respawn

    verdict, _ = reg.dispatch("/rewind 1")

    assert verdict == "handled"
    assert calls == [
        ("respawn", sid, "claude-opus-4-7"),
    ]


def test_regen_no_session_block_writes(monkeypatch) -> None:
    """Regen respawns without session_block writes."""
    import synapse_core.commands.registry as _reg_mod

    sid = "regen-sid"
    s = BridgeState(model="claude-opus-4-7", session_id=sid)
    calls: list[tuple] = []

    def audit(kind: str, call_sid: str, status: str) -> None:
        calls.append(("audit", kind, call_sid, status))

    def respawn(call_sid: str, model: str | None) -> None:
        calls.append(("respawn", call_sid, model))

    monkeypatch.setattr(
        _reg_mod.jsonl_edit,
        "drop_last_n_replies",
        lambda *_args, **_kwargs: [
            {"type": "assistant", "message": {"role": "assistant", "content": "stale"}}
        ],
    )
    reg, _, _ = _make(s)
    reg._ctx.audit_writer = audit
    reg._ctx.respawn_with_resume = respawn

    verdict, _ = reg.dispatch("/regen")

    assert verdict == "handled"
    assert calls == [
        ("respawn", sid, "claude-opus-4-7"),
    ]


# ── unknown / forward ─────────────────────────────────────────


def test_unknown_slash_returns_error() -> None:
    reg, hooks, _ = _make()
    verdict, reply = reg.dispatch("/unknown")
    assert verdict == "handled"
    assert reply == "啥玩意？没见过啊。看看小抄 /help"
    assert hooks.swap_calls == []


def test_plain_text_forwards() -> None:
    reg, _, _ = _make()
    assert reg.dispatch("hi") == ("forward", None)


def test_hold_word_forwards() -> None:
    reg, _, _ = _make()
    # ("等") looks like a hold word; not a command.
    assert reg.dispatch("等") == ("forward", None)


def test_empty_string_forwards() -> None:
    reg, _, _ = _make()
    assert reg.dispatch("   ") == ("forward", None)


def test_quote_on_sets_state_and_persists() -> None:
    s = BridgeState()
    assert s.quote_on is False
    persist_calls: list[int] = []
    hooks = FakeHooks()
    ctx = CommandContext(
        state=s,
        swap_provider=hooks.swap,
        close_provider=hooks.close,
        forget_session=hooks.forget,
        persist_state=lambda: persist_calls.append(1),
    )
    verdict, reply = Registry(ctx).dispatch("/quote on")
    assert verdict == "handled"
    assert reply == "引用已打开"
    assert s.quote_on is True
    assert persist_calls == [1]


def test_quote_off_sets_state_and_persists() -> None:
    s = BridgeState()
    s.quote_on = True
    persist_calls: list[int] = []
    hooks = FakeHooks()
    ctx = CommandContext(
        state=s,
        swap_provider=hooks.swap,
        close_provider=hooks.close,
        forget_session=hooks.forget,
        persist_state=lambda: persist_calls.append(1),
    )
    verdict, reply = Registry(ctx).dispatch("/quote off")
    assert verdict == "handled"
    assert reply == "引用已关闭"
    assert s.quote_on is False
    assert persist_calls == [1]


def test_quote_no_arg_reports_current() -> None:
    """Mirror /thinking: empty arg reports the current state."""
    s = BridgeState()
    reg, _, _ = _make(s)
    verdict, reply = reg.dispatch("/quote")
    assert verdict == "handled"
    assert reply is not None
    assert "现在:off" in reply
    s.quote_on = True
    _, reply2 = reg.dispatch("/quote")
    assert reply2 is not None and "现在:on" in reply2


def test_quote_bad_arg_returns_error() -> None:
    s = BridgeState()
    reg, _, _ = _make(s)
    verdict, reply = reg.dispatch("/quote sometimes")
    assert verdict == "handled"
    assert reply is not None and "开还是关？" in reply
    assert s.quote_on is False


def test_five_hour_resets_at_rendered() -> None:
    import time

    s = BridgeState(
        rate_limit_info={
            "rateLimitType": "five_hour",
            "resetsAt": int(time.time()) + 7200,  # 2h ahead
            "isUsingOverage": False,
        },
    )
    reg, _, _ = _make(s)
    _, reply = reg.dispatch("/info")
    assert reply is not None
    assert "(5h)" in reply
    assert "?(5h)" not in reply  # should compute, not placeholder


def test_five_hour_nested_payload_supported() -> None:
    import time

    s = BridgeState(
        rate_limit_info={
            "rate_limit_info": {
                "rateLimitType": "five_hour",
                "resetsAt": int(time.time()) + 3600,
            }
        },
    )
    reg, _, _ = _make(s)
    _, reply = reg.dispatch("/info")
    assert reply is not None and "?(5h)" not in reply


# ── /cwd ─────────────────────────────────────────────────────


def test_cwd_no_arg_lists_presets() -> None:
    s = BridgeState(cc_cwd="/Users/Gabrielle/Desktop/NY")
    reg, _, _ = _make(s)
    verdict, reply = reg.dispatch("/cwd")
    assert verdict == "handled"
    assert reply is not None
    assert "当前位置 /Users/Gabrielle/Desktop/NY" in reply
    assert "1 → NY" in reply
    assert "2 → Study" in reply
    assert "3 → marrow" in reply


def test_cwd_preset_digit_switches_and_clears(tmp_path, monkeypatch) -> None:
    import synapse_core.commands.registry as _reg_mod
    monkeypatch.setattr(
        _reg_mod,
        "_CWD_PRESETS",
        (
            "/Users/Gabrielle/Desktop/NY",
            "/Users/Gabrielle/Library/Mobile Documents/com~apple~CloudDocs/Study",
            "/Users/Gabrielle/CC-Lab/marrow",
        ),
    )
    s = BridgeState(
        cc_cwd="/Users/Gabrielle/Desktop/NY",
        session_id="old-sid",
        effort_level="low",
        thinking_on=True,
    )
    persisted: list[int] = []
    reg, hooks, _ = _make(s)
    reg._ctx.persist_state = lambda: persisted.append(1)
    reg._ctx.clear_default_model = "claude-opus-4-6[1m]"
    verdict, reply = reg.dispatch("/cwd 3")
    assert verdict == "handled"
    assert reply == "🚪任意门传送中: marrow"
    assert s.cc_cwd == "/Users/Gabrielle/CC-Lab/marrow"
    # Implicit /clear semantics: model + sid reset, session forgotten,
    # swap_provider called once with new model + None sid.
    # effort_level persists across /cwd (0614).
    assert s.session_id is None
    assert s.model == "claude-opus-4-6[1m]"
    assert s.effort_level == "low"
    assert s.thinking_on is True
    assert hooks.swap_calls == [("claude-opus-4-6[1m]", None)]
    assert hooks.forget_calls == 1
    assert hooks.fire_sessionend_calls == ["old-sid"]
    assert persisted  # persist_state fired at least once


def test_cwd_bad_digit_returns_error() -> None:
    s = BridgeState(cc_cwd="/Users/Gabrielle/Desktop/NY")
    reg, hooks, _ = _make(s)
    verdict, reply = reg.dispatch("/cwd 9")
    assert verdict == "handled"
    assert reply is not None and "查无此号" in reply
    assert s.cc_cwd == "/Users/Gabrielle/Desktop/NY"
    assert hooks.swap_calls == []


def test_cwd_arbitrary_path_accepted(tmp_path) -> None:
    s = BridgeState(cc_cwd="/Users/Gabrielle/Desktop/NY")
    reg, hooks, _ = _make(s)
    verdict, reply = reg.dispatch(f"/cwd {tmp_path}")
    assert verdict == "handled"
    assert reply is not None and tmp_path.name in reply
    assert s.cc_cwd == str(tmp_path.resolve())
    assert hooks.swap_calls == [(reg._ctx.clear_default_model, None)]


def test_cwd_arbitrary_path_missing_rejected() -> None:
    s = BridgeState(cc_cwd="/Users/Gabrielle/Desktop/NY")
    reg, hooks, _ = _make(s)
    verdict, reply = reg.dispatch("/cwd /this/does/not/exist/anywhere")
    assert verdict == "handled"
    assert reply is not None and "此路不通" in reply
    # cwd unchanged, no swap fired.
    assert s.cc_cwd == "/Users/Gabrielle/Desktop/NY"
    assert hooks.swap_calls == []


def test_cwd_path_is_file_rejected(tmp_path) -> None:
    f = tmp_path / "not_a_dir.txt"
    f.write_text("hi")
    s = BridgeState(cc_cwd="/Users/Gabrielle/Desktop/NY")
    reg, hooks, _ = _make(s)
    verdict, reply = reg.dispatch(f"/cwd {f}")
    assert verdict == "handled"
    assert reply is not None and "世上本没有路" in reply
    assert s.cc_cwd == "/Users/Gabrielle/Desktop/NY"
    assert hooks.swap_calls == []


def test_cwd_picker_bare_digit_switches(monkeypatch) -> None:
    import synapse_core.commands.registry as _reg_mod
    monkeypatch.setattr(
        _reg_mod,
        "_CWD_PRESETS",
        (
            "/Users/Gabrielle/Desktop/NY",
            "/Users/Gabrielle/Library/Mobile Documents/com~apple~CloudDocs/Study",
            "/Users/Gabrielle/CC-Lab/marrow",
        ),
    )
    s = BridgeState(cc_cwd="/Users/Gabrielle/Desktop/NY")
    reg, _, _ = _make(s)
    # /cwd alone arms the picker.
    verdict, _ = reg.dispatch("/cwd")
    assert verdict == "handled"
    assert s.pending_picker == "cwd"
    # Bare "3" right after picks preset 3 (marrow).
    verdict, reply = reg.dispatch("3")
    assert verdict == "handled"
    assert reply == "🚪任意门传送中: marrow"
    assert s.cc_cwd == "/Users/Gabrielle/CC-Lab/marrow"
    # Picker is consumed.
    assert s.pending_picker is None
