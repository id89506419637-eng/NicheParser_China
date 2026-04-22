"""
NicheParser_China — VED Calculator
Расчёт полной себестоимости импорта из Китая и маржинальности (на единицу и партию).
"""

import logging
import threading
import time
from typing import Optional

import requests

from core.config import CBR_DAILY_URL, CBR_CACHE_TTL_SECONDS
from core.models import VedSettings

logger = logging.getLogger(__name__)


# Кэш курсов ЦБ (process-wide) — не бьёмся в API на каждый товар.
_rates_cache: dict = {"rates": None, "timestamp": 0.0}
_rates_lock = threading.Lock()


def fetch_cbr_rates(force: bool = False) -> dict:
    """Курсы USD/CNY от ЦБ РФ с кэшем 1 час. Возвращает {"USD": float, "CNY": float}."""
    now = time.time()
    with _rates_lock:
        if (not force
            and _rates_cache["rates"] is not None
            and (now - _rates_cache["timestamp"]) < CBR_CACHE_TTL_SECONDS):
            return _rates_cache["rates"]

    try:
        resp = requests.get(CBR_DAILY_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        valutes = data.get("Valute", {})

        rates = {}
        for code in ("USD", "CNY"):
            v = valutes.get(code)
            if v:
                rates[code] = v["Value"] / v["Nominal"]

        with _rates_lock:
            _rates_cache["rates"] = rates
            _rates_cache["timestamp"] = now

        logger.info(f"CBR rates: USD={rates.get('USD')}, CNY={rates.get('CNY')}")
        return rates

    except Exception as e:
        logger.error(f"Failed to fetch CBR rates: {e}")
        # fallback на прежний кэш, если есть
        return _rates_cache["rates"] or {}


class VedCalculator:
    """Калькулятор ВЭД — считает стоимость на единицу и на партию MOQ."""

    def __init__(self, settings: Optional[VedSettings] = None):
        self.settings = settings or VedSettings()

        # Подтянуть курсы ЦБ, если не заданы вручную
        if self.settings.usd_rate <= 0 or self.settings.cny_rate <= 0:
            rates = fetch_cbr_rates()
            if rates:
                if self.settings.usd_rate <= 0:
                    self.settings.usd_rate = rates.get("USD", 90.0)
                if self.settings.cny_rate <= 0:
                    self.settings.cny_rate = rates.get("CNY", 12.5)

    def calculate(
        self,
        price_cn_usd: float,
        price_rf_rub: float,
        quantity: int = 1,
        weight_kg_per_unit: float = 0.5,
        volume_cbm_per_unit: float = 0.001,
    ) -> dict:
        """
        Рассчитать себестоимость единицы и маржу.

        Args:
            price_cn_usd: закупочная цена за 1 единицу, USD
            price_rf_rub: цена продажи в РФ за 1 единицу, RUB
            quantity: размер партии (MOQ или сколько везём)
            weight_kg_per_unit: вес единицы товара, кг
            volume_cbm_per_unit: объём единицы товара, м³

        Returns:
            dict с полной разбивкой на единицу и на партию.
        """
        s = self.settings

        # --- На единицу ---
        purchase = price_cn_usd * s.usd_rate
        duty = purchase * (s.duty_percent / 100)
        vat = (purchase + duty) * (s.vat_percent / 100)

        logistics_by_weight = weight_kg_per_unit * s.logistics_per_kg * s.usd_rate
        logistics_by_volume = volume_cbm_per_unit * s.logistics_per_cbm * s.usd_rate
        logistics = max(logistics_by_weight, logistics_by_volume)

        bank = purchase * (s.bank_percent / 100)

        cost_per_unit = purchase + duty + vat + logistics + bank

        # --- Маржа ---
        if price_rf_rub > 0:
            margin_percent = ((price_rf_rub - cost_per_unit) / price_rf_rub) * 100
            profit_per_unit = price_rf_rub - cost_per_unit
        else:
            margin_percent = 0.0
            profit_per_unit = 0.0

        # --- На партию ---
        qty = max(1, int(quantity))
        cost_batch = cost_per_unit * qty
        revenue_batch = price_rf_rub * qty
        margin_total_rub = profit_per_unit * qty

        return {
            # per-unit breakdown
            "purchase_rub": round(purchase, 2),
            "duty_rub": round(duty, 2),
            "vat_rub": round(vat, 2),
            "logistics_rub": round(logistics, 2),
            "bank_rub": round(bank, 2),
            "cost_per_unit_rub": round(cost_per_unit, 2),
            "price_rf_rub": round(price_rf_rub, 2),
            "profit_per_unit_rub": round(profit_per_unit, 2),
            # percent
            "margin_percent": round(margin_percent, 2),
            # batch
            "quantity": qty,
            "cost_batch_rub": round(cost_batch, 2),
            "revenue_batch_rub": round(revenue_batch, 2),
            "margin_total_rub": round(margin_total_rub, 2),
            # rates used
            "usd_rate": s.usd_rate,
            "cny_rate": s.cny_rate,
        }

    def verdict(self, margin_percent: float, margin_total_rub: float,
                has_pain_points: bool = False) -> str:
        """
        Вынести вердикт ВЕЗЁМ/ИЗУЧИТЬ/НЕ ВЕЗЁМ.

        Боли ниши (pain_points) — подтверждающий фактор в пограничной зоне:
        если маржа 40-50% И есть подтверждённые боли → ВЕЗЁМ вместо ИЗУЧИТЬ.
        """
        s = self.settings

        if margin_percent >= s.min_margin_percent and margin_total_rub >= s.min_margin_total_rub:
            return "ВЕЗЁМ"

        if (has_pain_points
            and margin_percent >= 40
            and margin_total_rub >= s.min_margin_total_rub * 0.7):
            return "ВЕЗЁМ"

        if margin_percent < 30:
            return "НЕ ВЕЗЁМ"

        return "ИЗУЧИТЬ"
