"""
NicheParser_China — zakupki.gov.ru parser
Тянет открытый веб-поиск госзакупок для сигнала «спрос прямо сейчас».
Используется Agent 0D (Tender Reader) как дополнение к статистике Comtrade.

Zakupki.gov.ru:
  • SOAP API требует токен + ЭЦП (после 1 января 2025) — сложно
  • HTML веб-поиск — открытый, работает без токенов
  • SSL-сертификат от нац. УЦ Минцифры → в Python нужен `verify=False`
    (или установить сертификат Минцифры в системном хранилище)
  • Из-за WAF Qrator режется VPN-трафик — нужен split-tunnel

Возвращаем: TenderSearchResult со счётчиком, списком цен и дат — этого
достаточно для сигнала «есть/нет тендеры, много/мало, свежие/старые».
"""

from __future__ import annotations

import logging
import re
import statistics
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import requests
from urllib3.exceptions import InsecureRequestWarning

logger = logging.getLogger(__name__)

# SSL-предупреждение мы намеренно игнорируем — сертификат от нац. УЦ РФ
# не в стандартном доверенном списке Python. Правильное решение —
# добавить корневой сертификат Минцифры, для MVP используем verify=False.
warnings.filterwarnings("ignore", category=InsecureRequestWarning)


SEARCH_URL = "https://zakupki.gov.ru/epz/order/extendedsearch/results.html"

_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}


@dataclass
class TenderSearchResult:
    """
    Результат одного поиска по zakupki.gov.ru. Все поля с дефолтами,
    чтобы можно было вернуть пустой результат при ошибке сети.
    """
    query: str = ""
    days_window: int = 0                 # окно фильтра по дате, дней
    total_found: int = 0                 # общий счётчик из шапки поиска
    prices_rub: list[float] = field(default_factory=list)  # цены со страницы
    dates: list[str] = field(default_factory=list)          # даты со страницы (DD.MM.YYYY)
    error: str = ""                      # непустое если запрос упал


def search_tenders(
    query: str,
    days_window: int = 90,
    include_completed: bool = True,
    timeout: int = 30,
) -> TenderSearchResult:
    """
    Один поиск по zakupki.gov.ru с фильтром по ключу и окну времени.
    Возвращает TenderSearchResult; при сетевой ошибке — результат с error.

    days_window: сколько дней назад включать (публикация тендера)
    include_completed: True = все стадии, False = только активные
    """
    query = (query or "").strip()
    if not query:
        return TenderSearchResult(error="empty query")

    # Формируем фильтр по дате: publishDateFrom/publishDateTo в формате DD.MM.YYYY
    date_to = datetime.now()
    date_from = date_to - timedelta(days=days_window)

    params = {
        "searchString": query,
        "morphology": "on",
        "fz44": "on",
        "fz223": "on",
        "recordsPerPage": "_20",
        "sortBy": "UPDATE_DATE",
        "sortDirection": "false",
        "publishDateFrom": date_from.strftime("%d.%m.%Y"),
        "publishDateTo": date_to.strftime("%d.%m.%Y"),
    }
    if not include_completed:
        # Только активные стадии (подача заявок / работа комиссии)
        params["orderStages"] = "0,1"

    try:
        r = requests.get(
            SEARCH_URL, params=params, headers=_HTTP_HEADERS,
            timeout=timeout, verify=False,
        )
    except requests.exceptions.SSLError as e:
        logger.warning(f"zakupki SSL '{query}': {e}")
        return TenderSearchResult(query=query, days_window=days_window,
                                  error=f"ssl: {e}")
    except requests.exceptions.ConnectionError as e:
        # Обычно значит VPN не в split-tunnel — Qrator режет иностранные IP.
        logger.warning(f"zakupki connect '{query}': проверь split-tunnel VPN — {e}")
        return TenderSearchResult(query=query, days_window=days_window,
                                  error=f"connect: {e}")
    except Exception as e:
        logger.error(f"zakupki '{query}': {type(e).__name__}: {e}")
        return TenderSearchResult(query=query, days_window=days_window,
                                  error=f"{type(e).__name__}: {e}")

    if r.status_code != 200:
        return TenderSearchResult(query=query, days_window=days_window,
                                  error=f"HTTP {r.status_code}")

    return _parse_search_html(query, days_window, r.text)


# ─── Парсинг HTML ───────────────────────────────────────────────────────

def _parse_search_html(query: str, days_window: int, html: str) -> TenderSearchResult:
    """
    Достаёт из HTML веб-поиска zakupki.gov.ru:
      - общий счётчик найденного (шапка «... записей»)
      - цены со страницы (по 20 карточек, class price-block__value)
      - даты со страницы (class data-block__value)
    Формат страницы стабильный, но при редизайне сайта регексы могут
    протухнуть — тогда обновим здесь и в тестовой фикстуре.
    """
    result = TenderSearchResult(query=query, days_window=days_window)

    # Общий счётчик: «61 000 записей» (или «Найдено 61 000»)
    m_total = re.search(
        r'(\d[\d\s ]*)\s*(?:записей|результатов)',
        html, re.IGNORECASE,
    )
    if m_total:
        num = re.sub(r"[\s ]+", "", m_total.group(1))
        try:
            result.total_found = int(num)
        except ValueError:
            pass

    # Цены — из блоков price-block__value: «365 400,00 &#8381;»
    for raw in re.findall(r'price-block__value[^>]*>\s*([^<]+?)\s*<', html):
        cleaned = raw.replace("&nbsp;", "").replace(" ", "").replace(" ", "")
        # Убираем символ рубля (&#8381; или ₽)
        cleaned = re.sub(r'(&#8381;|₽)', '', cleaned).strip()
        # Меняем запятую на точку для float
        cleaned = cleaned.replace(",", ".")
        try:
            price = float(cleaned)
            if price > 0:
                result.prices_rub.append(price)
        except ValueError:
            continue

    # Даты — из блоков data-block__value (формат DD.MM.YYYY)
    for raw in re.findall(r'data-block__value[^>]*>\s*([^<]+?)\s*<', html):
        d = raw.strip()
        if re.match(r'^\d{2}\.\d{2}\.\d{4}$', d):
            result.dates.append(d)

    return result


# ─── Агрегаты для агента ────────────────────────────────────────────────

def summarize(result: TenderSearchResult) -> dict:
    """
    Свести TenderSearchResult к компактному словарю для Agent 0D:
      total_found — сколько тендеров всего за окно
      price_median_rub / price_avg_rub — по 20 карточкам верхней страницы
      total_sum_rub — сумма всех цен видимой страницы (грубо, только для сигнала)
      sample_size — сколько цен реально удалось распарсить
    """
    prices = result.prices_rub
    return {
        "total_found": result.total_found,
        "sample_size": len(prices),
        "price_median_rub": round(statistics.median(prices), 0) if prices else 0.0,
        "price_avg_rub": round(sum(prices) / len(prices), 0) if prices else 0.0,
        "total_sum_rub_on_page": round(sum(prices), 0) if prices else 0.0,
        "days_window": result.days_window,
        "error": result.error or "",
    }
