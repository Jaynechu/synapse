"""B9: two-strike media inbound/outbound failure alerts."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from synapse_core.alerts import AlertSink
from synapse_wx.media import inbound as media_inbound
from synapse_wx.media import outbound as media_outbound


@pytest.fixture(autouse=True)
def outbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Prevent outbound fallback writes from leaking into the real iCloud outbox."""
    box = tmp_path / "outbox"
    monkeypatch.setattr(media_outbound, "_ICLOUD_OUTBOX", box)
    return box


@pytest.fixture(autouse=True)
def reset_counters():
    """Reset module-level counters and sink between tests."""
    media_inbound._inbound_fail_counts.clear()
    media_inbound._inbound_alert_sink = None
    media_outbound._outbound_fail_counts.clear()
    media_outbound._outbound_alert_sink = None
    yield
    media_inbound._inbound_fail_counts.clear()
    media_inbound._inbound_alert_sink = None
    media_outbound._outbound_fail_counts.clear()
    media_outbound._outbound_alert_sink = None


# ── inbound ──────────────────────────────────────────────────────────────────

def test_inbound_first_failure_no_alert(tmp_path: Path) -> None:
    """First download failure → counter=1, no alert file."""
    alerts = AlertSink(alerts_dir=tmp_path / "alerts")
    media_inbound.set_inbound_alert_sink(alerts)

    ilink = MagicMock()
    ilink.download_media.return_value = False

    result = media_inbound.materialize(
        {"type": "image", "cdn_url": "http://x", "aes_key": "", "encrypt_query_param": "q"},
        ilink,
        tmp_path / "media",
    )

    assert result == []
    assert media_inbound._inbound_fail_counts.get("image") == 1
    assert list((tmp_path / "alerts").glob("*.txt")) == []


def test_inbound_second_failure_emits_alert(tmp_path: Path) -> None:
    """Second consecutive download failure → warn alert with fingerprint media_in_failed."""
    alerts = AlertSink(alerts_dir=tmp_path / "alerts")
    media_inbound.set_inbound_alert_sink(alerts)

    ilink = MagicMock()
    ilink.download_media.return_value = False

    # First failure
    media_inbound.materialize(
        {"type": "image", "cdn_url": "http://x", "aes_key": "", "encrypt_query_param": "q"},
        ilink,
        tmp_path / "media",
    )
    # Second failure
    media_inbound.materialize(
        {"type": "image", "cdn_url": "http://x", "aes_key": "", "encrypt_query_param": "q"},
        ilink,
        tmp_path / "media",
    )

    alert_files = list((tmp_path / "alerts").glob("*.txt"))
    assert len(alert_files) == 1
    data = json.loads(alert_files[0].read_text())
    assert data["severity"] == "warn"
    assert data["fingerprint"] == "media_in_failed"
    assert "image" in data["message"]


def test_inbound_success_resets_counter(tmp_path: Path) -> None:
    """Success resets the per-kind counter so next failure is strike 1 again."""
    alerts = AlertSink(alerts_dir=tmp_path / "alerts")
    media_inbound.set_inbound_alert_sink(alerts)

    ilink = MagicMock()
    # First failure
    ilink.download_media.return_value = False
    out_path = tmp_path / "media" / "Images"
    out_path.mkdir(parents=True, exist_ok=True)
    media_inbound.materialize(
        {"type": "image", "cdn_url": "http://x", "aes_key": "", "encrypt_query_param": "q"},
        ilink,
        tmp_path / "media",
    )
    assert media_inbound._inbound_fail_counts.get("image") == 1

    # Success
    dest = tmp_path / "media" / "Images" / "ok.jpg"
    dest.write_bytes(b"img")
    ilink.download_media.side_effect = lambda cdn, key, path, qp: (
        path.write_bytes(b"img") or True
    )
    media_inbound.materialize(
        {"type": "image", "cdn_url": "http://x", "aes_key": "", "encrypt_query_param": "q"},
        ilink,
        tmp_path / "media",
    )
    assert media_inbound._inbound_fail_counts.get("image") is None

    # Second failure after reset → count=1, no alert
    ilink.download_media.side_effect = None
    ilink.download_media.return_value = False
    media_inbound.materialize(
        {"type": "image", "cdn_url": "http://x", "aes_key": "", "encrypt_query_param": "q"},
        ilink,
        tmp_path / "media",
    )
    assert media_inbound._inbound_fail_counts.get("image") == 1
    assert list((tmp_path / "alerts").glob("*.txt")) == []


def test_inbound_no_alert_without_sink(tmp_path: Path) -> None:
    """No sink wired → failure still tracked, no crash."""
    ilink = MagicMock()
    ilink.download_media.return_value = False

    media_inbound._inbound_fail_counts["image"] = 1  # prime for strike 2
    result = media_inbound.materialize(
        {"type": "image", "cdn_url": "http://x", "aes_key": "", "encrypt_query_param": "q"},
        ilink,
        tmp_path / "media",
    )
    assert result == []
    assert media_inbound._inbound_fail_counts.get("image") == 2


