"""Telegram 'typing...' indicator: re-pings while cc thinks."""

from __future__ import annotations

import asyncio
import logging

from telegram import Bot, constants

logger = logging.getLogger(__name__)

_INTERVAL = 4.5  # TG typing expires after 5s; re-ping with margin


class TypingAction:
    """Async background task that sends ChatAction.TYPING on repeat."""

    def __init__(self, bot: Bot, chat_id: int) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _loop(self) -> None:
        try:
            while True:
                await self._bot.send_chat_action(
                    chat_id=self._chat_id,
                    action=constants.ChatAction.TYPING,
                )
                await asyncio.sleep(_INTERVAL)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
