"""
NicheParser_China — Agent 0A: Industry Explorer
По выбранной индустрии («стройка-отделка», «мебельное производство», ...)
LLM генерирует 30-50 «гипотез о свободных нишах» — товарах, которые массово
используются в Китае для автоматизации труда, но ещё не пришли или редки в РФ.

Это РАЗВЕДКА, не финальный анализ. Каждая гипотеза:
  niche_name      — название ниши/товара
  pain            — какая боль профессии решается
  china_solution  — какое решение есть в Китае (с примерной ценой)
  why_free        — почему этой ниши ещё нет/мало в РФ
  llm_confidence  — «высокая» / «средняя» / «низкая»

В Wave 5B каждая гипотеза будет пропускаться через Agent 0B (Critic).
В Wave 5C — через 7-факторный скоринг.
"""

import json
import logging
import uuid
from datetime import datetime
from typing import List, Optional, Tuple

import requests

from core.config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL, AI_MODEL
from core.models import Hypothesis

logger = logging.getLogger(__name__)


_FALLBACK_MODELS = [
    "openai/gpt-oss-120b:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "z-ai/glm-4.5-air:free",
    "minimax/minimax-m2.5:free",
]

_VALID_CONFIDENCES = ("высокая", "средняя", "низкая")


# Стартовый список индустрий (по плану v3, расширим после первых прогонов).
INDUSTRIES = {
    "стройка-отделка": {
        "label": "Стройка и отделка",
        "context": (
            "Российские стройплощадки и бригады отделочников после 2022 года потеряли "
            "доступ к европейскому оборудованию (Wagner, Hilti, Festool) или цены на "
            "него выросли в 2-3 раза. Многие работы (штукатурка, покраска, укладка "
            "плитки, гипсокартон) до сих пор делаются вручную или морально устаревшим "
            "инструментом. В Китае массово производятся автоматизированные станции "
            "Wagner-клонов, лазерные сварочные аппараты, плиточные машины — но в РФ "
            "они или дорогие через дилеров, или их вообще нет на Авито."
        ),
        "typical_areas": (
            "штукатурные станции, покрасочное оборудование, лазерные сварочные, "
            "плиточные роботы, гипсокартонные подъёмники, безвоздушные распылители, "
            "пневмо- и аккумуляторный профессиональный инструмент, лазерные уровни, "
            "тепловизоры, измерительная техника"
        ),
    },
    "мебельное-производство": {
        "label": "Мебельное производство",
        "context": (
            "В РФ много мелких и средних мебельных цехов (кухни, корпусная, мягкая "
            "мебель). После ухода итальянских и немецких поставщиков оборудования "
            "цеха либо платят втридорога через серый импорт, либо работают на старом "
            "советском оборудовании. Китайские станки — раскроечные, кромкооблицовочные, "
            "присадочные — есть массово, но в РФ часто только под заказ через "
            "ограниченное число дилеров."
        ),
        "typical_areas": (
            "раскроечные центры, кромкооблицовочные станки, присадочные станки, "
            "ЧПУ-фрезеры для дерева, прессы для шпона, шлифовальное оборудование, "
            "распиловка с ЧПУ, упаковочное оборудование, фурнитурные комплекты, "
            "механизмы трансформации для мягкой мебели"
        ),
    },
    "металлообработка": {
        "label": "Металлообработка (малые цеха)",
        "context": (
            "Самый болезненный сегмент после 2022 — европейские станки (DMG MORI, "
            "Trumpf, Bystronic) либо недоступны, либо стоят в 2-3 раза дороже. "
            "Малые цеха не тянут такие бюджеты. Китайские аналоги — лазерные резаки, "
            "ЧПУ-фрезеры, токарные — есть массово и в 3-5 раз дешевле, но в РФ "
            "представлены через узкий круг дилеров с большой наценкой."
        ),
        "typical_areas": (
            "лазерные резаки по металлу, ЧПУ-фрезерные станки, токарные с ЧПУ, "
            "плазменная резка, гибочные пресс-листы, ленточно-пильные станки, "
            "сварочные полуавтоматы, лазерная сварка, шлифовальное оборудование, "
            "координатно-измерительные машины малого формата"
        ),
    },
    "деревообработка": {
        "label": "Деревообработка",
        "context": (
            "Малые и средние пилорамы, столярные мастерские, производство срубов и "
            "погонажа. Европейское оборудование (SCM, Weinig, Biesse) — премиум, "
            "часто не по карману российскому малому бизнесу даже до санкций. "
            "Китайские аналоги массовые и в разы дешевле — но в РФ их часто либо нет, "
            "либо завозят под заказ единичные дилеры."
        ),
        "typical_areas": (
            "ленточные пилорамы, четырёхсторонние станки, рейсмусы, фуганки, "
            "ЧПУ-фрезеры по дереву, сушильные камеры, сращивающие линии, "
            "шипорезное оборудование, окрасочные камеры для дерева, "
            "оборудование для производства срубов"
        ),
    },
}


SYSTEM_PROMPT = (
    "Ты — эксперт по импорту B2B-оборудования из Китая в Россию в 2026 году. "
    "Знаешь рынок РФ после 2022 года: где образовались дефициты, где европейские "
    "бренды ушли или подорожали в разы, где малый бизнес ищет более дешёвые "
    "альтернативы. Знаешь Китай: какие категории оборудования там массовые и "
    "доступные на Alibaba/1688. Отвечаешь СТРОГО в формате JSON, без пояснений "
    "снаружи."
)


