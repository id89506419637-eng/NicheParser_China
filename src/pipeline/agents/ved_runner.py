"""
NicheParser_China — Agent 5: ВЭД-расчёт
Для каждого продукта (keep=True) выбирает лучший оффер с Alibaba
(самый дешёвый из топ-5) и прогоняет через готовый VedCalculator:
закупка → пошлина → НДС → логистика → банк → себестоимость в РФ.

Цена продажи в РФ берётся из медианы Авито (Агент 6). Если Авито не нашёл
объявлений — fallback на старую эвристику «закупка × курс × 2.5», чтобы
вердикт всё равно посчитался.

К каждому продукту прикрепляется:
  ved_cost_per_unit_rub:    себестоимость 1 шт в РФ после растаможки
  ved_price_rf_rub:         цена продажи в РФ за 1 шт (из Авито или эвристики)
  ved_price_source:         "avito" | "heuristic" — что использовали
  ved_margin_percent:       маржа % на единицу
  ved_margin_per_moq_rub:   прибыль за минимальную партию MOQ
  ved_breakdown:            полная разбивка (покупка, пошлина, НДС, ...)
  ved_best_offer:           оффер Alibaba, на котором считали (для прозрачности)
"""

import logging
from typing import List, Optional

from src.calculator.ved_calculator import VedCalculator
from src.db import database as db
from core.models import VedSettings

logger = logging.getLogger(__name__)


def run_ved(products: List[dict]) -> List[dict]:
    """Прогнать продукты через ВЭД-калькулятор. Только для keep=True с офферами."""
    if not products:
        return []

    settings = _load_settings()
    calc = VedCalculator(settings)

    for p in products:
        if not p.get("keep", True):
            continue

        offers = p.get("alibaba_offers") or []
        if not offers:
            _attach_empty(p)
            continue

        best = _pick_best_offer(offers)
        if not best or best.get("price_usd_min", 0) <= 0:
            _attach_empty(p)
            continue

        price_cn = float(best["price_usd_min"])
        moq = max(1, int(best.get("moq") or 1))
        weight = float(best.get("weight_kg") or 0.5)

        # Цена продажи в РФ: медиана Авито, если Агент 6 что-то нашёл.
        # Иначе fallback — старая эвристика «закупка × курс × 2.5».
        avito_median = float(p.get("avito_price_rub_median") or 0)
        if avito_median > 0:
            price_rf_rub = avito_median
            price_source = "avito"
        else:
            price_rf_rub = price_cn * calc.settings.usd_rate * 2.5
            price_source = "heuristic"

        try:
            res = calc.calculate(
                price_cn_usd=price_cn,
                price_rf_rub=price_rf_rub,
                quantity=moq,
                weight_kg_per_unit=weight,
                volume_cbm_per_unit=0.001,
            )
        except Exception as e:
            logger.error(f"Agent 5: расчёт для '{p.get('title_ru')}' упал: {e}", exc_info=True)
            _attach_empty(p)
            continue

        p["ved_cost_per_unit_rub"] = res["cost_per_unit_rub"]
        p["ved_price_rf_rub"] = res["price_rf_rub"]
        p["ved_price_source"] = price_source
        p["ved_margin_percent"] = res["margin_percent"]
        p["ved_margin_per_moq_rub"] = res["margin_total_rub"]
        p["ved_breakdown"] = {
            "purchase_rub": res["purchase_rub"],
            "duty_rub": res["duty_rub"],
            "vat_rub": res["vat_rub"],
            "logistics_rub": res["logistics_rub"],
            "bank_rub": res["bank_rub"],
        }
        p["ved_best_offer"] = {
            "price_usd": price_cn,
            "moq": moq,
            "url": best.get("product_url"),
            "title_en": best.get("title_en"),
        }

    return products


def _pick_best_offer(offers: List[dict]) -> Optional[dict]:
    """Выбираем оффер с минимальной price_usd_min (самый дешёвый стартовый ценник)."""
    eligible = [o for o in offers if o.get("price_usd_min", 0) > 0]
    if not eligible:
        return None
    return min(eligible, key=lambda o: o["price_usd_min"])


def _load_settings() -> VedSettings:
    """Тянем ВЭД-настройки из БД; если что — дефолты из VedSettings()."""
    try:
        raw = db.get_ved_settings()
        if raw:
            return VedSettings(**{
                k: v for k, v in raw.items()
                if k in VedSettings.__dataclass_fields__
            })
    except Exception as e:
        logger.warning(f"Agent 5: не удалось загрузить настройки ВЭД ({e}), использую дефолт")
    return VedSettings()


def _attach_empty(p: dict) -> None:
    p["ved_cost_per_unit_rub"] = 0.0
    p["ved_price_rf_rub"] = 0.0
    p["ved_price_source"] = None
    p["ved_margin_percent"] = 0.0
    p["ved_margin_per_moq_rub"] = 0.0
    p["ved_breakdown"] = None
    p["ved_best_offer"] = None
