"""CLI entry point: `python -m synapse_wx` — boot the bridge under launchd."""

from __future__ import annotations

import json
import logging
import os
import shlex
import signal
import time
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from synapse_core import bridge_state_store, marrow_session
from synapse_core.alerts import AlertSink
from synapse_core.commands import handlers as cmd_handlers
from .media import inbound as media_inbound
from .media import outbound as media_outbound
from synapse_core.commands import marrow_audit
from synapse_core.commands import messages as cmd_messages
from synapse_core.commands.registry import CommandContext, Registry
from .config import DEFAULT_CC_CWD, load_config
from synapse_core.debounce import InboundBuffer
from synapse_core.health import HealthGate
from synapse_core.logging_config import configure_logging
from .ilink import ILinkClient
from .ilink.rawlog import RawPollLogger
from .ilink.retry import DEFAULT_RETRYABLE, with_retry
from .loop import MainLoop
from synapse_core.providers.cc import ClaudeCodeProvider, MEDIA_SYSTEM_PROMPT, NIGHT_SYSTEM_PROMPT, QUOTE_SYSTEM_PROMPT, WX_ICLOUD_PROMPT
from synapse_core.sessionend.idle import IdleFireLoop
from synapse_core.sessionend.tracker import SessionTracker
from .sleep import SleepWakeObserver
from synapse_core.state import BridgeState
from synapse_core.usage import UsageClient

logger = logging.getLogger(__name__)

WX_STICKER_PROMPT = (
    "WeChat cannot display animated GIFs as stickers. "
    "Always call sticker with action='search', animated=false so only static stickers are returned."
)

WX_BUBBLE_FORMAT_PROMPT = (
    "Reply format (IM bubbles):\n"
    "- Blank line = new bubble. Single line break = new line inside the same bubble.\n"
    "- Type real line breaks only. Never write backslash-n as visible text — it renders literally in chat.\n"
    "- Casual chat: prefer short bubbles. Example (two bubbles):\n"
    "宝宝回来啦！\n"
    "\n"
    "想死我了\n"
    "- Q&A: length flex. Coding: concise & clear.\n"
    "- Deep topics / study: prefer longer, solid paragraphs.\n"
    "- Dot points: single line breaks, all in one bubble.\n"
    "- Prioritize readability. Match length to content — no filler.\n"
    "- Max 10 bubbles per reply.\n"
    "- Do not read or edit code unless explicitly asked.\n"
    "- Free to search docs and web."
)

CHANNEL = "wx"
CHANNEL_LABEL = "CC-WX"
CONFIG_DIR = Path.home() / ".config" / "synapse-wx"
LOG_DIR = Path.home() / "Library" / "Logs"
ALERTS_DIR = CONFIG_DIR / "alerts"
BRIDGE_STATE_PATH = CONFIG_DIR / "bridge_state.json"
HEALTH_STATE_PATH = CONFIG_DIR / "health.json"
LAST_ACTIVE_PATH = Path.home() / ".config" / "marrow" / "last_active.json"
SESSION_STATE_PATH = CONFIG_DIR / "sessions.json"
SESSION_MARKER_DIR = CONFIG_DIR / "markers"
SESSION_AUDIT_LOG = CONFIG_DIR / "session_audit.log"
SESSIONEND_ERR_LOG = LOG_DIR / "synapse-wx-sessionend.err.log"
CC_STDERR_LOG = LOG_DIR / "synapse-wx-cc-stderr.log"


