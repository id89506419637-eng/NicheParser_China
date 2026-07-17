"""
NicheParser_China — Agent 8: Supplier Audit (Скоринг поставщика)
По данным Alibaba-оффера (рейтинг, число сделок, сертификаты, цена)
оценивает надёжность поставщика и выявляет красные флаги.

Работает в два слоя:
  1. Арифметический скор по формуле из docs/supplier_audit_kb.md
  2. LLM поверх — добавляет контекстные красные флаги и рекомендации

Если LLM упал — fallback на чистую арифметику, пайплайн не блокируется.

К каждому продукту (keep=True, есть alibaba_offers) прикрепляется:
  supplier_score:              int     — композитный балл 0–100
  supplier_red_flags:          list[str] — обнаруженные красные флаги
  supplier_risk_level:         str     — «низкий» / «средний» / «высокий»
  supplier_audit_recommendation: str  — что проверить вручную (1–2 предложения)
  supplier_audit_source:       str     — «llm» / «arithmetic»
"""

import json
import logging
from typing import List, Optional, Tuple

import requests

from core.config import (
    OPENROUTER_API_KEY, OPENROUTER_BASE_URL, AI_MODEL,
    OPENROUTER_FALLBACK_MODELS as _FALLBACK_MODELS,
)

logger = logging.getLogger(__name__)

_VALID_RISK_LEVELS = ("низкий", "средний", "высокий")


SYSTEM_PROMPT = (
    "Ты — эксперт по проверке китайских B2B-поставщиков для импорта в РФ "
    "в 2026 году. По данным об оффере с Alibaba оцениваешь надёжность "
    "поставщика и выявляешь красные флаги. Отвечаешь СТРОГО в формате JSON, "
    "без пояснений снаружи."
)


# ============ Арифметический скор ============

def _calc_arithmetic_score(offer: dict) -> int:
    """
    Формула из docs/supplier_audit_kb.md:
      supplier_rating * 15          → 0–75  (рейтинг 0–5)
      min(deals_count / 10, 10)     → 0–10  (за каждые 10 сделок)
      len(certificates) * 3         → 0–15  (CE/ISO/RoHS/FDA/FCC)
    Итого: 0–100.
    """
    rating = float(offer.get("supplier_rating") or 0)
    deals = int(offer.get("deals_count") or 0)
    certs = offer.get("certificates") or []
    if isinstance(certs, str):
        certs = [c.strip() for c in certs.split(",") if c.strip()]

    score = rating * 15 + min(deals / 10, 10) + len(certs) * 3
    return max(0, min(100, int(round(score))))


def _arithmetic_red_flags(offer: dict, product: dict) -> List[str]:
    """Детектируем красные флаги чисто по данным, без LLM."""
    flags: List[str] = []
    rating = float(offer.get("supplier_rating") or 0)
    deals = int(offer.get("deals_count") or 0)
    certs = offer.get("certificates") or []
    if isinstance(certs, str):
        certs = [c.strip() for c in certs.split(",") if c.strip()]
    price_min = float(offer.get("price_usd_min") or 0)
    price_max = float(offer.get("price_usd_max") or 0)

    if rating < 4.0:
        flags.append(f"Низкий рейтинг поставщика ({rating:.1f}/5.0)")
    if deals < 10:
        flags.append(f"Мало завершённых сделок ({deals})")
    if not certs:
        flags.append("Нет сертификатов (CE/ISO/RoHS)")
    if price_min > 0 and price_max > 0 and price_max > price_min * 3:
        flags.append(f"Огромный разброс цен (${price_min:.0f}–${price_max:.0f}) — возможна подмена товара")
    if rating == 0 and deals == 0:
        flags.append("Нет рейтинга и сделок — возможно новый или фиктивный поставщик")

    return flags


def _risk_from_score(score: int) -> str:
    if score >= 70:
        return "низкий"
    elif score >= 40:
        return "средний"
    return "высокий"


def _arithmetic_recommendation(score: int, flags: List[str]) -> str:
    if score >= 70 and not flags:
        return "Базовые показатели в норме. Запросите образец и Business License перед первым заказом."
    if score >= 40:
        return "Средний скор — рекомендуется видеозвонок на производство и проверка лицензии через GSXT.gov.cn."
    return "Высокий риск — обязательна инспекция SGS/Intertek перед оплатой. Не переводите 100% предоплату."


