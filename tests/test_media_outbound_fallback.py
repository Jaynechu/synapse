"""iCloud outbox fallback for outbound files exceeding the CDN ceiling."""

from __future__ import annotations

from pathlib import Path

import pytest

from synapse_core.commands.messages import t
from synapse_wx.ilink._media import _CDN_MAX_CIPHERTEXT
from synapse_wx.media import outbound as outbound_mod
from synapse_wx.media.outbound import _icloud_outbox_copy, _pkcs7_padded_size, send_media

_BIG = _CDN_MAX_CIPHERTEXT + 1024
_SMALL = 1024


class FakeClient:
    """Minimal client double — MagicMock getattr is always truthy, so the
    recipient fallback (`getattr(client, "_last_from_wxid", None)`) needs a
    plain object."""

    def __init__(self) -> None:
        self.sent_texts: list[tuple[str, str, str]] = []
        self.sent_files: list[str] = []
        self.sent_images: list[str] = []

    def send_text(self, to_user_id: str, context_token: str, text: str) -> bool:
        self.sent_texts.append((to_user_id, context_token, text))
        return True

    def send_file(self, path, *, to_user_id=None, context_token=None) -> bool:
        self.sent_files.append(str(path))
        return True

    def send_image(self, path, *, to_user_id=None, context_token=None) -> bool:
        self.sent_images.append(str(path))
        return True


@pytest.fixture
def outbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    box = tmp_path / "outbox"
    monkeypatch.setattr(outbound_mod, "_ICLOUD_OUTBOX", box)
    return box


def _write(path: Path, size: int) -> Path:
    path.write_bytes(b"x" * size)
    return path


def test_pkcs7_padded_size_block_boundary() -> None:
    assert _pkcs7_padded_size(16) == 32
    assert _pkcs7_padded_size(15) == 16


def test_large_file_copied_and_text_sent(tmp_path: Path, outbox: Path) -> None:
    f = _write(tmp_path / "big.pdf", _BIG)
    client = FakeClient()
    ok = send_media(
        client,
        kind="file",
        path=str(f),
        to_user_id="wx_1",
        context_token="ctx",
        channel_label="CC-WX",
    )
    assert ok is False
    assert client.sent_files == []
    assert (outbox / "big.pdf").read_bytes() == f.read_bytes()
    assert len(client.sent_texts) == 1
    to, ctx, msg = client.sent_texts[0]
    assert (to, ctx) == ("wx_1", "ctx")
    assert msg == t("media.icloud_outbox", name="big.pdf", folder="outbox")


def test_collision_suffix(tmp_path: Path, outbox: Path) -> None:
    f = _write(tmp_path / "big.pdf", _BIG)
    outbox.mkdir(parents=True)
    _write(outbox / "big.pdf", 1)
    _write(outbox / "big-1.pdf", 1)
    dest = _icloud_outbox_copy(f)
    assert dest.name == "big-2.pdf"
    assert dest.read_bytes() == f.read_bytes()


def test_small_file_unaffected(tmp_path: Path, outbox: Path) -> None:
    f = _write(tmp_path / "small.pdf", _SMALL)
    client = FakeClient()
    ok = send_media(
        client,
        kind="file",
        path=str(f),
        to_user_id="wx_1",
        context_token="ctx",
        channel_label="CC-WX",
    )
    assert ok is True
    assert client.sent_files == [str(f)]
    assert not outbox.exists()
    assert client.sent_texts == []


def test_large_image_downscales_instead_of_fallback(
    tmp_path: Path, outbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _write(tmp_path / "huge.jpg", _BIG)
    downscaled_dir = tmp_path / "ds"
    downscaled_dir.mkdir()
    downscaled = _write(downscaled_dir / "huge.jpg", _SMALL)
    monkeypatch.setattr(
        "synapse_wx.media.image.downscale_for_send", lambda p: downscaled
    )
    client = FakeClient()
    ok = send_media(
        client,
        kind="image",
        path=str(original),
        to_user_id="wx_1",
        context_token="ctx",
        channel_label="CC-WX",
    )
    assert ok is True
    assert client.sent_images == [str(downscaled)]
    assert not outbox.exists()
    assert client.sent_texts == []


def test_no_recipient_still_copies_no_text(tmp_path: Path, outbox: Path) -> None:
    f = _write(tmp_path / "big.bin", _BIG)
    client = FakeClient()
    ok = send_media(
        client,
        kind="file",
        path=str(f),
        to_user_id=None,
        context_token=None,
        channel_label="CC-WX",
    )
    assert ok is False
    assert (outbox / "big.bin").exists()
    assert client.sent_texts == []
