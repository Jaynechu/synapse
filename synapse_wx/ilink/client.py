"""iLink Bot API client — direct talk to https://ilinkai.weixin.qq.com.

All endpoint quirks preserved verbatim; helpers split into _auth.py / cursor.py
/ retry.py. cryptography import is deferred to download_media so the rest of
the client runs without it.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from ._auth import (
    CHANNEL_VERSION,
    ILINK_BASE_URL,
    POLL_TIMEOUT_S,
    TOKEN_FILE,
    auth_headers,
    load_token,
    validate_base_url,
)
from ._login import run_qr_login
from ._media import download_and_decrypt, upload_and_encrypt
from .cursor import Cursor
from .retry import with_retry

# Item type codes used in iLink `item_list` envelopes.
# Inbound mapping (verified): 1=text · 2=image · 3=voice · 4=file · 5=video.
# GIFs are sent as plain images (type 2) — reference never uses type 8.
_ITEM_TYPE_IMAGE = 2
_ITEM_TYPE_FILE = 4
_ITEM_TYPE_VIDEO = 5

logger = logging.getLogger(__name__)


class ILinkClient:
    """Minimal, secure iLink Bot API client."""

    def __init__(
        self,
        cursor: Cursor | None = None,
        raw_poll_logger: Any = None,
        *,
        quota_wait_sec: float = 65.0,
    ) -> None:
        self.bot_token: str | None = None
        self.base_url: str = ILINK_BASE_URL
        self._client = httpx.Client(timeout=POLL_TIMEOUT_S + 5)
        self._cursor_store = cursor or Cursor()
        self._cursor: str = self._cursor_store.get()
        self._typing_ticket: str = ""
        # On a business rejection (ret!=0) the iLink send quota (~10 msgs /
        # ~60s window) is exhausted; wait this long for it to roll over, then
        # retry the chunk once.
        self._quota_wait_sec = max(0.0, quota_wait_sec)
        # PLAN 2c: optional RawPollLogger dumping raw getupdates payloads
        # (pre-filter, pre-ret-check) for the inbound typing-event hunt.
        self._raw_poll_logger = raw_poll_logger
        self._try_restore_token()

    # -- Token / auth --------------------------------------------------------

    def _try_restore_token(self) -> None:
        data = load_token()
        if data:
            self.bot_token = data.get("bot_token")
            self.base_url = validate_base_url(data.get("base_url", ILINK_BASE_URL))
            logger.info("Restored saved token.")

    @property
    def is_logged_in(self) -> bool:
        return self.bot_token is not None

    def _headers(self) -> dict[str, str]:
        if not self.bot_token:
            raise RuntimeError("Not logged in. Call login() first.")
        return auth_headers(self.bot_token)

    # -- Login flow ----------------------------------------------------------

    def login(self) -> None:
        """Interactive QR login. Delegates to _login.run_qr_login."""
        run_qr_login(self)

    def logout(self) -> None:
        """Clear saved credentials."""
        self.bot_token = None
        if TOKEN_FILE.exists():
            TOKEN_FILE.unlink()
        logger.info("Logged out.")

    # -- Message polling -----------------------------------------------------

    @with_retry()
    def poll_messages(self) -> list[dict]:
        """Long-poll for new messages. Returns inbound message dicts."""
        resp = self._client.post(
            f"{self.base_url}/ilink/bot/getupdates",
            headers=self._headers(),
            json={
                "get_updates_buf": self._cursor,
                "base_info": {"channel_version": CHANNEL_VERSION},
            },
            timeout=POLL_TIMEOUT_S + 5,
        )
        resp.raise_for_status()
        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError):
            logger.warning("Non-JSON response from getupdates")
            return []

        if self._raw_poll_logger is not None:
            self._raw_poll_logger.log(data)

        ret = data.get("ret")
        if ret is not None and ret != 0:
            logger.warning("getupdates: ret=%s errmsg=%s", ret, data.get("errmsg", ""))
            return []

        # Advance cursor for next poll
        new_cursor = data.get("get_updates_buf", "")
        if new_cursor:
            self._cursor = new_cursor
            self._cursor_store.set(new_cursor)

        all_msgs = data.get("msgs", [])
        # Filter for inbound user messages (message_type=1)
        # server also emits ack/echo
        return [m for m in all_msgs if m.get("message_type") == 1]

    # -- Message sending -----------------------------------------------------

    @with_retry()
    def send_text(
        self,
        to_user_id: str,
        context_token: str,
        text: str,
    ) -> bool:
        """Send a text message back. Splits >4000-char text at line breaks.

        Note: real reply-quote rendering via ``ref_msg`` was attempted but
        WeChat does NOT render the resulting bubble as a quote. See MAP.md
        "Known limitations" — the bridge instead emits a visual fake-quote
        bubble (▎FRAGMENT) ahead of the reply in ``loop.maybe_flush``.
        """
        chunks = self._split_text(text, max_len=4000)
        for chunk in chunks:
            if not self._send_chunk(to_user_id, context_token, chunk):
                # Abandon remaining chunks — a partial turn is better than
                # duplicating already-delivered chunks via a whole-fn retry.
                return False
        return True

    def _send_chunk(self, to_user_id: str, context_token: str, chunk: str) -> bool:
        """POST one text chunk; on business rejection, wait out the send quota.

        The iLink send quota is a COUNT quota (~10 msgs per ~60s window), not a
        pacing limit — exponential backoff cannot beat it. On a rejection
        (non-200 or ret!=0) we sleep ``quota_wait_sec`` for the window to roll
        over, then retry the chunk exactly once. Chunk-local so a retry never
        re-sends earlier chunks. Transport exceptions bubble up to the
        ``@with_retry`` decorator on send_text.
        """
        ok, ret, errmsg = self._post_chunk(to_user_id, context_token, chunk)
        if ok:
            return True
        if self._quota_wait_sec <= 0:
            logger.error("Failed to send message: ret=%s, errmsg=%s", ret, errmsg)
            return False
        logger.warning(
            "send chunk rejected (ret=%s errmsg=%s) — waiting %.1fs for quota "
            "window to roll, then one retry",
            ret,
            errmsg,
            self._quota_wait_sec,
        )
        self._sleep_quota_window()
        ok, ret, errmsg = self._post_chunk(to_user_id, context_token, chunk)
        if ok:
            return True
        logger.error(
            "Failed to send message after quota wait: ret=%s, errmsg=%s", ret, errmsg
        )
        return False

    def _sleep_quota_window(self) -> None:
        """Sleep ``quota_wait_sec`` in ~1s slices so stop signals stay responsive."""
        remaining = self._quota_wait_sec
        while remaining > 0:
            slice_sec = 1.0 if remaining > 1.0 else remaining
            self._sleeper(slice_sec)
            remaining -= slice_sec

    # Overridable sleeper so tests never wait real seconds.
    _sleeper = staticmethod(time.sleep)

    def _post_chunk(
        self, to_user_id: str, context_token: str, chunk: str
    ) -> tuple[bool, Any, str]:
        """POST one chunk once. Returns (ok, ret, errmsg)."""
        client_id = f"synapse-wx:{uuid.uuid4().hex[:16]}"
        item: dict = {"type": 1, "text_item": {"text": chunk}}
        msg: dict = {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": client_id,
            "message_type": 2,
            "message_state": 2,
            "context_token": context_token,
            "item_list": [item],
        }
        payload = {
            "msg": msg,
            "base_info": {"channel_version": CHANNEL_VERSION},
        }
        resp = self._client.post(
            f"{self.base_url}/ilink/bot/sendmessage",
            headers=self._headers(),
            json=payload,
        )
        try:
            resp_data = resp.json()
        except (json.JSONDecodeError, ValueError):
            logger.error(
                "Non-JSON response from sendmessage: status=%d", resp.status_code
            )
            return False, None, "non-JSON response"
        ret = resp_data.get("ret")
        if resp.status_code == 200 and (ret is None or ret == 0):
            return True, ret, ""
        return False, ret, str(resp_data.get("errmsg", resp.text[:200]))

    def send_typing(self, to_user_id: str, context_token: str) -> None:
        """Best-effort typing indicator. Swallows all errors.

        TYPING_PROBE log measures wall-clock cost; first call pays two
        round-trips (getconfig + sendtyping), subsequent calls one
        (ticket cached on self._typing_ticket).
        """
        t0 = time.monotonic()
        try:
            if not self._typing_ticket:
                config_resp = self._client.post(
                    f"{self.base_url}/ilink/bot/getconfig",
                    headers=self._headers(),
                    json={
                        "ilink_user_id": to_user_id,
                        "context_token": context_token or "",
                    },
                    timeout=5,
                )
                if config_resp.status_code == 200:
                    data = config_resp.json()
                    self._typing_ticket = data.get("typing_ticket", "")
            if self._typing_ticket:
                self._client.post(
                    f"{self.base_url}/ilink/bot/sendtyping",
                    headers=self._headers(),
                    json={
                        "ilink_user_id": to_user_id,
                        "typing_ticket": self._typing_ticket,
                        "status": 1,
                    },
                    timeout=5,
                )
        except Exception:
            pass
        finally:
            elapsed = time.monotonic() - t0
            logger.info(
                "TYPING_PROBE: send_typing returned in %.3fs (ticket_cached=%s)",
                elapsed,
                bool(self._typing_ticket),
            )

    @staticmethod
    def _split_text(text: str, max_len: int = 4000) -> list[str]:
        if len(text) <= max_len:
            return [text]
        chunks = []
        while text:
            if len(text) <= max_len:
                chunks.append(text)
                break
            split_at = text.rfind("\n", 0, max_len)
            if split_at <= 0:
                split_at = max_len
            chunks.append(text[:split_at])
            text = text[split_at:].lstrip("\n")
        return chunks

    # -- Media send (C1: outbound) -------------------------------------------

    def send_image(
        self,
        path: str,
        *,
        to_user_id: str | None = None,
        context_token: str | None = None,
    ) -> bool:
        """Upload an image (AES-ECB) and dispatch as a WeChat image bubble."""
        return self._send_media(path, _ITEM_TYPE_IMAGE, to_user_id, context_token)

    def send_file(
        self,
        path: str,
        *,
        to_user_id: str | None = None,
        context_token: str | None = None,
    ) -> bool:
        """Upload a generic file (AES-ECB) and dispatch as a WeChat file bubble."""
        return self._send_media(path, _ITEM_TYPE_FILE, to_user_id, context_token)

    def send_gif(
        self,
        path: str,
        *,
        to_user_id: str | None = None,
        context_token: str | None = None,
    ) -> bool:
        """Upload a GIF (AES-ECB) and dispatch as an image bubble (type 2)."""
        return self._send_media(path, _ITEM_TYPE_IMAGE, to_user_id, context_token)

    def send_video(
        self,
        path: str,
        *,
        to_user_id: str | None = None,
        context_token: str | None = None,
    ) -> bool:
        """Upload a short video (AES-ECB) and dispatch as a video bubble."""
        return self._send_media(path, _ITEM_TYPE_VIDEO, to_user_id, context_token)

    def _send_media(
        self,
        path: str,
        item_type: int,
        to_user_id: str | None,
        context_token: str | None,
    ) -> bool:
        """Shared upload→sendmessage path for image/file/gif/video.

        Resolves recipient from explicit kwargs first, then the last inbound's
        from/ctx cache (mirrors how MainLoop tracks `_last_from_wxid`).
        """
        recipient = to_user_id or getattr(self, "_last_from_wxid", None)
        ctx = context_token if context_token is not None else (
            getattr(self, "_last_ctx_token", "") or ""
        )
        if not recipient:
            logger.warning("send_media: no recipient (path=%s)", path)
            return False

        type_name = {
            _ITEM_TYPE_IMAGE: "image",
            _ITEM_TYPE_FILE: "file",
            _ITEM_TYPE_VIDEO: "video",
        }.get(item_type, "file")
        meta = upload_and_encrypt(
            self._client,
            base_url=self.base_url,
            headers=self._headers(),
            path=Path(path),
            item_type=type_name,
            to_user_id=recipient,
        )
        if not meta:
            return False

        item = self._build_media_item(item_type, Path(path), meta)
        client_id = f"synapse-wx:{uuid.uuid4().hex[:16]}"
        payload = {
            "msg": {
                "from_user_id": "",
                "to_user_id": recipient,
                "client_id": client_id,
                "message_type": 2,
                "message_state": 2,
                "context_token": ctx,
                "item_list": [item],
            },
            "base_info": {"channel_version": CHANNEL_VERSION},
        }
        try:
            resp = self._client.post(
                f"{self.base_url}/ilink/bot/sendmessage",
                headers=self._headers(),
                json=payload,
            )
        except Exception as e:
            logger.error("send_media POST failed: %s", e)
            return False
        try:
            body = resp.json()
        except (json.JSONDecodeError, ValueError):
            logger.error("send_media: non-JSON response status=%s", resp.status_code)
            return False
        ret = body.get("ret")
        if resp.status_code != 200 or (ret is not None and ret != 0):
            logger.error(
                "send_media failed: ret=%s errmsg=%s",
                ret,
                body.get("errmsg", resp.text[:200]),
            )
            return False
        return True

    @staticmethod
    def _build_media_item(item_type: int, path: Path, meta: dict) -> dict:
        """Construct an `item_list` entry for outbound media sends."""
        qp = meta.get("encrypt_query_param", "")
        aes_key_b64 = meta.get("aes_key_b64", "")
        aes_key_hex = meta.get("aes_key_hex", "")
        padded_size = meta.get("padded_size", 0)
        rawsize = meta.get("rawsize", 0)
        media_ref = {
            "encrypt_query_param": qp,
            "aes_key": aes_key_b64,  # base64(hex_string.encode())
            "encrypt_type": 1,
        }
        if item_type == _ITEM_TYPE_IMAGE:
            return {
                "type": _ITEM_TYPE_IMAGE,
                "image_item": {
                    "media": media_ref,
                    "aeskey": aes_key_hex,
                    "mid_size": padded_size,
                    "hd_size": padded_size,
                },
            }
        if item_type == _ITEM_TYPE_FILE:
            return {
                "type": _ITEM_TYPE_FILE,
                "file_item": {
                    "media": media_ref,
                    "file_name": path.name,
                    "len": str(rawsize),
                },
            }
        if item_type == _ITEM_TYPE_VIDEO:
            return {
                "type": _ITEM_TYPE_VIDEO,
                "video_item": {
                    "media": media_ref,
                    "video_size": padded_size,
                },
            }
        # fallback: file shape
        return {
            "type": _ITEM_TYPE_FILE,
            "file_item": {
                "media": media_ref,
                "file_name": path.name,
                "len": str(rawsize),
            },
        }

    # -- Message extraction --------------------------------------------------

    @staticmethod
    def extract_text(message: dict) -> str:
        """Extract plain text content from a message bubble."""
        parts = []
        for item in message.get("item_list", []):
            if item.get("type") == 1:
                text_item = item.get("text_item", {})
                parts.append(text_item.get("text", ""))
        return "\n".join(parts)

    @staticmethod
    def extract_media(message: dict) -> list[dict]:
        """Extract media items (images, files, voice) from a message.

        # iLink uses both 'aeskey' (no underscore) and 'aes_key' depending on
        # item kind; we try all.
        """
        media: list[dict] = []
        for item in message.get("item_list", []):
            item_type = item.get("type")
            if item_type == 2:  # Image
                img = item.get("image_item", {})
                img_media = img.get("media", {})
                cdn_url = img.get("url", img.get("cdn_img_url", img.get("cdn_url", "")))
                aes_key = (
                    img_media.get("aes_key", "")
                    or img.get("aeskey", "")
                    or img.get("aes_key", "")
                )
                media.append(
                    {
                        "type": "image",
                        "cdn_url": cdn_url,
                        "aes_key": aes_key,
                        "encrypt_query_param": img_media.get("encrypt_query_param", ""),
                        "width": img.get("thumb_width", img.get("width", 0)),
                        "height": img.get("thumb_height", img.get("height", 0)),
                        "hd_size": img.get("hd_size", 0),
                    }
                )
            elif item_type == 3:  # Voice
                voice = item.get("voice_item", {})
                media.append({"type": "voice", "text": voice.get("text", "")})
            elif item_type == 4:  # File
                file_item = item.get("file_item", {})
                file_media = file_item.get("media", {})
                cdn_url = file_media.get("full_url", "") or file_item.get("cdn_url", "")
                aes_key = file_media.get("aes_key", "") or file_item.get("aes_key", "")
                media.append(
                    {
                        "type": "file",
                        "cdn_url": cdn_url,
                        "aes_key": aes_key,
                        "encrypt_query_param": file_media.get("encrypt_query_param", ""),
                        "filename": file_item.get("file_name", "unknown"),
                    }
                )
            elif item_type == 5:  # Video
                # Schema locked from live payload: video_item.media.{full_url,
                # aes_key, encrypt_query_param} + thumb_media triplet + play_length.
                video_item = item.get("video_item", {})
                video_media = video_item.get("media", {})
                thumb_media = video_item.get("thumb_media", {})
                try:
                    play_length = int(video_item.get("play_length", 0) or 0)
                except (TypeError, ValueError):
                    play_length = 0
                entry: dict = {
                    "type": "video",
                    "cdn_url": video_media.get("full_url", ""),
                    "aes_key": video_media.get("aes_key", ""),
                    "encrypt_query_param": video_media.get("encrypt_query_param", ""),
                    "play_length": play_length,
                    "thumb": {
                        "cdn_url": thumb_media.get("full_url", ""),
                        "aes_key": thumb_media.get("aes_key", ""),
                        "encrypt_query_param": thumb_media.get(
                            "encrypt_query_param", ""
                        ),
                    },
                }
                filename = video_item.get("file_name")
                if filename:
                    entry["filename"] = filename
                media.append(entry)
        return media

    # -- Media download (AES-128-ECB) ----------------------------------------

    def download_media(
        self,
        cdn_url: str,
        aes_key: str,
        save_path: Path,
        encrypt_query_param: str = "",
    ) -> bool:
        """Download + AES-128-ECB decrypt media. See _media.py for details."""
        return download_and_decrypt(
            self._client, cdn_url, aes_key, save_path, encrypt_query_param
        )

    def reconnect(self) -> None:
        """Drop the underlying httpx client and start fresh (sleep/wake recovery)."""
        try:
            self._client.close()
        except Exception:
            pass
        self._client = httpx.Client(timeout=POLL_TIMEOUT_S + 5)

    def close(self) -> None:
        self._client.close()
