"""Entry point: `python -m synapse_tg` — boot the Telegram bridge."""

from __future__ import annotations

import logging
import os
import signal
import time
import subprocess
import sys
from pathlib import Path

from telegram.error import NetworkError, TimedOut
from telegram.ext import Application, MessageHandler, filters

from synapse_core import marrow_session
from synapse_core.alerts import AlertSink
from synapse_core.commands import marrow_audit, messages
from synapse_core.commands.handlers import replay_for_channel
from synapse_core.commands.registry import CommandContext, Registry
from synapse_core.health import HealthGate
from synapse_core.logging_config import configure_logging
from synapse_core.sessionend.idle import IdleFireLoop
from synapse_core.sessionend.tracker import SessionTracker
from synapse_core.usage import UsageClient

from .config import load_config
from .loop import TgLoop

logger = logging.getLogger(__name__)

CHANNEL = "tg"


def main() -> int:
    configure_logging(Path.home() / ".config/marrow/logs/synapse-tg/synapse-tg.log")
    cfg = load_config()
    if cfg.ack_overrides:
        messages.load_overrides(cfg.ack_overrides)

    if not cfg.bot_token:
        print("bot_token missing — set [bot] token in config.toml", file=sys.stderr)
        return 1

    # --- paths ---
    data_dir = cfg.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)

    pid_path = data_dir / "synapse-tg.pid"
    if pid_path.exists():
        try:
            old_pid = int(pid_path.read_text().strip())
            os.kill(old_pid, signal.SIGTERM)
            logger.info("sent SIGTERM to stale process %d", old_pid)
            time.sleep(1)
            try:
                os.kill(old_pid, 0)
                os.kill(old_pid, signal.SIGKILL)
                logger.info("SIGKILL stale process %d", old_pid)
            except ProcessLookupError:
                pass
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    pid_path.write_text(str(os.getpid()))

    marker_dir = data_dir / "markers"
    marker_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "alerts").mkdir(parents=True, exist_ok=True)
    session_state_path = data_dir / "sessions.json"
    audit_log_path = data_dir / "session_audit.log"
    marrow_db = str(Path(cfg.marrow_db).expanduser()) if cfg.marrow_db else ""
    cc_projects_dir = str(Path(cfg.cc_projects_dir).expanduser())

    # --- infrastructure ---
    alerts = AlertSink(alerts_dir=data_dir / "alerts")
    health = HealthGate(state_path=data_dir / "health.json")
    boot_info = health.boot()
    if health.should_announce_restart():
        logger.warning("unclean restart detected: %s", boot_info)

    sessions = SessionTracker(state_path=session_state_path)
    usage_client = UsageClient()

    # --- closures (box pattern for deferred loop ref) ---
    loop_box: dict = {"loop": None}

    def _record_session(sid: str, model: str) -> None:
        if not cfg.session_record_command:
            return
        lp = loop_box["loop"]
        effort = lp._state.effort_level if lp else "medium"
        marrow_session.record_session(
            session_record_command=cfg.session_record_command,
            sid=sid, model=model, channel=CHANNEL, effort=effort,
        )

    # --- idle fire loop ---
    def _claimed_away(sid: str) -> None:
        lp = loop_box["loop"]
        if lp is not None:
            lp._close_provider()

    idle_loop = IdleFireLoop(
        sessions=sessions,
        marker_dir=marker_dir,
        audit_log=audit_log_path,
        channel=CHANNEL,
        cc_projects_dir=Path(cc_projects_dir),
        claimed_away_hook=_claimed_away,
    )

    # --- tg loop ---
    loop = TgLoop(
        cfg=cfg,
        sessions=sessions,
        record_session=_record_session,
        idle_loop=idle_loop,
        alerts=alerts,
    )
    loop_box["loop"] = loop
    state = loop._state

    # --- command closures ---
    def _audit_writer(kind: str, sid: str, status: str) -> None:
        if not marrow_db:
            return
        if kind == "manual_skip":
            marrow_audit.write_skip(marrow_db, sid, status)
        elif kind == "session_block":
            marrow_audit.write_block(marrow_db, sid, status)
        elif kind == "force_sessionend":
            marrow_audit.write_force(marrow_db, sid, status)
        elif kind == "sessionend_extract":
            marrow_audit.write_extract(marrow_db, sid, status)

    def _send_extra_bubbles(bubbles: list[str]) -> None:
        loop._queued_extra_bubbles.extend(bubbles)

    def _compact_handler() -> str:
        lp = loop_box["loop"]
        vs = lp._state.voice_style if lp else None
        if lp is None or lp._provider is None or not lp._provider.alive:
            return messages.t("compact.no_cc", vs)
        send_raw = getattr(lp._provider, "send_raw_user_text", None)
        if send_raw is None:
            return messages.t("compact.no_pipe", vs)
        send_raw("/compact")
        return messages.t("compact.piped", vs)

    # --- cwd presets (inject from config into registry module) ---
    if cfg.cwd_presets:
        import synapse_core.commands.registry as _reg
        _reg._CWD_PRESETS = tuple(
            v for _, v in sorted(cfg.cwd_presets.items()) if v
        )

    def _record_effort(sid: str, effort: str) -> None:
        try:
            subprocess.run(
                ["mw", "add-session", "--sid", sid, "--effort", effort],
                capture_output=True, timeout=5.0,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            logger.warning("record_effort failed: %s", e)

    # --- full command context ---
    ctx = CommandContext(
        state=state,
        swap_provider=loop._swap_provider,
        close_provider=loop._close_provider,
        forget_session=loop._forget_session,
        get_status=loop.get_status,
        commands_doc_path=Path(__file__).resolve().parents[1] / "COMMANDS.md",
        resolve_resume_model=lambda sid: marrow_session.resolve_resume_model(
            session_get_model_command=cfg.session_get_model_command,
            cc_projects_dir=cc_projects_dir,
            sid=sid,
        ),
        clear_default_model=cfg.default_model,
        list_recent_sessions=lambda: marrow_session.list_recent_sessions(
            session_list_recent_command=cfg.session_list_recent_command,
            cc_projects_dir=cc_projects_dir,
        ),
        persist_state=loop._persist_state,
        audit_writer=_audit_writer,
        replay_for_sid=lambda sid: replay_for_channel(
            sid=sid, n=2, cwd=state.cc_cwd,
        ),
        send_extra_bubbles=_send_extra_bubbles,
        respawn_with_resume=loop.respawn_with_resume,
        replay_user_text=loop.replay_user_text,
        cc_cwd=state.cc_cwd,
        channel="tg",
        cc_projects_root=Path(cc_projects_dir),
        usage_client=usage_client.fetch,
        resolve_session_cwd=lambda sid: marrow_session.session_cwd(
            session_cwd_command=cfg.session_cwd_command,
            cc_projects_dir=cc_projects_dir,
            sid=sid,
        ),
        fetch_diary=loop._make_fetch_diary(),
        compact_handler=_compact_handler,
        record_effort=_record_effort,
        resolve_session_effort=lambda sid: marrow_session.get_session_effort(
            cfg.session_get_effort_command, sid,
        ),
    )
    loop._registry = Registry(ctx)

    # --- boot resume ---
    # bridge_state.json (PERSISTED_KEYS) is authoritative; sessions.json
    # is fallback only when state has no session_id (e.g. first boot).
    if not state.session_id:
        snap = sessions.snapshot()
        if snap:
            candidate = next(iter(snap.values()), None)
            if candidate:
                p_root = Path(cc_projects_dir)
                if p_root.exists():
                    has_jsonl = any(
                        (p_root / d / f"{candidate}.jsonl").is_file()
                        for d in os.listdir(str(p_root))
                        if (p_root / d).is_dir()
                    )
                    if has_jsonl:
                        state.session_id = candidate
                        logger.info("boot resume (sessions.json fallback): sid=%s", candidate)
    else:
        logger.info("boot resume (persisted): sid=%s", state.session_id)

    # --- start ---
    idle_loop.start()

    app = Application.builder().token(cfg.bot_token).build()
    app.add_handler(MessageHandler(filters.TEXT, loop.on_message))
    app.add_handler(MessageHandler(filters.PHOTO, loop.on_photo))
    app.add_handler(MessageHandler(filters.ANIMATION, loop.on_animation))
    app.add_handler(MessageHandler(filters.Document.ALL, loop.on_document))
    app.add_handler(MessageHandler(filters.Sticker.ALL, loop.on_sticker))
    app.add_handler(MessageHandler(filters.VIDEO, loop.on_video))
    app.job_queue.run_repeating(loop.check_flush, interval=0.5, first=0.5)

    if cfg.chat_id is None:
        logger.warning("outbox: [tg].chat_id not set — outbound note delivery disabled")
    else:
        loop.sweep_outbox_orphans()
        app.job_queue.run_repeating(
            loop.outbox_poll,
            interval=cfg.outbox_poll_interval_s,
            first=cfg.outbox_poll_interval_s,
        )

    async def _error_handler(update, context):
        if isinstance(context.error, (NetworkError, TimedOut)):
            logger.warning("transient network error (auto-retry): %s", context.error)
            return
        logger.exception("unhandled error", exc_info=context.error)

    app.add_error_handler(_error_handler)

    logger.info("synapse-tg starting (long-poll)")
    try:
        app.run_polling()
    finally:
        loop._close_provider()
        health.stamp_clean_shutdown()
        idle_loop.stop()
        logger.info("synapse-tg shutdown complete")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
