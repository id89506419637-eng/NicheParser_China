"""
NicheParser_China — Alibaba parser (Playwright + stealth)
Поиск товаров на alibaba.com по ключевым словам.
"""

import asyncio
import logging
import re
from typing import List, Tuple
from urllib.parse import quote

from core.config import ALIBABA_MAX_PRODUCTS_PER_NICHE
from core.models import AlibabaProduct
from src.parsers.base import stealth_browser, random_delay, PlaywrightNotInstalled

logger = logging.getLogger(__name__)


BASE_URL = "https://www.alibaba.com"
SEARCH_URL = "https://www.alibaba.com/trade/search?SearchText={query}"


def search_alibaba(query: str, limit: int = ALIBABA_MAX_PRODUCTS_PER_NICHE) -> Tuple[List[AlibabaProduct], int]:
    """
    Синхронная обёртка. Возвращает (список товаров, всего результатов).
    Всего результатов = метрика «конкуренция».
    """
    try:
        return asyncio.run(_search_async(query, limit))
    except PlaywrightNotInstalled as e:
        logger.error(str(e))
        return [], 0
    except Exception as e:
        logger.error(f"Alibaba search failed for '{query}': {e}", exc_info=True)
        return [], 0


async def _search_async(query: str, limit: int) -> Tuple[List[AlibabaProduct], int]:
    url = SEARCH_URL.format(query=quote(query))
    logger.info(f"Alibaba: открываю {url}")

    async with stealth_browser(headless=True) as page:
        await page.goto(url, wait_until="domcontentloaded")

        # Детект блока / капчи / пустой страницы
        html = await page.content()
        if _is_blocked(html):
            logger.warning(f"Alibaba: обнаружен блок/капча для '{query}' — пропускаю")
            return [], 0

        await random_delay(1.5, 3.0)

        # Попытка подождать контейнер результатов
        try:
            await page.wait_for_selector(
                ".organic-list, [data-content='productList'], .list-no-v2-main",
                timeout=15_000,
            )
        except Exception:
            logger.warning(f"Alibaba: контейнер результатов не появился для '{query}'")

        competition = await _extract_total_results(page)
        products = await _extract_products(page, limit)

        logger.info(
            f"Alibaba: '{query}' — найдено {len(products)} товаров, конкуренция ≈ {competition}"
        )
        return products, competition


def _is_blocked(html: str) -> bool:
    markers = [
        "punish?x5secdata",
        "Please verify you are a human",
        "captcha-verify",
        "slider-verify",
        "Access Denied",
    ]
    lower = html.lower()
    return any(m.lower() in lower for m in markers) or len(html) < 2_000


async def _extract_total_results(page) -> int:
    """Выдёргиваем общее число результатов — ось конкуренции."""
    selectors = [
        "[class*='search-card-count']",
        ".seb-search-count",
        "[data-spm-anchor-id*='search-count']",
        "span:has-text('results')",
    ]
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                text = (await el.inner_text()).strip()
                nums = re.findall(r"[\d,]+", text.replace(" ", ""))
                if nums:
                    return int(nums[0].replace(",", ""))
        except Exception:
            continue
    return 0


async def _extract_products(page, limit: int) -> List[AlibabaProduct]:
    """Собираем карточки товаров."""
    # Универсальные селекторы — Alibaba часто меняет разметку
    card_selectors = [
        "[data-content='productItem']",
        ".list-no-v2-main .organic-offer-wrapper",
        ".organic-gallery-offer-wrapper",
        "div[class*='offer-card']",
    ]

    cards = []
    for sel in card_selectors:
        cards = await page.query_selector_all(sel)
        if cards:
            break

    if not cards:
        logger.warning("Alibaba: карточки не найдены ни одним селектором")
        return []

    products: List[AlibabaProduct] = []
    for card in cards[:limit]:
        try:
            product = await _parse_card(card)
            if product and (product.price_usd_min > 0 or product.product_url):
                products.append(product)
        except Exception as e:
            logger.debug(f"Alibaba: пропуск карточки: {e}")
            continue

    return products


async def _parse_card(card) -> AlibabaProduct:
    product = AlibabaProduct()

    title_el = await card.query_selector("h2, [class*='title'] a, a[class*='title']")
    if title_el:
        product.title_en = (await title_el.inner_text()).strip()

    price_el = await card.query_selector("[class*='price'], div[class*='priceWrap']")
    if price_el:
        price_text = (await price_el.inner_text()).strip()
        lo, hi = _parse_price_range(price_text)
        product.price_usd_min = lo
        product.price_usd_max = hi

    moq_el = await card.query_selector("[class*='moq'], [class*='minOrder']")
    if moq_el:
        moq_text = (await moq_el.inner_text()).strip()
        nums = re.findall(r"(\d[\d,]*)", moq_text)
        if nums:
            product.moq = int(nums[0].replace(",", ""))

    rating_el = await card.query_selector("[class*='star'], [class*='rating']")
    if rating_el:
        r = re.findall(r"(\d+\.?\d*)", (await rating_el.inner_text()).strip())
        if r:
            try:
                product.supplier_rating = float(r[0])
            except ValueError:
                pass

    deals_el = await card.query_selector("[class*='deals'], [class*='orders']")
    if deals_el:
        d = re.findall(r"(\d[\d,]*)", (await deals_el.inner_text()).strip())
        if d:
            product.deals_count = int(d[0].replace(",", ""))

    link_el = await card.query_selector("a[href]")
    if link_el:
        href = await link_el.get_attribute("href") or ""
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = BASE_URL + href
        product.product_url = href

    # Сертификаты — бейджи CE/ISO/RoHS
    for cert in ("CE", "ISO", "RoHS", "FDA", "FCC"):
        has = await card.query_selector(f"text=/\\b{cert}\\b/i")
        if has:
            product.certificates.append(cert)

    return product


def _parse_price_range(text: str) -> Tuple[float, float]:
    """$12.50 - $45.00 → (12.5, 45.0). $12.50 → (12.5, 12.5)."""
    nums = re.findall(r"(\d+(?:\.\d+)?)", text.replace(",", ""))
    vals: List[float] = []
    for n in nums:
        try:
            v = float(n)
            if 0.01 < v < 10_000_000:
                vals.append(v)
        except ValueError:
            continue
    if not vals:
        return 0.0, 0.0
    return min(vals), max(vals)


def get_search_url(query: str) -> str:
    return SEARCH_URL.format(query=quote(query))
