import pytest
from app.resolver import UnsafeURL, validate_public_url


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
