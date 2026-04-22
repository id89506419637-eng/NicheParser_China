"""
NicheParser_China — Avito parser (заглушка)
Будет активирован, когда появится доступ к официальному API Авито.
До тех пор возвращает пустые данные и пишет warning в лог.
"""

import logging
from typing import List

from core.config import ENABLE_AVITO

logger = logging.getLogger(__name__)


def is_enabled() -> bool:
    return bool(ENABLE_AVITO)


def search_avito(query: str) -> List[dict]:
    if not ENABLE_AVITO:
        logger.info("Avito: модуль отключён (ENABLE_AVITO=0) — возвращаю пустой список")
        return []

    # Заглушка — пока нет API-доступа.
    # В будущем здесь будет вызов Авито Pro API с переменной AVITO_API_KEY.
    logger.warning(
        "Avito: ENABLE_AVITO=1, но интеграция с Авито API ещё не реализована. "
        "Установи AVITO_API_KEY в .env и допиши модуль, когда появится доступ."
    )
    return []
