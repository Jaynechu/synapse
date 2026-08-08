"""Kick spawn (synapse_core.cortex_kick): argv shape, text truncation, no-op
without a configured command. Every spawn is mocked — never launch a real
cortex.kick."""

from __future__ import annotations

from synapse_core import cortex_kick


def test_kick_no_cmd_noop(monkeypatch):
    monkeypatch.setattr(cortex_kick, "_warned_no_cmd", False)
    assert cortex_kick.kick(None, "wake") is False   # no cmd -> no spawn


def test_kick_spawns_with_kind_and_ids(monkeypatch):
    captured = {}

    class _P:
        def __init__(self, argv, **kw):
            captured["argv"] = argv

    monkeypatch.setattr(cortex_kick.subprocess, "Popen", _P)
    assert cortex_kick.kick(["py", "-m", "cortex.kick"], "timeout",
                            note_id=5, minutes=30)
    assert captured["argv"] == [
        "py", "-m", "cortex.kick", "--kind", "timeout",
        "--note-id", "5", "--minutes", "30"]


def test_kick_carries_truncated_text(monkeypatch):
    captured = {}

    class _P:
        def __init__(self, argv, **kw):
            captured["argv"] = argv

    monkeypatch.setattr(cortex_kick.subprocess, "Popen", _P)
    assert cortex_kick.kick(["py", "-m", "cortex.kick"], "wake",
                            note_id=7, text="x" * 500, text_chars=200)
    argv = captured["argv"]
    assert argv[:6] == [
        "py", "-m", "cortex.kick", "--kind", "wake", "--note-id"]
    assert "--text" in argv
    assert argv[argv.index("--text") + 1] == "x" * 200
