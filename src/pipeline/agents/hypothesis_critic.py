"""
NicheParser_China — Agent 0B: Hypothesis Critic
По каждой гипотезе от Agent 0A второй LLM-агент пытается ДОКАЗАТЬ, что
гипотеза плохая. Это асимметричный фильтр: первый агент склонен генерить
красивые идеи, второй — резать слабые. Без этого Agent 0A быстро убедит
пользователя в правдоподобных, но нереализуемых нишах.

Каждой гипотезе приписываются:
  critic_score:   0-5, ЧЕМ ВЫШЕ ТЕМ ЛУЧШЕ.
                  5 = крепкая гипотеза, серьёзных контраргументов нет
                  3 = средние возражения
                  0 = куча проблем, скорее всего не работает
  critic_reasons: список конкретных контраргументов от LLM (max 5)

Используется типовой LLM с fallback-цепочкой, как в других агентах.
Если все модели упали — critic_score=-1 (не проверено), не блокирует пайплайн.
"""

import json
import logging
from typing import List, Optional, Tuple

import requests

from core.config import (
    OPENROUTER_API_KEY, OPENROUTER_BASE_URL, AI_MODEL,
    OPENROUTER_FALLBACK_MODELS as _FALLBACK_MODELS,
)
from core.models import Hypothesis

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = (
    "Ты — критичный B2B-консультант с опытом импорта оборудования из Китая в РФ. "
    "Твоя задача — ЛОМАТЬ чужие гипотезы о свободных нишах, находить причины "
    "почему ниша на самом деле плохая: дилеры уже есть в РФ, нужна сертификация, "
    "не продаётся без офлайн-демо, серая правовая зона, сезонный спрос, высокий "
    "гарантийный риск, массовый хайп в РФ-медиа. Не подыгрывай, не смягчай. "
    "Отвечаешь СТРОГО в формате JSON, без пояснений снаружи."
)


def critique_hypotheses(hypotheses: List[Hypothesis]) -> List[Hypothesis]:
    """
    Прогнать список гипотез через LLM-критика. Каждой проставляются
    critic_score и critic_reasons. Возвращает тот же список (мутирует
    входные объекты).
    """
    if not hypotheses:
        return hypotheses

    if not OPENROUTER_API_KEY:
        logger.warning("Agent 0B: нет OPENROUTER_API_KEY — пропускаем критику")
        return hypotheses

    # Чейн моделей
    seen = set()
    chain: List[str] = []
    for m in [AI_MODEL] + _FALLBACK_MODELS:
        if m and m not in seen:
            seen.add(m)
            chain.append(m)

    last_error = ""
    for model in chain:
        critiques, err = _try_one_model(model, hypotheses)
        if critiques is not None:
            _apply_critiques(hypotheses, critiques)
            logger.info(f"Agent 0B: критика {len(hypotheses)} гипотез через {model}")
            return hypotheses
        last_error = err
        logger.warning(f"Agent 0B: модель {model} не сработала ({err}) — пробую следующую")

    logger.error(f"Agent 0B: все модели упали ({last_error}) — гипотезы без критики")
    return hypotheses


# ============ LLM-путь ============

