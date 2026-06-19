"""B9 — atomic tail-truncate of cc's jsonl session file.

`drop_last_n_replies(sid, n, cwd, projects_root)` removes the last N
assistant reply cycles while keeping real user prompts in place. Returns
the dropped events in chronological order. Atomicity: write to `<file>.tmp`
then `os.replace`.

`extract_user_text(parsed)` pulls the user's typed text out of a
`type:user` jsonl event.

Both helpers tolerate missing files and N>turns_present without raising.
"""

from __future__ import annotations

import json
from pathlib import Path

from synapse_core import jsonl_edit, replay


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


# ── drop_last_n_replies ─────────────────────────────────────────────────────


def test_drop_last_n_replies_missing_file_returns_empty(tmp_path: Path) -> None:
    dropped = jsonl_edit.drop_last_n_replies(
        "no-such-sid",
        n=1,
        cwd=str(tmp_path),
        projects_root=tmp_path / ".claude" / "projects",
    )
    assert dropped == []


def test_drop_last_n_replies_empty_file_returns_empty(tmp_path: Path) -> None:
    sid = "empty"
    slug = replay.slug_for_cwd(str(tmp_path))
    jsonl = tmp_path / ".claude" / "projects" / slug / f"{sid}.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.touch()

    dropped = jsonl_edit.drop_last_n_replies(
        sid, n=2, cwd=str(tmp_path), projects_root=tmp_path / ".claude" / "projects"
    )
    assert dropped == []


def test_drop_last_n_replies_keeps_prompts_drops_replies(tmp_path: Path) -> None:
    sid = "abc12345"
    slug = replay.slug_for_cwd(str(tmp_path))
    jsonl = tmp_path / ".claude" / "projects" / slug / f"{sid}.jsonl"
    events = [
        _user("u1", "2026-06-02T10:00:00.000Z"),
        _assistant("a1", "2026-06-02T10:00:01.000Z"),
        _user("u2", "2026-06-02T10:01:00.000Z"),
        _assistant("a2", "2026-06-02T10:01:01.000Z"),
        _user("u3", "2026-06-02T10:02:00.000Z"),
        _assistant("a3", "2026-06-02T10:02:01.000Z"),
    ]
    _write_jsonl(jsonl, events)

    dropped = jsonl_edit.drop_last_n_replies(
        sid, n=2, cwd=str(tmp_path), projects_root=tmp_path / ".claude" / "projects"
    )
    remaining = _read_jsonl(jsonl)

    assert [_extract_text(e) for e in dropped] == ["a2", "u3", "a3"]
    assert [_extract_text(e) for e in remaining] == ["u1", "a1", "u2"]


def test_drop_last_n_replies_single_turn_keeps_user_prompt(tmp_path: Path) -> None:
    sid = "single"
    slug = replay.slug_for_cwd(str(tmp_path))
    jsonl = tmp_path / ".claude" / "projects" / slug / f"{sid}.jsonl"
    events = [
        _user("u1", "2026-06-02T10:00:00.000Z"),
        _assistant("a1", "2026-06-02T10:00:01.000Z"),
    ]
    _write_jsonl(jsonl, events)

    dropped = jsonl_edit.drop_last_n_replies(
        sid, n=1, cwd=str(tmp_path), projects_root=tmp_path / ".claude" / "projects"
    )
    remaining = _read_jsonl(jsonl)

    assert [_extract_text(e) for e in dropped] == ["a1"]
    assert [_extract_text(e) for e in remaining] == ["u1"]


def test_drop_last_n_replies_preserves_non_chat_events(tmp_path: Path) -> None:
    """system / summary / queue-operation lines are kept; only user/assistant
    reply cycles are removed, and ONLY the trailing N cycles."""
    sid = "withsys"
    slug = replay.slug_for_cwd(str(tmp_path))
    jsonl = tmp_path / ".claude" / "projects" / slug / f"{sid}.jsonl"
    events = [
        {"type": "summary", "summary": "k"},
        {"type": "system", "subtype": "init", "model": "claude-opus-4-6"},
        _user("u1", "2026-06-02T10:00:00.000Z"),
        _assistant("a1", "2026-06-02T10:00:01.000Z"),
        _user("u2", "2026-06-02T10:01:00.000Z"),
        _assistant("a2", "2026-06-02T10:01:01.000Z"),
    ]
    _write_jsonl(jsonl, events)

    dropped = jsonl_edit.drop_last_n_replies(
        sid, n=1, cwd=str(tmp_path), projects_root=tmp_path / ".claude" / "projects"
    )
    remaining = _read_jsonl(jsonl)

    assert [_extract_text(e) for e in dropped] == ["a2"]
    kept_types = [e.get("type") for e in remaining]
    assert kept_types == ["summary", "system", "user", "assistant", "user"]


