"""B9 — `/rewind N` and `/regen` registry dispatch + side effects.

Both commands:
  1. Truncate the jsonl on disk via `jsonl_edit.drop_last_n_pairs`
     (/regen always drops exactly one user/assistant pair).
  2. Trigger a respawn via `respawn_with_resume(sid, model)` so cc reloads
     the trimmed history.
  3. (/regen only) Resend the dropped user prompt via `replay_user_text`
     so cc actually regenerates — `--resume` does not auto-replay.

Note: session_block audit writes were removed — they clobbered mm- (latest-wins)
and served no purpose (dropped turns are already gone from jsonl).

Error path:
  - `/rewind 0` / `/rewind -3` / `/rewind` (no N) → `[error] ...`, no I/O.
"""

from __future__ import annotations

import json
from pathlib import Path

from synapse_core import replay
from synapse_core.commands.registry import CommandContext, Registry
from synapse_core.state import BridgeState


def _user(text: str, ts: str) -> dict:
    return {
        "type": "user",
        "timestamp": ts,
        "message": {"role": "user", "content": text},
    }


def _assistant(text: str, ts: str) -> dict:
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }


def _tool_use(name: str, ts: str, tool_id: str = "t1") -> dict:
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": {}}],
        },
    }


def _tool_result(out: str, ts: str, tool_id: str = "t1") -> dict:
    return {
        "type": "user",
        "timestamp": ts,
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": out}],
        },
    }


def _write_jsonl(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False))
            f.write("\n")


def _read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            out.append(json.loads(raw))
    return out


def _seed_jsonl(tmp_path: Path, sid: str, events: list[dict]) -> Path:
    slug = replay.slug_for_cwd(str(tmp_path))
    jsonl = tmp_path / ".claude" / "projects" / slug / f"{sid}.jsonl"
    _write_jsonl(jsonl, events)
    return jsonl


def _event_text(ev: dict) -> str | None:
    """Pull text from a stored user/assistant jsonl event for assertion."""
    msg = ev.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict):
            return first.get("text")
    return None


class _Hooks:
    """Capture every closure invocation for assertion."""

    def __init__(self) -> None:
        self.audit_calls: list[tuple[str, str, str]] = []
        self.respawn_calls: list[tuple[str, str | None]] = []
        self.swap_calls: list[tuple[str | None, str | None]] = []
        self.replay_calls: list[str] = []

    def audit(self, kind: str, sid: str, status: str) -> None:
        self.audit_calls.append((kind, sid, status))

    def respawn(self, sid: str, model: str | None) -> None:
        self.respawn_calls.append((sid, model))

    def swap(self, model: str | None, sid: str | None) -> None:
        self.swap_calls.append((model, sid))

    def replay(self, text: str) -> None:
        self.replay_calls.append(text)


def _make_ctx(
    *,
    state: BridgeState,
    hooks: _Hooks,
    cwd: str,
    projects_root: Path | None = None,
) -> Registry:
    ctx = CommandContext(
        state=state,
        swap_provider=hooks.swap,
        close_provider=lambda: None,
        forget_session=lambda: None,
        audit_writer=hooks.audit,
        respawn_with_resume=hooks.respawn,
        replay_user_text=hooks.replay,
        cc_cwd=cwd,
        cc_projects_root=projects_root,
    )
    return Registry(ctx)


# ── /rewind ──────────────────────────────────────────────────────────────────


