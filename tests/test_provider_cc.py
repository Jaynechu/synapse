from __future__ import annotations

import io
import json
import os
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from synapse_core.providers.cc import (
    MEDIA_SYSTEM_PROMPT,
    QUOTE_SYSTEM_PROMPT,
    ClaudeCodeProvider,
    _drain_stderr,
)
from synapse_core.providers.errors import ProviderDeadError


def _make_fake_popen(stdout_lines: list[str]):
    fake = MagicMock()
    fake.stdin = MagicMock()
    fake.stdin.closed = False
    fake.stdout = iter(stdout_lines)
    fake.stderr = io.StringIO("")
    fake.poll.return_value = None
    return fake


def _provider(**kwargs) -> ClaudeCodeProvider:
    params = {
        "channel": "test",
        "cwd": "/tmp",
        "stderr_log": None,
    }
    params.update(kwargs)
    return ClaudeCodeProvider(**params)


def test_spawn_args_have_required_flags():
    with patch("synapse_core.providers.cc.subprocess.Popen") as Popen:
        Popen.return_value = _make_fake_popen([])
        p = _provider(model="claude-sonnet-4-5")
        p.spawn()
        args, kwargs = Popen.call_args
        cmd = args[0]
        assert "--output-format" in cmd
        assert cmd[cmd.index("--output-format") + 1] == "stream-json"
        assert "--input-format" in cmd
        assert cmd[cmd.index("--input-format") + 1] == "stream-json"
        assert "--verbose" in cmd
        assert "--permission-mode" in cmd
        assert cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions"
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "claude-sonnet-4-5"
        # Isolation flags forbidden.
        assert "--setting-sources" not in cmd
        assert "--strict-mcp-config" not in cmd


def test_spawn_appends_quote_and_media_system_prompts():
    with patch("synapse_core.providers.cc.subprocess.Popen") as Popen:
        Popen.return_value = _make_fake_popen([])
        p = _provider(system_prompts=[QUOTE_SYSTEM_PROMPT, MEDIA_SYSTEM_PROMPT])
        p.spawn()
        cmd = Popen.call_args[0][0]
        appended = cmd[cmd.index("--append-system-prompt") + 1]
        assert cmd.count("--append-system-prompt") == 1
        assert "<quote>" in appended
        # All four parser tags taught verbatim; no generic <media> tag.
        for tag in ('<image path="', '<gif path="', '<video path="', '<file path="'):
            assert tag in appended
        assert "<media" not in appended
        assert "never fabricate" in appended


def test_spawn_omits_model_and_resume_when_unset():
    with patch("synapse_core.providers.cc.subprocess.Popen") as Popen:
        Popen.return_value = _make_fake_popen([])
        p = _provider()
        p.spawn()
        cmd = Popen.call_args[0][0]
        assert "--model" not in cmd
        assert "--resume" not in cmd


def test_spawn_passes_resume_sid():
    with patch("synapse_core.providers.cc.subprocess.Popen") as Popen:
        Popen.return_value = _make_fake_popen([])
        p = _provider(resume_sid="abc123")
        p.spawn()
        cmd = Popen.call_args[0][0]
        assert "--resume" in cmd
        assert cmd[cmd.index("--resume") + 1] == "abc123"


def test_spawn_env_includes_marrow_bridge_flag():
    with patch("synapse_core.providers.cc.subprocess.Popen") as Popen:
        Popen.return_value = _make_fake_popen([])
        p = _provider(extra_env={"FOO": "bar"}, marrow_bridge=True)
        p.spawn(env={"BAZ": "qux"})
        env = Popen.call_args.kwargs["env"]
        assert env["MARROW_BRIDGE"] == "1"
        assert env["MARROW_CHANNEL"] == "test"
        assert env["FOO"] == "bar"
        assert env["BAZ"] == "qux"


def test_spawn_extra_env_overrides_env_arg():
    with patch("synapse_core.providers.cc.subprocess.Popen") as Popen:
        Popen.return_value = _make_fake_popen([])
        p = _provider(extra_env={"K": "from_extra"})
        p.spawn(env={"K": "from_arg"})
        env = Popen.call_args.kwargs["env"]
        assert env["K"] == "from_extra"


