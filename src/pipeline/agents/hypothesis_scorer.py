"""
NicheParser_China — Agent: Hypothesis Validator + Scorer (Wave 5C)

По каждой гипотезе от Agent 0A+0B:
  1. ВАЛИДАЦИЯ — прогон через mock-парсеры (Wordstat / Alibaba / Avito),
     получаем frequency, alibaba_offers, avito_listings, цены, грубую маржу.
  2. СКОРИНГ — 7 факторов по плану v3, итого 0-100:
       Экономика сделки        0-22
       Спрос                   0-18
       Доступность в Китае     0-18
       Конкуренция на Авито    0-15
       Демо-продаваемость      0-12  (из Deal Readiness, по умолчанию 0)
       Операц. сложность       0-10  (из Deal Readiness, по умолчанию 0)
       LLM Critic Score        0-5

Без скоринга — на mock-данных всё быстро (~1 сек на гипотезу). На реальных
API в Wave 6 надо будет переделать в фоновую асинхронную обработку.
"""

import json
import logging
import statistics
from typing import List

from src.parsers.avito import search_avito
from src.parsers.alibaba import search_alibaba
from src.pipeline.agents.demand_checker import _mock_frequencies, _real_frequencies
from core.config import USE_MOCK_WORDSTAT, YANDEX_OAUTH_TOKEN
from core.models import Hypothesis

logger = logging.getLogger(__name__)


# Грубые константы для оценки экономики БЕЗ полного ВЭД-расчёта.
# Реальный ВЭД-расчёт по каждой модели делается уже в Этапе 5
# (когда юзер нажмёт «Взять в работу»).
USD_TO_RUB_FALLBACK = 95.0      # запасной курс если в settings нет
VED_MARKUP_FACTOR = 1.55         # ~55% надбавка: пошлина 10% + НДС 22% + лог + банк


def validate_and_score(hypotheses: List[Hypothesis]) -> List[Hypothesis]:
    """
    Прогнать каждую гипотезу через парсеры и посчитать 7-факторный балл.
    Мутирует входные объекты — добавляет score_total и score_breakdown (JSON).
    Возвращает тот же список.
    """
    if not hypotheses:
        return hypotheses

    # Wordstat для всех сразу (один запрос — массив частот)
    queries = [h.niche_name for h in hypotheses]
    try:
        if USE_MOCK_WORDSTAT or not YANDEX_OAUTH_TOKEN:
            frequencies = _mock_frequencies(queries)
        else:
            frequencies = _real_frequencies(queries)
    except Exception as e:
        logger.error(f"Scorer/Wordstat: {type(e).__name__}: {e}")
        frequencies = [0] * len(hypotheses)

    for i, h in enumerate(hypotheses):
        freq = int(frequencies[i] if i < len(frequencies) else 0)
        validation = _validate_one(h.niche_name, freq)
        breakdown = _score_one(h, validation)
        h.score_total = int(breakdown["total"])
        h.score_breakdown = json.dumps(breakdown, ensure_ascii=False)

    # Сортируем по убыванию балла — топ-10 пойдёт развёрнуто, остальные свёрнуто
    hypotheses.sort(key=lambda h: -h.score_total)
    return hypotheses


def _validate_one(niche_name: str, frequency: int) -> dict:
    """Один прогон валидации: дёргает Alibaba и Avito по строке гипотезы."""
    # Alibaba
    try:
        ali_products, ali_total = search_alibaba(niche_name)
    except Exception as e:
        logger.warning(f"Scorer/Alibaba '{niche_name}': {type(e).__name__}: {e}")
        ali_products, ali_total = [], 0

    ali_prices = [p.price_usd_min for p in ali_products if p.price_usd_min > 0]
    ali_min_usd = min(ali_prices) if ali_prices else 0.0
    ali_count = len(ali_products)

    # Avito
    try:
        av_listings, av_total = search_avito(niche_name)
    except Exception as e:
        logger.warning(f"Scorer/Avito '{niche_name}': {type(e).__name__}: {e}")
        av_listings, av_total = [], 0

    av_prices = sorted([l.get("price_rub", 0) for l in av_listings if l.get("price_rub", 0) > 0])
    av_median = float(statistics.median(av_prices)) if av_prices else 0.0
    av_count = len(av_listings)

    # Грубая экономика: маржа = (avito_median - alibaba_min*курс*наценка) / avito_median
    margin_pct = 0.0
    if ali_min_usd > 0 and av_median > 0:
        cost_rub = ali_min_usd * USD_TO_RUB_FALLBACK * VED_MARKUP_FACTOR
        if av_median > cost_rub:
            margin_pct = round((av_median - cost_rub) / av_median * 100, 1)
        else:
            margin_pct = 0.0

    return {
        "frequency": frequency,
        "alibaba_offers_count": ali_count,
        "alibaba_total_market": ali_total,
        "alibaba_min_usd": round(ali_min_usd, 2),
        "avito_listings_count": av_count,
        "avito_total_market": av_total,
        "avito_price_median_rub": round(av_median, 0),
        "estimated_margin_percent": margin_pct,
    }


