import asyncio
import os
import re
import time
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from .resolver import GATEWAY_HOSTS, UnsafeURL, _normalise_url, validate_public_url

BROWSER_WAIT_SECONDS = max(5.0, float(os.getenv("BROWSER_WAIT_SECONDS", "20")))

CLICK_WORDS_RE = re.compile(
    r"(continue|get\s*link|next|proceed|open|verify|skip|unlock|visit|go\s*to\s*link)",
    re.IGNORECASE,
)

BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}

AD_OR_SOCIAL_HOSTS = {
    "google-analytics.com",
    "googletagmanager.com",
    "googlesyndication.com",
    "doubleclick.net",
    "facebook.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "t.me",
    "telegram.me",
    "youtube.com",
    "youtu.be",
    "cloudflare.com",
    "challenges.cloudflare.com",
    "hcaptcha.com",
    "recaptcha.net",
}


def _host_matches(host: str, domains: set[str]) -> bool:
    return host in domains or any(host.endswith("." + item) for item in domains)


def _is_gateway_host(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
        return _host_matches(host, GATEWAY_HOSTS)
    except ValueError:
        return True


def _is_ignored_candidate_host(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
        return _host_matches(host, GATEWAY_HOSTS) or _host_matches(host, AD_OR_SOCIAL_HOSTS)
    except ValueError:
        return True


async def _find_non_gateway_candidate(page) -> str | None:
    try:
        candidates = await page.eval_on_selector_all(
            "a[href], [data-url], [data-href], [data-link], [data-target], button[onclick], form[action]",
            """els => els.map(el => ({
                tag: el.tagName.toLowerCase(),
                href: el.getAttribute('href') || '',
                action: el.getAttribute('action') || '',
                onclick: el.getAttribute('onclick') || '',
                dataUrl: el.getAttribute('data-url') || el.getAttribute('data-href') || el.getAttribute('data-link') || el.getAttribute('data-target') || '',
                text: (el.innerText || '').trim(),
                rel: (el.getAttribute('rel') || '').toLowerCase()
            }))""",
        )
    except PlaywrightError:
        return None

    for item in candidates:
        raw_values = [item.get("dataUrl", ""), item.get("href", ""), item.get("action", "")]
        onclick = item.get("onclick", "")
        if onclick:
            match = re.search(r"""https?://[^'"\s)]+""", onclick)
            if match:
                raw_values.append(match.group(0))

        for candidate in raw_values:
            candidate = (candidate or "").strip()
            if not candidate or not candidate.startswith(("http://", "https://")):
                continue
            try:
                normalised = _normalise_url(candidate)
            except UnsafeURL:
                continue
            if _is_ignored_candidate_host(normalised):
                continue
            # Prefer links with meaningful CTA text or explicit data attributes
            if item.get("dataUrl") or item.get("tag") == "form" or CLICK_WORDS_RE.search(item.get("text", "")):
                return normalised
    return None


async def resolve_with_browser(url: str, timeout: float = 12.0) -> tuple[str, int]:
    """Use headless Chromium for ordinary JS redirects, avoiding infinite wait loops."""
    wait_budget = max(timeout, BROWSER_WAIT_SECONDS)
    start_url = _normalise_url(url)
    await validate_public_url(start_url)

    validated_hosts: set[str] = set()
    blocked_hosts: set[str] = set()
    playwright = None
    browser = None
    context = None

    try:
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-extensions",
            ],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/133.0.0.0 Safari/537.36"
            ),
            ignore_https_errors=False,
        )
        page = await context.new_page()
        opened_pages: list = [page]
        context.on("page", lambda new_page: opened_pages.append(new_page))

        async def _safe_abort(route) -> None:
            try:
                await route.abort()
            except PlaywrightError:
                pass

        async def _safe_continue(route) -> None:
            try:
                await route.continue_()
            except PlaywrightError:
                pass

        async def handle_route(route) -> None:
            request = route.request
            if request.resource_type in BLOCKED_RESOURCE_TYPES:
                await _safe_abort(route)
                return

            request_url = request.url
            if request_url.startswith(("about:", "data:", "blob:")):
                await _safe_continue(route)
                return

            try:
                host = (urlsplit(request_url).hostname or "").lower().rstrip(".")
            except ValueError:
                await _safe_abort(route)
                return

            if host in blocked_hosts:
                await _safe_abort(route)
                return

            if host not in validated_hosts:
                try:
                    await validate_public_url(request_url)
                    validated_hosts.add(host)
                except UnsafeURL:
                    blocked_hosts.add(host)
                    await _safe_abort(route)
                    return

            await _safe_continue(route)

        await page.route("**/*", handle_route)

        try:
            await page.goto(
                start_url,
                wait_until="domcontentloaded",
                timeout=int(min(wait_budget, 15.0) * 1000),
            )
        except PlaywrightError:
            pass

        deadline = time.monotonic() + wait_budget
        clicked_texts: set[str] = set()

        while time.monotonic() < deadline:
            # Check all tabs (in case clicking opened a destination in a new tab)
            for active_page in list(reversed(opened_pages)):
                try:
                    current_url = _normalise_url(active_page.url)
                except (UnsafeURL, PlaywrightError):
                    continue
                if current_url and current_url != start_url and not _is_gateway_host(current_url):
                    await validate_public_url(current_url)
                    return current_url, 1

            candidate = await _find_non_gateway_candidate(page)
            if candidate:
                await validate_public_url(candidate)
                return candidate, 1

            try:
                clickable = page.locator(
                    "button, a, [role='button'], input[type='submit'], input[type='button']"
                )
                count = min(await clickable.count(), 12)
                for idx in range(count):
                    el = clickable.nth(idx)
                    try:
                        if not await el.is_visible() or not await el.is_enabled():
                            continue
                        text = (await el.inner_text(timeout=400)).strip()
                    except PlaywrightError:
                        continue
                    if text and text not in clicked_texts and CLICK_WORDS_RE.search(text):
                        clicked_texts.add(text)
                        try:
                            await el.click(timeout=1500)
                        except PlaywrightError:
                            pass
                        break
            except PlaywrightError:
                pass

            await asyncio.sleep(0.75)

        final_url = _normalise_url(page.url)
        if not final_url or _is_gateway_host(final_url):
            raise UnsafeURL("The gateway page did not expose a final destination.")
        await validate_public_url(final_url)
        return final_url, 1
    finally:
        if context is not None:
            try:
                await context.close()
            except PlaywrightError:
                pass
        if browser is not None:
            try:
                await browser.close()
            except PlaywrightError:
                pass
        if playwright is not None:
            try:
                await playwright.stop()
            except PlaywrightError:
                pass
