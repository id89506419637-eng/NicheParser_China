"""
Тесты слоя БД: CRUD round-trip, фильтры, pain_points как JSON, run logs.
"""

from __future__ import annotations

import json

from core.models import DemandSnapshot, Niche, Product
from src.db import database as db


def test_init_db_creates_default_ved_settings(tmp_db):
    settings = db.get_ved_settings()
    assert settings["id"] == 1
    assert settings["duty_percent"] == 10
    assert settings["vat_percent"] == 22
    assert settings["min_margin_total_rub"] == 100_000


def test_save_niche_and_read_back_with_pain_points(tmp_db):
    pains = ["Нет поставок из ЕС", "Высокий спрос у B2B"]
    niche = Niche(
        name_ru="лазерный станок 500w",
        name_en="laser cutter 500w",
        category="Промышленное оборудование и станки",
        niche_type="ДЕФИЦИТ",
        is_seasonal=False,
        last_frequency=12_500,
        pain_points=json.dumps(pains, ensure_ascii=False),
    )
    niche_id = db.save_niche(niche)
    assert niche_id > 0

    stored = db.get_niche_by_id(niche_id)
    assert stored["name_ru"] == niche.name_ru
    assert stored["niche_type"] == "ДЕФИЦИТ"
    assert stored["is_seasonal"] is False
    assert stored["last_frequency"] == 12_500
    assert stored["pain_points_list"] == pains


def test_save_niche_upserts_on_duplicate_name_ru(tmp_db):
    """Вторая вставка с тем же name_ru должна обновлять, а не плодить дубли."""
    n1 = Niche(name_ru="датчик давления", last_frequency=100)
    id1 = db.save_niche(n1)

    n2 = Niche(name_ru="датчик давления", last_frequency=500,
               niche_type="ВЫСОКИЙ СПРОС")
    id2 = db.save_niche(n2)

    assert id1 == id2
    all_niches = db.get_all_niches()
    assert len(all_niches) == 1
    assert all_niches[0]["last_frequency"] == 500
    assert all_niches[0]["niche_type"] == "ВЫСОКИЙ СПРОС"


def test_save_and_fetch_product(tmp_db):
    niche_id = db.save_niche(Niche(name_ru="солнечная панель 400w"))
    product = Product(
        niche_id=niche_id,
        title_en="Monocrystalline Solar Panel 400W",
        price_usd_min=85.0,
        price_usd_max=110.0,
        moq=50,
        certificates="CE,TUV",
        cost_total_rub=12_500.0,
        margin_percent=55.0,
        margin_total_rub=500_000.0,
        verdict="ВЕЗЁМ",
        competition_count=1847,
    )
    pid = db.save_product(product)

    fetched = db.get_product_by_id(pid)
    assert fetched["title_en"] == product.title_en
    assert fetched["margin_percent"] == 55.0
    assert fetched["verdict"] == "ВЕЗЁМ"
    assert fetched["niche_name_ru"] == "солнечная панель 400w"


def test_get_top_products_respects_min_margin_filter(tmp_db):
    niche_id = db.save_niche(Niche(
        name_ru="промышленный насос",
        category="Промышленное оборудование и станки",
    ))
    db.save_product(Product(niche_id=niche_id, title_en="low", margin_percent=20,
                            verdict="НЕ ВЕЗЁМ"))
    db.save_product(Product(niche_id=niche_id, title_en="mid", margin_percent=45,
                            verdict="ИЗУЧИТЬ"))
    db.save_product(Product(niche_id=niche_id, title_en="high", margin_percent=80,
                            verdict="ВЕЗЁМ"))

    filtered = db.get_top_products(filters={"min_margin": 40})
    titles = [p["title_en"] for p in filtered]
    assert "low" not in titles
    assert "mid" in titles
    assert "high" in titles
    # Должно быть отсортировано по убыванию маржи
    assert titles[0] == "high"


def test_get_top_products_filters_by_verdict_and_category(tmp_db):
    n1 = db.save_niche(Niche(name_ru="a", category="Медтехника и медоборудование"))
    n2 = db.save_niche(Niche(name_ru="b", category="Стройматериалы и строительное оборудование"))
    db.save_product(Product(niche_id=n1, title_en="medA", margin_percent=60, verdict="ВЕЗЁМ"))
    db.save_product(Product(niche_id=n2, title_en="stroyB", margin_percent=70, verdict="ВЕЗЁМ"))
    db.save_product(Product(niche_id=n1, title_en="medC", margin_percent=65, verdict="ИЗУЧИТЬ"))

    res = db.get_top_products(filters={
        "category": "Медтехника и медоборудование",
        "verdict": "ВЕЗЁМ",
    })
    titles = [p["title_en"] for p in res]
    assert titles == ["medA"]


def test_delete_product_cascades_not_niche(tmp_db):
    niche_id = db.save_niche(Niche(name_ru="test product delete"))
    pid = db.save_product(Product(niche_id=niche_id, title_en="to delete"))

    db.delete_product(pid)
    assert db.get_product_by_id(pid) is None
    # Ниша должна остаться
    assert db.get_niche_by_id(niche_id) is not None


def test_demand_history_roundtrip_and_timeline(tmp_db):
    niche_id = db.save_niche(Niche(name_ru="сварочный аппарат", last_frequency=1000))
    db.save_demand_snapshot(DemandSnapshot(niche_id=niche_id, frequency=900,
                                           snapshot_date="2026-01-15"))
    db.save_demand_snapshot(DemandSnapshot(niche_id=niche_id, frequency=1200,
                                           snapshot_date="2026-02-15"))
    db.save_demand_snapshot(DemandSnapshot(niche_id=niche_id, frequency=1500,
                                           snapshot_date="2026-03-15"))

    tl = db.get_demand_timeline(limit_niches=5)
    assert "сварочный аппарат" in tl
    points = tl["сварочный аппарат"]
    assert [p["date"] for p in points] == ["2026-01-15", "2026-02-15", "2026-03-15"]
    assert [p["frequency"] for p in points] == [900, 1200, 1500]


def test_update_ved_settings_changes_values_and_updated_at(tmp_db):
    before = db.get_ved_settings()
    db.update_ved_settings({"usd_rate": 95.5, "duty_percent": 15.0})
    after = db.get_ved_settings()

    assert after["usd_rate"] == 95.5
    assert after["duty_percent"] == 15.0
    assert after["updated_at"]
    assert after["updated_at"] != before.get("updated_at")


def test_run_log_lifecycle(tmp_db):
    rid = db.create_run_log()
    assert rid > 0
    active = db.get_active_run()
    assert active and active["id"] == rid and active["status"] == "running"

    db.finish_run_log(rid, status="done", niches_processed=3,
                      products_found=45, profitable_count=7)

    log = db.get_run_log(rid)
    assert log["status"] == "done"
    assert log["niches_processed"] == 3
    assert log["products_found"] == 45
    assert log["profitable_count"] == 7
    assert log["finished_at"]

    assert db.get_active_run() is None


def test_pain_points_invalid_json_returns_empty_list(tmp_db):
    """Если в pain_points лежит битый JSON — не падаем, отдаём []."""
    niche_id = db.save_niche(Niche(name_ru="bad json niche",
                                   pain_points="{not valid"))
    stored = db.get_niche_by_id(niche_id)
    assert stored["pain_points_list"] == []
