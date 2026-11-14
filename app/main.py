import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
import html
import hmac
import logging
import os
import re
import time

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand, Update
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from . import config
from .resolver import UnsafeURL, resolve_url
from .storage import allow_request, get_cached, init_storage, put_cached

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("link-resolver")
bot: Bot | None = None
dp = Dispatcher()
URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)

_SEEN_UPDATES: OrderedDict[int, float] = OrderedDict()
_MAX_SEEN_UPDATES = 2048

WELCOME_TEXT = (
    "👋 <b>Welcome to Link Resolver</b>\n"
    "<i>Your quick assistant for expanding supported shortened links.</i>\n\n"
    "🔎 <b>Features</b>\n"
    " • Trace public HTTP redirects and encoded destinations\n"
    " • Use Chromium for ordinary JavaScript navigation\n"
    " • Cache successful results for faster repeat lookups\n"
    " • Show live progress and the final resolved URL\n\n"
    "🚀 <b>How to use</b>\n"
    "Send a shortened link directly, or use <code>/bypass URL</code>.\n\n"
    "Use /help for commands and limitations."
)

HELP_TEXT = (
    "📘 <b>LINK RESOLVER — HELP</b>\n\n"
    "<b>Commands</b>\n"
    " /start — Welcome and getting started\n"
    " /bypass URL — Resolve a link\n"
    " /help — Show these instructions\n"
    " /about — About this bot\n\n"
    "<b>Resolution methods</b>\n"
    " • HTTP redirect tracing and static destination extraction\n"
    " • Base64/URL-safe-base64 destination extraction\n"
    " • Headless Chromium for ordinary JavaScript navigation\n"
    " • SQLite cache and a limit of 40 requests per hour per user\n\n"
    "<b>Limitations</b>\n"
    " • CAPTCHA, login, paywalls, and anti-bot challenges are not bypassed.\n"
    " • The bot does not click ads or replay protected gateway tokens.\n"
    " • Some sites may require a real user session.\n\n"
    "⚠️ A resolved URL is not a guarantee that its destination is safe."
)

ABOUT_TEXT = (
    "ℹ️ <b>About Link Resolver</b>\n\n"
    "A Telegram utility that follows supported public web redirects and displays "
    "the resulting destination. It uses HTTP tracing, static extraction, and a "
    "limited browser fallback for ordinary JavaScript navigation.\n\n"
    "It does not certify links as safe or bypass access controls."
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
            "Send a public <code>http://</code> or <code>https://</code> URL, "
            "or use <code>/bypass URL</code>.",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    allowed, retry_after = await asyncio.to_thread(allow_request, message.from_user.id)
    if not allowed:
        minutes = max(1, (retry_after + 59) // 60)
        await message.answer(
            f"⏳ <b>Rate limit reached</b>\nTry again in approximately {minutes} minute(s).",
            parse_mode="HTML",
        )
        return

    url = match.group(0).rstrip(".,;:!?)\\]")
    status = await message.answer(
        "⏳ <b>Resolving your link…</b>\n"
        "<i>Tracing redirects and checking supported page navigation.</i>",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

    try:
        started_at = time.perf_counter()
        cached = await asyncio.to_thread(get_cached, url)
        was_cached = cached is not None
        if cached:
            destination, hops = cached
        else:
            destination, hops = await resolve_url(
                url,
                max_redirects=config.MAX_REDIRECTS,
                timeout=config.REQUEST_TIMEOUT,
            )
            await asyncio.to_thread(put_cached, url, destination, hops)

        elapsed_seconds = time.perf_counter() - started_at
        safe_original = html.escape(url)
        safe_destination = html.escape(destination)
        safe_href = html.escape(destination, quote=True)
        cache_note = "⚡ <i>Served from cache</i>\n" if was_cached else ""
        result = (
            "🔗 <b>LINK RESOLUTION COMPLETE</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "📥 <b>Original Link</b>\n"
            f"<code>{safe_original}</code>\n\n"
            "🚀 <b>Final Destination</b>\n"
            f'<a href="{safe_href}">{safe_destination}</a>\n\n'
            f"⏱ <b>Time Taken:</b> <code>{elapsed_seconds:.2f} seconds</code>\n"
            f"🧭 <b>Redirect steps:</b> <code>{hops}</code>\n"
            f"{cache_note}\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "💠 <b>Powered by</b> @Link_Bypasss"
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
            "The bot will not label an intermediate page as the final destination.",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        log.exception("Unexpected resolution failure")
        await status.edit_text(
            "❌ <b>Resolution failed</b>\n"
            "The link could not be resolved right now. It may require a browser session "
            "or access that is not available to the bot.",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )


async def _process_update(current_bot: Bot, update: Update) -> None:
    try:
        await dp.feed_update(current_bot, update)
    except Exception:
        log.exception("Background update processing failed for update_id=%s", update.update_id)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot
    init_storage()
    if config.BOT_TOKEN:
        bot = Bot(token=config.BOT_TOKEN)
        await bot.set_my_commands([
            BotCommand(command="start", description="Welcome and getting started"),
            BotCommand(command="bypass", description="Resolve a shortened URL"),
            BotCommand(command="help", description="How to use the link resolver"),
            BotCommand(command="about", description="About this bot"),
        ])
        public_url = (
            os.getenv("RENDER_EXTERNAL_URL") or os.getenv("WEBHOOK_BASE_URL", "")
        ).strip().rstrip("/")
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
        await bot.session.close()


app = FastAPI(title="Telegram Link Resolver", lifespan=lifespan)


@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return {"service": "telegram-link-resolver", "status": "ok"}


@app.api_route("/healthz", methods=["GET", "HEAD"])
async def healthz():
    return {"status": "ok", "bot_configured": bool(config.BOT_TOKEN)}


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
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
    except Exception:
        log.exception("Invalid Telegram update payload")
        raise HTTPException(status_code=400, detail="Invalid update payload")

    if update.update_id in _SEEN_UPDATES:
        return JSONResponse({"ok": True, "duplicate": True})
    _SEEN_UPDATES[update.update_id] = time.time()
    while len(_SEEN_UPDATES) > _MAX_SEEN_UPDATES:
        _SEEN_UPDATES.popitem(last=False)

    background_tasks.add_task(_process_update, bot, update)
    return JSONResponse({"ok": True})
