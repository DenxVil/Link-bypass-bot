import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import httpx

REDIRECT_CODES = {301, 302, 303, 307, 308}


class UnsafeURL(ValueError):
    pass


async def validate_public_url(url: str) -> None:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise UnsafeURL("Only HTTP and HTTPS links are supported.")
        if not parsed.hostname or parsed.username or parsed.password:
            raise UnsafeURL("Invalid URL or embedded credentials are not supported.")
        if parsed.port and parsed.port not in {80, 443}:
            raise UnsafeURL("Non-standard ports are not supported.")
        host = parsed.hostname.rstrip(".").lower()
        if host == "localhost" or host.endswith(".localhost"):
            raise UnsafeURL("Localhost destinations are blocked.")
        try:
            ip = ipaddress.ip_address(host)
            if not ip.is_global:
                raise UnsafeURL("Private or reserved IP destinations are blocked.")
            return
        except ValueError:
            pass
        records = await asyncio.get_running_loop().getaddrinfo(
            host, parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM
        )
        if not records:
            raise UnsafeURL("The destination host did not resolve.")
        for record in records:
            address = ipaddress.ip_address(record[4][0])
            if not address.is_global:
                raise UnsafeURL("The destination resolves to a private or reserved network.")
    except UnsafeURL:
        raise
    except (ValueError, OSError) as exc:
        raise UnsafeURL("Invalid or unreachable destination host.") from exc


async def resolve_url(url: str, max_redirects: int = 10, timeout: float = 12.0) -> tuple[str, int]:
    current = url.strip()
    if len(current) > 2048:
        raise UnsafeURL("URL is too long.")
    visited: set[str] = set()
    limits = httpx.Limits(max_connections=10, max_keepalive_connections=5)
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(timeout),
        limits=limits,
        headers={"User-Agent": "Mozilla/5.0 (compatible; LinkResolverBot/1.0)", "Accept": "*/*"},
        trust_env=False,
    ) as client:
        for hop in range(max_redirects + 1):
            await validate_public_url(current)
            if current in visited:
                raise UnsafeURL("Redirect loop detected.")
            visited.add(current)
            try:
                response = await client.get(
                    current,
                    headers={"Range": "bytes=0-0"},
                )
            except httpx.TooManyRedirects as exc:
                raise UnsafeURL("Redirect limit exceeded.") from exc
            except httpx.TimeoutException as exc:
                raise UnsafeURL("The destination timed out.") from exc
            except httpx.HTTPError as exc:
                raise UnsafeURL("The destination could not be reached.") from exc

            if response.status_code not in REDIRECT_CODES:
                return current, hop
            location = response.headers.get("location")
            if not location:
                return current, hop
            if hop >= max_redirects:
                raise UnsafeURL("Too many redirects.")
            current = urljoin(current, location)
    return current, max_redirects