def explore_industry(industry_key: str) -> Tuple[List[Hypothesis], str, str]:
    """
    Сгенерировать гипотезы по выбранной индустрии.
    Возвращает (список гипотез, batch_id, source) где source = 'llm' или 'error'.
    Гипотезы НЕ сохраняются в БД здесь — это делает вызывающий код.
    """
    if industry_key not in INDUSTRIES:
        logger.error(f"Agent 0A: неизвестная индустрия '{industry_key}'")
        return [], "", "error"

    if not OPENROUTER_API_KEY:
        logger.warning("Agent 0A: нет OPENROUTER_API_KEY")
        return [], "", "error"

    industry_meta = INDUSTRIES[industry_key]
    batch_id = uuid.uuid4().hex[:12]
    now = datetime.now().isoformat()

    # Чейн моделей: AI_MODEL → fallback
    seen = set()
    chain: List[str] = []
    for m in [AI_MODEL] + _FALLBACK_MODELS:
        if m and m not in seen:
            seen.add(m)
            chain.append(m)

    last_error = ""
    for model in chain:
        raw_hypotheses, err = _try_one_model(model, industry_key, industry_meta)
        if raw_hypotheses is not None:
            hyps = _normalize(raw_hypotheses, industry_key, batch_id, now)
            logger.info(f"Agent 0A: {len(hyps)} гипотез по '{industry_key}' через {model}")
            return hyps, batch_id, "llm"
        last_error = err
        logger.warning(f"Agent 0A: модель {model} не сработала ({err}) — пробую следующую")

    logger.error(f"Agent 0A: все модели упали ({last_error})")
    return [], batch_id, "error"


# ============ LLM-путь ============

def _build_prompt(industry_key: str, meta: dict) -> str:
    return f"""Индустрия: «{meta['label']}»

Контекст рынка РФ в 2026 году:
{meta['context']}

Типичные подсегменты этой индустрии:
{meta['typical_areas']}

ЗАДАЧА: сгенерируй 30-40 гипотез о СВОБОДНЫХ НИШАХ — товарах/оборудовании,
которые подходят под критерии:
  • МАССОВО используются в Китае (можно найти на Alibaba десятки фабрик)
  • ЕЩЁ НЕ или МАЛО представлены в РФ (нет на Авито или единичные объявления
    по сильно завышенной цене через дилеров)
  • Решают РЕАЛЬНУЮ боль профессии: ускоряют работу, заменяют ручной труд,
    дешевле западных аналогов в 2-5 раз

Каждая гипотеза = РАЗНЫЙ ТИП оборудования, не разные модели одного и того же.

КРИТИЧЕСКИ ВАЖНО — формулировки:
- niche_name: конкретное название категории товара (НЕ бренд!), 2-6 слов
- pain: какую боль профессии этот товар решает в 1-2 предложениях
- china_solution: что есть в Китае + примерный диапазон цен в $ на Alibaba
- why_free: почему этой ниши мало/нет в РФ — короткий конкретный аргумент
- llm_confidence: твоя уверенность в гипотезе («высокая» / «средняя» / «низкая»)

ПЛОХО (не делать так):                ХОРОШО (так делать):
«Покрасочное оборудование»            «Безвоздушные покрасочные станции для фасадов»
«Решает проблему»                     «Маляры красят валиками — 3 человекодня на 100 м²»
«Дешевле»                             «Wagner-клоны от $800 vs европейские $5000»
«Нет в РФ»                            «На Авито 5-10 объявл. по 250-400к ₽ через дилеров»

llm_confidence ставь честно:
- «высокая» — точно знаешь, что в Китае массово, в РФ слабо представлено
- «средняя» — есть основания, но не уверен в текущей доступности в РФ
- «низкая» — гипотеза правдоподобная, но не проверял

Верни СТРОГО JSON:
{{
  "hypotheses": [
    {{
      "niche_name": "Безвоздушные покрасочные станции для фасадов",
      "pain": "Маляры красят валиками — 3 человекодня на 100 м² фасада",
      "china_solution": "Wagner-клоны на Alibaba от $800, десятки фабрик в Yongkang",
      "why_free": "В РФ только европейские Wagner/Graco от $5000 через дилеров",
      "llm_confidence": "высокая"
    }},
    ...
  ]
}}

ВАЖНО: верни 30-40 РАЗНЫХ гипотез. Не повторяйся. Не используй имена брендов
в niche_name (только в china_solution / why_free можно для контекста).
Никакого текста снаружи JSON. Никаких ```. Только объект.
"""


def _try_one_model(model: str, industry_key: str, meta: dict) -> Tuple[Optional[List[dict]], str]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_prompt(industry_key, meta)},
        ],
        "temperature": 0.7,  # выше чем у других агентов — нужна вариативность
        "max_tokens": 4000,  # 30-40 гипотез × ~80 токенов
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
            timeout=90,  # дольше — генерация большая
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
    if parsed is None or not isinstance(parsed.get("hypotheses"), list):
        return None, f"не JSON или нет hypotheses: {content[:120]}"

    return parsed["hypotheses"], ""


def _normalize(raw: List[dict], industry: str, batch_id: str, ts: str) -> List[Hypothesis]:
    """Очистить, дедуп по niche_name, ограничить длины строк."""
    out: List[Hypothesis] = []
    seen_names = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        niche_name = str(item.get("niche_name", "")).strip()
        if not niche_name or niche_name.lower() in seen_names:
            continue
        seen_names.add(niche_name.lower())

        confidence = str(item.get("llm_confidence", "")).strip().lower()
        if confidence not in _VALID_CONFIDENCES:
            confidence = "средняя"

        out.append(Hypothesis(
            batch_id=batch_id,
            industry=industry,
            niche_name=niche_name[:200],
            pain=str(item.get("pain", "")).strip()[:400],
            china_solution=str(item.get("china_solution", "")).strip()[:400],
            why_free=str(item.get("why_free", "")).strip()[:400],
            llm_confidence=confidence,
            created_at=ts,
        ))
    return out


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
