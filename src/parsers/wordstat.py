"""
NicheParser_China — Wordstat parser
Яндекс.Директ API (KeywordsResearch) + mock-источник на случай отсутствия токена.

Также: утилиты по «коммерческости» запроса — отделяем информационные
запросы («керамическая плитка» = поиск инфы) от коммерческих
(«купить керамическую плитку оптом» = реальный покупатель). Без этого
частотность Wordstat вводит в заблуждение — 90% запросов информационные.
"""

import logging
import random
import re
from typing import List

import requests

from core.config import (
    USE_MOCK_WORDSTAT,
    YANDEX_OAUTH_TOKEN,
    YANDEX_DIRECT_API_URL,
    TARGET_CATEGORIES,
)
from core.models import WordstatItem

logger = logging.getLogger(__name__)


# ── Коммерческие маркеры и генерация коммерческого варианта ──────────────

# Слова-маркеры реального покупательского интента (не «посмотреть картинки»,
# а «хочу купить»). Проверка по подстрокам с word boundaries — «купить»
# ловит и «купить», и «купила», и «покупка» и т.п.
_COMMERCIAL_MARKERS = (
    "куп",          # купить, покупка, куплю
    "оптом",
    "опт",          # опт (осторожно: короткое, но word-boundary спасает)
    "цена",         # цена, цены
    "прайс",
    "поставщик",
    "производител",  # от производителя, производителя
    "завод",
    "дилер",
    "дистрибьютор",
    "заказать",
    "куплю",
    "b2b",
    "оптов",        # оптовый, оптовая
    "мелкий опт",
)


def is_commercial_query(query: str) -> bool:
    """
    True если в запросе есть маркер коммерческого интента.
    Пример:  «купить эпоксидный клей» → True
             «эпоксидный клей»        → False
             «эпоксидный клей цена»   → True
    """
    q = (query or "").lower().strip()
    if not q:
        return False
    for marker in _COMMERCIAL_MARKERS:
        # \b работает плохо с кириллицей в стандартном re; используем
        # простую проверку: маркер в начале слова или после пробела/дефиса
        pattern = r"(?:^|[\s\-])" + re.escape(marker)
        if re.search(pattern, q):
            return True
    return False


def commercial_variant(query: str) -> str:
    """
    Превращает информационный запрос в коммерческий добавлением «купить … оптом».
    Если запрос уже коммерческий — возвращает как есть (нет смысла удваивать).
    Пример: «эпоксидный клей» → «купить эпоксидный клей оптом»
    """
    q = (query or "").strip()
    if not q or is_commercial_query(q):
        return q
    return f"купить {q} оптом"


# ====== MOCK ======

# Захардкоженные ниши по категориям (пока нет Директ-токена).
# Частотность правдоподобная; структура — плоский список.
_MOCK_NICHES: List[WordstatItem] = [
    # Промышленное оборудование и станки
    WordstatItem("лазерный станок по металлу", 48_500, TARGET_CATEGORIES[0]),
    WordstatItem("токарный станок с ЧПУ", 31_200, TARGET_CATEGORIES[0]),
    WordstatItem("гидравлический пресс 100 тонн", 14_800, TARGET_CATEGORIES[0]),
    # Медтехника
    WordstatItem("аппарат узи портативный", 39_100, TARGET_CATEGORIES[1]),
    WordstatItem("стоматологическая установка", 22_500, TARGET_CATEGORIES[1]),
    # Стройматериалы
    WordstatItem("сэндвич панели для цеха", 56_300, TARGET_CATEGORIES[2]),
    WordstatItem("теплоизоляция пенополиуретан", 27_400, TARGET_CATEGORIES[2]),
    # Сельхозтехника
    WordstatItem("мини трактор для фермы", 42_900, TARGET_CATEGORIES[3]),
    WordstatItem("доильный аппарат для коров", 18_600, TARGET_CATEGORIES[3]),
    # Электроника для бизнеса
    WordstatItem("промышленный 3d принтер", 24_700, TARGET_CATEGORIES[4]),
    WordstatItem("терминал сбора данных", 16_300, TARGET_CATEGORIES[4]),
    # Энергосбережение
    WordstatItem("солнечная панель 400 вт", 61_200, TARGET_CATEGORIES[5]),
    WordstatItem("инвертор гибридный 5 квт", 29_800, TARGET_CATEGORIES[5]),
    # Датчики и автоматизация
    WordstatItem("датчик давления промышленный", 19_400, TARGET_CATEGORIES[6]),
    WordstatItem("частотный преобразователь 22 квт", 34_100, TARGET_CATEGORIES[6]),
    # Химия и материалы
    WordstatItem("огнеупорная краска для металла", 21_700, TARGET_CATEGORIES[7]),
    WordstatItem("эпоксидный клей промышленный", 15_200, TARGET_CATEGORIES[7]),
    # Инновационные / дефицит
    WordstatItem("робот-пылесос для склада", 11_900, TARGET_CATEGORIES[8]),
    WordstatItem("автономный дрон для полей", 9_800, TARGET_CATEGORIES[8]),
]


