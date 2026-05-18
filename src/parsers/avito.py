"""
NicheParser_China — Avito parser
Поиск цен в РФ для оценки реальной цены продажи. Используется Агентом 6,
чтобы заменить эвристику «закупка × курс × 2.5» в ВЭД-расчёте.

Сейчас работает в mock-режиме (USE_MOCK_AVITO=1). Реальный парсинг подключим,
когда появится доступ к Авито Pro API (переменная AVITO_API_KEY в .env) —
бесплатно Авито нормально не парсится из-за антибот-защиты.
"""

import hashlib
import logging
import random
from typing import List, Tuple

from core.config import ENABLE_AVITO, USE_MOCK_AVITO

logger = logging.getLogger(__name__)


BASE_URL = "https://www.avito.ru"
SEARCH_URL = "https://www.avito.ru/all?q={query}"


def is_enabled() -> bool:
    return bool(ENABLE_AVITO)


def search_avito(query: str, limit: int = 10) -> Tuple[List[dict], int]:
    """
    Возвращает (список объявлений, общее число объявлений в выдаче).
    Объявление — dict с ключами: title, price_rub, url, seller_type.
    """
    if USE_MOCK_AVITO:
        return _mock_listings(query, limit)

    if not ENABLE_AVITO:
        logger.info("Avito: модуль отключён (ENABLE_AVITO=0, USE_MOCK_AVITO=0)")
        return [], 0

    logger.warning(
        "Avito: ENABLE_AVITO=1, но реальная интеграция ещё не реализована. "
        "Установи AVITO_API_KEY в .env и допиши модуль, когда появится доступ к Pro API."
    )
    return [], 0


def _mock_listings(query: str, limit: int) -> Tuple[List[dict], int]:
    """
    Детерминированный mock: для одного и того же query даёт ту же выборку,
    чтобы вердикты были воспроизводимы. Цена варьируется широко (3 000–250 000 ₽),
    чтобы в связке с Alibaba mock получался смешанный набор вердиктов.
    """
    seed = int(hashlib.md5(("avito::" + query).encode("utf-8")).hexdigest()[:8], 16)
    rng = random.Random(seed)

    base_price = rng.uniform(3_000, 250_000)
    competition = rng.randint(20, 8_000)

    count = min(limit, rng.randint(5, 12))
    listings: List[dict] = []
    for i in range(count):
        # Округление до 100 ₽ — реалистично для b2b-объявлений на Авито
        price = round(base_price * rng.uniform(0.6, 1.6), -2)
        seller_type = rng.choices(["компания", "частник"], weights=[2, 1])[0]
        listings.append({
            "title": f"{query.capitalize()} — вариант {i + 1}",
            "price_rub": float(price),
            "url": f"{BASE_URL}/mock/{seed}-{i}",
            "seller_type": seller_type,
        })

    logger.info(
        f"Avito [MOCK]: '{query}' — {len(listings)} объявлений, всего на Авито ≈ {competition}"
    )
    return listings, competition
