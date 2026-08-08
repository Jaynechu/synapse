"""C0 main-loop media injection.

When an inbound message carries media events (image / voice / pdf / video),
the loop materializes each via ILinkClient.download_media → builds a
`Use the Read tool to view: <path>` instruction → appends it to the
assembled prompt so cc can pick up the local file paths.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from synapse_core.debounce import InboundBuffer
from synapse_wx.loop import MainLoop
from synapse_core.providers.mock import EchoProvider
from synapse_core.sessionend.tracker import SessionTracker
from synapse_core.state import BridgeState


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, sec: float) -> None:
        self.now += sec


class FakeILink:
    """iLink stub with both text and media events."""

    def __init__(
        self,
        inbound_batches: list[list[dict]],
        media_per_msg: dict[int, list[dict]] | None = None,
    ) -> None:
        self._batches = list(inbound_batches)
        self._media_per_msg = media_per_msg or {}
        self.sent: list[tuple[str, str, str]] = []
        self.downloads: list[tuple[str, str, str, str]] = []
        self._msg_counter = 0

    def poll_messages(self) -> list[dict]:
        if self._batches:
            return self._batches.pop(0)
        return []

    def send_text(
        self, to_user_id: str, ctx_token: str, text: str, **_kwargs
    ) -> bool:
        self.sent.append((to_user_id, ctx_token, text))
        return True

    @staticmethod
    def extract_text(msg: dict) -> str:
        return msg.get("text", "")

    def extract_media(self, msg: dict) -> list[dict]:
        idx = msg.get("_idx", -1)
        return list(self._media_per_msg.get(idx, []))

    def download_media(
        self,
        cdn_url: str,
        aes_key: str,
        save_path: Path,
        encrypt_query_param: str = "",
    ) -> bool:
        self.downloads.append((cdn_url, aes_key, str(save_path), encrypt_query_param))
        save_path.write_bytes(b"fake-media-bytes")
        return True


class _CapturingProvider(EchoProvider):
    """EchoProvider that remembers the assembled prompt for assertion."""

    def __init__(self) -> None:
        super().__init__()
        self.received: list[str] = []

    def send(self, msg: str) -> None:
        self.received.append(msg)
        super().send(msg)


@pytest.fixture()
def env(tmp_path: Path):
    state = BridgeState()
    sessions = SessionTracker(state_path=tmp_path / "sessions.json")
    return {"state": state, "sessions": sessions, "tmp": tmp_path}


def _build_loop(env, ilink, clock, wallclock, *, provider=None) -> MainLoop:
    if provider is None:
        provider = _CapturingProvider()

    def factory(*_a, **_kw):  # noqa: ANN401
        return provider

    loop = MainLoop(
        ilink=ilink,
        provider_factory=factory,
        state=env["state"],
        sessions=env["sessions"],
        idle_loop=None,
        buffer=InboundBuffer(clock=clock),
        poll_interval_sec=0.01,
        clock=clock,
        wallclock=wallclock,
        sleeper=lambda _s: None,
        alert_dir=env["tmp"] / "alerts",
        channel="wx",
        last_active_path=env["tmp"] / "last_active.json",
        channel_label="CC-WX",
        media_dir=env["tmp"] / "media",
    )
    loop._provider = provider
    provider.spawn()
    return loop


def test_inbound_image_injects_read_tool_path(env) -> None:
    """One image bubble → prompt carries the Read-tool instruction with abs path."""
    inbound = [
        [
            {
                "_idx": 0,
                "from_wxid": "testuser",
                "context_token": "ctx-1",
                "text": "看这张图",
            },
        ],
    ]
    media_per_msg = {
        0: [
            {
                "type": "image",
                "cdn_url": "https://cdn/img.jpg",
                "aes_key": "K" * 22,
                "encrypt_query_param": "QP-IMG",
            }
        ],
    }
    ilink = FakeILink(inbound, media_per_msg)
    clock = FakeClock(1000.0)
    now = datetime(2026, 6, 2, 12, 0)
    loop = _build_loop(env, ilink, clock, lambda: now)

    loop.tick()
    clock.advance(6.0)
    loop.maybe_flush()

    # Provider got assembled = time anchor + text + Read-tool instruction
    assert loop._provider.received  # type: ignore[attr-defined]
    sent = loop._provider.received[0]  # type: ignore[attr-defined]
    assert "看这张图" in sent
    assert "Use the Read tool to view" in sent
    # The injected path must exist + be the materialized file.
    assert ilink.downloads
    materialized_path = ilink.downloads[0][2]
    assert materialized_path in sent
    assert Path(materialized_path).exists()


def test_inbound_voice_appends_transcribed_text(env) -> None:
    """voice event has built-in text; loop appends as txt sidecar referenced via Read."""
    inbound = [
        [
            {
                "_idx": 0,
                "from_wxid": "testuser",
                "context_token": "ctx-1",
                "text": "",
            },
        ],
    ]
    media_per_msg = {
        0: [{"type": "voice", "text": "你今天怎么样"}],
    }
    ilink = FakeILink(inbound, media_per_msg)
    clock = FakeClock(1000.0)
    now = datetime(2026, 6, 2, 12, 0)
    loop = _build_loop(env, ilink, clock, lambda: now)

    loop.tick()
    clock.advance(6.0)
    loop.maybe_flush()

    assert loop._provider.received  # type: ignore[attr-defined]
    sent = loop._provider.received[0]  # type: ignore[attr-defined]
    # voice goes through materialize → txt sidecar → Read tool instruction
    assert "Use the Read tool to view" in sent
    # No download required for voice (inline text).
    assert ilink.downloads == []


def test_no_media_no_injection(env) -> None:
    """Plain text bubble must not produce a Read-tool line."""
    inbound = [
        [
            {
                "_idx": 0,
                "from_wxid": "testuser",
                "context_token": "ctx-1",
                "text": "hello",
            },
        ],
    ]
    ilink = FakeILink(inbound)
    clock = FakeClock(1000.0)
    now = datetime(2026, 6, 2, 12, 0)
    loop = _build_loop(env, ilink, clock, lambda: now)

    loop.tick()
    clock.advance(6.0)
    loop.maybe_flush()

    sent = loop._provider.received[0]  # type: ignore[attr-defined]
    assert "Use the Read tool" not in sent
    assert ilink.downloads == []


def test_text_only_message_with_media_still_flushes(env) -> None:
    """A pure-media bubble (no text) still triggers a flush with the path."""
    inbound = [
        [
            {
                "_idx": 0,
                "from_wxid": "testuser",
                "context_token": "ctx-1",
                "text": "",
            },
        ],
    ]
    media_per_msg = {
        0: [
            {
                "type": "image",
                "cdn_url": "https://cdn/img.jpg",
                "aes_key": "K" * 22,
                "encrypt_query_param": "QP-IMG",
            }
        ],
    }
    ilink = FakeILink(inbound, media_per_msg)
    clock = FakeClock(1000.0)
    now = datetime(2026, 6, 2, 12, 0)
    loop = _build_loop(env, ilink, clock, lambda: now)

    loop.tick()
    clock.advance(6.0)
    loop.maybe_flush()

    # Even with empty text bubble, media alone must produce a prompt turn.
    assert loop._provider.received  # type: ignore[attr-defined]
    sent = loop._provider.received[0]  # type: ignore[attr-defined]
    assert "Use the Read tool to view" in sent
