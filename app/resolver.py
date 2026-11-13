import asyncio
import base64
import binascii
import html
import ipaddress
import json
import re
import socket
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit, urlunsplit

import httpx

REDIRECT_CODES = {301, 302, 303, 307, 308}
MAX_URL_LENGTH = 2048
MAX_BODY_BYTES = 256 * 1024
TARGET_KEYS = {"url", "target", "dest", "destination", "redirect", "redirect_url", "long_url", "link", "u"}


class UnsafeURL(ValueError):
    """Raised when a URL is invalid, unsafe, or cannot be resolved."""


def _normalise_url(url: str) -> str:
    url = url.strip()
    if not url or len(url) > MAX_URL_LENGTH:
        raise UnsafeURL("URL is empty or too long.")
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in {"http", "https"}:
            raise UnsafeURL("Only HTTP and HTTPS links are supported.")
        if not parts.hostname or parts.username is not None or parts.password is not None:
            raise UnsafeURL("Invalid URL or embedded credentials are not supported.")
        port = parts.port
        if port is not None and port not in {80, 443}:
            raise UnsafeURL("Non-standard ports are not supported.")
        host = parts.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        if not host or host == "localhost" or host.endswith(".localhost"):
            raise UnsafeURL("Local network destinations are not supported.")
        netloc = f"[{host}]" if ":" in host else host
        if port is not None:
            netloc += f":{port}"
        return urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, ""))
    except UnsafeURL:
        raise
    except (ValueError, UnicodeError) as exc:
        raise UnsafeURL("Malformed URL.") from exc


async def validate_public_url(url: str) -> None:
    normalised = _normalise_url(url)
    parts = urlsplit(normalised)
    host = parts.hostname
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise UnsafeURL("Private or reserved IP destinations are blocked.")
        return
    try:
        records = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(
                host, parts.port or (443 if parts.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            ),
            timeout=4.0,
        )
    except (OSError, asyncio.TimeoutError) as exc:
        raise UnsafeURL("The destination host could not be resolved.") from exc
    if not records:
        raise UnsafeURL("The destination host did not resolve.")
    for record in records:
        try:
            address = ipaddress.ip_address(record[4][0])
        except ValueError as exc:
            raise UnsafeURL("The destination returned an invalid IP address.") from exc
        if not address.is_global:
            raise UnsafeURL("The destination resolves to a private or reserved network.")


def _http_target(value: str, base_url: str) -> str | None:
    value = html.unescape(str(value)).strip().strip("\"'")
    value = value.replace("\\/", "/")
    if not value or value.startswith(("javascript:", "data:", "#")):
        return None
    try:
        return _normalise_url(urljoin(base_url, value))
    except UnsafeURL:
        return None


def _targets_from_json(value, base_url: str, depth: int = 0) -> list[str]:
    if depth > 6:
        return []
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in TARGET_KEYS and isinstance(item, str):
                target = _http_target(item, base_url)
                if target:
                    found.append(target)
            elif isinstance(item, (dict, list)):
                found.extend(_targets_from_json(item, base_url, depth + 1))
    elif isinstance(value, list):
        for item in value[:100]:
            if isinstance(item, (dict, list)):
                found.extend(_targets_from_json(item, base_url, depth + 1))
    return found



def _extract_base64_target(text: str, base_url: str) -> str | None:
    """Decode obvious base64/URL-safe-base64 destination strings."""
    for match in re.finditer(r"(?<![A-Za-z0-9_])[A-Za-z0-9_+/-]{20,}={0,2}", text[:MAX_BODY_BYTES]):
        token = match.group(0)
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            decoded = raw.decode("utf-8", errors="strict").strip().strip("\\\"'")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        url_match = re.search(r"""https?://[^\s"'<>]+""", decoded, re.IGNORECASE)
        candidate = url_match.group(0).rstrip(".,;:!?)") if url_match else decoded
        target = _http_target(candidate, base_url)
        if target and target != base_url:
            return target
    return None