def test_rewind_n_truncates_jsonl_and_flags_dropped_turns(tmp_path: Path) -> None:
    sid = "abc12345"
    state = BridgeState(model="claude-opus-4-7[1m]", session_id=sid)
    jsonl = _seed_jsonl(
        tmp_path,
        sid,
        [
            _user("u1", "2026-06-02T10:00:00.000Z"),
            _assistant("a1", "2026-06-02T10:00:01.000Z"),
            _user("u2", "2026-06-02T10:01:00.000Z"),
            _assistant("a2", "2026-06-02T10:01:01.000Z"),
            _user("u3", "2026-06-02T10:02:00.000Z"),
            _assistant("a3", "2026-06-02T10:02:01.000Z"),
        ],
    )
    projects_root = tmp_path / ".claude" / "projects"
    hooks = _Hooks()
    reg = _make_ctx(
        state=state, hooks=hooks, cwd=str(tmp_path), projects_root=projects_root
    )

    verdict, reply = reg.dispatch("/rewind 2")

    assert verdict == "handled"
    assert reply is not None
    assert "失忆" in reply
    # jsonl: last 2 pairs dropped
    remaining = _read_jsonl(jsonl)
    texts = [_event_text(ev) for ev in remaining]
    assert texts == ["u1", "a1"]
    # session_block writes removed — they clobbered mm- (latest-wins).
    # Dropped turns are already gone from jsonl, no need for audit block.
    assert ("session_block", sid, "archive") not in hooks.audit_calls
    # respawn was triggered with the same sid + model.
    assert hooks.respawn_calls == [(sid, "claude-opus-4-7[1m]")]
    # Sanity: projects_root resolves the file.
    assert projects_root.is_dir()


def test_rewind_counts_real_user_prompts_skipping_tool_loop(tmp_path: Path) -> None:
    """`/rewind 2` drops the last 2 user prompts AND every assistant/tool_use/
    tool_result entry that came after them — tool_result lines (also type:user)
    do not count as separate turns. Reply shows N=real prompts rewound.
    """
    sid = "tool1234"
    state = BridgeState(model="claude-opus-4-7[1m]", session_id=sid)
    jsonl = _seed_jsonl(
        tmp_path,
        sid,
        [
            _user("u1", "2026-06-02T10:00:00.000Z"),
            _tool_use("Bash", "2026-06-02T10:00:01.000Z"),
            _tool_result("ok", "2026-06-02T10:00:02.000Z"),
            _assistant("a1 reply", "2026-06-02T10:00:03.000Z"),
            _user("u2", "2026-06-02T10:01:00.000Z"),
            _assistant("a2", "2026-06-02T10:01:01.000Z"),
            _user("u3", "2026-06-02T10:02:00.000Z"),
            _assistant("a3", "2026-06-02T10:02:01.000Z"),
        ],
    )
    projects_root = tmp_path / ".claude" / "projects"
    hooks = _Hooks()
    reg = _make_ctx(
        state=state, hooks=hooks, cwd=str(tmp_path), projects_root=projects_root
    )

    verdict, reply = reg.dispatch("/rewind 2")

    assert verdict == "handled"
    assert reply == "🧠失忆中，请稍候...(2)"
    # u1's full tool-use round survives intact.
    remaining = _read_jsonl(jsonl)
    assert len(remaining) == 4
    assert remaining[0]["type"] == "user"
    assert remaining[1]["message"]["content"][0]["type"] == "tool_use"
    assert remaining[2]["message"]["content"][0]["type"] == "tool_result"
    assert remaining[3]["message"]["content"][0]["text"] == "a1 reply"


def test_rewind_one_drops_whole_tool_use_round(tmp_path: Path) -> None:
    """`/rewind 1` on a tail with tool use drops the user prompt + tool_use +
    tool_result + final reply — not just one jsonl line."""
    sid = "tool5678"
    state = BridgeState(model="claude-opus-4-7[1m]", session_id=sid)
    jsonl = _seed_jsonl(
        tmp_path,
        sid,
        [
            _user("u1", "2026-06-02T10:00:00.000Z"),
            _assistant("a1", "2026-06-02T10:00:01.000Z"),
            _user("u2", "2026-06-02T10:01:00.000Z"),
            _tool_use("Read", "2026-06-02T10:01:01.000Z"),
            _tool_result("file body", "2026-06-02T10:01:02.000Z"),
            _assistant("a2 reply", "2026-06-02T10:01:03.000Z"),
        ],
    )
    projects_root = tmp_path / ".claude" / "projects"
    hooks = _Hooks()
    reg = _make_ctx(
        state=state, hooks=hooks, cwd=str(tmp_path), projects_root=projects_root
    )

    verdict, reply = reg.dispatch("/rewind 1")

    assert reply == "🧠失忆中，请稍候...(1)"
    remaining = _read_jsonl(jsonl)
    texts = [_event_text(ev) for ev in remaining]
    assert texts == ["u1", "a1"]