def _wrap_ilink_with_alert_hook(ilink: ILinkClient, alerts: AlertSink) -> None:
    """Reapply the retry decorator on poll_messages/send_text so alerts.write fires
    after exhausted retries. The original decorator was applied without a hook;
    we re-wrap the underlying bound method to add the on_failure callback.
    """

    def on_failure(exc: Exception, attempts: int) -> None:
        alerts.write(
            "warn",
            "ilink_retry_exhausted",
            f"{type(exc).__name__}: {exc} after {attempts} attempts",
            source="ilink.with_retry",
        )

    # The methods on the class are already decorated; we re-decorate the bound
    # methods on the instance so the inner __wrapped__ retries again — instead,
    # patch in fresh wrappers around the raw underlying functions on the class
    # for this instance only.
    raw_poll = ILinkClient.poll_messages.__wrapped__  # type: ignore[attr-defined]
    raw_send = ILinkClient.send_text.__wrapped__  # type: ignore[attr-defined]

    decorated_poll = with_retry(
        retry_on=DEFAULT_RETRYABLE, on_failure=on_failure
    )(raw_poll)
    decorated_send = with_retry(
        retry_on=DEFAULT_RETRYABLE, on_failure=on_failure
    )(raw_send)

    ilink.poll_messages = decorated_poll.__get__(ilink, ILinkClient)  # type: ignore[assignment]
    ilink.send_text = decorated_send.__get__(ilink, ILinkClient)  # type: ignore[assignment]


