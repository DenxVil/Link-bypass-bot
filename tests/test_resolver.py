import pytest
from app.resolver import (
    UnsafeURL,
    _extract_embedded_target,
    _normalise_url,
    validate_public_url,
)


@pytest.mark.asyncio
async def test_rejects_localhost():
    with pytest.raises(UnsafeURL):
        await validate_public_url("http://localhost/admin")


@pytest.mark.asyncio
async def test_rejects_unsupported_scheme():
    with pytest.raises(UnsafeURL):
        await validate_public_url("file:///etc/passwd")


@pytest.mark.asyncio
async def test_rejects_private_ip():
    with pytest.raises(UnsafeURL):
        await validate_public_url("http://127.0.0.1/")


def test_extracts_meta_refresh():
    body = b'<html><meta http-equiv="refresh" content="0;url=https://example.org/final"></html>'
    assert _extract_embedded_target(body, "text/html", "https://short.test/abc") == "https://example.org/final"


def test_extracts_javascript_redirect():
    body = b'<script>window.location.href = "https://example.org/final";</script>'
    assert _extract_embedded_target(body, "text/html", "https://short.test/abc") == "https://example.org/final"


def test_extracts_json_destination():
    body = b'{"status":"ok","data":{"destination":"https://example.org/final"}}'
    assert _extract_embedded_target(body, "application/json", "https://short.test/abc") == "https://example.org/final"


def test_extracts_wrapped_query_destination():
    url = "https://resolver.test/go?url=https%3A%2F%2Fexample.org%2Ffinal"
    assert _extract_embedded_target(b"", "text/html", url) == "https://example.org/final"


def test_ignores_oversized_body():
    assert _extract_embedded_target(b"x" * (256 * 1024 + 1), "text/html", "https://short.test/") is None


def test_normalise_rejects_non_http_target():
    with pytest.raises(UnsafeURL):
        _normalise_url("javascript:alert(1)")


def test_extracts_base64_destination():
    import base64

    destination = "https://example.org/final/path"
    encoded = base64.b64encode(destination.encode()).decode()
    body = f'<script>const target = "{encoded}";</script>'.encode()
    assert _extract_embedded_target(body, "text/html", "https://short.test/abc") == destination
