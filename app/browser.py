import asyncio
import os
import re

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from .resolver import UnsafeURL, validate_public_url


BROWSER_SEMAPHORE = asyncio.Semaphore(1)


CHALLENGE_MARKERS = (
    "verify you are human",
    "checking your browser",
    "captcha",
    "turnstile",
    "hcaptcha",
    "recaptcha",
)


async def resolve_with_browser(url: str, timeout: float = 12.0) -> tuple[str, int]:
    # Keep one Chromium process active at a time on the small free instance.
    async with BROWSER_SEMAPHORE:
        return await _resolve_with_browser(url, timeout)


async def _resolve_with_browser(url: str, timeout: float = 12.0) -> tuple[str, int]:
    """Resolve ordinary client-side navigation in an isolated Chromium page.

    This deliberately does not solve CAPTCHAs, click ads, defeat challenges,
    or interact with protected gateway workflows.
    """
    timeout_ms = int(max(5.0, min(timeout, 15.0)) * 1000)
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
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            try:
                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
                    ),
                    locale="en-US",
                    java_script_enabled=True,
                    accept_downloads=False,
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
                await page.wait_for_timeout(1200)
                # Allow ordinary, visible countdown redirects to finish naturally.
                # No buttons are clicked and no challenge is solved.
                initial_url = page.url
                initial_text = (await page.locator("body").inner_text(timeout=1500)).lower()[:5000]
                if re.search(r"\b\d{1,2}\s*(seconds?|secs?)\b|please wait|redirecting in|continue in|link is ready", initial_text):
                    wait_seconds = max(0, min(int(os.getenv("BROWSER_WAIT_SECONDS", "15")), 15))
                    for _ in range(wait_seconds):
                        await page.wait_for_timeout(1000)
                        if page.url != initial_url:
                            break
                final_url = page.url
                title = (await page.title()).lower()
                body_text = (await page.locator("body").inner_text(timeout=1500)).lower()[:5000]
                if any(marker in title or marker in body_text for marker in CHALLENGE_MARKERS):
                    raise UnsafeURL(
                        "This destination requires a human verification or anti-bot check."
                    )
                await validate_public_url(final_url)
                return final_url, redirects
            finally:
                await browser.close()
    except UnsafeURL:
        raise
    except (PlaywrightError, OSError, RuntimeError) as exc:
        raise UnsafeURL(
            "Browser-based resolution was unavailable; this page may require a browser session."
        ) from exc
