"""Outbound media orchestrator (C1).

Glues `split_for_wechat_typed` bubble output → `ILinkClient.send_*` methods.
The bridge calls `dispatch_media_bubble(client, bubble, to_user_id, ctx)` per
non-text bubble. `send_media` is the lower-level kind-aware dispatcher.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from synapse_core.commands.messages import t
from synapse_wx.config import load_config
from synapse_wx.ilink._media import _CDN_MAX_CIPHERTEXT

logger = logging.getLogger(__name__)

# B9: per-kind consecutive outbound failure counter (reset on success).
# Alert only on 2nd+ consecutive failure (two-strike contract).
_outbound_fail_counts: dict[str, int] = {}
_outbound_alert_sink = None  # set by loop via set_outbound_alert_sink()


def set_outbound_alert_sink(sink: Any) -> None:
    """Wire the AlertSink from the bridge loop (called once at startup)."""
    global _outbound_alert_sink
    _outbound_alert_sink = sink


def _record_outbound_failure(kind: str, detail: str) -> None:
    _outbound_fail_counts[kind] = _outbound_fail_counts.get(kind, 0) + 1
    count = _outbound_fail_counts[kind]
    logger.warning("media outbound %s failed (consecutive=%d): %s", kind, count, detail)
    if count >= 2 and _outbound_alert_sink is not None:
        try:
            _outbound_alert_sink.write(
                "warn",
                "media_out_failed",
                f"kind={kind} consecutive={count}: {detail}",
                source="media.outbound",
                fingerprint="media_out_failed",
            )
        except Exception as e:
            logger.warning("alert sink write failed: %s", e)


def _record_outbound_success(kind: str) -> None:
    _outbound_fail_counts.pop(kind, None)

# iCloud outbox for files that exceed the CDN ceiling. config.toml [media]
# icloud_outbox overrides; default matches synapse_wx.config.Config.
_ICLOUD_OUTBOX = Path(load_config().icloud_outbox).expanduser()

_KIND_TO_METHOD = {
    "image": "send_image",
    "file": "send_file",
    "gif": "send_gif",
    "video": "send_video",
}


def _pkcs7_padded_size(raw_size: int) -> int:
    """AES-128-ECB PKCS7 padded size: always adds at least one pad block."""
    return (raw_size // 16 + 1) * 16


def _icloud_outbox_copy(src: Path, outbox: Path | None = None) -> Path:
    """Copy src to outbox, suffixing with -N on collision. Returns dest path."""
    if outbox is None:
        outbox = _ICLOUD_OUTBOX
    outbox.mkdir(parents=True, exist_ok=True)
    dest = outbox / src.name
    if not dest.exists():
        shutil.copy2(src, dest)
        return dest
    stem = src.stem
    suffix = src.suffix
    counter = 1
    while True:
        candidate = outbox / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            shutil.copy2(src, candidate)
            return candidate
        counter += 1


def send_media(
    client: Any,
    *,
    kind: str,
    path: str,
    to_user_id: str | None = None,
    context_token: str | None = None,
    style: str | None = None,
    channel_label: str,
) -> bool:
    """Dispatch to the right `client.send_*` based on `kind`. Returns send_* bool.

    If the file's ciphertext would exceed the CDN ceiling, copies it to the
    iCloud outbox and sends a WeChat text notification instead of uploading.
    """
    method_name = _KIND_TO_METHOD.get(kind)
    if method_name is None:
        logger.warning("send_media: unknown kind %r", kind)
        return False
    method = getattr(client, method_name, None)
    if method is None:
        logger.warning("send_media: client missing %s", method_name)
        return False

    send_path = path
    tmp_dir_to_clean: Path | None = None

    if kind == "image":
        from . import image as image_mod

        original = Path(path)
        thumb_path = original.parent / "_thumb" / f"{original.stem}.webp"
        if "/stickers/" in path and "/_thumb/" not in path and thumb_path.exists():
            send_path = str(thumb_path)
        else:
            result = image_mod.downscale_for_send(original)
            if result != original:
                send_path = str(result)
                tmp_dir_to_clean = result.parent

    # Size gate sits AFTER downscale so large images keep their downscale
    # path; only what would actually hit the CDN is measured.
    try:
        raw_size = Path(send_path).stat().st_size
    except OSError:
        raw_size = 0

    if raw_size > 0 and _pkcs7_padded_size(raw_size) > _CDN_MAX_CIPHERTEXT:
        if tmp_dir_to_clean is not None:
            shutil.rmtree(tmp_dir_to_clean, ignore_errors=True)
        logger.warning(
            "send_media: ciphertext would exceed CDN ceiling, using iCloud fallback: %s",
            path,
        )
        try:
            dest = _icloud_outbox_copy(Path(path))
            msg = t(
                "media.icloud_outbox",
                style,
                name=dest.name,
                folder=dest.parent.name,
            )
            recipient = to_user_id or getattr(client, "_last_from_wxid", None) or ""
            ctx = context_token if context_token is not None else (
                getattr(client, "_last_ctx_token", "") or ""
            )
            if recipient:
                client.send_text(recipient, ctx, msg)
        except Exception as e:
            logger.error("send_media: iCloud fallback failed: %s", e)
        _record_outbound_failure(kind, "ciphertext exceeds CDN ceiling")
        return False

    try:
        ok = bool(
            method(send_path, to_user_id=to_user_id, context_token=context_token)
        )
    finally:
        if tmp_dir_to_clean is not None:
            try:
                shutil.rmtree(tmp_dir_to_clean, ignore_errors=True)
            except Exception:
                pass

    if not ok:
        if "/stickers/" in path:
            logger.warning("send_media: sticker CDN upload failed, skipping iCloud fallback: %s", path)
        else:
            logger.warning("send_media: CDN upload failed, falling back to iCloud outbox: %s", path)
            try:
                dest = _icloud_outbox_copy(Path(path))
                msg = t(
                    "media.icloud_outbox",
                    style,
                    name=dest.name,
                    folder=dest.parent.name,
                )
                recipient = to_user_id or getattr(client, "_last_from_wxid", None) or ""
                ctx = context_token if context_token is not None else (
                    getattr(client, "_last_ctx_token", "") or ""
                )
                if recipient:
                    client.send_text(recipient, ctx, msg)
            except Exception as e:
                logger.error("send_media: iCloud fallback after CDN failure also failed: %s", e)
        _record_outbound_failure(kind, "CDN upload returned falsy")
        return False

    _record_outbound_success(kind)
    return ok


def dispatch_media_bubble(
    client: Any,
    bubble: dict,
    *,
    to_user_id: str | None = None,
    context_token: str | None = None,
    style: str | None = None,
    channel_label: str,
) -> bool:
    """Send a typed media bubble (`{kind, path}`) via the right client method.

    Text bubbles return False — caller decides text routing.
    """
    kind = bubble.get("kind")
    if kind == "text":
        return False
    path = bubble.get("path")
    if not isinstance(path, str) or not path:
        logger.warning("dispatch_media_bubble: missing path on %r", bubble)
        return False
    return send_media(
        client,
        kind=kind,
        path=path,
        to_user_id=to_user_id,
        context_token=context_token,
        style=style,
        channel_label=channel_label,
    )