def test_rewind_rejects_zero(tmp_path: Path) -> None:
    sid = "abc"
    state = BridgeState(model="opus", session_id=sid)
    hooks = _Hooks()
    reg = _make_ctx(state=state, hooks=hooks, cwd=str(tmp_path))

    verdict, reply = reg.dispatch("/rewind 0")
    assert verdict == "handled"
    assert reply is not None
    assert "正整数" in reply
    assert hooks.audit_calls == []
    assert hooks.respawn_calls == []


def test_rewind_rejects_negative(tmp_path: Path) -> None:
    state = BridgeState(model="opus", session_id="abc")
    hooks = _Hooks()
    reg = _make_ctx(state=state, hooks=hooks, cwd=str(tmp_path))

    verdict, reply = reg.dispatch("/rewind -3")
    assert verdict == "handled"
    assert reply is not None
    assert "正整数" in reply
    assert hooks.respawn_calls == []


def test_rewind_rejects_missing_arg(tmp_path: Path) -> None:
    state = BridgeState(model="opus", session_id="abc")
    hooks = _Hooks()
    reg = _make_ctx(state=state, hooks=hooks, cwd=str(tmp_path))

    verdict, reply = reg.dispatch("/rewind")
    assert verdict == "handled"
    assert reply is not None
    # Usage hint or positive-int complaint — either way, no side effects.
    assert hooks.respawn_calls == []


def test_rewind_rejects_non_integer(tmp_path: Path) -> None:
    state = BridgeState(model="opus", session_id="abc")
    hooks = _Hooks()
    reg = _make_ctx(state=state, hooks=hooks, cwd=str(tmp_path))

    verdict, reply = reg.dispatch("/rewind abc")
    assert verdict == "handled"
    assert reply is not None
    assert hooks.respawn_calls == []


def test_rewind_without_sid_returns_error(tmp_path: Path) -> None:
    state = BridgeState(model="opus", session_id=None)
    hooks = _Hooks()
    reg = _make_ctx(state=state, hooks=hooks, cwd=str(tmp_path))

    verdict, reply = reg.dispatch("/rewind 1")
    assert verdict == "handled"
    assert reply is not None
    assert "无事可忘" in reply
    assert hooks.respawn_calls == []


def test_rewind_n_exceeds_pairs_still_succeeds(tmp_path: Path) -> None:
    """N>pairs available — handler should still complete (drop what exists,
    respawn). Marrow flag still written so the dropped turns never ingest."""
    sid = "fewer"
    state = BridgeState(model="opus", session_id=sid)
    _seed_jsonl(
        tmp_path,
        sid,
        [
            _user("u1", "2026-06-02T10:00:00.000Z"),
            _assistant("a1", "2026-06-02T10:00:01.000Z"),
        ],
    )
    hooks = _Hooks()
    reg = _make_ctx(state=state, hooks=hooks, cwd=str(tmp_path))

    verdict, reply = reg.dispatch("/rewind 99")
    assert verdict == "handled"
    assert reply is not None
    # Even if jsonl was untouched (different cwd in test env), respawn fired.
    assert hooks.respawn_calls == [(sid, "opus")]


# ── /regen ───────────────────────────────────────────────────────────────────


