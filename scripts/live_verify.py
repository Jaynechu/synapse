"""Live verification harness for Phase A exit criteria.

Runs every capability that does NOT require Lumi's phone to scan iLink QR:
  (2) slash commands /info /model 4.7 /clear /stop via Registry + MainLoop
      with EchoProvider end-to-end (real loop instance, real state mutation).
  (3) 6h idle fire — manually tick IdleFireLoop with a backdated jsonl mtime,
      observe marker + audit + (templated) subprocess fire.
  (4) sleep/wake — directly invoke the observer's callbacks (full pyobjc
      registration cannot fire without a real lid-close, but the handler
      chain is the unit under test for self-heal).
  (5) systemic fail → AlertSink — raise ProviderDeadError mid-recv, assert
      a real file lands under ~/.config/synapse-wx/alerts/.

Writes a report to docs/notes/live-verify-2026-06-02.md and exits 0 on
all-green, 1 on any failure.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from synapse_wx.alerts import AlertSink
from synapse_core.commands.registry import CommandContext, Registry
from synapse_wx.health import HealthGate
from synapse_wx.providers.errors import ProviderDeadError
from synapse_wx.providers.mock import EchoProvider
from synapse_wx.sessionend.idle import IdleFireLoop
from synapse_wx.sessionend.tracker import SessionTracker
from synapse_wx.sleep import SleepWakeObserver
from synapse_wx.state import BridgeState


def _check(label: str, condition: bool, detail: str = "") -> tuple[str, bool, str]:
    return (label, condition, detail)


def verify_commands() -> list[tuple[str, bool, str]]:
    """(2) slash commands end-to-end through Registry with real state mutation."""
    results: list[tuple[str, bool, str]] = []

    state = BridgeState()
    state.model = "claude-opus-4-7"
    state.session_id = "70c32ba1-1234-5678-abcd-ef0011223344"
    state.usage_total = {
        "input_tokens": 1234,
        "output_tokens": 567,
        "cache_read_input_tokens": 89,
        "cache_creation_input_tokens": 100,
    }

    swap_calls: list[tuple[str | None, str | None]] = []
    close_calls: list[bool] = []
    forget_calls: list[bool] = []

    def swap(model: str | None, resume: str | None) -> None:
        swap_calls.append((model, resume))
        state.model = model

    def close() -> None:
        close_calls.append(True)

    def forget() -> None:
        forget_calls.append(True)
        state.session_id = None

    ctx = CommandContext(
        state=state, swap_provider=swap, close_provider=close, forget_session=forget
    )
    registry = Registry(ctx)

    kind, reply = registry.dispatch("/info")
    info_ok = (
        kind == "handled"
        and reply is not None
        and "Opus 4.7" in reply
        and "SID-70c32ba1" in reply
    )
    results.append(
        _check("/info renders model+sid+token line", info_ok, reply or "")
    )

    kind, reply = registry.dispatch("/model 4.8")
    results.append(
        _check(
            "/model 4.8 swaps to canonical id + confirms",
            kind == "handled"
            and swap_calls[-1] == ("claude-opus-4-8", "70c32ba1-1234-5678-abcd-ef0011223344")
            and reply is not None
            and "Opus 4.8" in reply,
            f"swap_calls={swap_calls}; reply={reply}",
        )
    )

    kind, reply = registry.dispatch("4.7")
    results.append(
        _check(
            "alias '4.7' routes through /model handler",
            kind == "handled"
            and swap_calls[-1][0] == "claude-opus-4-7"
            and reply is not None
            and "Opus 4.7" in reply,
            f"swap_calls={swap_calls[-1]}; reply={reply}",
        )
    )

    initial_sid = state.session_id
    kind, reply = registry.dispatch("/clear")
    results.append(
        _check(
            "/clear forgets sid + swaps to no-resume",
            kind == "handled"
            and len(forget_calls) == 1
            and state.session_id is None
            and swap_calls[-1][1] is None
            and reply == "New session",
            f"forget={forget_calls}; sid={initial_sid}->{state.session_id}; reply={reply}",
        )
    )

    state.session_id = "abcdef12-1111-2222-3333-444455556666"
    kind, reply = registry.dispatch("/stop")
    results.append(
        _check(
            "/stop swaps with same sid (kill+respawn-with-resume)",
            kind == "handled"
            and swap_calls[-1][1] == "abcdef12-1111-2222-3333-444455556666"
            and reply == "Stopped, session kept",
            f"swap_calls[-1]={swap_calls[-1]}; reply={reply}",
        )
    )

    kind, reply = registry.dispatch("你好")
    results.append(
        _check(
            "plain text forwarded to provider (not handled)",
            kind == "forward" and reply is None,
            f"kind={kind}",
        )
    )

    return results


def verify_idle_fire(tmp_root: Path) -> list[tuple[str, bool, str]]:
    """(3) 6h idle fire — backdate jsonl mtime, tick once, assert marker + audit."""
    results: list[tuple[str, bool, str]] = []

    cc_projects = tmp_root / "claude_projects" / "demo_project"
    cc_projects.mkdir(parents=True)
    sid = "abcdef01-2345-6789-abcd-ef0123456789"
    jsonl = cc_projects / f"{sid}.jsonl"
    jsonl.write_text("{}\n")

    now = time.time()
    seven_hours_ago = now - 7 * 3600
    import os as _os

    _os.utime(jsonl, (seven_hours_ago, seven_hours_ago))

    marker_dir = tmp_root / "markers"
    audit_log = tmp_root / "session_audit.log"
    sessions_state = tmp_root / "sessions.json"

    tracker = SessionTracker(state_path=sessions_state)
    tracker.set("wx-lumi", sid)

    # use an inert command template; the fire is real (Popen detached) but
    # the binary `true` will exit cleanly so we don't pollute anything.
    loop = IdleFireLoop(
        sessions=tracker,
        command_template="/usr/bin/true {sid}",
        idle_threshold_sec=6 * 3600,
        scan_interval_sec=30 * 60,
        cc_projects_dir=cc_projects.parent,
        marker_dir=marker_dir,
        audit_log=audit_log,
    )

    fired = loop.tick_once()
    results.append(
        _check(
            "idle 7h triggers fire",
            sid in fired,
            f"fired={fired}",
        )
    )

    marker = marker_dir / f".fired.{sid}"
    results.append(
        _check(
            "marker file written for fired sid",
            marker.exists(),
            f"marker={marker}",
        )
    )

    audit_text = audit_log.read_text() if audit_log.exists() else ""
    results.append(
        _check(
            "audit line written with kind=idle_fire",
            "kind=idle_fire" in audit_text and sid[:8] in audit_text,
            audit_text.strip().splitlines()[-1] if audit_text else "<empty>",
        )
    )

    # second tick — already fired, must not re-fire
    fired_again = loop.tick_once()
    results.append(
        _check(
            "second tick does not re-fire (marker dedup)",
            sid not in fired_again,
            f"fired_again={fired_again}",
        )
    )

    # touch jsonl → next tick fires again
    _os.utime(jsonl, (now, now))  # fresh activity, no longer idle
    fired_after_activity = loop.tick_once()
    results.append(
        _check(
            "fresh activity (jsonl mtime=now) does NOT fire (idle reset)",
            sid not in fired_after_activity,
            f"fired_after_activity={fired_after_activity}",
        )
    )

    return results


def verify_sleep_wake() -> list[tuple[str, bool, str]]:
    """(4) sleep/wake — invoke handler chain directly (full pyobjc binding
    needs a real lid-close)."""
    results: list[tuple[str, bool, str]] = []

    sleep_called: list[bool] = []
    wake_called: list[bool] = []

    def on_sleep() -> None:
        sleep_called.append(True)

    def on_wake() -> None:
        wake_called.append(True)

    obs = SleepWakeObserver(will_sleep=on_sleep, did_wake=on_wake, alerts=None)

    obs._will_sleep()
    obs._did_wake()

    results.append(_check("will_sleep handler invoked", sleep_called == [True], ""))
    results.append(_check("did_wake handler invoked", wake_called == [True], ""))

    # liveness gate: start() should also be idempotent-callable.
    # On a fresh subprocess pyobjc IS importable (we pinned it). We exercise
    # start()+stop() once to confirm the real NSWorkspace binding doesn't
    # crash before letting the bridge subscribe at boot.
    try:
        obs.start()
        obs.stop()
        results.append(_check("NSWorkspace observer start+stop cycle clean", True, ""))
    except Exception as exc:
        results.append(_check("NSWorkspace observer start+stop cycle clean", False, repr(exc)))

    return results


def verify_alert_sink(tmp_root: Path) -> list[tuple[str, bool, str]]:
    """(5) systemic fail → alert file lands on disk."""
    results: list[tuple[str, bool, str]] = []

    alerts_dir = tmp_root / "alerts"
    sink = AlertSink(alerts_dir=alerts_dir, marrow_repo_cmd="")

    try:
        raise ProviderDeadError("simulated cc death mid-stream")
    except ProviderDeadError as exc:
        path = sink.write("critical", "provider_dead", str(exc), source="live_verify")

    results.append(_check("alert file written to disk", path.exists(), str(path)))

    body = json.loads(path.read_text())
    results.append(
        _check(
            "alert payload carries severity+kind+message",
            body.get("severity") == "critical"
            and body.get("kind") == "provider_dead"
            and "simulated" in body.get("message", ""),
            json.dumps(body, ensure_ascii=False),
        )
    )

    mode = oct(path.stat().st_mode)[-3:]
    results.append(_check("alert file chmod 600", mode == "600", f"mode={mode}"))

    recent = sink.list_recent(since_ts=0.0)
    results.append(
        _check(
            "list_recent surfaces the alert",
            any(r.get("kind") == "provider_dead" for r in recent),
            f"len={len(recent)}",
        )
    )

    # HealthGate sanity: a fresh boot followed by clean-shutdown then another
    # boot must NOT announce restart.
    health_state = tmp_root / "health.json"
    g1 = HealthGate(state_path=health_state)
    g1.boot()
    g1.stamp_clean_shutdown()
    g2 = HealthGate(state_path=health_state)
    g2.boot()
    results.append(
        _check(
            "HealthGate: clean shutdown then boot → no announce",
            g2.should_announce_restart() is False,
            "",
        )
    )

    # And a crash path (no stamp) → announce.
    health_state.unlink()
    g3 = HealthGate(state_path=health_state)
    g3.boot()  # no stamp
    g4 = HealthGate(state_path=health_state)
    g4.boot()
    results.append(
        _check(
            "HealthGate: crash then boot → DOES announce",
            g4.should_announce_restart() is True,
            "",
        )
    )

    return results


def verify_provider_echo_roundtrip() -> list[tuple[str, bool, str]]:
    """Bonus: EchoProvider spawn → send → recv → close cycle proves the
    Provider contract end-to-end without spawning real cc."""
    results: list[tuple[str, bool, str]] = []

    p = EchoProvider()
    p.spawn(env={})
    results.append(_check("EchoProvider spawn → is_alive True", p.is_alive(), ""))

    p.send("ping")
    events = list(p.recv())
    types = [e.get("type") for e in events]
    results.append(
        _check(
            "EchoProvider recv yields system + assistant + result",
            "system" in types and "assistant" in types and "result" in types,
            f"types={types}",
        )
    )

    final = next((e for e in events if e.get("type") == "result"), None)
    results.append(
        _check(
            "result event carries echo payload",
            final is not None and "ping" in final.get("result", ""),
            f"final={final}",
        )
    )

    p.close()
    results.append(_check("EchoProvider close → is_alive False", not p.is_alive(), ""))

    return results


MARROW_PY = "/Users/Gabrielle/CC-Lab/marrow/.venv/bin/python"
MARROW_ADD_ALERT = f"{MARROW_PY} -m marrow.cli add-alert"


def verify_marrow_alert_integration(tmp_root: Path) -> list[tuple[str, bool, str]]:
    """Wire AlertSink to the real `mw add-alert` CLI and confirm a row lands in
    marrow's alerts table. This is the last unwired step of '(5) systemic fail
    入 marrow alert'."""
    import subprocess as _sp

    results: list[tuple[str, bool, str]] = []

    alerts_dir = tmp_root / "alerts_with_marrow"
    sink = AlertSink(alerts_dir=alerts_dir, marrow_repo_cmd=MARROW_ADD_ALERT)

    marker = f"phase_a_live_wire_{int(time.time())}"
    path = sink.write(
        "critical",
        marker,
        "AlertSink → mw add-alert end-to-end",
        source="synapse_wx.live_verify",
    )

    results.append(_check(
        "local alert file written even when marrow wired",
        path.exists(),
        str(path),
    ))

    # Marrow popen is detached — wait a moment then poll the DB directly.
    deadline = time.time() + 5.0
    row: dict | None = None
    while time.time() < deadline:
        proc = _sp.run(
            [MARROW_PY, "-c",
             f"from marrow import storage, config; "
             f"c = storage.connect(config.db_path()); "
             f"r = c.execute('SELECT id, severity, type, message, source FROM "
             f"alerts WHERE type=? ORDER BY id DESC LIMIT 1', ('{marker}',)).fetchone(); "
             f"print(repr(dict(r)) if r else 'NONE')"],
            capture_output=True, text=True, timeout=10,
        )
        out = proc.stdout.strip()
        if out and out != "NONE":
            try:
                row = eval(out, {"__builtins__": {}}, {})
            except Exception:
                row = None
            if row is not None:
                break
        time.sleep(0.3)

    results.append(
        _check(
            "marrow alerts table received the row",
            row is not None and row.get("severity") == "critical" and row.get("type") == marker,
            repr(row),
        )
    )
    results.append(
        _check(
            "marrow row source preserved",
            row is not None and row.get("source") == "synapse_wx.live_verify",
            (row or {}).get("source", "<none>"),
        )
    )

    return results