# ── outbound ─────────────────────────────────────────────────────────────────

def _make_image(tmp_path: Path, name: str = "img.jpg", size: int = 100) -> Path:
    p = tmp_path / name
    p.write_bytes(b"x" * size)
    return p


def test_outbound_first_failure_no_alert(tmp_path: Path) -> None:
    """First CDN upload failure → counter=1, no alert."""
    alerts = AlertSink(alerts_dir=tmp_path / "alerts")
    media_outbound.set_outbound_alert_sink(alerts)

    client = MagicMock()
    client.send_image.return_value = False

    img = _make_image(tmp_path)
    media_outbound.send_media(
        client,
        kind="image",
        path=str(img),
        to_user_id="lumi",
        context_token="ctx",
        channel_label="CC-WX",
    )

    assert media_outbound._outbound_fail_counts.get("image") == 1
    assert list((tmp_path / "alerts").glob("*.txt")) == []


def test_outbound_second_failure_emits_alert(tmp_path: Path) -> None:
    """Second consecutive CDN failure → warn alert with fingerprint media_out_failed."""
    alerts = AlertSink(alerts_dir=tmp_path / "alerts")
    media_outbound.set_outbound_alert_sink(alerts)

    client = MagicMock()
    client.send_image.return_value = False

    img = _make_image(tmp_path)
    media_outbound.send_media(
        client,
        kind="image",
        path=str(img),
        to_user_id="lumi",
        context_token="ctx",
        channel_label="CC-WX",
    )
    media_outbound.send_media(
        client,
        kind="image",
        path=str(img),
        to_user_id="lumi",
        context_token="ctx",
        channel_label="CC-WX",
    )

    alert_files = list((tmp_path / "alerts").glob("*.txt"))
    assert len(alert_files) == 1
    data = json.loads(alert_files[0].read_text())
    assert data["severity"] == "warn"
    assert data["fingerprint"] == "media_out_failed"
    assert "image" in data["message"]


def test_outbound_success_resets_counter(tmp_path: Path) -> None:
    """Successful send resets counter; subsequent failure is strike 1."""
    alerts = AlertSink(alerts_dir=tmp_path / "alerts")
    media_outbound.set_outbound_alert_sink(alerts)

    client = MagicMock()
    client.send_image.return_value = False
    img = _make_image(tmp_path)

    media_outbound.send_media(
        client,
        kind="image",
        path=str(img),
        to_user_id="lumi",
        context_token="ctx",
        channel_label="CC-WX",
    )
    assert media_outbound._outbound_fail_counts.get("image") == 1

    # Success
    client.send_image.return_value = True
    media_outbound.send_media(
        client,
        kind="image",
        path=str(img),
        to_user_id="lumi",
        context_token="ctx",
        channel_label="CC-WX",
    )
    assert media_outbound._outbound_fail_counts.get("image") is None

    # Next failure → count=1, no alert
    client.send_image.return_value = False
    media_outbound.send_media(
        client,
        kind="image",
        path=str(img),
        to_user_id="lumi",
        context_token="ctx",
        channel_label="CC-WX",
    )
    assert media_outbound._outbound_fail_counts.get("image") == 1
    assert list((tmp_path / "alerts").glob("*.txt")) == []


def test_outbound_kinds_tracked_independently(tmp_path: Path) -> None:
    """image failures don't affect file counter."""
    alerts = AlertSink(alerts_dir=tmp_path / "alerts")
    media_outbound.set_outbound_alert_sink(alerts)

    client = MagicMock()
    client.send_image.return_value = False
    client.send_file.return_value = False

    img = _make_image(tmp_path, "img.jpg")
    fil = _make_image(tmp_path, "doc.bin")

    media_outbound.send_media(
        client,
        kind="image",
        path=str(img),
        to_user_id="lumi",
        context_token="ctx",
        channel_label="CC-WX",
    )
    media_outbound.send_media(
        client,
        kind="image",
        path=str(img),
        to_user_id="lumi",
        context_token="ctx",
        channel_label="CC-WX",
    )
    # image at count=2 → alert emitted; file still at 0
    assert media_outbound._outbound_fail_counts.get("file", 0) == 0

    media_outbound.send_media(
        client,
        kind="file",
        path=str(fil),
        to_user_id="lumi",
        context_token="ctx",
        channel_label="CC-WX",
    )
    assert media_outbound._outbound_fail_counts.get("file") == 1
    # Only one alert (for image), not for file yet
    alert_files = list((tmp_path / "alerts").glob("*.txt"))
    assert len(alert_files) == 1
