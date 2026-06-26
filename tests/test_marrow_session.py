"""Tests for synapse_wx.marrow_session (B1 sessions table integration)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from synapse_core import marrow_session
from synapse_wx.config import Config


def _cfg(**overrides) -> Config:
    base = Config()
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def _record(
    cfg: Config, sid: str, model: str | None, channel: str = "wx", **kwargs
) -> None:
    marrow_session.record_session(
        cfg.session_record_command, sid, model, channel=channel, **kwargs
    )


def _get_model(cfg: Config, sid: str) -> str | None:
    return marrow_session.get_session_model(cfg.session_get_model_command, sid)


def _list_recent(cfg: Config) -> list[dict]:
    return marrow_session.list_recent_sessions(
        cfg.session_list_recent_command, cfg.cc_projects_dir
    )


def _jsonl_path(cfg: Config, sid: str) -> Path | None:
    return marrow_session.jsonl_path_for_sid(cfg.cc_projects_dir, sid)


def _fallback_model(cfg: Config, sid: str) -> str | None:
    return marrow_session.fallback_model_from_jsonl(cfg.cc_projects_dir, sid)


def _session_cwd(cfg: Config, sid: str) -> str | None:
    return marrow_session.session_cwd(
        cfg.session_cwd_command, cfg.cc_projects_dir, sid
    )


def _resolve_model(cfg: Config, sid: str) -> str | None:
    return marrow_session.resolve_resume_model(
        cfg.session_get_model_command, cfg.cc_projects_dir, sid
    )


# ── record_session ─────────────────────────────────────────────────────────


def test_record_session_empty_sid_noop() -> None:
    with patch("synapse_core.marrow_session.subprocess.run") as run:
        _record(_cfg(), "", "m", "wx")
        run.assert_not_called()


def test_record_session_empty_template_noop() -> None:
    with patch("synapse_core.marrow_session.subprocess.run") as run:
        _record(_cfg(session_record_command=""), "sid-1", "m", "wx")
        run.assert_not_called()


def test_record_session_invokes_subprocess_with_fields() -> None:
    captured: dict = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    with patch("synapse_core.marrow_session.subprocess.run", side_effect=fake_run):
        _record(_cfg(), "sid-1", "claude-opus-4-6[1m]", "wx")
    cmd = captured["cmd"]
    assert "--sid" in cmd and "sid-1" in cmd
    assert "--model" in cmd and "claude-opus-4-6[1m]" in cmd
    assert "--channel" in cmd and "wx" in cmd


def test_record_session_swallows_oserror() -> None:
    with patch("synapse_core.marrow_session.subprocess.run", side_effect=OSError("nope")):
        _record(_cfg(), "sid-1", "m", "wx")


# ── mid_scan_command ───────────────────────────────────────────────────────


def test_mid_scan_command_uses_sessionend_python() -> None:
    cmd = marrow_session.mid_scan_command(
        "/Users/Gabrielle/CC-Lab/marrow/.venv/bin/python "
        "-m marrow.sessionend_async --sid {sid}",
        "tg",
    )

    assert cmd == (
        "/Users/Gabrielle/CC-Lab/marrow/.venv/bin/python "
        "-m marrow.mid_scan --sid '{sid}' --jsonl-path '{jsonl}' --channel tg"
    )


def test_mid_scan_command_quotes_jsonl_placeholder() -> None:
    cmd = marrow_session.mid_scan_command(
        "/opt/bin/python -m marrow.sessionend_async --sid {sid}",
        "wx",
    )

    assert "--jsonl-path '{jsonl}'" in cmd


def test_mid_scan_command_empty_when_marrow_opted_out() -> None:
    assert marrow_session.mid_scan_command("", "wx") == ""


def test_mid_scan_command_empty_for_unsupported_template() -> None:
    assert marrow_session.mid_scan_command("mw sessionend --sid {sid}", "wx") == ""


# ── get_session_model ──────────────────────────────────────────────────────


def test_get_session_model_returns_stdout_strip() -> None:
    fake = type("R", (), {"returncode": 0, "stdout": "claude-opus-4-6[1m]\n", "stderr": ""})()
    with patch("synapse_core.marrow_session.subprocess.run", return_value=fake):
        out = _get_model(_cfg(), "sid-1")
    assert out == "claude-opus-4-6[1m]"


def test_get_session_model_empty_returns_none() -> None:
    fake = type("R", (), {"returncode": 0, "stdout": "\n", "stderr": ""})()
    with patch("synapse_core.marrow_session.subprocess.run", return_value=fake):
        assert _get_model(_cfg(), "sid-1") is None


def test_get_session_model_nonzero_rc_returns_none() -> None:
    fake = type("R", (), {"returncode": 1, "stdout": "x", "stderr": "boom"})()
    with patch("synapse_core.marrow_session.subprocess.run", return_value=fake):
        assert _get_model(_cfg(), "sid-1") is None


def test_get_session_model_empty_sid_returns_none() -> None:
    assert _get_model(_cfg(), "") is None


# ── list_recent_sessions ───────────────────────────────────────────────────


def test_list_recent_sessions_parses_tsv(tmp_path: Path) -> None:
    payload = (
        "sid-a\tclaude-opus-4-6[1m]\twx\t/Users/test/NY\t2026-06-02T20:00:00Z\tlumi-wx\n"
        "sid-b\t-\tcli\t/Users/test/marrow\t2026-06-02T19:00:00Z\t\n"
    )
    fake = type("R", (), {"returncode": 0, "stdout": payload, "stderr": ""})()
    cfg = _cfg(cc_projects_dir=str(tmp_path))
    with patch("synapse_core.marrow_session.subprocess.run", return_value=fake):
        rows = _list_recent(cfg)
    assert len(rows) == 2
    assert rows[0]["sid"] == "sid-a"
    assert rows[0]["model"] == "claude-opus-4-6[1m]"
    assert rows[0]["title"] == "lumi-wx"
    # `-` model placeholder normalised to empty, no jsonl on disk → stays "".
    assert rows[1]["model"] == ""
    assert rows[1]["channel"] == "cli"


def test_list_recent_sessions_empty_returns_empty() -> None:
    fake = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    with patch("synapse_core.marrow_session.subprocess.run", return_value=fake):
        assert _list_recent(_cfg()) == []


def test_list_recent_sessions_fills_missing_model_from_jsonl(tmp_path: Path) -> None:
    """marrow.sessions model column empty → fall back to jsonl init scan.

    Covers cli sessions (no bridge write) and pre-B1 wx sessions: the picker
    must surface the real model so users see "Opus 4.6 [1M]" not "?".
    """
    _make_jsonl(
        tmp_path,
        "sid-missing-model",
        [{"type": "system", "subtype": "init", "model": "claude-opus-4-6[1m]"}],
    )
    payload = "sid-missing-model\t-\tcli\t/Users/test/marrow\t2026-06-02T19:00:00Z\t\n"
    fake = type("R", (), {"returncode": 0, "stdout": payload, "stderr": ""})()
    cfg = _cfg(cc_projects_dir=str(tmp_path))
    with patch("synapse_core.marrow_session.subprocess.run", return_value=fake):
        rows = _list_recent(cfg)
    assert len(rows) == 1
    assert rows[0]["model"] == "claude-opus-4-6[1m]"


# ── jsonl fallback ─────────────────────────────────────────────────────────


def _make_jsonl(dir_: Path, sid: str, lines: list[dict]) -> Path:
    slug = dir_ / "test-slug"
    slug.mkdir(parents=True, exist_ok=True)
    p = slug / f"{sid}.jsonl"
    p.write_text("\n".join(json.dumps(line) for line in lines))
    return p


def test_jsonl_path_for_sid_found(tmp_path: Path) -> None:
    p = _make_jsonl(tmp_path, "sid-x", [{"type": "system", "subtype": "init", "model": "m"}])
    cfg = _cfg(cc_projects_dir=str(tmp_path))
    assert _jsonl_path(cfg, "sid-x") == p


def test_jsonl_path_for_sid_missing(tmp_path: Path) -> None:
    cfg = _cfg(cc_projects_dir=str(tmp_path))
    assert _jsonl_path(cfg, "missing") is None


def test_fallback_model_picks_last_init(tmp_path: Path) -> None:
    _make_jsonl(
        tmp_path,
        "sid-x",
        [
            {"type": "system", "subtype": "init", "model": "claude-sonnet-4-6"},
            {"type": "user", "message": "hi"},
            {"type": "system", "subtype": "init", "model": "claude-opus-4-6[1m]"},
        ],
    )
    cfg = _cfg(cc_projects_dir=str(tmp_path))
    out = _fallback_model(cfg, "sid-x")
    assert out == "claude-opus-4-6[1m]"


def test_fallback_model_missing_jsonl(tmp_path: Path) -> None:
    cfg = _cfg(cc_projects_dir=str(tmp_path))
    assert _fallback_model(cfg, "missing") is None


# ── session_cwd ────────────────────────────────────────────────────────────


def test_session_cwd_returns_subprocess_stdout(tmp_path: Path) -> None:
    fake = type("R", (), {
        "returncode": 0, "stdout": "/Users/Gabrielle/Desktop/NY\n", "stderr": "",
    })()
    cfg = _cfg(cc_projects_dir=str(tmp_path))
    with patch("synapse_core.marrow_session.subprocess.run", return_value=fake):
        out = _session_cwd(cfg, "sid-1")
    assert out == "/Users/Gabrielle/Desktop/NY"


def test_session_cwd_empty_stdout_falls_back_to_jsonl(tmp_path: Path) -> None:
    _make_jsonl(
        tmp_path,
        "sid-cwd",
        [
            {"type": "system", "subtype": "init", "model": "claude-opus-4-6[1m]"},
            {"type": "user", "message": "hi", "cwd": "/Users/Gabrielle/CC-Lab"},
        ],
    )
    fake = type("R", (), {"returncode": 0, "stdout": "\n", "stderr": ""})()
    cfg = _cfg(cc_projects_dir=str(tmp_path))
    with patch("synapse_core.marrow_session.subprocess.run", return_value=fake):
        out = _session_cwd(cfg, "sid-cwd")
    assert out == "/Users/Gabrielle/CC-Lab"


def test_session_cwd_no_jsonl_returns_none(tmp_path: Path) -> None:
    fake = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    cfg = _cfg(cc_projects_dir=str(tmp_path))
    with patch("synapse_core.marrow_session.subprocess.run", return_value=fake):
        out = _session_cwd(cfg, "missing-sid")
    assert out is None


def test_session_cwd_empty_sid_returns_none() -> None:
    assert _session_cwd(_cfg(), "") is None


def test_session_cwd_empty_command_falls_back_to_jsonl(tmp_path: Path) -> None:
    _make_jsonl(
        tmp_path,
        "sid-nocmd",
        [{"type": "user", "cwd": "/tmp/test-project", "message": "hi"}],
    )
    cfg = _cfg(session_cwd_command="", cc_projects_dir=str(tmp_path))
    out = _session_cwd(cfg, "sid-nocmd")
    assert out == "/tmp/test-project"


# ── resolve_resume_model: sessions table wins, jsonl fallback ─────────────


def test_resolve_resume_model_sessions_wins(tmp_path: Path) -> None:
    _make_jsonl(
        tmp_path, "sid-x",
        [{"type": "system", "subtype": "init", "model": "jsonl-model"}],
    )
    cfg = _cfg(cc_projects_dir=str(tmp_path))
    fake = type("R", (), {"returncode": 0, "stdout": "sessions-model\n", "stderr": ""})()
    with patch("synapse_core.marrow_session.subprocess.run", return_value=fake):
        out = _resolve_model(cfg, "sid-x")
    assert out == "sessions-model"


def test_resolve_resume_model_falls_back_to_jsonl(tmp_path: Path) -> None:
    _make_jsonl(
        tmp_path, "sid-x",
        [{"type": "system", "subtype": "init", "model": "claude-jsonl-model"}],
    )
    cfg = _cfg(cc_projects_dir=str(tmp_path))
    fake = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    with patch("synapse_core.marrow_session.subprocess.run", return_value=fake):
        out = _resolve_model(cfg, "sid-x")
    assert out == "claude-jsonl-model"
