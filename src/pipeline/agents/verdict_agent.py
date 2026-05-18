"""
NicheParser_China — Agent 7: LLM Verdict
По полному пакету данных (Wordstat-спрос, Alibaba-оффер, Avito-цена в РФ,
ВЭД-расчёт) для каждого товара с keep=True даёт финальный вердикт
ВЕЗЁМ / ИЗУЧИТЬ / НЕ ВЕЗЁМ + короткое обоснование (1–2 предложения).

Базовые пороги (из SPEC v5.1):
  ВЕЗЁМ     — маржа ≥ 50% И прибыль за MOQ ≥ 100 000 ₽
  ИЗУЧИТЬ   — 30 ≤ маржа < 50%  ИЛИ маржа ≥ 50% но прибыль < 100 000 ₽
  НЕ ВЕЗЁМ  — маржа < 30%

LLM может скорректировать вердикт с учётом «мягких» факторов:
насыщенности рынка на Авито, спроса в Wordstat, типа поставщика.
Если LLM упал — fallback на чистую арифметику по тем же порогам,
чтобы каждый товар всегда получил какой-то вердикт.

К каждому продукту прикрепляется:
  verdict:         'ВЕЗЁМ' | 'ИЗУЧИТЬ' | 'НЕ ВЕЗЁМ'
  verdict_reason:  короткое обоснование (1-2 предложения)
  verdict_source:  'llm' | 'arithmetic' — кто дал ответ
"""

import json
import logging
from typing import List, Optional, Tuple

import requests

from core.config import (
    OPENROUTER_API_KEY, OPENROUTER_BASE_URL, AI_MODEL,
    DEFAULT_MIN_MARGIN_PERCENT, DEFAULT_MIN_MARGIN_TOTAL_RUB,
)

logger = logging.getLogger(__name__)


_FALLBACK_MODELS = [
    "openai/gpt-oss-120b:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "z-ai/glm-4.5-air:free",
    "minimax/minimax-m2.5:free",
]


_VALID_VERDICTS = ("ВЕЗЁМ", "ИЗУЧИТЬ", "НЕ ВЕЗЁМ")


SYSTEM_PROMPT = (
    "Ты — эксперт по ВЭД и B2B-импорту из Китая в Россию в 2026 году. "
    "По пакету данных (спрос, цена в КНР, цена продажи в РФ, ВЭД-себестоимость, "
    "маржа) выдаёшь финальный вердикт по каждому товару. Отвечаешь СТРОГО "
    "в формате JSON, без пояснений снаружи."
)


def issue_verdicts(products: List[dict]) -> List[dict]:
    """
    Прогнать список через LLM-вердикт. Каждому товару с keep=True
    приписываются verdict / verdict_reason / verdict_source.
    Отброшенные на фильтре товары получают verdict='НЕ ВЕЗЁМ' с пометкой
    «дроп на этапе фильтра» — единый источник правды для UI.
    """
    if not products:
        return []

    # Сразу размечаем дропнутые
    eligible = []
    for p in products:
        if not p.get("keep", True):
            p["verdict"] = "НЕ ВЕЗЁМ"
            p["verdict_reason"] = "дроп на этапе LLM-фильтра"
            p["verdict_source"] = "arithmetic"
            continue
        eligible.append(p)

    if not eligible:
        return products

    if not OPENROUTER_API_KEY:
        logger.warning("Agent 7: нет OPENROUTER_API_KEY — использую арифметику")
        for p in eligible:
            _apply_arithmetic(p, note="LLM недоступен (нет API-ключа)")
        return products

    seen = set()
    chain: List[str] = []
    for m in [AI_MODEL] + _FALLBACK_MODELS:
        if m and m not in seen:
            seen.add(m)
            chain.append(m)

    last_error = ""
    for model in chain:
        decisions, err = _try_one_model(model, eligible)
        if decisions is not None:
            _apply_decisions(eligible, decisions)
            return products
        last_error = err
        logger.warning(f"Agent 7: модель {model} не сработала ({err}) — пробую следующую")

    logger.error(f"Agent 7: все модели упали ({last_error}) — fallback на арифметику")
    for p in eligible:
        _apply_arithmetic(p, note="LLM упал — арифметика")
    return products


