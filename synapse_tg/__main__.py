"""Entry point: `python -m synapse_tg` — boot the Telegram bridge."""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import sys
from pathlib import Path

from telegram.ext import Application, MessageHandler, filters

from synapse_core import marrow_session
from synapse_core.alerts import AlertSink
from synapse_core.commands import marrow_audit, messages
from synapse_core.commands.handlers import replay_for_channel
from synapse_core.commands.registry import CommandContext, Registry
from synapse_core.health import HealthGate
from synapse_core.sessionend.idle import IdleFireLoop
from synapse_core.sessionend.tracker import SessionTracker
from synapse_core.usage import UsageClient

from .config import load_config
from .loop import TgLoop

logger = logging.getLogger(__name__)

CHANNEL = "tg"


def _configure_logging() -> None:
    level = getattr(logging, os.environ.get("SYNAPSE_LOG_LEVEL", "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main() -> int:
    _configure_logging()
    cfg = load_config()
    if cfg.ack_overrides:
        messages.load_overrides(cfg.ack_overrides)

    if not cfg.bot_token:
        print("bot_token missing — set [bot] token in config.toml", file=sys.stderr)
        return 1

    # --- paths ---
    data_dir = cfg.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    marker_dir = data_dir / "markers"
    marker_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "alerts").mkdir(parents=True, exist_ok=True)
    session_state_path = data_dir / "sessions.json"
    audit_log_path = data_dir / "session_audit.log"
    sessionend_err_log = data_dir / "sessionend_err.log"
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

    def _idle_close(sid: str) -> None:
        lp = loop_box["loop"]
        if lp is not None:
            lp.idle_close_provider(sid)

    # --- idle fire loop ---
    idle_loop = IdleFireLoop(
        sessions=sessions,
        command_template=cfg.sessionend_command,
        marker_dir=marker_dir,
        audit_log=audit_log_path,
        sessionend_err_log=sessionend_err_log,
        channel=CHANNEL,
        cc_projects_dir=Path(cc_projects_dir),
        alerts=alerts,
        pre_spawn_hook=_idle_close,
    )

    # --- tg loop ---
    loop = TgLoop(
        cfg=cfg,
        sessions=sessions,
        record_session=_record_session,
        idle_loop=idle_loop,
    )
    loop_box["loop"] = loop
    state = loop._state

    # --- command closures ---
    def _fire_sessionend(sid: str) -> None:
        if not sid or not cfg.sessionend_command:
            return
        argv = shlex.split(cfg.sessionend_command.replace("{sid}", sid))
        try:
            subprocess.Popen(
                argv,
                stdout=open(sessionend_err_log, "a"),  # noqa: SIM115
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as e:
            logger.warning("fire_sessionend failed: %s", e)

    def _audit_writer(kind: str, sid: str, status: str) -> None:
        if not marrow_db:
            return
        if kind == "manual_skip":
            marrow_audit.write_skip(marrow_db, sid, status)
        elif kind == "session_block":
            marrow_audit.write_block(marrow_db, sid, status)

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

    # --- full command context ---
    ctx = CommandContext(
        state=state,
        swap_provider=loop._swap_provider,
        close_provider=loop._close_provider,
        forget_session=loop._forget_session,
        fire_sessionend=_fire_sessionend,
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
            sid=sid, n=5, cwd=state.cc_cwd,
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
    )
    loop._registry = Registry(ctx)

    # --- boot resume ---
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
                    logger.info("boot resume: sid=%s", candidate)

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

    logger.info("synapse-tg starting (long-poll)")
    try:
        app.run_polling()
    finally:
        health.stamp_clean_shutdown()
        idle_loop.stop()
        logger.info("synapse-tg shutdown complete")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
