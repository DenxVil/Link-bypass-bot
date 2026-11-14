import asyncio
import logging
import os
import re
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from .resolver import UnsafeURL, validate_public_url

BROWSER_SEMAPHORE = asyncio.Semaphore(1)
BROWSER_LOGGER = logging.getLogger(__name__)

CHALLENGE_MARKERS = (
    "verify you are human", "checking your browser", "captcha",
    "turnstile", "hcaptcha", "recaptcha",
)
INTERSTITIAL_MARKERS = (
    "click here to continue", "continue to destination", "get your link",
    "your link is ready", "skip this ad", "skip ad", "redirecting in",
    "please wait", "link will be ready",
)
GATEWAY_HOSTS = {
    "nowshort.com", "www.nowshort.com", "open.nowshort.com",
    "liteshort.com", "www.liteshort.com",
}


async def resolve_with_browser(url: str, timeout: float = 12.0) -> tuple[str, int]:
    async with BROWSER_SEMAPHORE:
        return await _resolve_with_browser(url, timeout)


async def _resolve_with_browser(url: str, timeout: float = 12.0) -> tuple[str, int]:
    """Follow ordinary browser redirects; never claim an obvious interstitial as final."""
    timeout_ms = int(max(5.0, min(timeout, 15.0)) * 1000)
    source_host = (urlsplit(url).hostname or "").lower()
    started_on_gateway = source_host in GATEWAY_HOSTS
    redirects = 0
    current_host = None

    async def guard_route(route):
        nonlocal redirects, current_host
        request_url = route.request.url
        if request_url.startswith(("data:", "blob:", "about:")):
            await route.continue_()
            return
        try:
            await validate_public_url(request_url)
        except UnsafeURL:
            await route.abort()
            return
        host_match = re.match(r"^https?://([^/]+)", request_url, re.I)
        host = host_match.group(1).lower() if host_match else None
        if route.request.is_navigation_request() and host and current_host and host != current_host:
            redirects += 1
        if route.request.is_navigation_request() and host:
            current_host = host
        if route.request.resource_type in {"image", "media", "font"}:
            await route.abort()
            return
        await route.continue_()

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            try:
                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
                    ),
                    locale="en-US", java_script_enabled=True, accept_downloads=False,
                )
                page = await context.new_page()
                await page.route("**/*", guard_route)
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                except PlaywrightTimeoutError:
                    pass
                try:
                    await page.wait_for_load_state("networkidle", timeout=2500)
                except PlaywrightTimeoutError:
                    pass

                # Critical: page.goto may already have crossed from NowShort to an
                # intermediate domain before the first URL snapshot. Always allow
                # the whole gateway flow to settle, not only pages still on gateway hosts.
                wait_seconds = max(0, min(int(os.getenv("BROWSER_WAIT_SECONDS", "20")), 25))
                if started_on_gateway:
                    last_url = page.url
                    stable_seconds = 0
                    for _ in range(wait_seconds):
                        await page.wait_for_timeout(1000)
                        observed = page.url
                        if observed != last_url:
                            last_url = observed
                            stable_seconds = 0
                        else:
                            stable_seconds += 1
                        # Require a short stable period after the last navigation,
                        # while still preserving a minimum observation window.
                        if _ >= 7 and stable_seconds >= 5:
                            break
                else:
                    await page.wait_for_timeout(1200)

                final_url = page.url
                try:
                    title = (await page.title()).lower()
                except PlaywrightTimeoutError:
                    title = ""
                try:
                    body_text = (await page.locator("body").inner_text(timeout=2500)).lower()[:5000]
                except PlaywrightTimeoutError:
                    body_text = ""

                if any(marker in title or marker in body_text for marker in CHALLENGE_MARKERS):
                    raise UnsafeURL("This destination requires human verification.")
                if started_on_gateway and any(marker in body_text for marker in INTERSTITIAL_MARKERS):
                    raise UnsafeURL(
                        "The shortener stopped at an intermediate page; it requires a page action or further redirect. No final destination was exposed."
                    )
                await validate_public_url(final_url)
                return final_url, redirects
            finally:
                await browser.close()
    except UnsafeURL:
        raise
    except (PlaywrightError, OSError, RuntimeError) as exc:
        BROWSER_LOGGER.exception(
            "Browser resolution failed (%s): %s", type(exc).__name__, str(exc)[:500]
        )
        raise UnsafeURL(
            f"Browser resolution failed ({type(exc).__name__}); check service runtime logs."
        ) from exc
