"""
NicheParser_China — Agent 0D: Tender Reader (Wave 6)

Дополняет Agent 0C (детектор импорта UN Comtrade) свежим сигналом
«кто прямо сейчас платит за это в РФ» — через веб-поиск zakupki.gov.ru.

Логика:
  Comtrade говорит про 2024 год (годовой лаг публикации).
  Госзакупки — актуальные тендеры за последние 90 дней.
  Комбинация: категория где Comtrade показывает рост И госзакупки
  показывают активные тендеры = сигнал двойной надёжности.

Каждой категории из списка (обычно берём из последнего прогона Agent 0C)
приписываем:
  tenders_count_90d       — сколько тендеров опубликовано за 90 дней
  tenders_price_median    — медианная цена тендера, ₽ (по видимой странице)
  tenders_price_avg       — средняя цена, ₽
  tender_density          — плотность = tenders_count_90d / 90 (тендеров/день)
  tender_activity         — «high» / «medium» / «low» / «none»
                            (по количеству тендеров за 90 дней)
  fetched_at              — когда прогнали

Между категориями делаем короткий sleep, чтобы не тревожить WAF Qrator.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional

from src.parsers.zakupki import search_tenders, summarize

logger = logging.getLogger(__name__)


# ── Пороги плотности тендеров (сколько за 90 дней) ─────────────────────
# Настроены на промышленных категориях: подшипники ~1600, автозапчасти ~160,
# лазерные станки ~38 — так что «300+» = массовый спрос, «50+» = регулярный.
_ACTIVITY_THRESHOLDS = (
    (300, "high"),      # массовый спрос, тендер каждые ~7 часов
    (50,  "medium"),    # регулярный спрос, тендер раз в 1-2 дня
    (5,   "low"),       # единичные тендеры
)


@dataclass
class TenderSignal:
    """Один сигнал по категории от Agent 0D — параллельно ImportSignal от 0C."""
    hs_code: str = ""                        # для связки с ImportSignal
    query: str = ""                          # что искали (category_name)
    days_window: int = 90
    tenders_count_90d: int = 0               # total_found из шапки поиска
    tenders_price_median_rub: float = 0.0
    tenders_price_avg_rub: float = 0.0
    tender_density_per_day: float = 0.0      # count / days_window
    tender_activity: str = "none"            # high/medium/low/none
    fetched_at: str = ""
    error: str = ""                          # если запрос не прошёл


def _classify_activity(total_90d: int) -> str:
    """По числу тендеров за 90 дней — human-label активности."""
    for threshold, label in _ACTIVITY_THRESHOLDS:
        if total_90d >= threshold:
            return label
    return "none"


def read_tenders(
    categories: Iterable[dict],
    days_window: int = 90,
    sleep_between: float = 1.0,
) -> list[TenderSignal]:
    """
    Прогнать список категорий через веб-поиск zakupki.gov.ru.

    categories: iterable словарей вида {"hs_code": "8482", "category_name": "..."}
                (обычно результат db.get_import_signals_by_batch)
    days_window: окно фильтра по дате публикации тендера (default 90 дней)
    sleep_between: пауза между запросами (сек) — уважаем WAF

    Возвращает список TenderSignal — по одному на категорию.
    Если zakupki недоступен (VPN не в split-tunnel) — все сигналы с error.
    """
    now_iso = datetime.now().isoformat()
    signals: list[TenderSignal] = []

    for i, cat in enumerate(categories):
        hs = str(cat.get("hs_code") or "").strip()
        query = str(cat.get("category_name") or "").strip()
        if not query:
            continue

        # Небольшая пауза между запросами, чтобы Qrator не считал нас ботом
        if i > 0 and sleep_between > 0:
            time.sleep(sleep_between)

        result = search_tenders(query, days_window=days_window)
        s = summarize(result)

        if s["error"]:
            signals.append(TenderSignal(
                hs_code=hs, query=query, days_window=days_window,
                fetched_at=now_iso, error=s["error"],
            ))
            continue

        count = s["total_found"]
        density = round(count / days_window, 2) if days_window > 0 else 0.0
        signals.append(TenderSignal(
            hs_code=hs,
            query=query,
            days_window=days_window,
            tenders_count_90d=count,
            tenders_price_median_rub=s["price_median_rub"],
            tenders_price_avg_rub=s["price_avg_rub"],
            tender_density_per_day=density,
            tender_activity=_classify_activity(count),
            fetched_at=now_iso,
        ))

    stats = {"high": 0, "medium": 0, "low": 0, "none": 0, "error": 0}
    for s in signals:
        stats["error" if s.error else s.tender_activity] += 1
    logger.info(
        f"Agent 0D: обработано {len(signals)} категорий за {days_window} дней. "
        f"Активность: {stats['high']} high, {stats['medium']} medium, "
        f"{stats['low']} low, {stats['none']} none, {stats['error']} error"
    )
    return signals