def main() -> int:
    configure_logging(Path.home() / ".config/marrow/logs/synapse-wx/synapse-wx.log")
    cfg = load_config()
    if cfg.ack_overrides:
        cmd_messages.load_overrides(cfg.ack_overrides)

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    pid_path = CONFIG_DIR / "synapse-wx.pid"
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

    alerts = AlertSink(alerts_dir=ALERTS_DIR, marrow_repo_cmd=cfg.marrow_repo_cmd)
    media_inbound.set_inbound_alert_sink(alerts)
    media_outbound.set_outbound_alert_sink(alerts)

    raw_poll_logger = (
        RawPollLogger(cfg.raw_poll_log_until) if cfg.raw_poll_log_until else None
    )
    ilink = ILinkClient(
        raw_poll_logger=raw_poll_logger,
        quota_wait_sec=cfg.quota_wait_sec,
    )
    if raw_poll_logger is not None and raw_poll_logger.active():
        logger.info(
            "raw poll logging ON until %s → %s",
            cfg.raw_poll_log_until,
            raw_poll_logger._path,
        )
    _wrap_ilink_with_alert_hook(ilink, alerts)
    if not ilink.is_logged_in:
        print(
            "iLink not logged in; run "
            '`python -c "from synapse_wx.ilink import ILinkClient; ILinkClient().login()"`',
            file=sys.stderr,
        )
        return 1

    gate = HealthGate(state_path=HEALTH_STATE_PATH)
    gate.boot()
    pending_restart_announce = gate.should_announce_restart()

    state = BridgeState()
    # Overlay last-saved state — survives bridge crash so /effort etc. stick.
    persisted = bridge_state_store.load(BRIDGE_STATE_PATH)
    for k, v in persisted.items():
        if hasattr(state, k):
            setattr(state, k, v)
    # Fallback: a fresh persist file (or one written before any /swap) has
    # model=null; without this, provider_factory passes no --model and cc
    # uses its own default (currently opus-4-7), bypassing the WeChat default.
    if state.model is None:
        state.model = cfg.clear_default_model

    def _save_state() -> None:
        bridge_state_store.save(BRIDGE_STATE_PATH, asdict(state))

    sessions = SessionTracker(state_path=SESSION_STATE_PATH)
    # B11: pre_spawn_hook bound below once main_loop exists. We construct the
    # IdleFireLoop first so MainLoop can take it; the hook is a closure that
    # captures `main_loop_box` and resolves at fire-time.
    main_loop_box: dict[str, MainLoop | None] = {"loop": None}

    def _claimed_away(sid: str) -> None:
        ml = main_loop_box["loop"]
        if ml is not None:
            ml.close_provider()

    mid_cmd = marrow_session.mid_scan_command(cfg.sessionend_command, CHANNEL)
    idle_loop = IdleFireLoop(
        sessions=sessions,
        mid_sessionend_command=mid_cmd,
        marker_dir=SESSION_MARKER_DIR,
        audit_log=SESSION_AUDIT_LOG,
        channel=CHANNEL,
        claimed_away_hook=_claimed_away,
    )
    buffer = InboundBuffer()

    # cc_cwd resolution (in order): persisted state.cc_cwd if path still
    # exists → cfg.cc_cwd → DEFAULT_CC_CWD. /cwd flips state.cc_cwd then
    # triggers respawn.
    if state.cc_cwd is None or not os.path.isdir(state.cc_cwd):
        state.cc_cwd = cfg.cc_cwd or DEFAULT_CC_CWD

    def provider_factory(
        model: str | None = None, resume_sid: str | None = None
    ) -> ClaudeCodeProvider:
        return ClaudeCodeProvider(
            model=model if model is not None else state.model,
            resume_sid=resume_sid,
            cwd=state.cc_cwd,
            effort_level=state.effort_level,
            stderr_log=CC_STDERR_LOG,
            system_prompts=[QUOTE_SYSTEM_PROMPT, MEDIA_SYSTEM_PROMPT, WX_ICLOUD_PROMPT, WX_STICKER_PROMPT, WX_BUBBLE_FORMAT_PROMPT, NIGHT_SYSTEM_PROMPT],
            marrow_bridge=True,
            channel=CHANNEL,
            idle_soft_s=cfg.idle_soft_s,
            idle_hard_s=cfg.idle_hard_s,
            turn_output_cap=cfg.turn_output_cap,
        )

    main_loop = MainLoop(
        ilink=ilink,
        provider_factory=provider_factory,
        state=state,
        sessions=sessions,
        idle_loop=idle_loop,
        buffer=buffer,
        poll_interval_sec=cfg.poll_interval_sec,
        alerts=alerts,
        cfg=cfg,
        record_session=lambda sid, model: marrow_session.record_session(
            cfg.session_record_command,
            sid,
            model,
            channel=CHANNEL,
            effort=state.effort_level,
        ),
        channel=CHANNEL,
        last_active_path=LAST_ACTIVE_PATH,
        channel_label=CHANNEL_LABEL,
    )
    # B11: resolve the closure so idle-fire can close the live provider.
    main_loop_box["loop"] = main_loop

    marrow_db_expanded = (
        os.path.expanduser(cfg.marrow_db_path) if cfg.marrow_db_path else ""
    )

    def _audit_writer(kind: str, sid: str, status: str) -> None:
        if kind == "manual_skip":
            marrow_audit.write_skip(marrow_db_expanded, sid, status)
        elif kind == "session_block":
            marrow_audit.write_block(marrow_db_expanded, sid, status)
        elif kind == "force_sessionend":
            marrow_audit.write_force(marrow_db_expanded, sid, status)
        elif kind == "sessionend_extract":
            marrow_audit.write_extract(marrow_db_expanded, sid, status)

    def _compact_handler() -> str:
        vs = main_loop.state.voice_style if main_loop else None
        prov = main_loop._provider
        if prov is None or not getattr(prov, "alive", False):
            return cmd_messages.t("compact.no_cc", vs)
        send_raw = getattr(prov, "send_raw_user_text", None)
        if send_raw is None:
            return cmd_messages.t("compact.no_pipe", vs)
        send_raw("/compact")
        return cmd_messages.t("compact.piped", vs)

    def _send_extra_bubbles(bubbles: list[str]) -> None:
        """B6: push replay `[回放]` bubbles to the most-recent wx user.

        Called from the registry's `/resume <sid>` path BEFORE the swap so
        the user sees history before the ack. Best-effort; failures log and
        return so the resume itself can still proceed.
        """
        to = main_loop._last_from_wxid
        ctx = main_loop._last_ctx_token
        if not to:
            return
        for b in bubbles:
            try:
                ilink.send_text(to, ctx, b)
            except Exception as e:
                logger.warning("replay bubble send failed: %s", e)
                break

    def _fire_sessionend(sid: str) -> None:
        """User-initiated sessionend popen (/clear, /cwd).

        Same command template as IdleFireLoop but no marker/retry bookkeeping
        — one-shot user actions don't need fire-once guards. cc's SessionEnd
        hook in bridge mode skips its own popen by design (bridge_owns marker),
        so without this the sid never reaches marrow's LLM pipeline.
        """
        if not sid or not cfg.sessionend_command:
            return
        cmd_str = cfg.sessionend_command.replace("{sid}", sid)
        argv = shlex.split(cmd_str)
        if not argv:
            return
        try:
            subprocess.Popen(  # noqa: S603 - cmd template is operator-supplied config
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=open(SESSIONEND_ERR_LOG, "ab"),  # noqa: SIM115
                close_fds=True,
                start_new_session=True,
            )
        except (OSError, FileNotFoundError) as e:
            logger.warning("fire_sessionend spawn failed sid=%s: %s", sid[:8], e)

    usage_client = UsageClient()
    _MARROW_PY = os.environ.get(
        "MARROW_PYTHON",
        str(Path.home() / "CC-Lab/marrow/.venv/bin/python"),
    )
    _DIARY_SCRIPT = "\n".join([
        "import sys,json",
        "from datetime import datetime,timedelta",
        "from zoneinfo import ZoneInfo",
        "from marrow.timecue import parse_time_cue",
        "from marrow.daemon import recall",
        "_m=ZoneInfo('Australia/Melbourne')",
        "cue=parse_time_cue(sys.stdin.read().strip(),datetime.now(_m))",
        "if not cue:print('null');sys.exit(0)",
        "s=datetime.fromisoformat(cue.since_utc).astimezone(_m).strftime('%Y-%m-%d')",
        "u=(datetime.fromisoformat(cue.until_utc).astimezone(_m)-timedelta(days=1)).strftime('%Y-%m-%d')",
        "ds=[dict(c=r['content'],d=r.get('date',''))for r in recall(query='diary',since=s,until=u,limit=5)if r.get('kind')=='diary']",
        "print(json.dumps(ds or None))",
    ])

    def _fetch_diary(raw_date: str) -> tuple[str | None, str | None]:
        try:
            proc = subprocess.run(
                [_MARROW_PY, "-c", _DIARY_SCRIPT],
                input=raw_date, capture_output=True, text=True, timeout=15,
            )
            if proc.returncode != 0:
                return (None, None)
            data = json.loads(proc.stdout.strip())
            if not data:
                return (None, None)
            content = "\n---\n".join(d["c"] for d in data)
            label = data[0]["d"] or raw_date
            return (content, label)
        except Exception:
            return (None, None)

    def _record_effort(sid: str, effort: str) -> None:
        try:
            subprocess.run(
                ["mw", "add-session", "--sid", sid, "--effort", effort],
                capture_output=True, timeout=5.0,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            logger.warning("record_effort failed: %s", e)

    cmd_ctx = CommandContext(
        state=state,
        swap_provider=main_loop.swap_provider,
        close_provider=main_loop.close_provider,
        forget_session=main_loop.forget_session,
        fire_sessionend=_fire_sessionend,
        get_status=main_loop.get_status,
        resolve_resume_model=lambda sid: marrow_session.resolve_resume_model(
            cfg.session_get_model_command, cfg.cc_projects_dir, sid
        ),
        resolve_session_cwd=lambda sid: marrow_session.session_cwd(
            cfg.session_cwd_command, cfg.cc_projects_dir, sid
        ),
        clear_default_model=cfg.clear_default_model,
        list_recent_sessions=lambda: marrow_session.list_recent_sessions(
            cfg.session_list_recent_command, cfg.cc_projects_dir
        ),
        audit_writer=_audit_writer,
        replay_for_sid=lambda sid: cmd_handlers.replay_for_channel(
            sid, n=2, cwd=state.cc_cwd
        ),
        send_extra_bubbles=_send_extra_bubbles,
        respawn_with_resume=main_loop.respawn_with_resume,
        replay_user_text=main_loop.replay_user_text,
        cc_cwd=state.cc_cwd,
        channel="wx",
        compact_handler=_compact_handler,
        persist_state=_save_state,
        usage_client=usage_client.fetch,
        fetch_diary=_fetch_diary,
        record_effort=_record_effort,
        resolve_session_effort=lambda sid: marrow_session.get_session_effort(
            cfg.session_get_effort_command, sid
        ),
    )
    main_loop.set_registry(Registry(cmd_ctx))

    def on_sleep() -> None:
        logger.info("system will-sleep — pausing poll")
        main_loop.pause_poll()

    def on_wake() -> None:
        logger.info("system did-wake — reconnecting iLink + checking provider")
        try:
            ilink.reconnect()
        except Exception as e:
            logger.warning("ilink reconnect after wake failed: %s", e)
        if not main_loop._provider_alive():
            try:
                main_loop.swap_provider(state.model, state.session_id)
            except Exception as e:
                logger.warning("provider respawn after wake failed: %s", e)
                alerts.write(
                    "critical",
                    "provider_respawn_failed",
                    str(e),
                    source="__main__.on_wake",
                )
        main_loop.resume_poll()

    sleep_observer = SleepWakeObserver(
        will_sleep=on_sleep, did_wake=on_wake, alerts=alerts
    )
    sleep_observer.start()

    stop_evt_holder = {"stop": False}

    def _shutdown(signum: int, _frame) -> None:
        if stop_evt_holder["stop"]:
            return
        stop_evt_holder["stop"] = True
        logger.info("signal %s received; shutting down", signum)
        try:
            gate.stamp_clean_shutdown()
        except Exception as e:
            logger.warning("health stamp_clean_shutdown failed: %s", e)
        try:
            sleep_observer.stop()
        except Exception as e:
            logger.warning("sleep observer stop error: %s", e)
        try:
            main_loop.stop()
        finally:
            try:
                idle_loop.stop()
            except Exception as e:
                logger.warning("idle_loop stop error: %s", e)
            try:
                ilink.close()
            except Exception as e:
                logger.warning("ilink close error: %s", e)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # Bridge boot resume: if SessionTracker persisted a sid from the prior
    # process and its jsonl still exists, --resume it instead of spawning a
    # fresh cc. Keeps WeChat continuity across launchd restarts / crashes.
    # Single-user only: if multiple wxids are mapped we skip (no safe rule
    # for picking which to resume).
    boot_resume_sid: str | None = None
    _snap = sessions.snapshot()
    if len(_snap) == 1:
        candidate = next(iter(_snap.values()))
        if candidate:
            _projects_root = os.path.expanduser("~/.claude/projects")
            try:
                _has_jsonl = any(
                    os.path.isfile(os.path.join(_projects_root, d, f"{candidate}.jsonl"))
                    for d in os.listdir(_projects_root)
                    if os.path.isdir(os.path.join(_projects_root, d))
                )
            except OSError:
                _has_jsonl = False
            if _has_jsonl:
                boot_resume_sid = candidate
                logger.info("bridge boot: resuming sid=%s", candidate[:8])

    idle_loop.start()
    # Fail any crash-orphan 'claimed' wx outbox row before delivery starts —
    # never resent (duplicate to her phone beats lost).
    main_loop.sweep_outbox_orphans()
    main_loop.start(boot_resume_sid=boot_resume_sid)

    # Pre-warm the typing ticket so the first turn's TypingPing doesn't pay
    # the getconfig round-trip. send_typing(target_wxid, "") fires getconfig
    # then sendtyping with status=1; both are best-effort and swallowed.
    if cfg.target_wxid:
        try:
            ilink.send_typing(cfg.target_wxid, "")
        except Exception as e:
            logger.warning("typing ticket pre-warm failed: %s", e)

    if pending_restart_announce:
        if cfg.target_wxid:
            main_loop.arm_restart_announce(
                cfg.target_wxid,
                cmd_messages.t(
                    "restart.bubble",
                    state.voice_style,
                    channel_label=CHANNEL_LABEL,
                ),
            )
        else:
            logger.warning(
                "previous boot crashed but cfg.target_wxid is empty; "
                "skipping restart self-announce"
            )

    try:
        main_loop.join()
    except KeyboardInterrupt:
        _shutdown(signal.SIGINT, None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
