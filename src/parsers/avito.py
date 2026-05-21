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


# ── Словарь ценовых диапазонов по ключевым словам ─────────────────────
# Если в запросе встречается одно из слов — базовая цена берётся из
# соответствующего диапазона. Это намного реалистичнее, чем рандом 3k–250k.
_PRICE_ANCHORS = [
    # (ключевые слова, price_min, price_max, competition_range)
    # Тяжёлое оборудование / станки
    ({"станок", "станки", "чпу", "cnc", "lathe", "milling", "фрезерный",
      "токарный", "лазерный", "плазменный", "сварочный", "компрессор",
      "генератор", "трансформатор", "экскаватор", "погрузчик", "кран"},
     120_000, 1_800_000, (300, 4_000)),

    # Средне-дорогое оборудование
    ({"насос", "pump", "двигатель", "motor", "конвейер", "conveyor",
      "дробилка", "crusher", "бетономешалка", "mixer", "пресс", "press",
      "вентиляция", "кондиционер", "котёл", "boiler", "печь", "furnace"},
     45_000, 450_000, (500, 6_000)),

    # Электроника / приборы / автоматизация
    ({"датчик", "sensor", "контроллер", "controller", "plc", "частотник",
      "инвертер", "inverter", "панель", "panel", "дисплей", "display",
      "камера", "camera", "видеонаблюдение", "сервопривод", "servo"},
     8_000, 85_000, (800, 12_000)),

    # Расходники / мелкие комплектующие
    ({"гайка", "болт", "винт", "шайба", "подшипник", "bearing", "фильтр",
      "filter", "ремень", "belt", "сальник", "seal", "прокладка", "gasket",
      "кабель", "cable", "провод", "wire", "шланг", "hose", "клей", "glue",
      "герметик", "sealant", "абразив", "abrasive", "лента", "tape"},
     800, 15_000, (2_000, 25_000)),

    # Инструмент
    ({"инструмент", "tool", "сверло", "drill", "фреза", "cutter", "резец",
      "ключ", "wrench", "отвёртка", "screwdriver", "пила", "saw",
      "шлифовальный", "grinder", "полировальный"},
     3_000, 45_000, (1_500, 15_000)),

    # Медицина / лаборатория
    ({"медицинский", "medical", "стерилизатор", "sterilizer", "центрифуга",
      "centrifuge", "микроскоп", "microscope", "анализатор", "analyzer",
      "рентген", "xray", "узи", "ultrasound", "эндоскоп", "endoscope"},
     35_000, 800_000, (200, 3_000)),

    # Солнечная энергетика
    ({"солнечная", "solar", "панель", "инвертор", "аккумулятор", "battery",
      "литий", "lithium"},
     15_000, 120_000, (600, 8_000)),

    # Мебель / офис
    ({"стол", "desk", "стул", "chair", "шкаф", "cabinet", "стеллаж", "rack",
      "полка", "shelf"},
     5_000, 65_000, (3_000, 30_000)),
]


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


def _estimate_price_range(query: str, rng: random.Random) -> Tuple[float, float, int]:
    """
    Определяет реалистичный ценовой диапазон по содержимому запроса.

    Стратегия:
    1. Проверяем словарь _PRICE_ANCHORS — если нашли совпадение, берём оттуда.
    2. Иначе — эвристика по «тяжести» запроса:
       - Короткие запросы (1-2 слова, <15 символов) → дешёвые товары
       - Длинные технические (3+ слов, >25 символов) → дорогие товары
       - Средние → средний диапазон
    """
    words = query.lower().split()
    words_set = set(words)

    # 1. Поиск по словарю
    for anchors, p_min, p_max, comp_range in _PRICE_ANCHORS:
        if words_set & anchors:
            base_price = rng.uniform(p_min, p_max)
            competition = rng.randint(*comp_range)
            return base_price, base_price * rng.uniform(1.5, 3.0), competition

    # 2. Эвристика по длине/сложности запроса
    word_count = len(words)
    char_count = len(query)

    if word_count <= 1 and char_count < 12:
        # Очень короткий запрос → скорее расходник/мелочь
        base = rng.uniform(2_000, 25_000)
        competition = rng.randint(3_000, 20_000)
    elif word_count >= 4 or char_count > 30:
        # Длинный технический запрос → скорее оборудование
        base = rng.uniform(80_000, 600_000)
        competition = rng.randint(100, 3_000)
    elif word_count >= 3 or char_count > 20:
        # Средний
        base = rng.uniform(20_000, 180_000)
        competition = rng.randint(500, 8_000)
    else:
        # 2 слова, средняя длина
        base = rng.uniform(8_000, 90_000)
        competition = rng.randint(1_000, 12_000)

    return base, base * rng.uniform(1.5, 3.0), competition


def _mock_listings(query: str, limit: int) -> Tuple[List[dict], int]:
    """
    Детерминированный mock: для одного и того же query даёт ту же выборку,
    чтобы вердикты были воспроизводимы.

    Цена определяется по типу товара через _estimate_price_range:
    - «гайка м6» → 800–15 000 ₽
    - «чпу фрезерный станок» → 120 000–1 800 000 ₽
    - неизвестный средний запрос → эвристика по длине
    """
    seed = int(hashlib.md5(("avito::" + query).encode("utf-8")).hexdigest()[:8], 16)
    rng = random.Random(seed)

    price_low, price_high, competition = _estimate_price_range(query, rng)

    count = min(limit, rng.randint(5, 12))
    listings: List[dict] = []
    for i in range(count):
        # Цена в пределах [price_low, price_high], разброс ±30% от центра
        center = rng.uniform(price_low, price_high)
        price = round(center * rng.uniform(0.7, 1.3), -2)
        price = max(price, 100)  # не ниже 100 ₽

        seller_type = rng.choices(["компания", "частник"], weights=[2, 1])[0]
        listings.append({
            "title": f"{query.capitalize()} — вариант {i + 1}",
            "price_rub": float(price),
            "url": f"{BASE_URL}/mock/{seed}-{i}",
            "seller_type": seller_type,
        })

    logger.info(
        f"Avito [MOCK]: '{query}' — {len(listings)} объявлений, "
        f"цены {int(price_low):,}–{int(price_high):,} ₽, "
        f"всего на Авито ≈ {competition}"
    )
    return listings, competition
