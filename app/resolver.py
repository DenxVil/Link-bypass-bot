import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

REDIRECT_CODES = {301, 302, 303, 307, 308}
MAX_URL_LENGTH = 2048


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
        port = parts.port  # raises ValueError for malformed ports
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
                host,
                parts.port or (443 if parts.scheme == "https" else 80),
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


async def resolve_url(
    url: str,
    max_redirects: int = 8,
    timeout: float = 10.0,
) -> tuple[str, int]:
    """Follow public HTTP redirects without downloading response bodies.

    DNS is checked before each hop and automatic redirects are disabled.
    The HTTP client's own DNS lookup is separate; use network-level egress
    restrictions for stronger protection against DNS-rebinding attacks.
    """
    current = _normalise_url(url)
    max_redirects = max(0, min(int(max_redirects), 10))
    timeout = max(3.0, min(float(timeout), 20.0))
    visited: set[str] = set()
    limits = httpx.Limits(max_connections=8, max_keepalive_connections=4)
    request_timeout = httpx.Timeout(timeout, connect=min(timeout, 5.0))

    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=request_timeout,
        limits=limits,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; LinkResolverBot/1.0)",
            "Accept": "*/*",
        },
        trust_env=False,
    ) as client:
        for hop in range(max_redirects + 1):
            current = _normalise_url(current)
            if current in visited:
                raise UnsafeURL("Redirect loop detected.")
            visited.add(current)
            await validate_public_url(current)

            try:
                # Stream and close immediately: redirect resolution needs
                # response headers, not the destination page's content.
                async with client.stream(
                    "GET", current, headers={"Range": "bytes=0-0"}
                ) as response:
                    if response.status_code not in REDIRECT_CODES:
                        return current, hop
                    location = response.headers.get("location")
            except httpx.TimeoutException as exc:
                raise UnsafeURL("The destination timed out.") from exc
            except httpx.HTTPError as exc:
                raise UnsafeURL("The destination could not be reached.") from exc

            if not location:
                return current, hop
            if hop >= max_redirects:
                raise UnsafeURL("Too many redirects.")
            current = urljoin(current, location)

    return current, max_redirects
