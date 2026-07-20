"""
NicheParser_China — Agent 2: Demand Checker

Берёт продукты от Агента 1 и обогащает их частотностью из Яндекс.Wordstat.
Реальный Direct API если есть токен; иначе детерминированный mock.

Wave-6 UX-волна: помимо общей частотности возвращаем ОТДЕЛЬНО коммерческую
частотность («купить X оптом»). Это критично для B2B: запрос «керамическая
плитка» — 90% информационных (для дизайнеров, ремонта), а «купить
керамическую плитку оптом» — реальный покупательский интент.

Output — те же продукты, обогащённые полями:
  frequency              — общая частотность (что было раньше, для совместимости)
  frequency_commercial   — коммерческая частотность («купить X оптом»)
  commercial_ratio       — доля коммерческих в общей (0.0–1.0)
  is_commercial_query    — исходный wordstat_query уже коммерческий?
"""

import logging
import random
from typing import List

from core.config import USE_MOCK_WORDSTAT, YANDEX_OAUTH_TOKEN
from src.parsers.wordstat import (
    _MOCK_NICHES, _fetch_direct_api,
    is_commercial_query, commercial_variant,
)

logger = logging.getLogger(__name__)


def check_demand(products: List[dict]) -> List[dict]:
    """
    Для каждого продукта добавить поля частотностей.
    Использует поле 'wordstat_query' из продукта (его кладёт Агент 1);
    если пусто — fallback на title_ru без скобок.

    Не отсеивает ничего — просто обогащает. Решение «оставлять/выкидывать»
    принимается на следующем шаге (Агент 3 — фильтр).
    """
    if not products:
        return []

    # Собираем базовые запросы + их коммерческие варианты
    queries_info: List[str] = []
    queries_comm: List[str] = []
    for p in products:
        q = (p.get("wordstat_query") or "").strip().lower()
        if not q:
            q = (p.get("title_ru") or "").strip().lower()
        queries_info.append(q)
        queries_comm.append(commercial_variant(q))
        p["wordstat_query"] = q

    if USE_MOCK_WORDSTAT or not YANDEX_OAUTH_TOKEN:
        logger.info(f"Agent 2: mock, {len(queries_info)} запросов + {len(queries_comm)} коммерческих")
        freqs_info = _mock_frequencies(queries_info)
        freqs_comm = _mock_commercial_frequencies(queries_info, freqs_info, queries_comm)
    else:
        logger.info(f"Agent 2: Direct API, {len(queries_info)} + {len(queries_comm)} запросов")
        freqs_info = _real_frequencies(queries_info)
        freqs_comm = _real_frequencies(queries_comm)

    for p, q, f_info, f_comm in zip(products, queries_info, freqs_info, freqs_comm):
        p["frequency"] = int(f_info)
        p["frequency_commercial"] = int(f_comm)
        p["commercial_ratio"] = round(f_comm / f_info, 3) if f_info > 0 else 0.0
        p["is_commercial_query"] = is_commercial_query(q)

    return products


def _mock_frequencies(queries: List[str]) -> List[int]:
    """
    Детерминированный mock для базовых (информационных) запросов.
    Если запрос совпадает с известным из словаря 19 ниш — отдаём оттуда.
    Иначе — псевдослучайное число в диапазоне [400, 28000], жёстко привязанное
    к строке (от запуска к запуску одинаковое).
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


def _mock_commercial_frequencies(
    queries_info: List[str],
    freqs_info: List[int],
    queries_comm: List[str],
) -> List[int]:
    """
    Mock коммерческой частотности. Правдоподобная эвристика:
      - если исходный запрос уже коммерческий («купить X») — берём full
        (базовое число и есть коммерческий спрос)
      - иначе — коэффициент 3-15% от общего (реальная B2B пропорция), с
        небольшим стабильным разбросом от seed запроса
    """
    out: List[int] = []
    for q_info, f_info, q_comm in zip(queries_info, freqs_info, queries_comm):
        if not q_info or f_info == 0:
            out.append(0)
            continue

        if is_commercial_query(q_info):
            # Запрос уже коммерческий — full = коммерческий спрос
            out.append(f_info)
            continue

        # Стабильный seed от строки: 3-15% от общего трафика — реальный B2B
        seed = sum(ord(c) for c in q_comm) * 2654435761 + len(q_comm) * 37
        rng = random.Random(seed)
        ratio = rng.uniform(0.03, 0.15)
        out.append(int(f_info * ratio))
    return out


def _real_frequencies(queries: List[str]) -> List[int]:
    """Зовёт Yandex.Direct hasSearchVolume через существующий парсер."""
    items = _fetch_direct_api(queries)
    by_kw = {it.keyword.lower(): it.frequency for it in items}
    return [by_kw.get(q, 0) for q in queries]
