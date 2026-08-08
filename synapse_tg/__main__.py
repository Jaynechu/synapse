"""Entry point: `python -m synapse_tg` — boot the Telegram bridge."""

from __future__ import annotations

import fcntl
import logging
import os
import subprocess
import sys
from pathlib import Path

from telegram.error import NetworkError, TimedOut
from telegram.ext import Application, MessageHandler, filters
from telegram.request import HTTPXRequest

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

from .config import DEFAULT_LOG_PATH, load_config
from .loop import TgLoop

logger = logging.getLogger(__name__)

CHANNEL = "tg"


def _acquire_singleton_lock(path: Path) -> int:
    """Non-blocking exclusive flock on `path`, held for the caller's lifetime.
    Returns the open fd on success, -1 if another live process holds it. A
    stale lock from a dead pid is reclaimed automatically — the kernel drops
    an flock when its holder exits, so no pid probing is needed here."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return -1
    return fd


def _whitelist_filter(cfg) -> "filters.BaseFilter | None":
    """Build the sender gate from the effective whitelist (by Telegram user
    id, so an allowed sender is recognised in a group too). When group_ids is
    non-empty the gate also admits any member of those groups; private messages
    still require the user whitelist. None = accept-all (caller must log a
    startup warning)."""
    ids = cfg.effective_allowed_user_ids()
    user_filter = filters.User(user_id=set(ids)) if ids else None
    group_filter = (
        filters.Chat(chat_id=set(cfg.group_ids)) if cfg.group_ids else None
    )
    if user_filter is None and group_filter is None:
        return None
    if user_filter is None:
        return group_filter
    if group_filter is None:
        return user_filter
    return user_filter | group_filter


def main() -> int:
    # SYNAPSE_TG_CONFIG lets a second bot instance run the same code against
    # its own config (own token, data_dir, log file).
    config_path = os.environ.get("SYNAPSE_TG_CONFIG", "").strip()
    cfg = load_config(Path(config_path) if config_path else None)
    configure_logging(Path(cfg.log_file) if cfg.log_file else DEFAULT_LOG_PATH)
    if cfg.ack_overrides:
        messages.load_overrides(cfg.ack_overrides)

    if not cfg.bot_token:
        print("bot_token missing — set [bot] token in config.toml", file=sys.stderr)
        return 1

    # --- paths ---
    data_dir = cfg.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)

    lock_fd = _acquire_singleton_lock(data_dir / "synapse-tg.lock")
    if lock_fd < 0:
        print("synapse-tg already running — exiting", file=sys.stderr)
        return 1

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
            (k, v) for k, v in cfg.cwd_presets.items() if v
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
        # Empty on purpose: default_model only SEEDS a never-switched bridge.
        # /clear must follow the saved state.model so a /model switch sticks.
        clear_default_model="",
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
    # bridge_state.json (PERSISTED_KEYS) is the only source. No session_id
    # means the last window was retired on purpose (rotate / clear / fuse) or
    # this is a first boot — both start fresh. Never resurrect a sid from
    # anywhere else: "state has no sid" is, in practice, only ever reached
    # right after a deliberate retire.
    if state.session_id:
        logger.info("boot resume (persisted): sid=%s", state.session_id)

    # --- start ---
    idle_loop.start()

    # HTTPX timeouts. get_updates uses a separate request; its read timeout must
    # stay above the long-poll timeout (PTB default 10s) so a poll never times
    # out client-side before the server returns.
    _timeouts = dict(
        connect_timeout=cfg.http_connect_timeout_s,
        read_timeout=cfg.http_read_timeout_s,
        write_timeout=cfg.http_write_timeout_s,
        pool_timeout=cfg.http_pool_timeout_s,
    )
    # Resident idle listener: drains unsolicited (background-task) turns while
    # no send is pending so they deliver on completion instead of mispairing.
    listener_box: dict = {"task": None}
    # Cortex shell host (T9): scheduler task owning the silence cycle, wake
    # ledger and token fuse. Absent unless shell_id is in marrow's
    # [cortex].shells (T7, cfg.shell_active()).
    shell_box: dict = {"host": None, "task": None}

    async def _post_init(application) -> None:
        # Bridge-initiated rounds (shell note, unsolicited turn) must ship
        # straight after a restart, before any inbound message arrives.
        loop.attach_bot(application.bot)
        listener_box["task"] = application.create_task(loop._idle_listener())
        if cfg.shell_active():
            from .shell import ShellHost
            host = ShellHost(cfg, loop)
            shell_box["host"] = host
            loop.attach_shell(host)
            shell_box["task"] = application.create_task(host.run())
            logger.info("cortex shell host started (shell=%s)", cfg.shell_id)

    async def _post_shutdown(application) -> None:
        loop.stop_listener()
        host = shell_box["host"]
        if host is not None:
            host.stop()
        for task in (listener_box["task"], shell_box["task"]):
            if task is not None:
                task.cancel()

    app = (
        Application.builder()
        .token(cfg.bot_token)
        .request(HTTPXRequest(**_timeouts))
        .get_updates_request(HTTPXRequest(**_timeouts))
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    whitelist = _whitelist_filter(cfg)
    if whitelist is None:
        logger.warning(
            "tg bridge accepts inbound messages from ANY user — "
            "set [tg].chat_id or [tg].allowed_user_ids to restrict"
        )

    def _gated(f):
        return f if whitelist is None else whitelist & f

    app.add_handler(MessageHandler(_gated(filters.TEXT), loop.on_message))
    app.add_handler(MessageHandler(_gated(filters.PHOTO), loop.on_photo))
    app.add_handler(MessageHandler(_gated(filters.ANIMATION), loop.on_animation))
    app.add_handler(MessageHandler(_gated(filters.Document.ALL), loop.on_document))
    app.add_handler(MessageHandler(_gated(filters.Sticker.ALL), loop.on_sticker))
    app.add_handler(MessageHandler(_gated(filters.VIDEO), loop.on_video))
    app.job_queue.run_repeating(loop.check_flush, interval=0.5, first=0.5)

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