# ============ LLM-путь ============

def _build_prompt(products: List[dict]) -> str:
    lines = []
    for i, p in enumerate(products):
        title = p.get("title_ru") or p.get("title_en") or "?"
        freq = int(p.get("frequency") or 0)
        ali_usd = p.get("alibaba_min_usd") or 0
        ali_moq = p.get("alibaba_min_moq") or 0
        avito_med = p.get("avito_price_rub_median") or 0
        avito_count = p.get("avito_listings_count") or 0
        ved_cost = p.get("ved_cost_per_unit_rub") or 0
        margin = p.get("ved_margin_percent") or 0
        profit_moq = p.get("ved_margin_per_moq_rub") or 0
        price_source = p.get("ved_price_source") or "?"

        lines.append(
            f"{i}. {title}\n"
            f"   спрос: {freq} запр/мес · "
            f"Alibaba: ${ali_usd:.2f}/шт, MOQ {ali_moq} · "
            f"Avito (РФ): медиана {int(avito_med):,} ₽ из {avito_count} объявл. "
            f"(источник цены: {price_source})\n"
            f"   ВЭД: {int(ved_cost):,} ₽/шт себестоимость, маржа {margin:.0f}%, "
            f"прибыль за MOQ {int(profit_moq):,} ₽"
        )
    block = "\n".join(lines).replace(",", " ")  # пробелы вместо запятых в тысячах

    return f"""Дано {len(products)} товаров со всеми данными после прохождения 6 агентов:

{block}

ЗАДАЧА: для КАЖДОГО товара по индексу 0..{len(products)-1} выдай:
  verdict: одно из «ВЕЗЁМ», «ИЗУЧИТЬ», «НЕ ВЕЗЁМ»
  reason:  1-2 коротких предложения, до 180 символов

БАЗОВЫЕ ПОРОГИ (отталкивайся от них, но имеешь право скорректировать):
- ВЕЗЁМ:    маржа ≥ {DEFAULT_MIN_MARGIN_PERCENT:.0f}% И прибыль за MOQ ≥ {int(DEFAULT_MIN_MARGIN_TOTAL_RUB):,} ₽
- ИЗУЧИТЬ:  30% ≤ маржа < {DEFAULT_MIN_MARGIN_PERCENT:.0f}%  ИЛИ маржа выше, но прибыль < {int(DEFAULT_MIN_MARGIN_TOTAL_RUB):,} ₽
- НЕ ВЕЗЁМ: маржа < 30%

МЯГКИЕ КОРРЕКТИРОВКИ:
- Перегретость по Авито: если объявлений >5000 при марже <70% — опусти вердикт
  на ступень (ВЕЗЁМ → ИЗУЧИТЬ, ИЗУЧИТЬ → НЕ ВЕЗЁМ): рынок насыщен, продать
  сложно даже при бумажной марже.
- Слабый спрос: если запросов <500/мес — опусти вердикт на ступень: товар
  не ищут.
- Дефицит: если объявлений <200 при марже 30-50% — можешь поднять до ВЕЗЁМ:
  низкая конкуренция компенсирует среднюю маржу.
- Источник цены = «heuristic» означает, что Авито пуст и цена прикинута:
  такие вердикты помечай в reason припиской «цена прикинута».

Верни СТРОГО JSON:
{{
  "verdicts": [
    {{"index": 0, "verdict": "ВЕЗЁМ", "reason": "Маржа 92%, прибыль 4.2M₽ за MOQ. Авито: 5293 объявл., но спрос 8500/мес — берём."}},
    {{"index": 1, "verdict": "НЕ ВЕЗЁМ", "reason": "Маржа 12% — ниже порога 30%."}}
  ]
}}

ВАЖНО: верни ровно {len(products)} вердиктов (индексы 0..{len(products)-1}).
Никакого текста снаружи JSON. Никаких ```. Только объект.
"""


