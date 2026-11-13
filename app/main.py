import html
import hmac
import logging
import os
import re
import time
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand, Update
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

WELCOME_TEXT = (
    "👋 <b>Welcome to Link Resolver</b>\n"
    "<i>Your quick assistant for expanding shortened links.</i>\n\n"
    "🔎 <b>What I do</b>\n"
    "  • Follow public HTTP(S) redirects\n"
    "  • Expand supported short links\n"
    "  • Show the resolved destination clearly\n"
    "  • Provide an optional AI-generated context note\n\n"
    "🚀 <b>How to use</b>\n"
    "  1. Copy a shortened link\n"
    "  2. Send it here\n"
    "  3. I’ll trace the redirects and return the result\n\n"
    "💡 <b>Tip:</b> You can send a link by itself or include it in a message.\n\n"
    "Use /help to see commands and limitations."
)

HELP_TEXT = (
    "📘 <b>LINK RESOLVER — HELP</b>\n\n"
    "<b>Available commands</b>\n"
    "  /start — Open the welcome message\n"
    "  /help — Show these instructions\n"
    "  /about — About this bot\n\n"
    "<b>Resolve a link</b>\n"
    "Send any public link beginning with <code>http://</code> or <code>https://</code>.\n\n"
    "<b>Important limitations</b>\n"
    "  • Some links need JavaScript, cookies, or a browser session.\n"
    "  • CAPTCHA, login pages, paywalls, and anti-bot checks cannot be bypassed.\n"
    "  • A resolved URL is not a guarantee that its destination is safe.\n\n"
    "⚠️ <b>Stay cautious:</b> Check unfamiliar destinations before opening them."
)

ABOUT_TEXT = (
    "ℹ️ <b>About Link Resolver</b>\n\n"
    "A Telegram utility that follows supported public web redirects and displays "
    "the resulting destination in a clean, readable format.\n\n"
    "Built for convenience. It does not certify links as safe or bypass access controls."
)


@dp.message(lambda message: message.text and message.text.startswith("/start"))
async def start_command(message):
    await message.answer(WELCOME_TEXT, parse_mode="HTML", disable_web_page_preview=True)


@dp.message(lambda message: message.text and message.text.startswith("/help"))
async def help_command(message):
    await message.answer(HELP_TEXT, parse_mode="HTML", disable_web_page_preview=True)


@dp.message(lambda message: message.text and message.text.startswith("/about"))
async def about_command(message):
    await message.answer(ABOUT_TEXT, parse_mode="HTML", disable_web_page_preview=True)


@dp.message()
async def handle_message(message):
    if not message.from_user:
        return

    incoming_text = message.text or message.caption or ""
    match = URL_RE.search(incoming_text)
    if not match:
        await message.answer(
            "🔗 <b>No link detected</b>\n\n"
            "Please send a public <code>http://</code> or <code>https://</code> URL.\n"
            "Use /help for instructions.",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    now = time.monotonic()
    hits = [
        stamp for stamp in user_hits.get(message.from_user.id, [])
        if now - stamp < 60
    ]
    if len(hits) >= 8:
        await message.answer(
            "⏳ <b>Rate limit reached</b>\nPlease wait a minute before resolving another link.",
            parse_mode="HTML",
        )
        return
    hits.append(now)
    user_hits[message.from_user.id] = hits

    url = match.group(0).rstrip(".,;:!?)\\]")
    status = await message.answer(
        "⏳ <b>Resolving your link…</b>\n"
        "<i>Following supported redirects. This may take a few seconds.</i>",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

    try:
        started_at = time.perf_counter()
        destination, _hops = await resolve_url(
            url,
            max_redirects=config.MAX_REDIRECTS,
            timeout=config.REQUEST_TIMEOUT,
        )
        elapsed_seconds = int(time.perf_counter() - started_at)
        safe_original = html.escape(url)
        safe_destination = html.escape(destination)
        safe_href = html.escape(destination, quote=True)
        result = (
            f"<b>Original Link :</b> ✅ {safe_original}\n\n"
            f"<b>Bypassed Link :</b> ✅ <a href=\"{safe_href}\">{safe_destination}</a>\n\n"
            f"<b>Time Taken :</b> {elapsed_seconds} seconds\n\n"
            "─────────────────\n\n"
            "<b>Powered By @Link_Bypasss</b>"
        )

        await status.edit_text(
            result,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except UnsafeURL as exc:
        await status.edit_text(
            "⚠️ <b>Could not resolve this link</b>\n"
            f"<i>{html.escape(str(exc))}</i>\n\n"
            "Check the URL and try again.",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        log.exception("Unexpected resolution failure")
        await status.edit_text(
            "❌ <b>Resolution failed</b>\n"
            "The link could not be resolved right now. It may require a browser session "
            "or access that is not available to the bot. Please try again later.",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot
    if config.BOT_TOKEN:
        bot = Bot(token=config.BOT_TOKEN)
        await bot.set_my_commands([
            BotCommand(command="start", description="Welcome and getting started"),
            BotCommand(command="help", description="How to use the link resolver"),
            BotCommand(command="about", description="About this bot"),
        ])
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
