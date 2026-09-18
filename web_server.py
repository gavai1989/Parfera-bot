from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.types import Update

from webhook import is_valid_webhook_secret

logger = logging.getLogger(__name__)


def create_web_app(bot: Bot, dispatcher: Dispatcher, webhook_secret: str) -> web.Application:
    app = web.Application()

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def webhook_handler(request: web.Request) -> web.Response:
        received = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not is_valid_webhook_secret(webhook_secret, received):
            return web.json_response({"error": "unauthorized"}, status=401)

        try:
            payload: dict[str, Any] = await request.json()
            update = Update.model_validate(payload)
        except Exception:
            return web.json_response({"error": "invalid update"}, status=400)

        # Return to Telegram immediately; image generation can take several seconds.
        task = asyncio.create_task(dispatcher.feed_update(bot, update))
        task.add_done_callback(_log_background_error)
        return web.json_response({"ok": True})

    app.router.add_get("/health", health)
    app.router.add_post("/webhook/{secret_path}", webhook_handler)
    return app


def _log_background_error(task: asyncio.Task[Any]) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("Webhook update processing failed")