def _try_one_model(model: str, products: List[dict]) -> Tuple[Optional[List[dict]], str]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_prompt(products)},
        ],
        "temperature": 0.3,
        "max_tokens": 1500,
    }
    try:
        resp = requests.post(
            f"{OPENROUTER_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://nicheparser.local",
                "X-Title": "NicheParser_China",
            },
            json=payload,
            timeout=45,
        )
    except Exception as e:
        return None, f"network: {e}"

    if resp.status_code == 429:
        return None, "rate-limited upstream (429)"
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}: {resp.text[:200]}"

    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
    except Exception as e:
        return None, f"bad response shape: {e}"

    parsed = _parse_json_block(content)
    if parsed is None or not isinstance(parsed.get("verdicts"), list):
        return None, f"не JSON или нет verdicts: {content[:120]}"

    return parsed["verdicts"], ""


def _apply_decisions(products: List[dict], decisions: List[dict]) -> None:
    """Привязать вердикты к продуктам по index. Пропущенные — арифметика."""
    by_index = {}
    for d in decisions:
        if not isinstance(d, dict):
            continue
        try:
            idx = int(d.get("index"))
        except (TypeError, ValueError):
            continue
        by_index[idx] = d

    for i, p in enumerate(products):
        d = by_index.get(i)
        if d is None:
            _apply_arithmetic(p, note="LLM не вернул решение для индекса")
            continue
        verdict = str(d.get("verdict", "")).strip()
        if verdict not in _VALID_VERDICTS:
            _apply_arithmetic(p, note=f"LLM вернул невалидный вердикт «{verdict[:30]}»")
            continue
        reason = str(d.get("reason", "")).strip()
        p["verdict"] = verdict
        p["verdict_reason"] = reason[:240] or "без обоснования"
        p["verdict_source"] = "llm"

    logged = {
        v: sum(1 for p in products if p.get("verdict") == v)
        for v in _VALID_VERDICTS
    }
    logger.info(f"Agent 7: вердикты {logged}")


# ============ Арифметика-fallback ============

def _apply_arithmetic(p: dict, note: str = "") -> None:
    """Чистая арифметика по тем же порогам, что и LLM."""
    margin = float(p.get("ved_margin_percent") or 0)
    profit_moq = float(p.get("ved_margin_per_moq_rub") or 0)

    if margin >= DEFAULT_MIN_MARGIN_PERCENT and profit_moq >= DEFAULT_MIN_MARGIN_TOTAL_RUB:
        verdict = "ВЕЗЁМ"
        reason = (
            f"Маржа {margin:.0f}% и прибыль {int(profit_moq):,} ₽ за MOQ — "
            f"оба порога пройдены."
        )
    elif margin >= 30:
        verdict = "ИЗУЧИТЬ"
        if margin >= DEFAULT_MIN_MARGIN_PERCENT:
            reason = (
                f"Маржа {margin:.0f}% хорошая, но прибыль {int(profit_moq):,} ₽ "
                f"за MOQ ниже порога {int(DEFAULT_MIN_MARGIN_TOTAL_RUB):,} ₽."
            )
        else:
            reason = f"Маржа {margin:.0f}% — средняя, нужна доп. проверка."
    else:
        verdict = "НЕ ВЕЗЁМ"
        reason = f"Маржа {margin:.0f}% ниже минимума 30%."

    if note:
        reason = f"{reason} ({note})"

    p["verdict"] = verdict
    p["verdict_reason"] = reason.replace(",", " ")
    p["verdict_source"] = "arithmetic"


# ============ Парсинг JSON ============

def _parse_json_block(content: str) -> Optional[dict]:
    text = content.strip()
    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0]
    elif "```" in text:
        text = text.split("```", 1)[1].split("```", 1)[0]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