def _build_prompt(hypotheses: List[Hypothesis]) -> str:
    lines = []
    for i, h in enumerate(hypotheses):
        lines.append(
            f"{i}. {h.niche_name}\n"
            f"   Боль: {h.pain}\n"
            f"   Китай: {h.china_solution}\n"
            f"   Почему свободна: {h.why_free}"
        )
    block = "\n\n".join(lines)

    return f"""Даны {len(hypotheses)} гипотез о «свободных нишах» из Китая в РФ
(их сформулировал другой LLM-агент — могут быть красивые, но ложные).

{block}

ЗАДАЧА: по КАЖДОЙ гипотезе по индексу 0..{len(hypotheses)-1} выдай:
  critic_score: целое 0-5, где
    5 = серьёзных контраргументов НЕТ, гипотеза крепкая
    3 = средние возражения, требует проверки
    1 = много проблем, очень рискованно
    0 = ниша мёртвая, не стоит рассматривать
  critic_reasons: список из 1-5 конкретных причин почему ниша может не сработать.
    Каждая причина = одно короткое предложение, до 120 символов.
  regulatory_risk: одно из значений строкой:
    "none"      — видимых регуляторных рисков нет, обычный гражданский товар
    "dual_use"  — возможное двойное назначение / экспортный контроль ФСТЭК
                  (высокоточные подшипники ISO 4+, дроны от 35кг, тепловизоры,
                  мульти-спектральные камеры, рации/шифрование, лазеры высокой
                  мощности, прецизионная оптика, навигация GPS/ГЛОНАСС-модули)
    "regulated" — особый контроль / сложная обязательная сертификация
                  (медтехника → Росздравнадзор 200-500к₽ и 6-12 мес;
                  продукты/корма → Россельхознадзор; детские товары → ТР ТС;
                  газовое оборудование → Ростехнадзор; химия → особый ТР)
    "uncertain" — категория размытая, на стыке регуляторов, нужно ручное
                  уточнение через брокера

ПРОВЕРЯЙ ПО КАЖДОЙ ГИПОТЕЗЕ:
- Есть ли уже сильные дилеры/импортёры в РФ (Wagner, Graco, Trumpf и т.п.)?
- Нужна ли сложная сертификация (РСТ, ГОСТ, медицинская)?
- Можно ли продать БЕЗ офлайн-демонстрации товара?
- Есть ли серая зона (бренды, патенты, реплики)?
- Высокий ли гарантийный риск (тонкая электроника, расходники)?
- Сезонный ли спрос (стройка летом, агро весной)?
- Хайповая ли тема в РФ-медиа сейчас (множество перепродавцов уже зашли)?
- Подходит ли для предзаказа Авито (клиент готов ждать 30-60 дней)?
- РЕГУЛЯТОРНЫЙ РИСК: не попадает ли товар в двойное назначение /
  обязательную сертификацию (см. список выше для regulatory_risk)?

ЕСЛИ ГИПОТЕЗА ХОРОША — поставь высокий балл, но всё равно перечисли 1-2
реальных нюанса. Не оставляй critic_reasons пустым.

ВАЖНО: ты КРИТИК. Не подыгрывай. Если ниша банальна или хайповая — снижай балл.

ВАЖНО про regulatory_risk: это сигнальный флаг, НЕ юридическая консультация.
Точный код ТН ВЭД и допуски выясняет таможенный брокер. Твоя задача —
предупредить пользователя что для этой категории нужна экспертная проверка.
Если регуляторный риск есть — обязательно упомяни его коротко в одной из
critic_reasons тоже (например «⚠ возможное двойное назначение — нужен брокер»).
По умолчанию ставь "none", только если категория явно безопасна.

Верни СТРОГО JSON:
{{
  "critiques": [
    {{
      "index": 0,
      "critic_score": 4,
      "regulatory_risk": "none",
      "critic_reasons": [
        "В РФ уже работает Bosch Professional с похожим оборудованием",
        "Требует калибровки и обучения оператора — без видео не продать"
      ]
    }},
    {{
      "index": 1,
      "critic_score": 2,
      "regulatory_risk": "dual_use",
      "critic_reasons": [
        "⚠ Возможное двойное назначение (высокоточная оптика) — нужен брокер",
        "Mасс-маркет хайп: уже сотни перепродавцов на Авито",
        "Длинная цепочка послепродажки: запчасти, ремонт, расходники"
      ]
    }}
  ]
}}

ВАЖНО: верни ровно {len(hypotheses)} критик (индексы 0..{len(hypotheses)-1}).
Никакого текста снаружи JSON. Никаких ```. Только объект.
"""


def _try_one_model(model: str, hypotheses: List[Hypothesis]) -> Tuple[Optional[List[dict]], str]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_prompt(hypotheses)},
        ],
        "temperature": 0.5,
        "max_tokens": 4000,
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
            timeout=90,
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
    if parsed is None or not isinstance(parsed.get("critiques"), list):
        return None, f"не JSON или нет critiques: {content[:120]}"

    return parsed["critiques"], ""


_VALID_RISK = {"none", "dual_use", "regulated", "uncertain"}


def _apply_critiques(hypotheses: List[Hypothesis], critiques: List[dict]) -> None:
    """Привязать критики к гипотезам по index. Пропущенные — без критики."""
    by_index = {}
    for c in critiques:
        if not isinstance(c, dict):
            continue
        try:
            idx = int(c.get("index"))
        except (TypeError, ValueError):
            continue
        by_index[idx] = c

    for i, h in enumerate(hypotheses):
        c = by_index.get(i)
        if c is None:
            continue
        try:
            score = max(0, min(5, int(c.get("critic_score", -1))))
        except (TypeError, ValueError):
            score = -1
        reasons = c.get("critic_reasons") or []
        if not isinstance(reasons, list):
            reasons = [str(reasons)]
        reasons = [str(r).strip()[:160] for r in reasons if str(r).strip()][:5]

        # regulatory_risk — нормализуем строго: либо одно из 4 разрешённых,
        # либо пустая строка (= не оценено). LLM не всегда возвращает поле.
        risk = str(c.get("regulatory_risk", "")).strip().lower()
        if risk not in _VALID_RISK:
            risk = ""

        h.critic_score = score
        h.critic_reasons = json.dumps(reasons, ensure_ascii=False)
        h.regulatory_risk = risk


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