# ============ LLM-путь ============

def _build_prompt(eligible: List[dict]) -> str:
    lines = []
    for i, (p, offer) in enumerate(eligible):
        title = p.get("title_ru") or p.get("title_en") or "?"
        rating = float(offer.get("supplier_rating") or 0)
        deals = int(offer.get("deals_count") or 0)
        certs = offer.get("certificates") or []
        if isinstance(certs, str):
            certs = [c.strip() for c in certs.split(",") if c.strip()]
        price_min = float(offer.get("price_usd_min") or 0)
        price_max = float(offer.get("price_usd_max") or 0)
        moq = int(offer.get("moq") or 0)
        arith_score = _calc_arithmetic_score(offer)

        lines.append(
            f"{i}. {title}\n"
            f"   Alibaba: рейтинг {rating}/5, сделок {deals}, "
            f"серт-ы [{', '.join(certs) if certs else 'нет'}]\n"
            f"   Цена ${price_min:.2f}–${price_max:.2f}, MOQ {moq}\n"
            f"   Арифметический скор: {arith_score}/100"
        )
    block = "\n".join(lines)

    return f"""Дано {len(eligible)} поставщиков с Alibaba. По каждому есть данные оффера.

{block}

ЗАДАЧА: для КАЖДОГО поставщика по индексу 0..{len(eligible)-1} выдай:
  score:          int 0–100 (можешь скорректировать арифметический скор)
  risk_level:     одно из «низкий», «средний», «высокий»
  red_flags:      список строк — конкретные красные флаги (0–5 шт)
  recommendation: что проверить вручную перед закупкой (1–2 предложения, до 200 символов)

КРАСНЫЕ ФЛАГИ (проверяй каждый):
- Рейтинг ниже 4.0 — ненадёжный поставщик
- Мало сделок (<10) — неопытный или новый
- Нет сертификатов CE/ISO/RoHS — продукция может не пройти таможню
- Цена на 30%+ ниже рынка — возможна экономия на материалах
- Огромный разброс цен — подмена товара в зависимости от заказа
- MOQ = 1 при промышленном товаре — скорее торговая компания, не завод
- Отсутствие реального рейтинга при заявленном большом опыте

РЕКОМЕНДАЦИИ должны быть конкретными:
- Запросить Business License и проверить через GSXT.gov.cn
- Видеозвонок на производство
- Заказать образец перед партией
- Нанять инспектора SGS/Intertek/Bureau Veritas
- Проверить домен сайта через WHOIS

Верни СТРОГО JSON:
{{
  "audits": [
    {{"index": 0, "score": 72, "risk_level": "низкий", "red_flags": ["Нет сертификата ISO"], "recommendation": "Запросить Business License и образец перед первым заказом."}},
    {{"index": 1, "score": 35, "risk_level": "высокий", "red_flags": ["Рейтинг 3.2/5", "Всего 3 сделки"], "recommendation": "Обязательна инспекция SGS. Не переводить 100% предоплату."}}
  ]
}}

ВАЖНО: верни ровно {len(eligible)} записей (индексы 0..{len(eligible)-1}).
Никакого текста снаружи JSON. Никаких ```. Только объект.
"""


def _try_one_model(model: str, eligible: List[dict]) -> Tuple[Optional[List[dict]], str]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_prompt(eligible)},
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
    if parsed is None or not isinstance(parsed.get("audits"), list):
        return None, f"не JSON или нет audits: {content[:120]}"

    return parsed["audits"], ""


# ============ Главная функция ============