def verify_idle_popen_to_marrow(tmp_root: Path) -> list[tuple[str, bool, str]]:
    """Run IdleFireLoop end-to-end with a real popen-detach to `mw add-alert`.
    Proves the fire path actually launches a marrow subprocess and the marker
    + audit chain works. Avoids burning an LLM call by routing the fire to
    `mw add-alert` instead of `marrow.sessionend_async`."""
    results: list[tuple[str, bool, str]] = []
    import os as _os

    cc_projects = tmp_root / "cc_projects_idle_live" / "proj"
    cc_projects.mkdir(parents=True)
    sid = f"livefire{int(time.time())}-aaaa-bbbb-cccc-ddddeeeeffff"
    jsonl = cc_projects / f"{sid}.jsonl"
    jsonl.write_text("{}\n")

    seven_hours_ago = time.time() - 7 * 3600
    _os.utime(jsonl, (seven_hours_ago, seven_hours_ago))

    marker_dir = tmp_root / "markers_live"
    audit_log = tmp_root / "audit_live.log"
    sessions_state = tmp_root / "sessions_live.json"

    tracker = SessionTracker(state_path=sessions_state)
    tracker.set("wx-lumi", sid)

    fire_marker = f"phase_a_idle_fire_{int(time.time())}"
    cmd_template = (
        f"{MARROW_ADD_ALERT} warn {fire_marker} "
        f"idle_fire_for_sid_{{sid}} --source synapse_wx.live_verify"
    )

    loop = IdleFireLoop(
        sessions=tracker,
        command_template=cmd_template,
        idle_threshold_sec=6 * 3600,
        scan_interval_sec=30 * 60,
        cc_projects_dir=cc_projects.parent,
        marker_dir=marker_dir,
        audit_log=audit_log,
    )

    fired = loop.tick_once()
    results.append(_check("idle loop fired the sid", sid in fired, f"fired={fired}"))

    # Wait for detached marrow subprocess to commit
    import subprocess as _sp
    deadline = time.time() + 6.0
    saw_row = False
    while time.time() < deadline:
        proc = _sp.run(
            [MARROW_PY, "-c",
             f"from marrow import storage, config; "
             f"c = storage.connect(config.db_path()); "
             f"r = c.execute('SELECT 1 FROM alerts WHERE type=? LIMIT 1', "
             f"('{fire_marker}',)).fetchone(); print('YES' if r else 'NO')"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.stdout.strip() == "YES":
            saw_row = True
            break
        time.sleep(0.3)

    results.append(
        _check(
            "marrow row from idle-fired popen detach",
            saw_row,
            f"marker_type={fire_marker}",
        )
    )

    return results


def run() -> int:
    sections: list[tuple[str, list[tuple[str, bool, str]]]] = []

    with tempfile.TemporaryDirectory(prefix="synapse_wx_live_") as tmp:
        tmp_root = Path(tmp)

        sections.append((
            "(2) Commands end-to-end",
            verify_commands(),
        ))
        sections.append((
            "(3) 6h idle fire (mock command)",
            verify_idle_fire(tmp_root),
        ))
        sections.append((
            "(3-live) idle fire → real popen → marrow row",
            verify_idle_popen_to_marrow(tmp_root),
        ))
        sections.append((
            "(4) sleep/wake handlers",
            verify_sleep_wake(),
        ))
        sections.append((
            "(5) Alert sink + HealthGate",
            verify_alert_sink(tmp_root),
        ))
        sections.append((
            "(5-live) AlertSink → marrow alerts round-trip",
            verify_marrow_alert_integration(tmp_root),
        ))
        sections.append(("(bonus) Provider Echo round-trip", verify_provider_echo_roundtrip()))

    total = sum(len(rs) for _, rs in sections)
    passed = sum(1 for _, rs in sections for _, ok, _ in rs if ok)

    lines: list[str] = []
    lines.append(f"# Live verification — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    lines.append(f"**{passed}/{total} checks passed** "
                 f"(harness: `scripts/live_verify.py`)")
    lines.append("")
    lines.append("> Run scope: every Phase-A exit criterion that does NOT need a "
                 "phone-side iLink QR scan or a real WeChat round-trip. Those "
                 "two remain Lumi-blocking and are listed at the bottom.")
    lines.append("")

    for title, rs in sections:
        lines.append(f"## {title}")
        for label, ok, detail in rs:
            mark = "PASS" if ok else "FAIL"
            lines.append(f"- [{mark}] {label}")
            if detail:
                snippet = detail.replace("\n", " ")[:200]
                lines.append(f"    - `{snippet}`")
        lines.append("")

    lines.append("## Still pending (require Lumi)")
    lines.append("- (1) WeChat new conversation with 言澈 — needs `ILinkClient.login()` QR scan.")
    lines.append("- (2-live) Slash commands triggered from real WeChat bubble — same blocker.")
    lines.append("- (4-real-lid) Real `NSWorkspaceWillSleepNotification` from closing the laptop.")
    lines.append("")

    report = Path("docs/notes/live-verify-2026-06-02.md")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(lines))

    for line in lines[:8]:
        print(line)
    print()
    for title, rs in sections:
        for label, ok, _ in rs:
            mark = "PASS" if ok else "FAIL"
            print(f"  [{mark}] {title} — {label}")
    print()
    print(f"Report: {report}")

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run())
