"""
NicheParser_China — Niche classifier (AI)
AI-классификация ниши через OpenRouter: тип, сезонность, боли предпринимателей.
"""

import json
import logging
import time
from typing import List, Optional

import requests

from core.config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL, AI_MODEL, NICHE_TYPES

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = (
    "Ты — эксперт по ВЭД (внешнеэкономической деятельности) и анализу товарных "
    "ниш для импорта из Китая в Россию в 2026 году. Учитываешь дефицит иностранных "
    "брендов после 2022, рост локального производства, санкционные ограничения. "
    "Отвечаешь строго в формате JSON, без пояснений снаружи."
)


def _build_prompt(keyword: str, category: str, frequency: int) -> str:
    types_str = " / ".join(NICHE_TYPES)
    return f"""Проанализируй товарную нишу для импорта из Китая в РФ.

Ключ: "{keyword}"
Категория: "{category}"
Частотность Яндекс Wordstat (запросов/мес): {frequency}

Верни СТРОГО JSON по схеме:
{{
  "niche_type": "один из: {types_str}",
  "is_seasonal": true или false (сезонный ли товар),
  "pain_points": [
    "Боль 1 — конкретная проблема, которую решает этот товар, либо типичная проблема предпринимателей в этой нише",
    "Боль 2 — ещё одна боль (2-4 штуки всего)",
    "..."
  ],
  "reasoning": "1-2 предложения почему именно такой тип ниши"
}}

Важно:
- niche_type обязан быть ОДНОЙ из перечисленных строк (с заглавными буквами как указано).
- pain_points — конкретные проблемы предпринимателей или конечных покупателей, 2-4 шт.
- is_seasonal=true только для реально сезонных (новогодние, пляжные, школьные и т.п.).
"""


def _fallback(keyword: str, reason: str) -> dict:
    return {
        "niche_type": "ВЫСОКИЙ СПРОС",
        "is_seasonal": False,
        "pain_points": [],
        "reasoning": f"AI недоступен ({reason})",
    }


def classify_niche(keyword: str, category: str = "", frequency: int = 0) -> dict:
    """
    Вернуть классификацию ниши: {niche_type, is_seasonal, pain_points, reasoning}.
    При любой ошибке отдаёт fallback, pipeline не ломается.
    """
    if not OPENROUTER_API_KEY:
        return _fallback(keyword, "нет OPENROUTER_API_KEY")

    payload = {
        "model": AI_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_prompt(keyword, category, frequency)},
        ],
        "temperature": 0.4,
        "max_tokens": 500,
    }

    # Задержка между запросами — OpenRouter free tier имеет лимит скорости
    time.sleep(6)

    for attempt in range(2):
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
                timeout=30,
            )
            if resp.status_code == 429:
                if attempt == 0:
                    logger.warning("OpenRouter 429 — жду 15 сек и повторяю...")
                    time.sleep(15)
                    continue
                return _fallback(keyword, "rate limit 429")

            if resp.status_code != 200:
                logger.error(f"OpenRouter {resp.status_code}: {resp.text[:200]}")
                return _fallback(keyword, f"HTTP {resp.status_code}")

            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            parsed = _parse_json_block(content)
            if parsed is None:
                return _fallback(keyword, "ответ AI не JSON")

            return _normalize(parsed)

        except Exception as e:
            logger.error(f"AI classify failed: {e}")
            return _fallback(keyword, str(e))


def _parse_json_block(content: str) -> Optional[dict]:
    """Ищет JSON в тексте AI-ответа, допуская ```json ... ``` обёртку."""
    text = content.strip()
    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0]
    elif "```" in text:
        text = text.split("```", 1)[1].split("```", 1)[0]

    # иногда AI ставит объяснение перед/после — ищем первый { ... }
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def _normalize(data: dict) -> dict:
    """Нормализация ответа AI под ожидаемые значения."""
    nt = str(data.get("niche_type", "")).strip().upper()
    if nt not in NICHE_TYPES:
        # Попытка сопоставить похожее
        for t in NICHE_TYPES:
            if t in nt:
                nt = t
                break
        else:
            nt = "ВЫСОКИЙ СПРОС"

    pains = data.get("pain_points") or []
    if not isinstance(pains, list):
        pains = [str(pains)]
    pains = [str(p).strip() for p in pains if str(p).strip()][:5]

    return {
        "niche_type": nt,
        "is_seasonal": bool(data.get("is_seasonal", False)),
        "pain_points": pains,
        "reasoning": str(data.get("reasoning", "")).strip(),
    }


def pain_points_as_json(pains: List[str]) -> str:
    return json.dumps(pains, ensure_ascii=False)