def _fetch_mock() -> List[WordstatItem]:
    """Возвращает mock-данные с лёгким случайным шумом ±10% — чтоб динамика выглядела живой."""
    result = []
    for item in _MOCK_NICHES:
        noise = random.uniform(0.9, 1.1)
        result.append(WordstatItem(
            keyword=item.keyword,
            frequency=int(item.frequency * noise),
            category=item.category,
        ))
    return result


# ====== REAL API ======

def _fetch_direct_api(keywords: List[str]) -> List[WordstatItem]:
    """
    Реальный вызов Яндекс.Директ Keywords Research API.
    Требует OAuth-токен с правом на получение статистики.
    """
    if not YANDEX_OAUTH_TOKEN:
        logger.warning("YANDEX_OAUTH_TOKEN пуст — переключаюсь на mock")
        return _fetch_mock()

    headers = {
        "Authorization": f"Bearer {YANDEX_OAUTH_TOKEN}",
        "Accept-Language": "ru",
        "Content-Type": "application/json; charset=utf-8",
    }
    body = {
        "method": "hasSearchVolume",
        "params": {"Keywords": keywords, "GeoIDs": [225]},  # 225 = Россия
    }

    try:
        resp = requests.post(YANDEX_DIRECT_API_URL, headers=headers, json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        if "error" in data:
            logger.error(f"Yandex.Direct API error: {data['error']}")
            return []

        result = []
        items = data.get("result", {}).get("SearchVolumeItems", [])
        for item in items:
            result.append(WordstatItem(
                keyword=item.get("Keyword", ""),
                frequency=int(item.get("SearchVolume", 0) or 0),
                category="",  # категорию заполнит caller
            ))
        return result

    except requests.RequestException as e:
        logger.error(f"Wordstat API request failed: {e}")
        return []


# ====== PUBLIC ======

def fetch_wordstat() -> List[WordstatItem]:
    """
    Главная точка входа. Возвращает список ниш с частотностью.
    Если USE_MOCK_WORDSTAT=1 или нет токена — использует mock.
    """
    if USE_MOCK_WORDSTAT or not YANDEX_OAUTH_TOKEN:
        logger.info("Wordstat: использую mock-источник")
        return _fetch_mock()

    logger.info("Wordstat: запрос в Яндекс.Директ API")
    keywords = [item.keyword for item in _MOCK_NICHES]  # стартовый словарь ниш
    items = _fetch_direct_api(keywords)

    # Прокидываем категории из mock-словаря
    cat_map = {m.keyword: m.category for m in _MOCK_NICHES}
    for it in items:
        it.category = cat_map.get(it.keyword, "")

    return items or _fetch_mock()