def test_drop_last_n_replies_n_exceeds_available_drops_all_replies(tmp_path: Path) -> None:
    """N>turns_present should drop every reply without raising."""
    sid = "fewer"
    slug = replay.slug_for_cwd(str(tmp_path))
    jsonl = tmp_path / ".claude" / "projects" / slug / f"{sid}.jsonl"
    events = [
        {"type": "system", "subtype": "init"},
        _user("u1", "2026-06-02T10:00:00.000Z"),
        _assistant("a1", "2026-06-02T10:00:01.000Z"),
    ]
    _write_jsonl(jsonl, events)

    dropped = jsonl_edit.drop_last_n_replies(
        sid, n=10, cwd=str(tmp_path), projects_root=tmp_path / ".claude" / "projects"
    )
    remaining = _read_jsonl(jsonl)
    assert [_extract_text(e) for e in dropped] == ["a1"]
    assert [_extract_text(e) for e in remaining] == ["", "u1"]


def test_drop_last_n_replies_atomic_no_tmp_left_behind(tmp_path: Path) -> None:
    """After a successful drop, no `.tmp` file should linger next to the jsonl."""
    sid = "atomic"
    slug = replay.slug_for_cwd(str(tmp_path))
    jsonl_dir = tmp_path / ".claude" / "projects" / slug
    jsonl = jsonl_dir / f"{sid}.jsonl"
    _write_jsonl(
        jsonl,
        [
            _user("u1", "2026-06-02T10:00:00.000Z"),
            _assistant("a1", "2026-06-02T10:00:01.000Z"),
        ],
    )
    jsonl_edit.drop_last_n_replies(
        sid, n=1, cwd=str(tmp_path), projects_root=tmp_path / ".claude" / "projects"
    )
    tmp_siblings = [
        p for p in jsonl_dir.iterdir() if p.name.endswith(".tmp")
    ]
    assert tmp_siblings == []


def test_drop_last_n_replies_zero_or_negative_is_noop(tmp_path: Path) -> None:
    sid = "noop"
    slug = replay.slug_for_cwd(str(tmp_path))
    jsonl = tmp_path / ".claude" / "projects" / slug / f"{sid}.jsonl"
    events = [
        _user("u1", "2026-06-02T10:00:00.000Z"),
        _assistant("a1", "2026-06-02T10:00:01.000Z"),
    ]
    _write_jsonl(jsonl, events)

    dropped_zero = jsonl_edit.drop_last_n_replies(
        sid, n=0, cwd=str(tmp_path), projects_root=tmp_path / ".claude" / "projects"
    )
    dropped_neg = jsonl_edit.drop_last_n_replies(
        sid, n=-3, cwd=str(tmp_path), projects_root=tmp_path / ".claude" / "projects"
    )
    assert dropped_zero == []
    assert dropped_neg == []
    # File untouched.
    remaining = _read_jsonl(jsonl)
    assert [_extract_text(e) for e in remaining] == ["u1", "a1"]


def test_drop_last_n_replies_unmatched_trailing_user_is_noop(tmp_path: Path) -> None:
    """A trailing user without an assistant reply has nothing to regenerate."""
    sid = "trailing-user"
    slug = replay.slug_for_cwd(str(tmp_path))
    jsonl = tmp_path / ".claude" / "projects" / slug / f"{sid}.jsonl"
    _write_jsonl(
        jsonl,
        [
            _user("u1", "2026-06-02T10:00:00.000Z"),
            _assistant("a1", "2026-06-02T10:00:01.000Z"),
            _user("u2-orphan", "2026-06-02T10:01:00.000Z"),
        ],
    )
    dropped = jsonl_edit.drop_last_n_replies(
        sid, n=1, cwd=str(tmp_path), projects_root=tmp_path / ".claude" / "projects"
    )
    remaining = _read_jsonl(jsonl)

    assert dropped == []
    assert [_extract_text(e) for e in remaining] == ["u1", "a1", "u2-orphan"]


