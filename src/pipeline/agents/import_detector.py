"""
NicheParser_China — Agent 0C: Import Detector

Второй вход в систему (первый — Agent 0A Industry Explorer).
Тянет реальную статистику UN Comtrade по китайскому экспорту в РФ,
считает дельту к прошлому году и сравнивает с общемировым трендом Китая.
Результат — ранжированный список HS-4 категорий с «расходящимися кривыми».

Логика:
  1. Для каждой из ~60 промышленных HS-4 категорий берём:
     - Китай → РФ за 2 года (например 2023 и 2024) = ΔRU
     - Китай → весь мир за те же годы = ΔWorld
  2. «Специфически российский сигнал» = ΔRU − ΔWorld (в п.п.)
     — если РФ растёт быстрее общего китайского экспорта, это НАШ сигнал
     (уход брендов, санкции, локальная замена), а не глобальный тренд
  3. Классифицируем:
     RU-спец: ΔRU−ΔWorld > +20 п.п. и ΔRU>0  → 🔥 HOT
     Растёт:  ΔRU > +10%                     → 🟢
     Средне:  ΔRU−ΔWorld > −5 п.п.           → 🟡
     Слабо:   иначе                          → 🔴
  4. Плюс: доля РФ в общем китайском экспорте по категории
     (если 15%+ — Китай уже перестроил линии под РФ)

Работает без токена UN Comtrade (публичный preview, 500 записей за запрос).
При частых запусках лучше зарегистрировать токен на comtradedeveloper.un.org
и положить в .env как COMTRADE_TOKEN.

ВАЖНО: HS-4 — это категория, не конкретный товар. Дальше юзер жмёт
«Взять в работу», HS-4 передаётся в Agent 0A как затравка, тот генерирует
конкретные ниши внутри категории. Точный подкод ТН ВЭД для расчёта пошлин
всё равно даёт брокер (см. feedback [no-llm-tnved-codes]).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import comtradeapicall as ct

logger = logging.getLogger(__name__)


# --- Список категорий-кандидатов ---
# HS-4 промышленные группы: машины, оборудование, компоненты, инструмент.
# Не берём чисто розничные категории (обувь, одежда, косметика) — не наш B2B.
# При расширении добавлять сюда, всё остальное подтянется.
CATEGORIES: dict[str, str] = {
    # Металлы и крепёж
    '7318': 'Крепёж (болты/гайки)',
    '7308': 'Металлоконструкции',
    '7315': 'Цепи стальные',
    '7326': 'Изделия из чёрных металлов проч.',
    '8302': 'Мебельная фурнитура',
    # Механика / подшипники
    '8482': 'Подшипники',
    '8483': 'Валы/шкивы/коробки',
    '8484': 'Прокладки/сальники',
    # Насосы, компрессоры, клапаны
    '8413': 'Насосы для жидкостей',
    '8414': 'Компрессоры/вентиляторы',
    '8481': 'Краны/клапаны',
    # Двигатели и приводы
    '8407': 'ДВС искр.зажигания',
    '8408': 'Дизели',
    '8412': 'Двигатели прочие',
    '8501': 'Электродвигатели/генераторы',
    '8502': 'Электроагрегаты',
    # Электрика
    '8504': 'Трансформаторы/инверторы',
    '8536': 'Переключатели/реле',
    '8537': 'Щиты/распределители',
    '8544': 'Кабели изолированные',
    # Промышленные машины
    '8419': 'Печи/сушилки',
    '8421': 'Фильтры/центрифуги',
    '8422': 'Посудомоечные/упаковочные',
    '8423': 'Весы',
    '8424': 'Мех. распылители/огнетушители',
    '8425': 'Тали/подъёмники',
    '8426': 'Краны судовые/мостовые',
    '8427': 'Автопогрузчики',
    '8428': 'Прочее подъёмное',
    '8429': 'Бульдозеры/грейдеры/экскаваторы',
    '8430': 'Проч. землеройное',
    '8438': 'Пищевое оборудование',
    '8443': 'Печатные машины',
    '8445': 'Текстильные машины',
    '8446': 'Ткацкие станки',
    '8450': 'Стиральные машины',
    '8452': 'Швейные машины',
    '8455': 'Прокатные станы',
    '8456': 'Станки лазерные/электроразрядные',
    '8457': 'Обрабатывающие центры',
    '8458': 'Токарные станки',
    '8459': 'Сверлильные/фрезерные',
    '8462': 'Кузнечно-прессовые',
    '8464': 'Станки по камню/бетону',
    '8465': 'Станки по дереву',
    '8467': 'Инструмент пневм./электр.',
    '8477': 'Оборудование для пластмасс',
    '8479': 'Машины прочие спец. назначения',
    '8480': 'Формы для литья',
    '8486': 'Оборудование для полупроводн.',
    # Транспорт
    '4011': 'Шины пневматические',
    '8708': 'Части автомобилей',
    # Инструмент
    '8202': 'Пилы ручные',
    '8203': 'Ножницы/клещи',
    '8205': 'Ручной инструмент',
    '8207': 'Инструмент сменный',
    # Оптика/приборы
    '9026': 'Приборы измерения потока',
    '9027': 'Приборы физ./хим. анализа',
    '9031': 'Приборы измерит. проч.',
    '9032': 'Приборы автоматич. регулирования',
}


@dataclass
class ImportSignal:
    """Один сигнал по HS-4 категории — результат работы Agent 0C."""
    hs_code: str = ""
    category_name: str = ""
    value_current_usd: float = 0.0      # объём Китай→РФ за последний доступный год, USD
    delta_ru_percent: float = 0.0       # ΔRU за год (last vs prev), %
    delta_world_percent: float = 0.0    # ΔWorld за тот же период, %
    russia_specific_pp: float = 0.0     # ΔRU − ΔWorld, п.п. (главный сигнал)
    ru_share_of_world: float = 0.0      # % российского направления в общем экспорте Китая
    classification: str = ""            # "hot" | "growing" | "medium" | "weak"
    period_current: int = 0             # год, за который свежие данные
    period_prev: int = 0                # год, с которым сравниваем


def classify_signal(delta_ru: float, delta_world: float) -> str:
    """
    Классификация сигнала по правилам из плана Wave 6.
    Пороги подобраны на 60-категорийном прогоне 2023→2024.
    """
    spec = delta_ru - delta_world
    if spec > 20 and delta_ru > 0:
        return "hot"        # 🔥 специфически российский рост
    if delta_ru > 10:
        return "growing"    # 🟢 растёт (даже если мир растёт быстрее)
    if spec > -5:
        return "medium"     # 🟡 держится в пределах мирового тренда
    return "weak"           # 🔴 падает сильнее мира


def _fetch_year(partner_code: str, year: int, cmd_codes: str) -> dict[str, float]:
    """
    Один запрос к UN Comtrade: китайский экспорт за год к указанному партнёру.
    partner_code='643' → Россия, '0' → весь мир.
    Возвращает {hs_code: value_usd}. Пустой dict при провале — не бросает.
    """
    try:
        df = ct.previewFinalData(
            typeCode='C', freqCode='A', clCode='HS',
            period=str(year),
            reporterCode='156',          # Китай — репортёр
            cmdCode=cmd_codes,
            flowCode='X',                # экспорт
            partnerCode=partner_code,
            partner2Code=None, customsCode=None, motCode=None,
            maxRecords=500, includeDesc=False,
        )
    except Exception as e:
        logger.error(f"Agent 0C/Comtrade {partner_code} {year}: {type(e).__name__}: {e}")
        return {}
    if df is None or len(df) == 0:
        logger.warning(f"Agent 0C/Comtrade {partner_code} {year}: пустой ответ")
        return {}
    return dict(zip(df['cmdCode'].astype(str), df['primaryValue']))


def detect_import_signals(
    categories: Optional[dict[str, str]] = None,
    period_current: int = 2024,
    period_prev: int = 2023,
) -> list[ImportSignal]:
    """
    Главная функция агента: тянет статистику, считает сигналы, сортирует.
    Возвращает список ImportSignal, отсортированный по russia_specific_pp
    убывающе (топ-«специфически российских» вверху).

    По умолчанию сравниваем 2023 vs 2024 — самые свежие полные годы.
    Данные за 2025 появятся в UN Comtrade ближе к концу 2026.
    """
    cats = categories or CATEGORIES
    if not cats:
        return []

    cmd_str = ','.join(cats.keys())
    logger.info(f"Agent 0C: детектор запущен по {len(cats)} категориям, {period_prev} vs {period_current}")

    ru_curr = _fetch_year('643', period_current, cmd_str)
    ru_prev = _fetch_year('643', period_prev, cmd_str)
    w_curr  = _fetch_year('0',   period_current, cmd_str)
    w_prev  = _fetch_year('0',   period_prev, cmd_str)

    signals: list[ImportSignal] = []
    for code, name in cats.items():
        rc = float(ru_curr.get(code, 0))
        rp = float(ru_prev.get(code, 0))
        wc = float(w_curr.get(code, 0))
        wp = float(w_prev.get(code, 0))

        d_ru = (rc - rp) / rp * 100 if rp > 0 else 0.0
        d_w  = (wc - wp) / wp * 100 if wp > 0 else 0.0
        spec = d_ru - d_w
        share = (rc / wc * 100) if wc > 0 else 0.0

        signals.append(ImportSignal(
            hs_code=code,
            category_name=name,
            value_current_usd=rc,
            delta_ru_percent=round(d_ru, 1),
            delta_world_percent=round(d_w, 1),
            russia_specific_pp=round(spec, 1),
            ru_share_of_world=round(share, 2),
            classification=classify_signal(d_ru, d_w),
            period_current=period_current,
            period_prev=period_prev,
        ))

    signals.sort(key=lambda s: -s.russia_specific_pp)
    hot = sum(1 for s in signals if s.classification == "hot")
    growing = sum(1 for s in signals if s.classification == "growing")
    logger.info(f"Agent 0C: получено {len(signals)} сигналов, из них {hot} 🔥HOT + {growing} 🟢растущих")
    return signals
