"""
NicheParser_China — Agent 6: Avito Finder
Для каждого товара (keep=True) ищет на Авито аналогичные предложения в РФ
и вытаскивает реальную цену продажи: минимум, медиану, максимум, число
объявлений. Медиана идёт в Агента 5 (ВЭД) вместо эвристики «закупка × 2.5».

К каждому продукту прикрепляется:
  avito_offers:           list[dict]  — топ объявлений (title, price_rub, url, seller_type)
  avito_price_rub_min:    float       — самая дешёвая позиция в выдаче
  avito_price_rub_median: float       — медианная цена продажи (используется ВЭД)
  avito_price_rub_max:    float       — самая дорогая позиция
  avito_listings_count:   int         — общее число объявлений по запросу (насыщение рынка)
"""

import logging
import statistics
from typing import List

from src.parsers.avito import search_avito

logger = logging.getLogger(__name__)


def find_on_avito(products: List[dict], top_per_query: int = 10) -> List[dict]:
    """
    Обогащает товары с keep=True ценами с Авито. Дроп-товары пропускаем
    (нет смысла искать рыночную цену тому, что фильтр уже выкинул).
    """
    if not products:
        return []

    for p in products:
        if not p.get("keep", True):
            continue

        # Авито — русскоязычный, поэтому запрос берём из той же фразы,
        # по которой проверяли спрос в Wordstat (она реалистично-формулирована).
        query = (p.get("wordstat_query") or p.get("title_ru") or "").strip()
        if not query:
            _attach_empty(p)
            continue

        try:
            listings, competition = search_avito(query, limit=top_per_query)
        except Exception as e:
            logger.error(f"Agent 6: '{query}' упал: {e}", exc_info=True)
            _attach_empty(p)
            continue

        prices = sorted(
            l["price_rub"] for l in listings if l.get("price_rub", 0) > 0
        )
        if not prices:
            logger.info(f"Agent 6: '{query}' — 0 объявлений с ценой")
            _attach_empty(p)
            continue

        p["avito_offers"] = listings
        p["avito_price_rub_min"] = round(prices[0], 2)
        p["avito_price_rub_median"] = round(statistics.median(prices), 2)
        p["avito_price_rub_max"] = round(prices[-1], 2)
        p["avito_listings_count"] = int(competition or 0)

    return products


def _attach_empty(p: dict) -> None:
    p["avito_offers"] = []
    p["avito_price_rub_min"] = 0.0
    p["avito_price_rub_median"] = 0.0
    p["avito_price_rub_max"] = 0.0
    p["avito_listings_count"] = 0