def audit_suppliers(products: List[dict]) -> List[dict]:
    """
    Скоринг поставщиков для товаров с keep=True и alibaba_offers.
    Каждому такому продукту приписываются supplier_score / supplier_red_flags /
    supplier_risk_level / supplier_audit_recommendation / supplier_audit_source.
    """
    if not products:
        return []

    # Собираем пары (product, лучший оффер) для тех, кого есть смысл проверять
    eligible: List[Tuple[dict, dict]] = []
    for p in products:
        if not p.get("keep", True):
            _attach_empty(p, "дроп на этапе фильтра")
            continue
        offers = p.get("alibaba_offers") or []
        if not offers:
            _attach_empty(p, "нет офферов Alibaba")
            continue
        # Берём тот же оффер, что использовал Агент 5 (ВЭД) — по url,
        # или первый если url не совпал.
        ved_offer = p.get("ved_best_offer") or {}
        best = next(
            (o for o in offers if o.get("product_url") == ved_offer.get("url")),
            offers[0],
        )
        eligible.append((p, best))

    if not eligible:
        return products

    # Пробуем LLM
    if OPENROUTER_API_KEY:
        seen = set()
        chain: List[str] = []
        for m in [AI_MODEL] + _FALLBACK_MODELS:
            if m and m not in seen:
                seen.add(m)
                chain.append(m)

        for model in chain:
            decisions, err = _try_one_model(model, eligible)
            if decisions is not None:
                _apply_llm_decisions(eligible, decisions)
                logger.info(f"Agent 8: LLM-скоринг {len(eligible)} поставщиков через {model}")
                return products
            logger.warning(f"Agent 8: модель {model} не сработала ({err}) — пробую следующую")

        logger.error("Agent 8: все модели упали — fallback на арифметику")
    else:
        logger.warning("Agent 8: нет OPENROUTER_API_KEY — использую арифметику")

    # Fallback: чистая арифметика
    for p, offer in eligible:
        _apply_arithmetic(p, offer)

    return products


def _apply_llm_decisions(eligible: List[Tuple[dict, dict]], decisions: List[dict]) -> None:
    """Привязать LLM-решения к продуктам. Пропущенные — арифметика."""
    by_index = {}
    for d in decisions:
        if not isinstance(d, dict):
            continue
        try:
            idx = int(d.get("index"))
        except (TypeError, ValueError):
            continue
        by_index[idx] = d

    for i, (p, offer) in enumerate(eligible):
        d = by_index.get(i)
        if d is None:
            _apply_arithmetic(p, offer, note="LLM не вернул решение для индекса")
            continue

        score = d.get("score")
        if score is not None:
            try:
                score = max(0, min(100, int(score)))
            except (TypeError, ValueError):
                score = _calc_arithmetic_score(offer)
        else:
            score = _calc_arithmetic_score(offer)

        risk = str(d.get("risk_level", "")).strip().lower()
        if risk not in _VALID_RISK_LEVELS:
            risk = _risk_from_score(score)

        flags = d.get("red_flags") or []
        if not isinstance(flags, list):
            flags = [str(flags)]
        flags = [str(f).strip() for f in flags if str(f).strip()][:5]

        rec = str(d.get("recommendation", "")).strip()
        if not rec:
            rec = _arithmetic_recommendation(score, flags)

        p["supplier_score"] = score
        p["supplier_red_flags"] = flags
        p["supplier_risk_level"] = risk
        p["supplier_audit_recommendation"] = rec[:300]
        p["supplier_audit_source"] = "llm"

    logged = {
        level: sum(1 for p, _ in eligible if p.get("supplier_risk_level") == level)
        for level in _VALID_RISK_LEVELS
    }
    logger.info(f"Agent 8: риски {logged}")


def _apply_arithmetic(p: dict, offer: dict, note: str = "") -> None:
    """Чистая арифметика без LLM."""
    score = _calc_arithmetic_score(offer)
    flags = _arithmetic_red_flags(offer, p)
    risk = _risk_from_score(score)
    rec = _arithmetic_recommendation(score, flags)

    if note:
        rec = f"{rec} ({note})"

    p["supplier_score"] = score
    p["supplier_red_flags"] = flags
    p["supplier_risk_level"] = risk
    p["supplier_audit_recommendation"] = rec
    p["supplier_audit_source"] = "arithmetic"


def _attach_empty(p: dict, reason: str) -> None:
    p["supplier_score"] = 0
    p["supplier_red_flags"] = []
    p["supplier_risk_level"] = "высокий"
    p["supplier_audit_recommendation"] = reason
    p["supplier_audit_source"] = "skip"


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