def _extract_embedded_target(body: bytes, content_type: str, base_url: str) -> str | None:
    if not body or len(body) > MAX_BODY_BYTES:
        return None
    text = body.decode("utf-8", errors="replace")
    lowered_type = content_type.lower()
    decoded_target = _extract_base64_target(text, base_url)
    if decoded_target:
        return decoded_target
    if "json" in lowered_type or text.lstrip().startswith(("{", "[")):
        try:
            targets = _targets_from_json(json.loads(text), base_url)
            if targets:
                return targets[0]
        except (json.JSONDecodeError, RecursionError):
            pass

    meta = re.search(
        r"<meta[^>]+http-equiv\s*=\s*['\"]?refresh['\"]?[^>]+content\s*=\s*['\"][^'\"]*?url\s*=\s*([^;'\"]+)",
        text, re.IGNORECASE,
    )
    if meta:
        target = _http_target(meta.group(1), base_url)
        if target:
            return target

    patterns = (
        r"(?:window\.)?location\.href\s*=\s*['\"]([^'\"]+)['\"]",
        r"(?:window\.)?location\.assign\(\s*['\"]([^'\"]+)['\"]\s*\)",
        r"(?:window\.)?location\.replace\(\s*['\"]([^'\"]+)['\"]\s*\)",
        r"window\.open\(\s*['\"]([^'\"]+)['\"]",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            target = _http_target(match.group(1), base_url)
            if target:
                return target

    for key, value in parse_qsl(urlsplit(base_url).query, keep_blank_values=False):
        if key.lower() in TARGET_KEYS:
            target = _http_target(unquote(value), base_url)
            if target and target != base_url:
                return target
    return None


async def _read_limited_body(response: httpx.Response) -> bytes:
    chunks = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            return b""
        chunks.append(chunk)
    return b"".join(chunks)


async def resolve_url(
    url: str,
    max_redirects: int = 8,
    timeout: float = 10.0,
) -> tuple[str, int]:
    """Follow HTTP and common static HTML/JSON/JavaScript redirects safely."""
    current = _normalise_url(url)
    max_redirects = max(0, min(int(max_redirects), 10))
    timeout = max(3.0, min(float(timeout), 20.0))
    visited: set[str] = set()
    last_unique = current
    limits = httpx.Limits(max_connections=8, max_keepalive_connections=4)
    request_timeout = httpx.Timeout(timeout, connect=min(timeout, 5.0))

    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=request_timeout,
        limits=limits,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; LinkResolverBot/1.0)",
            "Accept": "text/html,application/json,text/plain,*/*",
        },
        trust_env=False,
    ) as client:
        for hop in range(max_redirects + 1):
            current = _normalise_url(current)
            if current in visited:
                # Never report an intermediate tracking page as a successful
                # destination when the shortener is cycling between URLs.
                raise UnsafeURL(
                    "The shortener is cycling between redirect pages and did not reveal a final destination."
                )
            visited.add(current)
            last_unique = current
            await validate_public_url(current)
            try:
                async with client.stream("GET", current) as response:
                    if response.status_code in REDIRECT_CODES:
                        location = response.headers.get("location")
                        if not location:
                            return current, hop
                        if hop >= max_redirects:
                            raise UnsafeURL("Too many redirects.")
                        current = urljoin(current, location)
                        continue
                    content_type = response.headers.get("content-type", "")
                    if not any(t in content_type.lower() for t in ("text/html", "json", "text/plain", "javascript")):
                        return current, hop
                    body = await _read_limited_body(response)
                    embedded = _extract_embedded_target(body, content_type, current)
            except httpx.TimeoutException as exc:
                raise UnsafeURL("The destination timed out.") from exc
            except httpx.HTTPError as exc:
                raise UnsafeURL("The destination could not be reached.") from exc
            if not embedded:
                if "html" in content_type.lower() and re.search(r"<script\\b", body.decode("utf-8", errors="replace"), re.IGNORECASE):
                    # Browser fallback handles ordinary JavaScript-driven
                    # navigation. It does not interact with ads or challenges.
                    from .browser import resolve_with_browser

                    browser_url, browser_hops = await resolve_with_browser(current, timeout)
                    if browser_url != current:
                        if browser_url in visited:
                            raise UnsafeURL(
                                "The browser encountered a redirect loop before exposing a final destination."
                            )
                        current = browser_url
                        continue
                    return current, hop + browser_hops
                return current, hop
            # An embedded redirect back to a visited page is also a loop.
            # Do not return the current interstitial page as if it were final.
            if embedded in visited:
                raise UnsafeURL(
                    "The shortener returned a redirect loop before exposing the final destination."
                )
            if hop >= max_redirects:
                raise UnsafeURL("Too many redirects.")
            current = embedded
    return current, max_redirects
