"""
NicheParser_China — Alibaba parser (Playwright + stealth)
Поиск товаров на alibaba.com по ключевым словам.
"""

import asyncio
import hashlib
import logging
import random
import re
from typing import List, Tuple
from urllib.parse import quote

from core.config import ALIBABA_MAX_PRODUCTS_PER_NICHE, USE_MOCK_ALIBABA
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
    if USE_MOCK_ALIBABA:
        return _mock_products(query, limit)

    try:
        return asyncio.run(_search_async(query, limit))
    except PlaywrightNotInstalled as e:
        logger.error(str(e))
        return [], 0
    except Exception as e:
        logger.error(f"Alibaba search failed for '{query}': {e}", exc_info=True)
        return [], 0


# ── Якори цен по типу товара (для mock) ───────────────────────────────
# (keywords, price_usd_min, price_usd_max, weight_kg_range, moq_options, competition_range)
_ALIBABA_ANCHORS = [
    # Тяжёлое оборудование / станки
    ({"станок", "станки", "чпу", "cnc", "lathe", "milling", "фрезерный",
      "токарный", "лазерный", "плазменный", "сварочный", "компрессор",
      "генератор", "трансформатор", "экскаватор", "погрузчик", "кран",
      "machine", "cutting"},
     500, 15_000, (50, 2500), [1, 1, 2, 5], (800, 12_000)),

    # Средне-дорогое оборудование
    ({"насос", "pump", "двигатель", "motor", "конвейер", "conveyor",
      "дробилка", "crusher", "бетономешалка", "mixer", "пресс", "press",
      "вентиляция", "кондиционер", "котёл", "boiler", "печь", "furnace"},
     80, 2_000, (15, 300), [1, 2, 5, 10], (1_000, 20_000)),

    # Электроника / приборы / автоматизация
    ({"датчик", "sensor", "контроллер", "controller", "plc", "частотник",
      "инвертер", "inverter", "панель", "panel", "дисплей", "display",
      "камера", "camera", "видеонаблюдение", "сервопривод", "servo"},
     15, 300, (0.3, 5), [5, 10, 20, 50, 100], (2_000, 45_000)),

    # Расходники / мелкие комплектующие
    ({"гайка", "болт", "винт", "шайба", "подшипник", "bearing", "фильтр",
      "filter", "ремень", "belt", "сальник", "seal", "прокладка", "gasket",
      "кабель", "cable", "провод", "wire", "шланг", "hose", "клей", "glue",
      "герметик", "sealant", "абразив", "abrasive", "лента", "tape",
      "bolt", "nut", "washer", "screw"},
     0.5, 20, (0.01, 2), [50, 100, 200, 500, 1000], (5_000, 45_000)),

    # Инструмент
    ({"инструмент", "tool", "сверло", "drill", "фреза", "cutter", "резец",
      "ключ", "wrench", "отвёртка", "screwdriver", "пила", "saw",
      "шлифовальный", "grinder", "полировальный"},
     5, 150, (0.5, 15), [5, 10, 20, 50], (3_000, 35_000)),

    # Медицина / лаборатория
    ({"медицинский", "medical", "стерилизатор", "sterilizer", "центрифуга",
      "centrifuge", "микроскоп", "microscope", "анализатор", "analyzer",
      "рентген", "xray", "узи", "ultrasound", "эндоскоп", "endoscope"},
     200, 5_000, (10, 200), [1, 1, 2, 5], (500, 8_000)),

    # Солнечная энергетика
    ({"солнечная", "solar", "инвертор", "аккумулятор", "battery",
      "литий", "lithium"},
     30, 500, (5, 30), [5, 10, 20, 50], (2_000, 25_000)),
]


def _estimate_alibaba_params(query: str, rng: random.Random) -> dict:
    """Определяет реалистичные параметры mock-оффера по типу товара."""
    words = set(query.lower().split())

    for anchors, p_min, p_max, w_range, moqs, comp_range in _ALIBABA_ANCHORS:
        if words & anchors:
            return {
                "price_min": p_min,
                "price_max": p_max,
                "weight_range": w_range,
                "moq_options": moqs,
                "competition": rng.randint(*comp_range),
            }

    # Эвристика по длине запроса (аналог avito.py)
    word_count = len(query.split())
    if word_count >= 4 or len(query) > 30:
        return {"price_min": 200, "price_max": 5_000, "weight_range": (20, 500),
                "moq_options": [1, 2, 5], "competition": rng.randint(500, 8_000)}
    elif word_count >= 3 or len(query) > 20:
        return {"price_min": 30, "price_max": 800, "weight_range": (2, 50),
                "moq_options": [5, 10, 20, 50], "competition": rng.randint(1_000, 20_000)}
    elif word_count <= 1 and len(query) < 12:
        return {"price_min": 1, "price_max": 50, "weight_range": (0.1, 5),
                "moq_options": [20, 50, 100, 200], "competition": rng.randint(5_000, 45_000)}
    else:
        return {"price_min": 10, "price_max": 300, "weight_range": (1, 25),
                "moq_options": [10, 20, 50, 100], "competition": rng.randint(2_000, 30_000)}


