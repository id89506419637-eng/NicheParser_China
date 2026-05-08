"""
NicheParser_China — Agent 3: Niche Filter (LLM)
По полному списку товаров с частотностями LLM решает, какие имеет смысл
тащить дальше через дорогие шаги (Alibaba/ВЭД), а какие отбросить.

Каждому товару добавляются поля:
  keep: bool       — оставлять (True) или отбросить (False)
  filter_reason:   — краткое объяснение решения (1 предложение)

Если LLM недоступен — просто помечаем все как keep=True с причиной
«фильтр недоступен» (мы не блокируем дальнейший пайплайн).
"""

import json
import logging
from typing import List, Optional, Tuple

import requests

from core.config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL, AI_MODEL

logger = logging.getLogger(__name__)


_FALLBACK_MODELS = [
    "openai/gpt-oss-120b:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "z-ai/glm-4.5-air:free",
    "minimax/minimax-m2.5:free",
]


SYSTEM_PROMPT = (
    "Ты — эксперт по B2B-импорту из Китая в РФ в 2026 году. Тебе дают список "
    "товаров-гипотез с месячной частотностью в Яндексе. Решаешь, по каким "
    "имеет смысл тратить время на дорогую проверку (Alibaba, ВЭД-расчёт), а "
    "по каким — нет. Отвечаешь строго в формате JSON, без пояснений снаружи."
)


def _build_prompt(products: List[dict]) -> str:
    lines = []
    for i, p in enumerate(products):
        title = p.get("title_ru") or p.get("title_en") or "?"
        freq = p.get("frequency", 0)
        lines.append(f"{i}. {title} — {freq} запр/мес")
    products_block = "\n".join(lines)

    return f"""Список товаров-гипотез:

{products_block}

Задача: для КАЖДОГО товара по индексу определи keep (true/false) и reason.

Отбрасывай (keep=false), если:
- Перегретый мейнстрим: все везут с 2022, конкуренция огромная, маржа выжата
  (например: «солнечная панель 400 вт» для дачи, обычные шуруповёрты,
  бытовые роботы-пылесосы для квартир).
- Спроса фактически нет (<300 запр/мес) И ниша не премиально-узкая.
- Это не B2B (попал ритейл, одежда, косметика, БАДы).
- Жёсткая локализация в РФ (бетон, кирпич, песок — везти из Китая нет смысла).
- Сильный регуляторный риск 2026: товары двойного назначения, медтехника
  с обязательной регистрацией МЗ РФ для конечного пользования.

Оставляй (keep=true), если:
- B2B-товар с разумным спросом (1k–60k запр/мес) и не пик хайпа.
- Узкоспециализированный товар с малым спросом (<3k), но высокой ценой
  и маржей — нишевая премиальная.
- Дефицит-товар после ухода европейских/американских брендов 2022+.

Верни СТРОГО JSON:
{{
  "decisions": [
    {{"index": 0, "keep": true, "reason": "B2B-дефицит после ухода Haas/DMG, маржа высокая"}},
    {{"index": 1, "keep": false, "reason": "Перегрет — массово везут с 2022, маржа ~5%"}}
  ]
}}

ВАЖНО: верни решения для ВСЕХ {len(products)} товаров (все индексы 0..{len(products)-1}).
Reason — короткое предложение, до 90 символов.
Никакого текста снаружи JSON. Никаких ```. Только объект.
"""


def filter_niches(products: List[dict]) -> List[dict]:
    """
    Прогнать список через LLM-фильтр. Каждому продукту приписываются keep
    и filter_reason. Не выкидывает из списка — обогащает.
    """
    if not products:
        return []

    if not OPENROUTER_API_KEY:
        logger.warning("Agent 3: нет OPENROUTER_API_KEY — пропускаю фильтр")
        return _passthrough(products, "фильтр недоступен (нет API-ключа)")

    seen = set()
    chain: List[str] = []
    for m in [AI_MODEL] + _FALLBACK_MODELS:
        if m and m not in seen:
            seen.add(m)
            chain.append(m)

    last_error = ""
    for model in chain:
        decisions, err = _try_one_model(model, products)
        if decisions is not None:
            return _apply_decisions(products, decisions)
        last_error = err
        logger.warning(f"Agent 3: модель {model} не сработала ({err}) — пробую следующую")

    logger.error(f"Agent 3: все модели упали ({last_error}) — пропускаю фильтр")
    return _passthrough(products, "фильтр упал — все модели заняты")


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
    if parsed is None or not isinstance(parsed.get("decisions"), list):
        return None, f"не JSON или нет decisions: {content[:120]}"

    return parsed["decisions"], ""


def _apply_decisions(products: List[dict], decisions: List[dict]) -> List[dict]:
    """Привязать decisions к продуктам по index. Без решения — keep=True."""
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
            p["keep"] = True
            p["filter_reason"] = "решения от LLM не пришло — оставляем"
        else:
            p["keep"] = bool(d.get("keep", True))
            reason = str(d.get("reason", "")).strip()
            p["filter_reason"] = reason or ("оставлено" if p["keep"] else "отброшено без причины")

    kept = sum(1 for p in products if p.get("keep"))
    logger.info(f"Agent 3: оставлено {kept}/{len(products)}")
    return products


def _passthrough(products: List[dict], reason: str) -> List[dict]:
    for p in products:
        p["keep"] = True
        p["filter_reason"] = reason
    return products


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