def test_drop_last_n_replies_drops_tool_result_cycle_keeps_prompt(tmp_path: Path) -> None:
    sid = "tool-cycle"
    slug = replay.slug_for_cwd(str(tmp_path))
    jsonl = tmp_path / ".claude" / "projects" / slug / f"{sid}.jsonl"
    _write_jsonl(
        jsonl,
        [
            _user("u1", "2026-06-02T10:00:00.000Z"),
            _tool_use("Read", "2026-06-02T10:00:01.000Z"),
            _tool_result("body", "2026-06-02T10:00:02.000Z"),
            _assistant("a1", "2026-06-02T10:00:03.000Z"),
        ],
    )

    dropped = jsonl_edit.drop_last_n_replies(
        sid, n=1, cwd=str(tmp_path), projects_root=tmp_path / ".claude" / "projects"
    )
    remaining = _read_jsonl(jsonl)

    assert [e["type"] for e in dropped] == ["assistant", "user", "assistant"]
    assert dropped[0]["message"]["content"][0]["type"] == "tool_use"
    assert dropped[1]["message"]["content"][0]["type"] == "tool_result"
    assert [_extract_text(e) for e in remaining] == ["u1"]


# ── drop_last_pair (regen) ───────────────────────────────────────────────────


def test_drop_last_pair_one_turn(tmp_path: Path) -> None:
    sid = "pair-one"
    slug = replay.slug_for_cwd(str(tmp_path))
    jsonl = tmp_path / ".claude" / "projects" / slug / f"{sid}.jsonl"
    events = [
        _user("u1", "2026-06-02T10:00:00.000Z"),
        _assistant("a1", "2026-06-02T10:00:01.000Z"),
    ]
    _write_jsonl(jsonl, events)

    dropped, has_remaining = jsonl_edit.drop_last_pair(
        sid, cwd=str(tmp_path), projects_root=tmp_path / ".claude" / "projects"
    )

    assert [_extract_text(e) for e in dropped] == ["u1", "a1"]
    assert has_remaining is False


def test_drop_last_pair_three_turns(tmp_path: Path) -> None:
    sid = "pair-three"
    slug = replay.slug_for_cwd(str(tmp_path))
    jsonl = tmp_path / ".claude" / "projects" / slug / f"{sid}.jsonl"
    events = [
        _user("u1", "2026-06-02T10:00:00.000Z"),
        _assistant("a1", "2026-06-02T10:00:01.000Z"),
        _user("u2", "2026-06-02T10:01:00.000Z"),
        _assistant("a2", "2026-06-02T10:01:01.000Z"),
        _user("u3", "2026-06-02T10:02:00.000Z"),
        _assistant("a3", "2026-06-02T10:02:01.000Z"),
    ]
    _write_jsonl(jsonl, events)

    dropped, has_remaining = jsonl_edit.drop_last_pair(
        sid, cwd=str(tmp_path), projects_root=tmp_path / ".claude" / "projects"
    )
    remaining = _read_jsonl(jsonl)

    assert [_extract_text(e) for e in dropped] == ["u3", "a3"]
    assert has_remaining is True
    assert [_extract_text(e) for e in remaining] == ["u1", "a1", "u2", "a2"]


# ── drop_last_n_pairs ───────────────────────────────────────────────────────


def test_drop_last_n_pairs_one_keeps_rewind_prompt(tmp_path: Path) -> None:
    sid = "pairs-one"
    slug = replay.slug_for_cwd(str(tmp_path))
    jsonl = tmp_path / ".claude" / "projects" / slug / f"{sid}.jsonl"
    events = [
        _user("u1", "2026-06-02T10:00:00.000Z"),
        _assistant("a1", "2026-06-02T10:00:01.000Z"),
        _user("u2", "2026-06-02T10:01:00.000Z"),
        _assistant("a2", "2026-06-02T10:01:01.000Z"),
        _user("u3", "2026-06-02T10:02:00.000Z"),
        _assistant("a3", "2026-06-02T10:02:01.000Z"),
    ]
    _write_jsonl(jsonl, events)

    dropped = jsonl_edit.drop_last_n_pairs(
        sid, n=1, cwd=str(tmp_path), projects_root=tmp_path / ".claude" / "projects"
    )
    remaining = _read_jsonl(jsonl)

    assert [_extract_text(e) for e in dropped] == ["a3"]
    assert [_extract_text(e) for e in remaining] == ["u1", "a1", "u2", "a2", "u3"]


