import hmac
import logging
import os
import re
import time
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher
from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from . import config
from .ai import explain_destination
from .resolver import UnsafeURL, resolve_url

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("link-resolver")
bot: Bot | None = None
dp = Dispatcher()
user_hits: dict[int, list[float]] = {}
URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


@dp.message()
async def handle_message(message):
    if not message.from_user:
        return
    text = message.text or message.caption or ""
    match = URL_RE.search(text)
    if not match:
        await message.answer("Send a public HTTP(S) link and I’ll follow its redirects.")
        return

    now = time.monotonic()
    hits = [stamp for stamp in user_hits.get(message.from_user.id, []) if now - stamp < 60]
    if len(hits) >= 8:
        await message.answer("Rate limit reached. Please try again in a minute.")
        return
    hits.append(now)
    user_hits[message.from_user.id] = hits

    url = match.group(0).rstrip(".,;:!?)\\]")
    status = await message.answer("Resolving public redirects…")
    try:
        destination, hops = await resolve_url(
            url, max_redirects=config.MAX_REDIRECTS, timeout=config.REQUEST_TIMEOUT
        )
        reply = f"Resolved destination:\n{destination}\n\nRedirects followed: {hops}"
        note = await explain_destination(destination)
        if note:
            reply += f"\n\nAI note (not a safety verdict):\n{note}"
        await status.edit_text(reply, disable_web_page_preview=True)
    except UnsafeURL as exc:
        await status.edit_text(str(exc))
    except Exception:
        log.exception("Unexpected resolution failure")
        await status.edit_text("Could not resolve this link. It may require a browser session or access you have not provided.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot
    if config.BOT_TOKEN:
        bot = Bot(token=config.BOT_TOKEN)
        public_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
        if public_url:
            await bot.set_webhook(
                url=f"{public_url}/telegram/webhook",
                secret_token=config.WEBHOOK_SECRET,
                allowed_updates=dp.resolve_used_update_types(),
            )
            log.info("Telegram webhook registered")
        else:
            log.warning("RENDER_EXTERNAL_URL missing; webhook not registered")
    else:
        log.warning("BOT_TOKEN missing; service is healthy but Telegram bot is inactive")
    yield
    if bot:
        # Do not delete the webhook during graceful shutdown: overlapping
        # zero-downtime deploys could otherwise unregister the new instance.
        await bot.session.close()


app = FastAPI(title="Telegram Link Resolver", lifespan=lifespan)


@app.get("/")
async def root():
    return {"service": "telegram-link-resolver", "status": "ok"}


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "bot_configured": bool(config.BOT_TOKEN)}


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    if not bot:
        raise HTTPException(status_code=503, detail="Bot is not configured")
    if not x_telegram_bot_api_secret_token or not hmac.compare_digest(
        x_telegram_bot_api_secret_token, config.WEBHOOK_SECRET
    ):
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        payload = await request.json()
        update = Update.model_validate(payload, context={"bot": bot})
        await dp.feed_update(bot, update)
    except Exception:
        log.exception("Webhook update processing failed")
        raise HTTPException(status_code=500, detail="Update processing failed")
    return JSONResponse({"ok": True})