def _mock_products(query: str, limit: int) -> Tuple[List[AlibabaProduct], int]:
    """
    Детерминированный mock: для одного и того же query даёт одинаковые товары,
    чтобы история и динамика выглядели стабильно. Цены/MOQ/вес определяются
    типом товара через _estimate_alibaba_params, чтобы ВЭД-калькулятор давал
    адекватные вердикты (а не $15 за токарный станок).
    """
    seed = int(hashlib.md5(query.encode("utf-8")).hexdigest()[:8], 16)
    rng = random.Random(seed)

    params = _estimate_alibaba_params(query, rng)
    p_min = params["price_min"]
    p_max = params["price_max"]
    w_lo, w_hi = params["weight_range"]
    competition = params["competition"]

    count = min(limit, rng.randint(4, 8))
    products: List[AlibabaProduct] = []
    for i in range(count):
        base = rng.uniform(p_min, p_max)
        price_min = round(base * rng.uniform(0.85, 1.0), 2)
        price_max = round(base * rng.uniform(1.1, 1.8), 2)
        moq = rng.choice(params["moq_options"])
        weight = round(rng.uniform(w_lo, w_hi), 2)
        # Габариты из веса (грубая эвристика: плотность ~500 кг/м³)
        volume_m3 = weight / 500
        length = round((volume_m3 ** (1 / 3)) * 100 * rng.uniform(0.8, 1.2), 1)

        certs = rng.sample(["CE", "ISO", "RoHS", "FDA", "FCC"], k=rng.randint(0, 3))

        products.append(AlibabaProduct(
            title_en=f"{query.title()} — Model {chr(65 + i)}{rng.randint(100, 999)}",
            price_usd_min=price_min,
            price_usd_max=price_max,
            moq=moq,
            supplier_rating=round(rng.uniform(4.0, 5.0), 1),
            deals_count=rng.randint(5, 500),
            certificates=certs,
            weight_kg=weight,
            length_cm=length,
            width_cm=round(length * rng.uniform(0.4, 0.9), 1),
            height_cm=round(length * rng.uniform(0.3, 0.7), 1),
            product_url=f"{BASE_URL}/product-detail/mock-{seed}-{i}.html",
            weight_source="mock",  # mock даёт правдоподобный вес по типу товара
        ))

    logger.info(
        f"Alibaba [MOCK]: '{query}' — {len(products)} товаров, "
        f"${p_min}–${p_max}/шт, конкуренция ≈ {competition}"
    )
    return products, competition


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

    # Вес — часто отсутствует на карточке в выдаче, обычно только на странице
    # товара («Package Weight», «N.W.», «Weight»). Пробуем несколько атрибутов;
    # если не удалось — оставляем weight_source="unknown", тогда Agent 5
    # применит консервативную эвристику по цене товара, а UI покажет
    # предупреждение «вес не определён — маржа может врать».
    weight_kg = _parse_weight_from_card(card)
    if weight_kg is not None and weight_kg > 0:
        product.weight_kg = weight_kg
        product.weight_source = "parsed"
    else:
        product.weight_kg = 0.0
        product.weight_source = "unknown"

    return product


async def _parse_weight_from_card(card) -> float | None:
    """
    Попытка выдернуть вес единицы товара с карточки выдачи Alibaba.
    Возвращает вес в кг или None если не смогли.
    Alibaba меняет вёрстку часто — селекторы могут протухнуть, тогда None.
    """
    # Кандидатные селекторы (порядок — от специфичного к общему)
    candidates = [
        "[class*='weight']",
        "[class*='Weight']",
        "[data-testid*='weight']",
        "[class*='specification']",
    ]
    for sel in candidates:
        try:
            el = await card.query_selector(sel)
            if not el:
                continue
            text = (await el.inner_text()).strip()
            kg = _parse_weight_text(text)
            if kg and kg > 0:
                return kg
        except Exception:
            continue
    return None


def _parse_weight_text(text: str) -> float | None:
    """
    Достаёт вес из строки. Поддерживает: kg, kgs, g, gram, lb, pound.
    «Weight: 45.5 kg» → 45.5
    «N.W.: 500g»     → 0.5
    «10 lb»          → 4.535
    """
    if not text:
        return None
    t = text.lower().replace(",", ".")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(kg|kgs|g|gram|lb|lbs|pound)", t)
    if not m:
        return None
    try:
        val = float(m.group(1))
    except ValueError:
        return None
    unit = m.group(2)
    if unit in ("kg", "kgs"):
        return val
    if unit in ("g", "gram"):
        return val / 1000
    if unit in ("lb", "lbs", "pound"):
        return val * 0.4535924
    return None


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