def test_regen_drops_last_pair_respawns_and_replays_user(tmp_path: Path) -> None:
    sid = "regen-sid"
    state = BridgeState(model="claude-opus-4-6[1m]", session_id=sid)
    jsonl = _seed_jsonl(
        tmp_path,
        sid,
        [
            _user("u1", "2026-06-02T10:00:00.000Z"),
            _assistant("a1", "2026-06-02T10:00:01.000Z"),
            _user("u2", "2026-06-02T10:01:00.000Z"),
            _assistant("a2-stale", "2026-06-02T10:01:01.000Z"),
        ],
    )
    projects_root = tmp_path / ".claude" / "projects"
    hooks = _Hooks()
    reg = _make_ctx(
        state=state, hooks=hooks, cwd=str(tmp_path), projects_root=projects_root
    )
    verdict, reply = reg.dispatch("/regen")

    assert verdict == "handled"
    assert reply is not None
    assert "失忆" in reply
    # Respawn fired with the same sid+model.
    assert hooks.respawn_calls == [(sid, "claude-opus-4-6[1m]")]
    # The dropped user prompt is pushed back on stdin so cc actually
    # regenerates — cc's --resume does not auto-replay.
    assert hooks.replay_calls == ["u2"]
    # session_block writes removed — clobbered mm- via latest-wins.
    block_calls = [c for c in hooks.audit_calls if c[0] == "session_block"]
    assert not block_calls, "session_block should not be written by regen/rewind"
    # On-disk: u2 + a2-stale both gone, u1/a1 kept.
    remaining = _read_jsonl(jsonl)
    assert [_event_text(ev) for ev in remaining] == ["u1", "a1"]


def test_regen_drops_whole_tool_use_round_and_replays_user(tmp_path: Path) -> None:
    """/regen on a tool-use tail drops the user prompt, tool_use, tool_result,
    and the final reply; replay_user_text receives the user prompt so cc
    regenerates it on the fresh provider."""
    sid = "regn1234"
    state = BridgeState(model="claude-opus-4-7[1m]", session_id=sid)
    jsonl = _seed_jsonl(
        tmp_path,
        sid,
        [
            _user("u1", "2026-06-02T10:00:00.000Z"),
            _tool_use("Bash", "2026-06-02T10:00:01.000Z"),
            _tool_result("ok", "2026-06-02T10:00:02.000Z"),
            _assistant("a1 stale", "2026-06-02T10:00:03.000Z"),
        ],
    )
    projects_root = tmp_path / ".claude" / "projects"
    hooks = _Hooks()
    reg = _make_ctx(
        state=state, hooks=hooks, cwd=str(tmp_path), projects_root=projects_root
    )

    verdict, reply = reg.dispatch("/regen")

    assert verdict == "handled"
    assert reply == "🧠失忆中，请稍候..."
    remaining = _read_jsonl(jsonl)
    assert [_event_text(ev) for ev in remaining] == []
    assert hooks.respawn_calls == [(sid, "claude-opus-4-7[1m]")]
    assert hooks.replay_calls == ["u1"]


def test_regen_without_sid_returns_error(tmp_path: Path) -> None:
    state = BridgeState(model="opus", session_id=None)
    hooks = _Hooks()
    reg = _make_ctx(state=state, hooks=hooks, cwd=str(tmp_path))

    verdict, reply = reg.dispatch("/regen")
    assert verdict == "handled"
    assert reply is not None
    assert "无事可忘" in reply
    assert hooks.respawn_calls == []


# ── default ctx hooks (no marrow / no respawn wired) ─────────────────────────


def test_rewind_default_ctx_hooks_are_safe(tmp_path: Path) -> None:
    """Bridge without marrow + without respawn wired must still ack /rewind."""
    state = BridgeState(model="opus", session_id="abc")
    ctx = CommandContext(
        state=state,
        swap_provider=lambda *_a, **_kw: None,
        close_provider=lambda: None,
        forget_session=lambda: None,
    )
    verdict, reply = Registry(ctx).dispatch("/rewind 1")
    assert verdict == "handled"
    assert reply is not None


