"""Bridge ↔ marrow `sessions` table thin layer.

All marrow access goes through the templated subprocess pattern
(DESIGN goal #2 — marrow integration is one templated command string in
config). Bridge does best-effort fire-and-forget for writes; reads collect
stdout. Failures NEVER block the loop — they downgrade to None / jsonl
fallback so the bridge runs even when marrow is offline or absent.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_MODEL_RE = re.compile(rb'"model"\s*:\s*"(claude-[^"]+)"')


def _format(template: str, **fields: str) -> list[str] | None:
    """Substitute templated fields and shlex-split for subprocess. Returns
    None when the template is empty (marrow integration opted-out)."""
    if not template:
        return None
    formatted = template.format(**fields)
    try:
        return shlex.split(formatted)
    except ValueError as e:
        logger.warning("marrow_session: shlex split failed for %r: %s", formatted, e)
        return None


def record_session(
    session_record_command: str,
    sid: str,
    model: str | None,
    channel: str,
    title: str = "",
    effort: str = "",
) -> None:
    """Best-effort upsert into marrow.sessions. Never raises."""
    if not sid:
        return
    cmd = _format(
        session_record_command,
        sid=sid,
        model=model or "",
        channel=channel or "",
        title=title or "",
        effort=effort or "",
    )
    if cmd is None:
        return
    try:
        subprocess.run(cmd, check=False, capture_output=True, timeout=5.0)
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("record_session subprocess failed: %s", e)


def get_session_model(session_get_model_command: str, sid: str) -> str | None:
    """Best-effort read of the persisted model for sid. None on miss/error."""
    if not sid:
        return None
    cmd = _format(session_get_model_command, sid=sid)
    if cmd is None:
        return None
    try:
        r = subprocess.run(cmd, check=False, capture_output=True, timeout=5.0, text=True)
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("get_session_model subprocess failed: %s", e)
        return None
    if r.returncode != 0:
        return None
    out = (r.stdout or "").strip()
    return out or None


def get_session_created_at(session_created_command: str, sid: str) -> str | None:
    """Best-effort read of created_at ISO timestamp for sid. None on miss/error."""
    if not sid:
        return None
    cmd = _format(session_created_command, sid=sid)
    if cmd is None:
        return None
    try:
        r = subprocess.run(cmd, check=False, capture_output=True, timeout=5.0, text=True)
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("get_session_created_at subprocess failed: %s", e)
        return None
    if r.returncode != 0:
        return None
    out = (r.stdout or "").strip()
    return out or None


def list_recent_sessions(
    session_list_recent_command: str,
    cc_projects_dir: str | Path | None,
) -> list[dict]:
    """Best-effort fetch of the recent-session picker rows.

    Output is tab-separated lines: `sid\\tmodel\\tchannel\\tcwd\\tlast_active\\ttitle`.
    Returns [] on miss / error.

    For any row with an empty model column the jsonl fallback fills it from
    the session's last system/init event — covers cli sessions the bridge
    never recorded, and wx sessions written before B1 wired record_session.
    """
    cmd = _format(session_list_recent_command)
    if cmd is None:
        return []
    try:
        r = subprocess.run(cmd, check=False, capture_output=True, timeout=5.0, text=True)
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("list_recent_sessions subprocess failed: %s", e)
        return []
    if r.returncode != 0:
        return []
    out: list[dict] = []
    for raw in (r.stdout or "").splitlines():
        parts = raw.split("\t")
        if len(parts) < 5:
            continue
        sid, model, channel, cwd = parts[0], parts[1], parts[2], parts[3]
        last_active = parts[4]
        title = parts[5] if len(parts) > 5 else ""
        model_clean = model if model != "-" else ""
        if not model_clean and sid:
            try:
                model_clean = fallback_model_from_jsonl(cc_projects_dir, sid) or ""
            except Exception as e:
                logger.warning("list_recent_sessions jsonl fallback failed for %s: %s", sid, e)
                model_clean = ""
        out.append(
            {
                "sid": sid,
                "model": model_clean,
                "channel": channel if channel != "-" else "",
                "cwd": cwd,
                "last_active": last_active,
                "title": title,
            }
        )
    return out


# ── jsonl fallback ─────────────────────────────────────────────────────────


def _projects_dir(cc_projects_dir: str | Path | None) -> Path:
    return Path(cc_projects_dir) if cc_projects_dir else Path.home() / ".claude" / "projects"


def jsonl_path_for_sid(cc_projects_dir: str | Path | None, sid: str) -> Path | None:
    """Find ~/.claude/projects/<slug>/<sid>.jsonl across all slug dirs."""
    if not sid:
        return None
    root = _projects_dir(cc_projects_dir)
    if not root.is_dir():
        return None
    for slug_dir in root.iterdir():
        if not slug_dir.is_dir():
            continue
        candidate = slug_dir / f"{sid}.jsonl"
        if candidate.is_file():
            return candidate
    return None


def fallback_model_from_jsonl(cc_projects_dir: str | Path | None, sid: str) -> str | None:
    """Grep the whole jsonl for ``"model":"claude-..."``; return the LAST match.

    Lumi never mid-session swaps model, but cc's system/init event lives at
    the file START — a tail-only scan misses it on long sessions. Full-file
    last-match is correct for both new sessions and the (unused) swap case.
    """
    path = jsonl_path_for_sid(cc_projects_dir, sid)
    if path is None:
        return None
    try:
        matches = _MODEL_RE.findall(path.read_bytes())
    except OSError as e:
        logger.warning("fallback_model jsonl read failed: %s", e)
        return None
    return matches[-1].decode("utf-8") if matches else None


def session_cwd(
    session_cwd_command: str,
    cc_projects_dir: str | Path | None,
    sid: str,
) -> str | None:
    """Best-effort cwd lookup for a session: mw command → jsonl fallback.

    Returns the stripped cwd string, or None if both paths miss.
    """
    if not sid:
        return None
    # Primary: mw get-session-cwd --sid <sid>
    cmd = _format(session_cwd_command, sid=sid)
    if cmd is not None:
        try:
            r = subprocess.run(cmd, check=False, capture_output=True, timeout=5.0, text=True)
            out = (r.stdout or "").strip()
            if out:
                return out
        except (OSError, subprocess.TimeoutExpired) as e:
            logger.warning("session_cwd subprocess failed: %s", e)
    # Fallback: first top-level "cwd" value in the first 20 jsonl lines.
    path = jsonl_path_for_sid(cc_projects_dir, sid)
    if path is None:
        return None
    try:
        with path.open(encoding="utf-8") as fh:
            for i, raw in enumerate(fh):
                if i >= 20:
                    break
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                cwd = obj.get("cwd") if isinstance(obj, dict) else None
                if cwd and isinstance(cwd, str):
                    return cwd
    except OSError as e:
        logger.warning("session_cwd jsonl fallback failed: %s", e)
    return None


def resolve_resume_model(
    session_get_model_command: str,
    cc_projects_dir: str | Path | None,
    sid: str,
) -> str | None:
    """B1 /resume lookup: sessions table → jsonl tail-grep fallback.

    Each branch is logged at DEBUG so future bugs are debuggable; the WARN
    line only fires when BOTH lookups miss (so the registry's downstream
    fallback chain has to land on state.model / clear_default).
    """
    if not sid:
        return None
    db_model = get_session_model(session_get_model_command, sid)
    if db_model:
        logger.debug("resolve_resume_model(%s): marrow → %s", sid, db_model)
        return db_model
    try:
        jsonl_model = fallback_model_from_jsonl(cc_projects_dir, sid)
    except Exception as e:
        logger.warning("resolve_resume_model jsonl fallback raised for %s: %s", sid, e)
        jsonl_model = None
    if jsonl_model:
        logger.debug("resolve_resume_model(%s): jsonl → %s", sid, jsonl_model)
        return jsonl_model
    logger.warning("resolve_resume_model(%s): both marrow + jsonl miss", sid)
    return None
