"""
NicheParser_China — Configuration
Загрузка настроек из .env и константы проекта.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _bool(value: str, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


# === Flask ===
_INSECURE_SECRET = "dev-insecure-change-me"
SECRET_KEY = os.getenv("SECRET_KEY", _INSECURE_SECRET)
FLASK_DEBUG = _bool(os.getenv("FLASK_DEBUG"), default=False)
FLASK_HOST = os.getenv("FLASK_HOST", "127.0.0.1")
FLASK_PORT = int(os.getenv("FLASK_PORT", "5000"))

# В проде с дефолтным ключом — сессии подделываемы. Падаем громко.
if not FLASK_DEBUG and SECRET_KEY == _INSECURE_SECRET:
    raise RuntimeError(
        "SECRET_KEY не задан в .env — сессии Flask станут подделываемыми. "
        "Сгенерируй случайный ключ: python -c \"import secrets; print(secrets.token_hex(32))\" "
        "и добавь в .env как SECRET_KEY=..."
    )

# === Paths ===
DB_PATH = BASE_DIR / "data" / "niches.db"
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)
(BASE_DIR / "data").mkdir(exist_ok=True)

# === AI (OpenRouter) ===
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
AI_MODEL = os.getenv("AI_MODEL", "google/gemini-2.0-flash-001")

# === Yandex Wordstat ===
YANDEX_OAUTH_TOKEN = os.getenv("YANDEX_OAUTH_TOKEN", "")
USE_MOCK_WORDSTAT = _bool(os.getenv("USE_MOCK_WORDSTAT"), default=True)
YANDEX_DIRECT_API_URL = "https://api.direct.yandex.com/json/v5/keywordsresearch"

# === Alibaba ===
# Alibaba активно блокирует скрейпинг (AWSC). Mock-режим даёт правдоподобные
# товары, чтобы пайплайн давал end-to-end результат без антибот-инфраструктуры.
USE_MOCK_ALIBABA = _bool(os.getenv("USE_MOCK_ALIBABA"), default=True)

# === Feature flags ===
ENABLE_WORDSTAT = _bool(os.getenv("ENABLE_WORDSTAT"), default=True)
ENABLE_ALIBABA = _bool(os.getenv("ENABLE_ALIBABA"), default=True)
ENABLE_AVITO = _bool(os.getenv("ENABLE_AVITO"), default=False)

# === ВЭД дефолты ===
DEFAULT_DUTY_PERCENT = float(os.getenv("DEFAULT_DUTY_PERCENT", "10.0"))
DEFAULT_VAT_PERCENT = float(os.getenv("VAT_PERCENT", "22.0"))
DEFAULT_MIN_MARGIN_PERCENT = float(os.getenv("MIN_MARGIN_PERCENT", "50.0"))
DEFAULT_MIN_MARGIN_TOTAL_RUB = float(os.getenv("MIN_MARGIN_TOTAL_RUB", "100000"))
DEFAULT_LOGISTICS_PER_KG_USD = 3.0
DEFAULT_LOGISTICS_PER_CBM_USD = 350.0
DEFAULT_BANK_PERCENT = 2.0

# === Парсинг ===
REQUEST_DELAY_MIN = 2.0
REQUEST_DELAY_MAX = 5.0
ALIBABA_MAX_PRODUCTS_PER_NICHE = 20
ALIBABA_PAGE_TIMEOUT_MS = 30_000

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
]

# === Курсы валют (API ЦБ РФ) — кэшируем на весь процесс ===
CBR_DAILY_URL = "https://www.cbr-xml-daily.ru/daily_json.js"
CBR_CACHE_TTL_SECONDS = 3600  # 1 час

# === Категории для Wordstat (whitelist) ===
TARGET_CATEGORIES = [
    "Промышленное оборудование и станки",
    "Медтехника и медоборудование",
    "Стройматериалы и строительное оборудование",
    "Сельхозтехника и агрооборудование",
    "Электроника для бизнеса и производства",
    "Энергосберегающие технологии и солнечная энергетика",
    "Датчики, автоматизация, роботизация",
    "Химия и промышленные материалы",
    "Инновационные товары (дефицит в РФ)",
]

# Типы ниш (для AI-классификации)
NICHE_TYPES = ("ДЕФИЦИТ", "ИННОВАЦИЯ", "СЛАБАЯ НИША", "ВЫСОКИЙ СПРОС", "ОБЪЁМНЫЙ ТОВАР")

# Вердикты
VERDICTS = ("ВЕЗЁМ", "ИЗУЧИТЬ", "НЕ ВЕЗЁМ")
