"""
NicheParser_China — Agent 1: Product Generator
По нише от пользователя ("станки", "медоборудование" и т.п.) генерит 5–10
конкретных B2B-продуктов через OpenRouter LLM.

Output: list[dict] — [{"title_ru": ..., "title_en": ..., "rationale": ...}, ...]
"""

import json
import logging
import re
from typing import List, Optional

import requests

from core.config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL, AI_MODEL

logger = logging.getLogger(__name__)

# Fallback-цепочка free-моделей разных провайдеров. Если один upstream
# перегружен (429), Агент 1 идёт по списку дальше. AI_MODEL из .env пробуется
# первым. Имена сверены с GET /api/v1/models на 2026-05.
_FALLBACK_MODELS = [
    "openai/gpt-oss-120b:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "z-ai/glm-4.5-air:free",
    "minimax/minimax-m2.5:free",
]


SYSTEM_PROMPT = (
    "Ты — эксперт по ВЭД и анализу B2B-ниш для импорта из Китая в Россию в 2026 году. "
    "Учитываешь дефицит после 2022, рост локального производства, санкционные риски. "
    "Отвечаешь СТРОГО в формате JSON, без пояснений снаружи."
)


def _build_prompt(niche: str) -> str:
    return f"""Пользователь задал нишу: "{niche}".

Сгенерируй 5–10 РАЗНЫХ типов B2B-товаров внутри этой ниши, которые имеет смысл
рассматривать для импорта из Китая в РФ в 2026 году.

КРИТИЧЕСКИ ВАЖНО — НЕ ДУБЛИРУЙ:
- Каждая запись = РАЗНЫЙ ТИП товара, а не разные марки/бренды/модели одного и того же.
- Плохо (так делать НЕЛЬЗЯ): "Эпоксидный клей EP-20", "Эпоксидный клей EP-21",
  "Эпоксидный клей Henkel", "Эпоксидный клей 3M" — это всё ОДИН товар разных марок,
  его нужно объединить в ОДНУ запись.
- Хорошо: 1) "Эпоксидный клей промышленный (марки EP-20, EP-21, Henkel)",
  2) "Полиуретановый герметик", 3) "Цианоакрилатный клей" — это РАЗНЫЕ типы.
- Если внутри типа товара есть популярные марки/модификации — упомяни их в скобках
  в одной строке title_ru, не плоди дубликаты.

Остальные правила:
- Товары должны реально искаться на Яндексе и Алибабе (конкретные, не абстракции).
- B2B-сегмент: для бизнеса, производства, строительства — не массовый ритейл
  (одежда, косметика, БАДы).
- Избегай товаров с санкциями / двойным назначением.
- title_en — точное англоязычное название как ищут на Alibaba.
- rationale — 1 короткое предложение, почему этот товар имеет смысл для РФ.
- wordstat_query — КОРОТКИЙ поисковый запрос для Яндекс.Wordstat: 2–4 слова,
  без скобок, без марок, без перечислений. Это то, что реальный покупатель
  вбивает в Яндекс. Пример: для title_ru «Эпоксидный клей промышленный
  (марки EP-20, Henkel)» wordstat_query = «эпоксидный клей промышленный».

Верни СТРОГО JSON по схеме:
{{
  "products": [
    {{
      "title_ru": "ЧПУ-фрезерный станок по металлу (3-осевой)",
      "title_en": "CNC milling machine for metal",
      "wordstat_query": "чпу фрезерный станок",
      "rationale": "Дефицит после ухода европейских брендов, спрос от малых цехов."
    }},
    ...
  ]
}}

Никакого текста снаружи JSON. Никаких ```. Только объект.
"""


def generate_products(niche: str) -> List[dict]:
    """
    По строке-нише вернуть 5-10 продуктов. При 429/ошибке проходит по
    fallback-цепочке моделей. Возвращает [] если все модели упали.
    """
    niche = (niche or "").strip()
    if not niche:
        return []

    if not OPENROUTER_API_KEY:
        logger.warning("Agent 1: нет OPENROUTER_API_KEY")
        return []

    # AI_MODEL первым, остальные free — fallback. Без дублей, в порядке.
    seen = set()
    chain: List[str] = []
    for m in [AI_MODEL] + _FALLBACK_MODELS:
        if m and m not in seen:
            seen.add(m)
            chain.append(m)

    last_error = ""
    for model in chain:
        result, err = _try_one_model(model, niche)
        if result is not None:
            if err:  # сменили модель из-за upstream — записываем в лог
                logger.info(f"Agent 1: получили ответ от fallback-модели {model}")
            return result
        last_error = err
        logger.warning(f"Agent 1: модель {model} не сработала ({err}) — пробую следующую")

    logger.error(f"Agent 1: все модели упали, последняя ошибка: {last_error}")
    return []


def _try_one_model(model: str, niche: str) -> tuple[Optional[List[dict]], str]:
    """
    Один заход на одну модель. Возвращает (products, error_msg).
    products = None означает «не удалось, иди дальше по fallback-цепочке».
    products = [] означает «модель ответила, но нормализованный список пуст»
    (тоже не успех, но вызывающий уже не пробует другие).
    """
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_prompt(niche)},
        ],
        "temperature": 0.6,
        "max_tokens": 1200,
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
    if parsed is None:
        return None, f"не JSON: {content[:120]}"

    return _normalize(parsed), ""


def _parse_json_block(content: str) -> Optional[dict]:
    """Извлекает JSON из ответа AI, прощая ```json ... ``` обёртку и текст вокруг."""
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


def _normalize(data: dict) -> List[dict]:
    raw = data.get("products") or []
    if not isinstance(raw, list):
        return []
    out: List[dict] = []
    for item in raw[:10]:
        if not isinstance(item, dict):
            continue
        title_ru = str(item.get("title_ru", "")).strip()
        title_en = str(item.get("title_en", "")).strip()
        rationale = str(item.get("rationale", "")).strip()
        wordstat_query = str(item.get("wordstat_query", "")).strip().lower()
        if not title_ru and not title_en:
            continue
        # Если LLM не вернул wordstat_query — соберём из title_ru: убираем скобки и хвост.
        if not wordstat_query:
            wordstat_query = _strip_for_wordstat(title_ru or title_en)
        out.append({
            "title_ru": title_ru or title_en,
            "title_en": title_en or title_ru,
            "wordstat_query": wordstat_query,
            "rationale": rationale,
        })
    return out


def _strip_for_wordstat(text: str) -> str:
    """«Эпоксидный клей промышленный (марки EP-20, Henkel)» → «эпоксидный клей промышленный»."""
    s = re.sub(r"\([^)]*\)", " ", text)  # убрать всё в скобках
    s = re.sub(r"\s+", " ", s).strip().lower()
    # Ограничим до 5 слов — Wordstat не любит длинные хвосты
    parts = s.split()
    return " ".join(parts[:5])
