"""C2: sticker caption routing in maybe_flush().

When a user sends an image followed by a digit caption within the quiet window,
the bridge intercepts the pattern before media materialisation and either:
  - "0"        → suppress image(s); skip the provider turn entirely
  - "1"        → rewrite body to sticker_admin(action='ingest') instruction (vision desc)
  - "1 <text>" → rewrite body to sticker_admin(action='ingest') instruction with desc
  - anything else / no text → normal pass-through, no change
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from synapse_core.debounce import InboundBuffer
from synapse_wx.loop import MainLoop, _parse_sticker_caption
from synapse_core.providers.mock import EchoProvider
from synapse_core.sessionend.tracker import SessionTracker
from synapse_core.state import BridgeState


# ── shared fixtures / stubs ──────────────────────────────────────────────────


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, sec: float) -> None:
        self.now += sec


class FakeILink:
    """Minimal iLink stub with text + configurable per-message media events."""

    def __init__(
        self,
        inbound_batches: list[list[dict]],
        media_per_msg: dict[int, list[dict]] | None = None,
    ) -> None:
        self._batches = list(inbound_batches)
        self._media_per_msg = media_per_msg or {}
        self.sent: list[tuple[str, str, str]] = []
        self.downloads: list[tuple[str, str, str, str]] = []

    def poll_messages(self) -> list[dict]:
        if self._batches:
            return self._batches.pop(0)
        return []

    def send_text(self, to_user_id: str, ctx_token: str, text: str, **_kw) -> bool:
        self.sent.append((to_user_id, ctx_token, text))
        return True

    @staticmethod
    def extract_text(msg: dict) -> str:
        return msg.get("text", "")

    def extract_media(self, msg: dict) -> list[dict]:
        idx = msg.get("_idx", -1)
        return list(self._media_per_msg.get(idx, []))

    def download_media(
        self, cdn_url: str, aes_key: str, save_path: Path, encrypt_query_param: str = ""
    ) -> bool:
        self.downloads.append((cdn_url, aes_key, str(save_path), encrypt_query_param))
        save_path.write_bytes(b"fake-image-bytes")
        return True


class CapturingProvider(EchoProvider):
    def __init__(self) -> None:
        super().__init__()
        self.received: list[str] = []

    def send(self, msg: str) -> None:
        self.received.append(msg)
        super().send(msg)


_IMAGE_EVENT = {
    "type": "image",
    "cdn_url": "https://cdn/img.jpg",
    "aes_key": "K" * 22,
    "encrypt_query_param": "QP",
}


@pytest.fixture()
def env(tmp_path: Path):
    state = BridgeState()
    sessions = SessionTracker(state_path=tmp_path / "sessions.json")
    return {"state": state, "sessions": sessions, "tmp": tmp_path}


def _build_loop(env, ilink, clock, *, provider=None) -> tuple[MainLoop, CapturingProvider]:
    if provider is None:
        provider = CapturingProvider()

    def factory(*_a, **_kw):
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
        wallclock=lambda: datetime(2026, 6, 14, 12, 0),
        sleeper=lambda _s: None,
        alert_dir=env["tmp"] / "alerts",
        channel="wx",
        last_active_path=env["tmp"] / "last_active.json",
        channel_label="CC-WX",
        media_dir=env["tmp"] / "media",
    )
    loop._provider = provider
    provider.spawn()
    return loop, provider


def _run(loop: MainLoop, ilink: FakeILink, clock: FakeClock) -> None:
    """tick + advance quiet window + flush."""
    loop.tick()
    clock.advance(6.0)
    loop.maybe_flush()


# ── unit tests for _parse_sticker_caption ────────────────────────────────────


def test_parse_returns_none_no_images() -> None:
    assert _parse_sticker_caption("1", [{"type": "voice"}]) is None


def test_parse_returns_none_no_text() -> None:
    # body is all sentinels → no text after stripping
    assert _parse_sticker_caption(".\n.", [_IMAGE_EVENT]) is None


def test_parse_suppress() -> None:
    assert _parse_sticker_caption(".\n0", [_IMAGE_EVENT]) == ("suppress", "")


def test_parse_save_no_desc() -> None:
    assert _parse_sticker_caption(".\n1", [_IMAGE_EVENT]) == ("save", "")


def test_parse_save_with_desc() -> None:
    assert _parse_sticker_caption(".\n1 cute cat", [_IMAGE_EVENT]) == ("save", "cute cat")


def test_parse_save_with_multiword_desc() -> None:
    assert _parse_sticker_caption(".\n1 hello world", [_IMAGE_EVENT]) == ("save", "hello world")


def test_parse_passthrough_on_other_text() -> None:
    assert _parse_sticker_caption(".\nhello", [_IMAGE_EVENT]) is None


def test_parse_multi_sentinels() -> None:
    # 3 images' sentinels + caption "1"
    assert _parse_sticker_caption(".\n.\n.\n1", [_IMAGE_EVENT, _IMAGE_EVENT, _IMAGE_EVENT]) == ("save", "")


# ── integration tests through maybe_flush ────────────────────────────────────


def test_caption_1_rewrites_body_image_materialised(env) -> None:
    """Image + '1' → body is sticker-save instruction; image is still materialised."""
    inbound = [[{"_idx": 0, "from_wxid": "testuser", "context_token": "ctx", "text": ""}]]
    # tick() will push "." sentinel for the image; then user sends "1"
    ilink = FakeILink(
        inbound_batches=[
            [{"_idx": 0, "from_wxid": "testuser", "context_token": "ctx", "text": ""}],
            [{"_idx": 1, "from_wxid": "testuser", "context_token": "ctx", "text": "1"}],
        ],
        media_per_msg={0: [dict(_IMAGE_EVENT)]},
    )
    clock = FakeClock(1000.0)
    loop, provider = _build_loop(env, ilink, clock)

    loop.tick()   # msg 0: image → "." sentinel pushed
    loop.tick()   # msg 1: "1" text buffered
    clock.advance(6.0)
    loop.maybe_flush()

    assert provider.received, "provider.send should have been called"
    sent = provider.received[0]
    assert "[sticker-save]" in sent
    assert "sticker_admin" in sent
    assert "vision" in sent
    assert "Use the Read tool" in sent  # image still materialised
    assert ilink.downloads, "image should be downloaded"


def test_caption_0_suppresses_image_no_send(env) -> None:
    """Image + '0' → provider.send never called."""
    ilink = FakeILink(
        inbound_batches=[
            [{"_idx": 0, "from_wxid": "testuser", "context_token": "ctx", "text": ""}],
            [{"_idx": 1, "from_wxid": "testuser", "context_token": "ctx", "text": "0"}],
        ],
        media_per_msg={0: [dict(_IMAGE_EVENT)]},
    )
    clock = FakeClock(1000.0)
    loop, provider = _build_loop(env, ilink, clock)

    loop.tick()
    loop.tick()
    clock.advance(6.0)
    loop.maybe_flush()

    assert provider.received == [], "provider.send must NOT be called on suppress"
    assert ilink.downloads == [], "image must NOT be downloaded on suppress"


def test_caption_1_with_desc(env) -> None:
    """Image + '1 cute cat' → body includes the desc."""
    ilink = FakeILink(
        inbound_batches=[
            [{"_idx": 0, "from_wxid": "testuser", "context_token": "ctx", "text": ""}],
            [{"_idx": 1, "from_wxid": "testuser", "context_token": "ctx", "text": "1 cute cat"}],
        ],
        media_per_msg={0: [dict(_IMAGE_EVENT)]},
    )
    clock = FakeClock(1000.0)
    loop, provider = _build_loop(env, ilink, clock)

    loop.tick()
    loop.tick()
    clock.advance(6.0)
    loop.maybe_flush()

    assert provider.received
    sent = provider.received[0]
    assert "[sticker-save]" in sent
    assert "Desc: cute cat" in sent
    assert "vision" not in sent  # desc provided; no "Use vision" instruction


def test_normal_text_passthrough(env) -> None:
    """Image + normal text → no sticker rewrite, normal pass-through."""
    ilink = FakeILink(
        inbound_batches=[
            [{"_idx": 0, "from_wxid": "testuser", "context_token": "ctx", "text": ""}],
            [{"_idx": 1, "from_wxid": "testuser", "context_token": "ctx", "text": "what is this?"}],
        ],
        media_per_msg={0: [dict(_IMAGE_EVENT)]},
    )
    clock = FakeClock(1000.0)
    loop, provider = _build_loop(env, ilink, clock)

    loop.tick()
    loop.tick()
    clock.advance(6.0)
    loop.maybe_flush()

    assert provider.received
    sent = provider.received[0]
    assert "[sticker-save]" not in sent
    assert "what is this?" in sent
    assert "Use the Read tool" in sent  # image still materialised normally


def test_multi_image_caption_1(env) -> None:
    """3 images + '1' → all images routed, body says 'these images'."""
    ilink = FakeILink(
        inbound_batches=[
            [{"_idx": 0, "from_wxid": "testuser", "context_token": "ctx", "text": ""}],
            [{"_idx": 1, "from_wxid": "testuser", "context_token": "ctx", "text": ""}],
            [{"_idx": 2, "from_wxid": "testuser", "context_token": "ctx", "text": ""}],
            [{"_idx": 3, "from_wxid": "testuser", "context_token": "ctx", "text": "1"}],
        ],
        media_per_msg={
            0: [dict(_IMAGE_EVENT)],
            1: [dict(_IMAGE_EVENT)],
            2: [dict(_IMAGE_EVENT)],
        },
    )
    clock = FakeClock(1000.0)
    loop, provider = _build_loop(env, ilink, clock)

    loop.tick()
    loop.tick()
    loop.tick()
    loop.tick()
    clock.advance(6.0)
    loop.maybe_flush()

    assert provider.received
    sent = provider.received[0]
    assert "these images" in sent
    assert "[sticker-save]" in sent
    assert len(ilink.downloads) == 3


def test_no_images_digit_1_passthrough(env) -> None:
    """No images + '1' → just text '1', no caption routing triggered."""
    ilink = FakeILink(
        inbound_batches=[
            [{"_idx": 0, "from_wxid": "testuser", "context_token": "ctx", "text": "1"}],
        ],
    )
    clock = FakeClock(1000.0)
    loop, provider = _build_loop(env, ilink, clock)

    loop.tick()
    clock.advance(6.0)
    loop.maybe_flush()

    assert provider.received
    sent = provider.received[0]
    assert "[sticker-save]" not in sent
    assert "1" in sent


def test_image_only_no_caption_passthrough(env) -> None:
    """Image with no caption (body = '.') → normal pass-through, no routing."""
    ilink = FakeILink(
        inbound_batches=[
            [{"_idx": 0, "from_wxid": "testuser", "context_token": "ctx", "text": ""}],
        ],
        media_per_msg={0: [dict(_IMAGE_EVENT)]},
    )
    clock = FakeClock(1000.0)
    loop, provider = _build_loop(env, ilink, clock)

    loop.tick()
    clock.advance(6.0)
    loop.maybe_flush()

    assert provider.received
    sent = provider.received[0]
    assert "[sticker-save]" not in sent
    assert "Use the Read tool" in sent  # image materialised normally
