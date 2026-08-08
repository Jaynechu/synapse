"""Bark push notification helper for synapse-tg.

Fires a push to a Bark device URL after an assistant reply is delivered.
No-ops silently when bark_push_url is not configured.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from .config import TgConfig

logger = logging.getLogger(__name__)


async def push(cfg: "TgConfig", title: str, body: str) -> None:
    """Send a Bark push notification. Never raises — a Bark outage must not
    affect message delivery."""
    if not cfg.bark_push_url:
        return

    if len(body) > cfg.bark_max_chars:
        body = body[: cfg.bark_max_chars] + "…"

    payload: dict = {"title": title, "body": body}
    if cfg.bark_icon:
        payload["icon"] = cfg.bark_icon

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(cfg.bark_push_url, json=payload)
            if resp.status_code != 200:
                logger.warning(
                    "bark push returned HTTP %s: %s", resp.status_code, resp.text[:200]
                )
    except Exception as exc:
        logger.warning("bark push failed: %s", exc)