def test_spawn_fills_launchd_missing_env():
    with (
        patch.dict(os.environ, {"HOME": "/Users/Gabrielle", "PATH": "/bin"}, clear=True),
        patch("synapse_core.providers.cc._account_env") as account_env,
        patch("synapse_core.providers.cc._darwin_user_temp_dir") as temp_dir,
        patch("synapse_core.providers.cc.subprocess.Popen") as Popen,
    ):
        account_env.return_value = ("Gabrielle", "/bin/zsh", "/Users/Gabrielle")
        temp_dir.return_value = "/var/folders/test/T/"
        Popen.return_value = _make_fake_popen([])

        p = _provider()
        p.spawn()

        env = Popen.call_args.kwargs["env"]
        assert env["USER"] == "Gabrielle"
        assert env["LOGNAME"] == "Gabrielle"
        assert env["SHELL"] == "/bin/zsh"
        assert env["TERM"] == "xterm-256color"
        assert env["TMPDIR"] == "/var/folders/test/T/"


def test_spawn_keeps_explicit_launchd_env_values():
    with (
        patch.dict(os.environ, {"HOME": "/Users/Gabrielle", "PATH": "/bin"}, clear=True),
        patch("synapse_core.providers.cc._account_env") as account_env,
        patch("synapse_core.providers.cc._darwin_user_temp_dir") as temp_dir,
        patch("synapse_core.providers.cc.subprocess.Popen") as Popen,
    ):
        account_env.return_value = ("Gabrielle", "/bin/zsh", "/Users/Gabrielle")
        temp_dir.return_value = "/var/folders/test/T/"
        Popen.return_value = _make_fake_popen([])

        p = _provider(extra_env={"TMPDIR": "/custom/tmp", "TERM": "dumb"})
        p.spawn(env={"USER": "bridge-user", "LOGNAME": "bridge-logname"})

        env = Popen.call_args.kwargs["env"]
        assert env["USER"] == "bridge-user"
        assert env["LOGNAME"] == "bridge-logname"
        assert env["TERM"] == "dumb"
        assert env["TMPDIR"] == "/custom/tmp"


def test_send_writes_user_frame_line_delimited():
    with patch("synapse_core.providers.cc.subprocess.Popen") as Popen:
        fake = _make_fake_popen([])
        Popen.return_value = fake
        p = _provider(stderr_log=Path("/tmp/test-cc-stderr.log"))
        p.spawn()
        p.send("hi")
        written = fake.stdin.write.call_args[0][0]
        assert written.endswith("\n")
        frame = json.loads(written.rstrip("\n"))
        assert frame == {
            "type": "user",
            "message": {"role": "user", "content": "hi"},
        }
        fake.stdin.flush.assert_called()


def test_recv_parses_session_id_and_usage_and_breaks_on_result():
    lines = [
        json.dumps({"type": "system", "subtype": "init", "session_id": "sid-xyz"}) + "\n",
        json.dumps({
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "hello"}],
                "usage": {
                    "input_tokens": 3,
                    "output_tokens": 7,
                    "cache_read_input_tokens": 2,
                },
            },
        }) + "\n",
        json.dumps({"type": "result", "result": "hello", "session_id": "sid-xyz"}) + "\n",
        # Trailing line should NOT be yielded (loop must break on result).
        json.dumps({"type": "ignored"}) + "\n",
    ]
    with patch("synapse_core.providers.cc.subprocess.Popen") as Popen:
        Popen.return_value = _make_fake_popen(lines)
        p = _provider()
        p.spawn()
        out = list(p.recv())
        assert [e["type"] for e in out] == ["system", "assistant", "result"]
        assert p.session_id == "sid-xyz"
        assert p.usage_total == {
            "input_tokens": 3,
            "output_tokens": 7,
            "cache_read_input_tokens": 2,
        }


def test_recv_skips_bad_json_lines():
    lines = [
        "not json at all\n",
        json.dumps({"type": "result", "result": "ok"}) + "\n",
    ]
    with patch("synapse_core.providers.cc.subprocess.Popen") as Popen:
        Popen.return_value = _make_fake_popen(lines)
        p = _provider()
        p.spawn()
        out = list(p.recv())
        assert len(out) == 1
        assert out[0]["type"] == "result"


