"""
NicheParser_China — Agent 4: Alibaba Finder
Для каждого товара (keep=True) ищет 3-5 лучших офферов на Alibaba через
существующий парсер src/parsers/alibaba.py. В mock-режиме отрабатывает
мгновенно, в реальном — 5–15с на запрос (Playwright + stealth).

К каждому продукту прикрепляет:
  alibaba_offers: list[dict]  — топ N предложений (title, price min/max, MOQ, url, certs)
  alibaba_min_usd: float       — минимальная цена из найденных
  alibaba_min_moq: int         — минимальный MOQ
  alibaba_competition: int     — общее число результатов в выдаче (метрика конкуренции)
"""

import logging
from typing import List

from src.parsers.alibaba import search_alibaba

logger = logging.getLogger(__name__)


def find_on_alibaba(products: List[dict], top_per_query: int = 5) -> List[dict]:
    """
    Обогащает товары с keep=True данными с Alibaba. Дроп-товары пропускаем
    (нет смысла парсить отброшенные на этапе фильтра).
    """
    if not products:
        return []

    for p in products:
        if not p.get("keep", True):
            continue

        # Что отправляем в поиск Alibaba: title_en (он чистый, без скобок,
        # подходит для англоязычной выдачи).
        query = (p.get("title_en") or p.get("title_ru") or "").strip()
        if not query:
            _attach_empty(p)
            continue

        try:
            offers, competition = search_alibaba(query, limit=top_per_query)
        except Exception as e:
            logger.error(f"Agent 4: '{query}' упал: {e}", exc_info=True)
            _attach_empty(p)
            continue

        if not offers:
            logger.info(f"Agent 4: '{query}' — 0 офферов (блок/капча/пусто)")
            _attach_empty(p)
            continue

        prices = [o.price_usd_min for o in offers if o.price_usd_min > 0]
        moqs = [o.moq for o in offers if o.moq > 0]

        p["alibaba_offers"] = [_offer_to_dict(o) for o in offers]
        p["alibaba_min_usd"] = round(min(prices), 2) if prices else 0.0
        p["alibaba_max_usd"] = round(max((o.price_usd_max for o in offers if o.price_usd_max > 0), default=0.0), 2)
        p["alibaba_min_moq"] = min(moqs) if moqs else 0
        p["alibaba_competition"] = int(competition or 0)

    return products


def _attach_empty(p: dict) -> None:
    p["alibaba_offers"] = []
    p["alibaba_min_usd"] = 0.0
    p["alibaba_max_usd"] = 0.0
    p["alibaba_min_moq"] = 0
    p["alibaba_competition"] = 0


def _offer_to_dict(o) -> dict:
    return {
        "title_en": o.title_en,
        "price_usd_min": o.price_usd_min,
        "price_usd_max": o.price_usd_max,
        "moq": o.moq,
        "supplier_rating": o.supplier_rating,
        "deals_count": o.deals_count,
        "certificates": list(o.certificates) if o.certificates else [],
        "weight_kg": o.weight_kg,
        # weight_source: "parsed" | "mock" | "unknown" — используется Agent 5
        # чтобы отличать честный вес от подставленного дефолта 0.5 кг.
        "weight_source": getattr(o, "weight_source", "unknown"),
        "product_url": o.product_url,
    }
