"""
NicheParser_China — Agent 2: Demand Checker
Берёт продукты от Агента 1 и обогащает их частотностью из Яндекс.Wordstat
(реальный Direct API, если есть токен; иначе детерминированный mock).

Output: тот же список dict, но с добавленным полем 'frequency' (запросов/мес).
"""

import logging
import random
from typing import List

from core.config import USE_MOCK_WORDSTAT, YANDEX_OAUTH_TOKEN
from src.parsers.wordstat import _MOCK_NICHES, _fetch_direct_api

logger = logging.getLogger(__name__)


def check_demand(products: List[dict]) -> List[dict]:
    """
    Для каждого продукта добавить поле 'frequency' (число запросов/мес в РФ).
    Использует поле 'wordstat_query' из продукта (его кладёт Агент 1);
    если пусто — fallback на title_ru без скобок.

    Не отсеивает ничего — просто обогащает. Решение «оставлять/выкидывать»
    принимается на следующем шаге (Агент 3 — фильтр).
    """
    if not products:
        return []

    queries: List[str] = []
    for p in products:
        q = (p.get("wordstat_query") or "").strip().lower()
        if not q:
            q = (p.get("title_ru") or "").strip().lower()
        queries.append(q)
        p["wordstat_query"] = q

    if USE_MOCK_WORDSTAT or not YANDEX_OAUTH_TOKEN:
        logger.info(f"Agent 2: mock-режим, {len(queries)} запросов")
        freqs = _mock_frequencies(queries)
    else:
        logger.info(f"Agent 2: Yandex.Direct API, {len(queries)} запросов")
        freqs = _real_frequencies(queries)

    for p, f in zip(products, freqs):
        p["frequency"] = int(f)

    return products


def _mock_frequencies(queries: List[str]) -> List[int]:
    """
    Детерминированный mock. Если запрос совпадает с известным из словаря
    19 ниш — отдаём оттуда. Иначе — псевдослучайное число в диапазоне
    [400, 28000], жёстко привязанное к строке (от запуска к запуску одинаковое).
    """
    known = {n.keyword.lower(): n.frequency for n in _MOCK_NICHES}
    out: List[int] = []
    for q in queries:
        if not q:
            out.append(0)
            continue
        if q in known:
            out.append(known[q])
            continue
        # Stable hash от строки → число в диапазоне типичных B2B-частотностей
        seed = sum(ord(c) for c in q) * 1000003 + len(q) * 17
        rng = random.Random(seed)
        # B2B-распределение: чаще средние частоты, реже 20k+
        base = rng.randint(400, 18000)
        if rng.random() < 0.18:
            base = rng.randint(18000, 60000)
        out.append(base)
    return out


def _real_frequencies(queries: List[str]) -> List[int]:
    """Зовёт Yandex.Direct hasSearchVolume через существующий парсер."""
    items = _fetch_direct_api(queries)
    by_kw = {it.keyword.lower(): it.frequency for it in items}
    return [by_kw.get(q, 0) for q in queries]