def test_recv_raises_provider_dead_on_eof_without_result():
    lines = [
        json.dumps({"type": "system", "subtype": "init", "session_id": "sid"}) + "\n",
        json.dumps({"type": "assistant", "message": {"content": []}}) + "\n",
    ]
    with patch("synapse_core.providers.cc.subprocess.Popen") as Popen:
        Popen.return_value = _make_fake_popen(lines)
        p = _provider()
        p.spawn()
        with pytest.raises(ProviderDeadError):
            list(p.recv())


def test_close_three_stage_shutdown_order():
    with patch("synapse_core.providers.cc.subprocess.Popen") as Popen:
        fake = _make_fake_popen([])
        Popen.return_value = fake
        # First wait (stdin close) times out, terminate-wait times out, kill-wait succeeds.
        fake.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="claude", timeout=2),
            subprocess.TimeoutExpired(cmd="claude", timeout=3),
            0,
        ]
        p = _provider()
        p.spawn()
        p.close()
        fake.stdin.close.assert_called()
        fake.terminate.assert_called()
        fake.kill.assert_called()
        # Order: stdin.close -> wait -> terminate -> wait -> kill -> wait.
        call_order = [c[0] for c in fake.mock_calls]
        idx_close = next(i for i, n in enumerate(call_order) if n == "stdin.close")
        idx_term = call_order.index("terminate")
        idx_kill = call_order.index("kill")
        assert idx_close < idx_term < idx_kill
        assert p.alive is False


def test_close_natural_exit_stops_at_stage_one():
    with patch("synapse_core.providers.cc.subprocess.Popen") as Popen:
        fake = _make_fake_popen([])
        Popen.return_value = fake
        fake.wait.return_value = 0
        p = _provider()
        p.spawn()
        p.close()
        fake.stdin.close.assert_called()
        fake.terminate.assert_not_called()
        fake.kill.assert_not_called()


def test_send_raises_when_stdin_closed():
    with patch("synapse_core.providers.cc.subprocess.Popen") as Popen:
        fake = _make_fake_popen([])
        Popen.return_value = fake
        p = _provider()
        p.spawn()
        fake.stdin.closed = True
        with pytest.raises(ProviderDeadError):
            p.send("hi")


def test_stderr_drain_writes_timestamped_line(tmp_path):
    """Drain thread appends stderr lines with ISO timestamp prefix."""
    log_path = tmp_path / "test-stderr.log"
    stderr_pipe = io.StringIO("boom\n\n  \nline2\n")

    import threading
    t = threading.Thread(target=_drain_stderr, args=(stderr_pipe, log_path))
    t.start()
    t.join(timeout=5)

    assert log_path.exists()
    lines = log_path.read_text().splitlines()
    # Only non-empty lines written — "boom" and "line2"
    assert len(lines) == 2
    # Each line has a timestamp prefix followed by the content
    for line in lines:
        # ISO timestamp: YYYY-MM-DDTHH:MM:SS+HH:MM or Z
        assert line[0:4].isdigit(), f"expected timestamp prefix, got: {line!r}"
    assert lines[0].endswith("boom")
    assert lines[1].endswith("line2")


def test_spawn_starts_stderr_drain_thread(tmp_path):
    """spawn() must start the cc-stderr-drain daemon thread."""
    import threading

    class SlowStderr:
        def __iter__(self):
            return self

        def __next__(self):
            time.sleep(0.5)
            raise StopIteration

        def close(self) -> None:
            return None

    with patch("synapse_core.providers.cc.subprocess.Popen") as Popen:
        fake = _make_fake_popen([])
        fake.stderr = SlowStderr()
        Popen.return_value = fake
        before = {t.name for t in threading.enumerate()}
        p = _provider(stderr_log=tmp_path / "cc-stderr.log")
        p.spawn()
        after = {t.name for t in threading.enumerate()}
        assert "cc-stderr-drain" in after - before or "cc-stderr-drain" in after
