"""iLink client tests — hand-rolled httpx mocking, no real network."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from synapse_wx.ilink import _auth
from synapse_wx.ilink import client as client_module
from synapse_wx.ilink.client import ILinkClient
from synapse_wx.ilink.cursor import Cursor


def _make_response(
    status: int = 200, json_body: dict | None = None, content: bytes | None = None
) -> MagicMock:
    """Build a minimal MagicMock that quacks like httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.content = content if content is not None else b""
    resp.text = json.dumps(json_body) if json_body is not None else ""
    resp.json.return_value = json_body if json_body is not None else {}
    if status >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "boom", request=MagicMock(), response=resp
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


@pytest.fixture
def isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect token + cursor to tmp_path so tests don't touch real config."""
    token_file = tmp_path / "token.json"
    monkeypatch.setattr(_auth, "TOKEN_FILE", token_file)
    monkeypatch.setattr(client_module, "TOKEN_FILE", token_file)
    return tmp_path


@pytest.fixture
def logged_in_client(
    isolated_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> ILinkClient:
    """Build a client pre-loaded with a fake token + injected mock http client."""
    token_file = isolated_paths / "token.json"
    token_file.write_text(
        json.dumps(
            {
                "bot_token": "tok-abc",
                "base_url": "https://ilinkai.weixin.qq.com",
            }
        )
    )
    cursor = Cursor(isolated_paths / "cursor.json")
    c = ILinkClient(cursor=cursor)
    c._client = MagicMock(spec=httpx.Client)
    return c


# -- Token restore + auth header injection -------------------------------------


def test_restores_token_from_disk(isolated_paths: Path) -> None:
    token_file = isolated_paths / "token.json"
    token_file.write_text(
        json.dumps(
            {"bot_token": "tok-xyz", "base_url": "https://ilinkai.weixin.qq.com"}
        )
    )
    cursor = Cursor(isolated_paths / "cursor.json")
    c = ILinkClient(cursor=cursor)
    assert c.is_logged_in
    assert c.bot_token == "tok-xyz"


def test_headers_include_bearer_token(logged_in_client: ILinkClient) -> None:
    headers = logged_in_client._headers()
    assert headers["Authorization"] == "Bearer tok-abc"
    assert headers["AuthorizationType"] == "ilink_bot_token"
    assert "X-WECHAT-UIN" in headers


def test_headers_raise_when_not_logged_in(isolated_paths: Path) -> None:
    cursor = Cursor(isolated_paths / "cursor.json")
    c = ILinkClient(cursor=cursor)
    with pytest.raises(RuntimeError, match="Not logged in"):
        c._headers()


# -- poll_messages -------------------------------------------------------------


def test_poll_messages_posts_to_getupdates_with_cursor(
    logged_in_client: ILinkClient,
) -> None:
    logged_in_client._cursor = "old-cursor"
    logged_in_client._client.post.return_value = _make_response(
        200,
        {
            "ret": 0,
            "get_updates_buf": "new-cursor",
            "msgs": [
                {"message_type": 1, "from_user_id": "u1", "item_list": []},
                {"message_type": 5, "from_user_id": "u2", "item_list": []},  # ack/echo
            ],
        },
    )

    msgs = logged_in_client.poll_messages()

    call = logged_in_client._client.post.call_args
    assert call.args[0].endswith("/ilink/bot/getupdates")
    body: dict[str, Any] = call.kwargs["json"]
    assert body["get_updates_buf"] == "old-cursor"
    assert body["base_info"]["channel_version"] == "1.0.2"

    # cursor advanced + persisted; message_type=1 only
    assert logged_in_client._cursor == "new-cursor"
    assert logged_in_client._cursor_store.get() == "new-cursor"
    assert len(msgs) == 1
    assert msgs[0]["from_user_id"] == "u1"


def test_poll_messages_returns_empty_on_ret_error(
    logged_in_client: ILinkClient,
) -> None:
    logged_in_client._client.post.return_value = _make_response(
        200, {"ret": 500, "errmsg": "server sad"}
    )
    assert logged_in_client.poll_messages() == []


def test_poll_messages_returns_empty_on_non_json(
    logged_in_client: ILinkClient,
) -> None:
    resp = _make_response(200)
    resp.json.side_effect = json.JSONDecodeError("bad", "", 0)
    logged_in_client._client.post.return_value = resp
    assert logged_in_client.poll_messages() == []


# -- send_text -----------------------------------------------------------------


def test_send_text_payload_shape(logged_in_client: ILinkClient) -> None:
    logged_in_client._client.post.return_value = _make_response(200, {"ret": 0})

    ok = logged_in_client.send_text("user-9", "ctx-1", "hi there")

    assert ok is True
    call = logged_in_client._client.post.call_args
    assert call.args[0].endswith("/ilink/bot/sendmessage")
    payload = call.kwargs["json"]
    msg = payload["msg"]
    assert msg["to_user_id"] == "user-9"
    assert msg["context_token"] == "ctx-1"
    assert msg["message_type"] == 2
    assert msg["item_list"][0]["text_item"]["text"] == "hi there"
    assert msg["client_id"].startswith("synapse-wx:")


def test_send_text_returns_false_on_error_ret(logged_in_client: ILinkClient) -> None:
    logged_in_client._client.post.return_value = _make_response(
        200, {"ret": -1, "errmsg": "nope"}
    )
    assert logged_in_client.send_text("u", "c", "hi") is False


def test_send_text_retries_rejected_chunk_then_succeeds(
    logged_in_client: ILinkClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_module.time, "sleep", lambda _s: None)
    logged_in_client._send_retry_base_sec = 0.0
    # First POST rejected (ret=-2 rate limit), second POST accepted.
    logged_in_client._client.post.side_effect = [
        _make_response(200, {"ret": -2, "errmsg": "rate limited"}),
        _make_response(200, {"ret": 0}),
    ]
    assert logged_in_client.send_text("u", "c", "hi") is True
    assert logged_in_client._client.post.call_count == 2


def test_send_text_gives_up_after_n_attempts(
    logged_in_client: ILinkClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_module.time, "sleep", lambda _s: None)
    logged_in_client._send_retry_attempts = 3
    logged_in_client._send_retry_base_sec = 0.0
    logged_in_client._client.post.return_value = _make_response(
        200, {"ret": -2, "errmsg": "rate limited"}
    )
    assert logged_in_client.send_text("u", "c", "hi") is False
    # 3 attempts on the single chunk, then abandon.
    assert logged_in_client._client.post.call_count == 3


def test_send_text_abandons_remaining_chunks_on_failure(
    logged_in_client: ILinkClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_module.time, "sleep", lambda _s: None)
    logged_in_client._send_retry_attempts = 2
    logged_in_client._send_retry_base_sec = 0.0
    # chunk 1 ok, chunk 2 rejected on both attempts → 3 POSTs total, no chunk 3.
    logged_in_client._client.post.side_effect = [
        _make_response(200, {"ret": 0}),
        _make_response(200, {"ret": -2}),
        _make_response(200, {"ret": -2}),
    ]
    long_text = ("a" * 3000) + "\n" + ("b" * 3000) + "\n" + ("c" * 3000)
    assert logged_in_client.send_text("u", "c", long_text) is False
    assert logged_in_client._client.post.call_count == 3


def test_send_text_splits_long_text(
    logged_in_client: ILinkClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    logged_in_client._client.post.return_value = _make_response(200, {"ret": 0})
    long_text = ("a" * 3000) + "\n" + ("b" * 3000)
    assert logged_in_client.send_text("u", "c", long_text) is True
    # 2 chunks -> 2 POSTs
    assert logged_in_client._client.post.call_count == 2


# -- extract_text / extract_media ---------------------------------------------


def test_extract_text_joins_text_items() -> None:
    msg = {
        "item_list": [
            {"type": 1, "text_item": {"text": "line 1"}},
            {"type": 2, "image_item": {}},  # ignored
            {"type": 1, "text_item": {"text": "line 2"}},
        ]
    }
    assert ILinkClient.extract_text(msg) == "line 1\nline 2"


def test_extract_media_image_uses_url_field() -> None:
    msg = {
        "item_list": [
            {
                "type": 2,
                "image_item": {
                    "url": "https://cdn/img.jpg",
                    "aeskey": "AABB",
                    "thumb_width": 100,
                    "thumb_height": 200,
                    "hd_size": 4096,
                    "media": {"encrypt_query_param": "qp"},
                },
            }
        ]
    }
    media = ILinkClient.extract_media(msg)
    assert len(media) == 1
    item = media[0]
    assert item["type"] == "image"
    assert item["cdn_url"] == "https://cdn/img.jpg"
    assert item["aes_key"] == "AABB"
    assert item["encrypt_query_param"] == "qp"
    assert item["width"] == 100
    assert item["height"] == 200


def test_extract_media_file_and_voice() -> None:
    msg = {
        "item_list": [
            {"type": 3, "voice_item": {"text": "hello transcribed"}},
            {
                "type": 4,
                "file_item": {
                    "file_name": "report.pdf",
                    "media": {
                        "full_url": "https://cdn/file",
                        "aes_key": "KEY",
                        "encrypt_query_param": "Q",
                    },
                },
            },
        ]
    }
    media = ILinkClient.extract_media(msg)
    assert media[0] == {"type": "voice", "text": "hello transcribed"}
    assert media[1]["type"] == "file"
    assert media[1]["filename"] == "report.pdf"
    assert media[1]["cdn_url"] == "https://cdn/file"
    assert media[1]["aes_key"] == "KEY"


# -- logout --------------------------------------------------------------------


def test_logout_clears_token_file(
    isolated_paths: Path, logged_in_client: ILinkClient
) -> None:
    token_file = isolated_paths / "token.json"
    assert token_file.exists()
    logged_in_client.logout()
    assert not token_file.exists()
    assert logged_in_client.bot_token is None