def test_drop_last_n_pairs_two_keeps_rewind_prompt(tmp_path: Path) -> None:
    sid = "pairs-two"
    slug = replay.slug_for_cwd(str(tmp_path))
    jsonl = tmp_path / ".claude" / "projects" / slug / f"{sid}.jsonl"
    events = [
        _user("u1", "2026-06-02T10:00:00.000Z"),
        _assistant("a1", "2026-06-02T10:00:01.000Z"),
        _user("u2", "2026-06-02T10:01:00.000Z"),
        _assistant("a2", "2026-06-02T10:01:01.000Z"),
        _user("u3", "2026-06-02T10:02:00.000Z"),
        _assistant("a3", "2026-06-02T10:02:01.000Z"),
    ]
    _write_jsonl(jsonl, events)

    dropped = jsonl_edit.drop_last_n_pairs(
        sid, n=2, cwd=str(tmp_path), projects_root=tmp_path / ".claude" / "projects"
    )
    remaining = _read_jsonl(jsonl)

    assert [_extract_text(e) for e in dropped] == ["a2", "u3", "a3"]
    assert [_extract_text(e) for e in remaining] == ["u1", "a1", "u2"]


def test_drop_last_n_pairs_large_n_keeps_first_prompt(tmp_path: Path) -> None:
    sid = "pairs-large"
    slug = replay.slug_for_cwd(str(tmp_path))
    jsonl = tmp_path / ".claude" / "projects" / slug / f"{sid}.jsonl"
    events = [
        _user("u1", "2026-06-02T10:00:00.000Z"),
        _assistant("a1", "2026-06-02T10:00:01.000Z"),
        _user("u2", "2026-06-02T10:01:00.000Z"),
        _assistant("a2", "2026-06-02T10:01:01.000Z"),
        _user("u3", "2026-06-02T10:02:00.000Z"),
        _assistant("a3", "2026-06-02T10:02:01.000Z"),
    ]
    _write_jsonl(jsonl, events)

    dropped = jsonl_edit.drop_last_n_pairs(
        sid, n=100, cwd=str(tmp_path), projects_root=tmp_path / ".claude" / "projects"
    )
    remaining = _read_jsonl(jsonl)

    assert [_extract_text(e) for e in dropped] == ["a1", "u2", "a2", "u3", "a3"]
    assert [_extract_text(e) for e in remaining] == ["u1"]


# ── extract_user_text ───────────────────────────────────────────────────────


def test_extract_user_text_string_content() -> None:
    ev = _user("hello", "2026-06-02T10:00:00.000Z")
    assert jsonl_edit.extract_user_text(ev) == "hello"


def test_extract_user_text_list_content_concats_text_blocks() -> None:
    ev = {
        "type": "user",
        "timestamp": "2026-06-02T10:00:00.000Z",
        "message": {
            "role": "user",
            "content": [
                {"type": "text", "text": "hi "},
                {"type": "text", "text": "there"},
            ],
        },
    }
    assert jsonl_edit.extract_user_text(ev) == "hi there"


def test_extract_user_text_skips_tool_result_user_frames() -> None:
    # tool_result user frames are NOT Lumi's prose; helper must return None.
    ev = {
        "type": "user",
        "timestamp": "2026-06-02T10:00:00.000Z",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
            ],
        },
    }
    assert jsonl_edit.extract_user_text(ev) is None


def test_extract_user_text_handles_empty_and_non_user() -> None:
    assert jsonl_edit.extract_user_text(None) is None
    assert jsonl_edit.extract_user_text({"type": "assistant"}) is None
    assert (
        jsonl_edit.extract_user_text(
            {"type": "user", "message": {"role": "user", "content": ""}}
        )
        is None
    )


# ── helpers ─────────────────────────────────────────────────────────────────


def _extract_text(ev: dict) -> str:
    """Pull plain text from a stored user/assistant event for assertion."""
    msg = ev.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text")
                if isinstance(t, str):
                    return t
    return ""
