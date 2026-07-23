"""Provider poll_line + recv(first_line) for the resident idle listener.

Mock at the subprocess boundary only; never spawn real claude. poll_line has
NO liveness clock (idle silence is normal); recv(first_line=...) processes a
pre-pulled line before reading the queue and still terminates on result.
"""

from __future__ import annotations

import json
import queue
from unittest.mock import MagicMock, patch

from synapse_core.providers.cc import POLL_EOF, ClaudeCodeProvider, _STDOUT_EOF


def _provider(**kwargs) -> ClaudeCodeProvider:
    params = {"channel": "test", "cwd": "/tmp", "stderr_log": None}
    params.update(kwargs)
    return ClaudeCodeProvider(**params)


def _spawn_with_queue(p: ClaudeCodeProvider) -> queue.Queue:
    """Spawn against a fake Popen and hand back the reader queue directly so
    tests can push lines without a background reader thread racing."""
    fake = MagicMock()
    fake.stdin = MagicMock()
    fake.stdin.closed = False
    fake.stdout = iter([])
    fake.stderr = MagicMock()
    fake.pid = 12345
    fake.poll.return_value = None
    with patch("synapse_core.providers.cc.subprocess.Popen", return_value=fake), \
            patch("synapse_core.providers.cc.threading.Thread"):
        p.spawn()
    q: queue.Queue = queue.Queue()
    p._stdout_q = q
    return q


def test_poll_line_returns_line_when_available():
    p = _provider()
    q = _spawn_with_queue(p)
    q.put('{"type":"assistant"}\n')
    assert p.poll_line(0.1) == '{"type":"assistant"}\n'
    assert p.alive is True


def test_poll_line_returns_none_on_empty_queue():
    p = _provider()
    _spawn_with_queue(p)
    assert p.poll_line(0.05) is None
    assert p.alive is True


def test_poll_line_returns_eof_after_reader_eof():
    p = _provider()
    q = _spawn_with_queue(p)
    q.put(_STDOUT_EOF)
    assert p.poll_line(0.1) is POLL_EOF
    assert p.alive is False


def test_poll_line_returns_eof_on_read_error():
    p = _provider()
    q = _spawn_with_queue(p)
    q.put(RuntimeError("stdout read error"))
    assert p.poll_line(0.1) is POLL_EOF
    assert p.alive is False


def test_poll_line_no_liveness_kill_on_long_idle():
    """poll_line must never invoke the idle-hard kill even for a tiny timeout
    with a live-but-silent process — idle silence is normal and unbounded."""
    p = _provider(idle_hard_s=0.01)
    _spawn_with_queue(p)
    with patch.object(p, "_kill_process_group") as kill:
        assert p.poll_line(0.02) is None
    kill.assert_not_called()
    assert p.alive is True


def test_recv_first_line_processed_before_queue_and_terminates_on_result():
    p = _provider()
    q = _spawn_with_queue(p)
    first = json.dumps({"type": "system", "subtype": "init", "session_id": "s1"}) + "\n"
    q.put(json.dumps({"type": "assistant",
                      "message": {"content": [{"type": "text", "text": "hi"}]}}) + "\n")
    q.put(json.dumps({"type": "result", "result": "hi"}) + "\n")
    out = list(p.recv(first_line=first))
    assert [e["type"] for e in out] == ["system", "assistant", "result"]
    assert p.session_id == "s1"


def test_recv_first_line_is_notification_frame():
    """An unsolicited turn opens with system(task_notification): recv still
    collects to result; the notification frame is yielded as-is (no text)."""
    p = _provider()
    q = _spawn_with_queue(p)
    first = json.dumps({"type": "system", "subtype": "task_notification"}) + "\n"
    q.put(json.dumps({"type": "system", "subtype": "init", "session_id": "s2"}) + "\n")
    q.put(json.dumps({"type": "assistant",
                      "message": {"content": [{"type": "text", "text": "done"}]}}) + "\n")
    q.put(json.dumps({"type": "result", "result": "done"}) + "\n")
    out = list(p.recv(first_line=first))
    assert out[0] == {"type": "system", "subtype": "task_notification"}
    assert out[-1]["type"] == "result"


def test_recv_without_first_line_unchanged():
    """Plain recv() (no first_line) reads only from the queue as before."""
    p = _provider()
    q = _spawn_with_queue(p)
    q.put(json.dumps({"type": "result", "result": "ok"}) + "\n")
    out = list(p.recv())
    assert [e["type"] for e in out] == ["result"]