def test_regen_default_ctx_hooks_are_safe(tmp_path: Path) -> None:
    state = BridgeState(model="opus", session_id="abc")
    ctx = CommandContext(
        state=state,
        swap_provider=lambda *_a, **_kw: None,
        close_provider=lambda: None,
        forget_session=lambda: None,
    )
    verdict, reply = Registry(ctx).dispatch("/regen")
    assert verdict == "handled"
    assert reply is not None


# ── loop integration: respawn_with_resume ────────────────────────────────────


def test_loop_respawn_with_resume_closes_and_lazyspawns(tmp_path: Path) -> None:
    """`MainLoop.respawn_with_resume(sid, model)` must close the live provider
    (so cc re-reads the trimmed jsonl) and spawn a fresh one with the given
    sid + model."""
    from datetime import datetime

    from synapse_core.debounce import InboundBuffer
    from synapse_wx.loop import MainLoop
    from synapse_core.providers.mock import EchoProvider
    from synapse_core.sessionend.tracker import SessionTracker

    state = BridgeState(model="claude-opus-4-6[1m]", session_id="sid-x")
    sessions = SessionTracker(state_path=tmp_path / "sessions.json")
    factory_calls: list[dict] = []

    def factory(model=None, resume_sid=None):
        factory_calls.append({"model": model, "resume_sid": resume_sid})
        p = EchoProvider()
        p.spawn()
        return p

    loop = MainLoop(
        ilink=object(),
        provider_factory=factory,
        state=state,
        sessions=sessions,
        idle_loop=None,
        buffer=InboundBuffer(),
        poll_interval_sec=0.01,
        wallclock=lambda: datetime(2026, 6, 2, 12, 0),
        sleeper=lambda _s: None,
        alert_dir=tmp_path / "alerts",
        channel="wx",
        last_active_path=tmp_path / "last_active.json",
        channel_label="CC-WX",
    )
    # Pre-spawn a live provider so we can verify it gets closed.
    live = EchoProvider()
    live.spawn()
    loop._provider = live

    loop.respawn_with_resume("sid-x", "claude-opus-4-6[1m]")

    assert not live.is_alive()  # closed
    assert factory_calls == [
        {"model": "claude-opus-4-6[1m]", "resume_sid": "sid-x"}
    ]
    assert loop._provider is not live
    assert loop._provider.is_alive()


def test_loop_respawn_with_resume_no_live_provider_still_spawns(tmp_path: Path) -> None:
    """Even if no provider is alive (e.g. post-idle close), respawn must spawn."""
    from datetime import datetime

    from synapse_core.debounce import InboundBuffer
    from synapse_wx.loop import MainLoop
    from synapse_core.providers.mock import EchoProvider
    from synapse_core.sessionend.tracker import SessionTracker

    state = BridgeState(model="opus", session_id="sid-y")
    sessions = SessionTracker(state_path=tmp_path / "sessions.json")
    calls: list[dict] = []

    def factory(model=None, resume_sid=None):
        calls.append({"model": model, "resume_sid": resume_sid})
        p = EchoProvider()
        p.spawn()
        return p

    loop = MainLoop(
        ilink=object(),
        provider_factory=factory,
        state=state,
        sessions=sessions,
        idle_loop=None,
        buffer=InboundBuffer(),
        poll_interval_sec=0.01,
        wallclock=lambda: datetime(2026, 6, 2, 12, 0),
        sleeper=lambda _s: None,
        alert_dir=tmp_path / "alerts",
        channel="wx",
        last_active_path=tmp_path / "last_active.json",
        channel_label="CC-WX",
    )
    assert loop._provider is None

    loop.respawn_with_resume("sid-y", "opus")
    assert calls == [{"model": "opus", "resume_sid": "sid-y"}]
    assert loop._provider is not None
    assert loop._provider.is_alive()
