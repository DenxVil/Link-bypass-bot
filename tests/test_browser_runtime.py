import pytest
from playwright.async_api import async_playwright


@pytest.mark.asyncio
async def test_chromium_can_launch():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            page = await browser.new_page()
            await page.set_content("<html><title>browser-ok</title></html>")
            assert await page.title() == "browser-ok"
        finally:
            await browser.close()