def _score_one(h: Hypothesis, v: dict) -> dict:
    """Применить 7-факторную модель скоринга к одной гипотезе."""

    # === 1. Экономика сделки (0-22): по грубой марже ===
    m = v["estimated_margin_percent"]
    if m >= 100: score_economy = 22
    elif m >= 70: score_economy = 18
    elif m >= 50: score_economy = 14
    elif m >= 30: score_economy = 9
    elif m >= 15: score_economy = 5
    else: score_economy = 0

    # === 2. Спрос (0-18): Wordstat запросов/мес ===
    f = v["frequency"]
    if f >= 5000: score_demand = 18
    elif f >= 2000: score_demand = 15
    elif f >= 800: score_demand = 11
    elif f >= 300: score_demand = 7
    elif f >= 50: score_demand = 4
    else: score_demand = 0  # «невидимый рынок» — нулевой спрос

    # === 3. Доступность в Китае (0-18): число офферов + наличие минимальной цены ===
    ali_count = v["alibaba_offers_count"]
    if ali_count >= 10 and v["alibaba_min_usd"] > 0: score_china = 18
    elif ali_count >= 5 and v["alibaba_min_usd"] > 0: score_china = 14
    elif ali_count >= 3: score_china = 9
    elif ali_count >= 1: score_china = 4
    else: score_china = 0

    # === 4. Конкуренция на Авито (0-15): обратная зависимость от числа объявлений ===
    av_count = v["avito_listings_count"]
    av_total = v["avito_total_market"]
    # Меньше объявлений = выше балл, НО 0 = подозрительно (либо ниша мёртвая)
    if av_count == 0 and av_total == 0:
        score_avito = 5  # подозрительно — может рынка нет
    elif av_count <= 5: score_avito = 15
    elif av_count <= 20: score_avito = 12
    elif av_count <= 50: score_avito = 8
    elif av_count <= 200: score_avito = 4
    else: score_avito = 1

    # === 5. Демо-продаваемость (0-12) ===
    # 2026-07-31: если ручной DR ещё не заполнен — используем AI-оценку
    # (LLM критик отвечает на 7 вопросов за пользователя). Пользователь
    # физически не может ответить про 40 незнакомых ниш.
    dr_ai = _extract_dr_ai(h)
    score_demo, demo_label = _score_demo_from_dr(dr_ai, is_ai=bool(dr_ai))

    # === 6. Операц. сложность (0-10) ===
    score_ops, ops_label = _score_ops_from_dr(dr_ai, is_ai=bool(dr_ai))

    # === 7. LLM Critic Score (0-5) ===
    score_critic = max(0, h.critic_score) if h.critic_score >= 0 else 0

    total = score_economy + score_demand + score_china + score_avito + score_demo + score_ops + score_critic

    return {
        "total": total,
        "factors": {
            "economy":  {"score": score_economy, "max": 22, "value": f"маржа ~{m:.0f}%"},
            "demand":   {"score": score_demand,  "max": 18, "value": f"{f} запр/мес в Яндексе"},
            "china":    {"score": score_china,   "max": 18, "value": f"{ali_count} офферов от ${v['alibaba_min_usd']:.2f}"},
            "avito":    {"score": score_avito,   "max": 15, "value": f"{av_count} объявл. на Авито"},
            "demo":     {"score": score_demo,    "max": 12, "value": demo_label},
            "ops":      {"score": score_ops,     "max": 10, "value": ops_label},
            "critic":   {"score": score_critic,  "max": 5,  "value": f"contras: {len(json.loads(h.critic_reasons or '[]'))}"},
        },
        "validation": v,
    }


