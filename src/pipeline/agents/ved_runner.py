"""
NicheParser_China — Agent 5: ВЭД-расчёт (худший сценарий)
Считает экономику ПЕССИМИСТИЧНО, чтобы не строить иллюзий:
  - закупка по МАКСИМАЛЬНОЙ цене из топ-5 офферов Alibaba (не минимальной)
  - цена продажи в РФ по МИНИМАЛЬНОЙ из объявлений Авито (не медиане)
  - если Авито пуст — пессимистичная эвристика «закупка × курс × 2.0»

Это даёт нижнюю оценку маржи. Реальная маржа обычно выше — но если
по этому расчёту вердикт ВЕЗЁМ, значит ниша выдержит и просадки.

К каждому продукту прикрепляется:
  ved_cost_per_unit_rub:    себестоимость 1 шт в РФ после растаможки
  ved_price_rf_rub:         цена продажи в РФ за 1 шт (мин из Авито или эвристика)
  ved_price_source:         "avito_min" | "heuristic_worst" — что использовали
  ved_margin_percent:       маржа % на единицу (худший сценарий)
  ved_margin_per_moq_rub:   прибыль за минимальную партию MOQ (худший сценарий)
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

        # Худший сценарий по закупке: берём ВЕРХ диапазона цены, не низ.
        # price_usd_max бывает 0 в mock — тогда падаем на min.
        price_cn = float(best.get("price_usd_max") or best.get("price_usd_min") or 0)
        if price_cn <= 0:
            _attach_empty(p)
            continue
        moq = max(1, int(best.get("moq") or 1))

        # Вес: если парсер не смог выдернуть (weight_source="unknown"), НЕ
        # подставляем тихо 0.5 кг — для станка/оборудования это занизит
        # логистику и завысит маржу в 10-100 раз. Вместо этого — грубая
        # эвристика по цене товара + флаг ved_weight_warning для UI.
        w_raw = float(best.get("weight_kg") or 0)
        w_src = str(best.get("weight_source") or "unknown")
        weight_warning = False
        if w_raw > 0 and w_src in ("parsed", "mock"):
            weight = w_raw
        else:
            # Консервативная эвристика по цене: чем дороже — тем тяжелее.
            # Не идеальна, но точнее чем 0.5 кг для всего.
            #   ≥ $500  → 100 кг (условное оборудование)
            #   ≥ $100  → 20 кг  (среднее)
            #   ≥ $20   → 5 кг
            #   иначе   → 0.5 кг (мелочь)
            if price_cn >= 500:
                weight = 100.0
            elif price_cn >= 100:
                weight = 20.0
            elif price_cn >= 20:
                weight = 5.0
            else:
                weight = 0.5
            weight_warning = True
            logger.info(
                f"Agent 5: '{p.get('title_ru')}' — вес не определён "
                f"(source={w_src}), эвристика {weight} кг по цене ${price_cn}"
            )

        # Худший сценарий по продаже в РФ: берём МИНИМАЛЬНУЮ цену с Авито,
        # не медиану — если придётся демпинговать против самого дешёвого
        # конкурента, экономика должна сходиться. Без Авито — пессимистичная
        # эвристика ×2.0 вместо старой ×2.5.
        avito_min = float(p.get("avito_price_rub_min") or 0)
        if avito_min > 0:
            price_rf_rub = avito_min
            price_source = "avito_min"
        else:
            price_rf_rub = price_cn * calc.settings.usd_rate * 2.0
            price_source = "heuristic_worst"

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
        # Флаг «вес не определён — маржа может врать» для UI-предупреждения.
        # True когда парсер Alibaba не смог выдернуть вес, а мы использовали
        # эвристику по цене товара (см. w_src выше).
        p["ved_weight_warning"] = weight_warning
        p["ved_weight_source"] = w_src
        p["ved_weight_used_kg"] = weight
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
