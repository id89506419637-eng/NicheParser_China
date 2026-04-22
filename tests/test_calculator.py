"""
Тесты VedCalculator: базовый расчёт, вердикты, pain_points как подтверждающий фактор.
"""

from __future__ import annotations

import pytest

from core.models import VedSettings
from src.calculator import ved_calculator as vc


@pytest.fixture
def settings() -> VedSettings:
    return VedSettings(
        usd_rate=90.0,
        cny_rate=12.5,
        duty_percent=10.0,
        vat_percent=20.0,
        logistics_per_kg=3.0,
        logistics_per_cbm=350.0,
        bank_percent=2.0,
        min_margin_percent=50.0,
        min_margin_total_rub=100_000.0,
    )


def test_calculate_breakdown_matches_manual(settings):
    """Проверяем, что все компоненты себестоимости считаются корректно."""
    calc = vc.VedCalculator(settings)
    result = calc.calculate(
        price_cn_usd=10.0,
        price_rf_rub=3000.0,
        quantity=100,
        weight_kg_per_unit=1.0,
        volume_cbm_per_unit=0.001,
    )

    # purchase = 10 * 90 = 900
    assert result["purchase_rub"] == pytest.approx(900.0)
    # duty = 900 * 0.10 = 90
    assert result["duty_rub"] == pytest.approx(90.0)
    # vat = (900 + 90) * 0.20 = 198
    assert result["vat_rub"] == pytest.approx(198.0)
    # logistics_by_weight = 1 * 3 * 90 = 270
    # logistics_by_volume = 0.001 * 350 * 90 = 31.5
    # max → 270
    assert result["logistics_rub"] == pytest.approx(270.0)
    # bank = 900 * 0.02 = 18
    assert result["bank_rub"] == pytest.approx(18.0)
    # total per unit = 900 + 90 + 198 + 270 + 18 = 1476
    assert result["cost_per_unit_rub"] == pytest.approx(1476.0)

    # batch: quantity=100, cost_batch = 147600, revenue = 300000
    assert result["quantity"] == 100
    assert result["cost_batch_rub"] == pytest.approx(147_600.0)
    assert result["revenue_batch_rub"] == pytest.approx(300_000.0)
    # profit per unit = 3000 - 1476 = 1524
    assert result["profit_per_unit_rub"] == pytest.approx(1524.0)
    # margin % = 1524 / 3000 * 100 = 50.8
    assert result["margin_percent"] == pytest.approx(50.8, abs=0.05)
    # margin total = 1524 * 100 = 152400
    assert result["margin_total_rub"] == pytest.approx(152_400.0)


def test_calculate_volume_wins_for_bulky_cargo(settings):
    """Объёмный товар → логистика считается по CBM, а не по весу."""
    calc = vc.VedCalculator(settings)
    result = calc.calculate(
        price_cn_usd=5.0,
        price_rf_rub=2000.0,
        quantity=10,
        weight_kg_per_unit=0.1,     # лёгкий
        volume_cbm_per_unit=0.05,   # объёмный
    )
    # logistics_by_weight = 0.1 * 3 * 90 = 27
    # logistics_by_volume = 0.05 * 350 * 90 = 1575 → побеждает
    assert result["logistics_rub"] == pytest.approx(1575.0)


def test_calculate_quantity_floor_is_one(settings):
    """Даже если передали 0, партия считается как минимум за 1 штуку."""
    calc = vc.VedCalculator(settings)
    result = calc.calculate(price_cn_usd=1.0, price_rf_rub=1000.0, quantity=0)
    assert result["quantity"] == 1


def test_calculate_zero_price_rf_yields_zero_margin(settings):
    """Если цена продажи не задана — маржа = 0, без деления на ноль."""
    calc = vc.VedCalculator(settings)
    result = calc.calculate(price_cn_usd=10.0, price_rf_rub=0.0, quantity=5)
    assert result["margin_percent"] == 0.0
    assert result["profit_per_unit_rub"] == 0.0


def test_verdict_vezem_when_both_thresholds_met(settings):
    calc = vc.VedCalculator(settings)
    assert calc.verdict(margin_percent=60.0, margin_total_rub=150_000) == "ВЕЗЁМ"


def test_verdict_ne_vezem_when_margin_below_30(settings):
    calc = vc.VedCalculator(settings)
    assert calc.verdict(margin_percent=25.0, margin_total_rub=500_000) == "НЕ ВЕЗЁМ"


def test_verdict_izuchit_in_middle_zone(settings):
    """30 ≤ margin% < 50 без болей → ИЗУЧИТЬ."""
    calc = vc.VedCalculator(settings)
    assert calc.verdict(margin_percent=45.0, margin_total_rub=150_000,
                        has_pain_points=False) == "ИЗУЧИТЬ"


def test_verdict_pain_upgrades_to_vezem_in_edge_zone(settings):
    """Маржа 40-50% + боли + партия ≥ 70% от порога → ВЕЗЁМ (ключевая бизнес-логика)."""
    calc = vc.VedCalculator(settings)
    # 42% margin, 75_000 ₽ (=75% от 100_000), боли есть → ВЕЗЁМ
    assert calc.verdict(margin_percent=42.0, margin_total_rub=75_000,
                        has_pain_points=True) == "ВЕЗЁМ"


def test_verdict_pain_doesnt_help_below_40(settings):
    """Даже с болями 35% маржи → ИЗУЧИТЬ, а 25% → НЕ ВЕЗЁМ."""
    calc = vc.VedCalculator(settings)
    assert calc.verdict(margin_percent=35.0, margin_total_rub=200_000,
                        has_pain_points=True) == "ИЗУЧИТЬ"
    assert calc.verdict(margin_percent=25.0, margin_total_rub=200_000,
                        has_pain_points=True) == "НЕ ВЕЗЁМ"


def test_verdict_pain_requires_min_batch_size(settings):
    """Боли есть, маржа 45%, но партия меньше 70% порога → ИЗУЧИТЬ."""
    calc = vc.VedCalculator(settings)
    # 0.7 * 100_000 = 70_000 — нужна партия выше этого
    assert calc.verdict(margin_percent=45.0, margin_total_rub=50_000,
                        has_pain_points=True) == "ИЗУЧИТЬ"


def test_fetch_cbr_rates_uses_cache(monkeypatch):
    """fetch_cbr_rates должна кэшировать результат между вызовами."""
    # сбросить кэш
    vc._rates_cache["rates"] = None
    vc._rates_cache["timestamp"] = 0.0

    calls = {"n": 0}

    def fake_get(url, timeout=10):
        calls["n"] += 1

        class Resp:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {"Valute": {
                    "USD": {"Value": 92.5, "Nominal": 1},
                    "CNY": {"Value": 126.0, "Nominal": 10},
                }}
        return Resp()

    monkeypatch.setattr(vc.requests, "get", fake_get)

    r1 = vc.fetch_cbr_rates()
    r2 = vc.fetch_cbr_rates()
    assert calls["n"] == 1
    assert r1 == r2
    assert r1["USD"] == pytest.approx(92.5)
    assert r1["CNY"] == pytest.approx(12.6)  # 126/10

    # force=True обходит кэш
    vc.fetch_cbr_rates(force=True)
    assert calls["n"] == 2
