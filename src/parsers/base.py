"""
NicheParser_China — Playwright helper
Общая обёртка над Playwright с stealth-режимом, ротацией UA и случайными задержками.
"""

import asyncio
import logging
import random
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from core.config import (
    USER_AGENTS,
    REQUEST_DELAY_MIN,
    REQUEST_DELAY_MAX,
    ALIBABA_PAGE_TIMEOUT_MS,
)

logger = logging.getLogger(__name__)


class PlaywrightNotInstalled(RuntimeError):
    """Бросаем, если playwright не установлен или браузер не скачан."""


async def random_delay(min_s: Optional[float] = None, max_s: Optional[float] = None) -> None:
    delay = random.uniform(
        min_s if min_s is not None else REQUEST_DELAY_MIN,
        max_s if max_s is not None else REQUEST_DELAY_MAX,
    )
    await asyncio.sleep(delay)


def pick_user_agent() -> str:
    return random.choice(USER_AGENTS)


@asynccontextmanager
async def stealth_browser(headless: bool = True) -> AsyncIterator:
    """
    Запустить Chromium через Playwright с антидетект-патчем.
    Использует async_playwright; playwright_stealth по возможности.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        raise PlaywrightNotInstalled(
            "Playwright не установлен. Выполни: pip install playwright && playwright install chromium"
        ) from e

    stealth_apply = None
    try:
        from playwright_stealth import stealth_async  # type: ignore
        stealth_apply = stealth_async
    except ImportError:
        logger.warning("playwright_stealth не найден — работаем без stealth-патча")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = await browser.new_context(
            user_agent=pick_user_agent(),
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="Asia/Shanghai",
        )
        page = await context.new_page()
        page.set_default_timeout(ALIBABA_PAGE_TIMEOUT_MS)

        if stealth_apply is not None:
            try:
                await stealth_apply(page)
            except Exception as e:
                logger.warning(f"stealth_async failed: {e}")

        try:
            yield page
        finally:
            await context.close()
            await browser.close()