# === Пересчёт по Deal Readiness (Wave 5E) =================================
# Когда юзер заполняет/меняет чеклист — пересчитываем ТОЛЬКО demo+ops+total,
# остальные факторы (экономика/спрос/китай/авито/критик) не трогаем — они
# приходят от парсеров и LLM, перепрогон стоит дорого.

def _extract_dr_ai(h: Hypothesis) -> dict | None:
    """
    Достаём AI-оценку Deal Readiness из h.deal_readiness_ai (JSON строка).
    Ключи в AI-версии называются q1_ai, q2_ai, ... (см. hypothesis_critic._apply_critiques).
    Нормализуем под формат которого ждёт _score_demo_from_dr / _score_ops_from_dr
    (там ключи q1_demo, q2_warranty, q4_term, q5_legal).
    """
    raw = getattr(h, "deal_readiness_ai", "") or ""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    # Мапим q1_ai → q1_demo, q2_ai → q2_warranty, q4_ai → q4_term, q5_ai → q5_legal
    return {
        "q1_demo":     int(parsed.get("q1_ai", 0) or 0),
        "q2_warranty": int(parsed.get("q2_ai", 0) or 0),
        "q4_term":     int(parsed.get("q4_ai", 0) or 0),
        "q5_legal":    int(parsed.get("q5_ai", 0) or 0),
    }


def _score_demo_from_dr(dr: dict | None, is_ai: bool = False) -> tuple[int, str]:
    """
    Демо-продаваемость (0-12).
    Q1 = «можно объяснить ценность за 30 секунд без физического показа».
    Это бинарный ключевой вопрос: либо товар продаётся фото+видео, либо нет.
    """
    if not dr:
        return 0, "не заполнено (Deal Readiness)"
    prefix = "🤖 AI: " if is_ai else ""
    if int(dr.get("q1_demo", 0)) == 1:
        return 12, f"{prefix}✓ продаётся без офлайн-демо"
    return 0, f"{prefix}✗ нужен офлайн-показ — не продать по объявлению"


def _score_ops_from_dr(dr: dict | None, is_ai: bool = False) -> tuple[int, str]:
    """
    Операц. сложность (0-10).
    Q2 (гарантия) + Q4 (срок поставки) + Q5 (юр.чистота). Все три — фундамент
    предоплатной модели B2B-Авито. Каждый «да» даёт ≈3.33 балла.
    """
    if not dr:
        return 0, "не заполнено (Deal Readiness)"
    prefix = "🤖 AI: " if is_ai else ""
    yes = int(dr.get("q2_warranty", 0)) + int(dr.get("q4_term", 0)) + int(dr.get("q5_legal", 0))
    if yes == 3: return 10, f"{prefix}✓ все 3 операц. условия (гарантия/срок/юр.чистота)"
    if yes == 2: return 6,  f"{prefix}~ {yes}/3 операц. условий"
    if yes == 1: return 3,  f"{prefix}⚠ только {yes}/3 операц. условий"
    return 0, f"{prefix}✗ ни одно операц. условие не выполнено"


def recompute_with_dr(breakdown: dict, h_critic_score: int, dr: dict | None) -> dict:
    """
    Пересчитать score_total и обновить demo/ops факторы по ответам DR.
    breakdown: уже сохранённый score_breakdown (dict, не JSON-строка).
    Возвращает обновлённый breakdown (новый dict, не мутирует входной).
    """
    out = json.loads(json.dumps(breakdown))  # глубокая копия
    if not isinstance(out.get("factors"), dict):
        return out

    demo_score, demo_label = _score_demo_from_dr(dr)
    ops_score, ops_label = _score_ops_from_dr(dr)

    out["factors"]["demo"] = {"score": demo_score, "max": 12, "value": demo_label}
    out["factors"]["ops"]  = {"score": ops_score,  "max": 10, "value": ops_label}

    # critic мог поменяться (мы могли исправить hypothesis_critic вручную)
    critic_score = max(0, h_critic_score) if h_critic_score >= 0 else 0
    if "critic" in out["factors"]:
        out["factors"]["critic"]["score"] = critic_score

    # Пересчёт total: суммируем то, что есть
    out["total"] = sum(int(f.get("score", 0)) for f in out["factors"].values())
    return out
