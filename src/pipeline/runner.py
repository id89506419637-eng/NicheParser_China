"""
NicheParser_China — Pipeline Runner
Оркестратор 5 этапов: Wordstat → Classify → Alibaba → ВЭД → Verdict.
"""

import logging
from datetime import datetime
from typing import Optional

from core.config import (
    ENABLE_WORDSTAT,
    ENABLE_ALIBABA,
    TARGET_CATEGORIES,
    ALIBABA_MAX_PRODUCTS_PER_NICHE,
)
from core.models import Niche, Product, DemandSnapshot, VedSettings
from src.db import database as db
from src.parsers import wordstat as wordstat_parser
from src.parsers import alibaba as alibaba_parser
from src.calculator.ved_calculator import VedCalculator
from src.analytics import niche_classifier

logger = logging.getLogger(__name__)


class PipelineRunner:
    """Запускает полный аналитический цикл."""

    def __init__(self, max_niches: Optional[int] = None,
                 max_products_per_niche: int = ALIBABA_MAX_PRODUCTS_PER_NICHE):
        self.max_niches = max_niches
        self.max_products_per_niche = max_products_per_niche

    def run(self) -> int:
        """Запустить пайплайн. Возвращает run_id."""
        run_id = db.create_run_log()
        logger.info(f"=== Pipeline run #{run_id} START ===")

        niches_processed = 0
        products_found = 0
        profitable_count = 0

        try:
            # === ЭТАП 1: Wordstat ===
            wordstat_items = self._stage1_wordstat()
            if self.max_niches:
                wordstat_items = wordstat_items[:self.max_niches]

            # Подгружаем настройки ВЭД один раз
            ved_raw = db.get_ved_settings()
            settings = VedSettings(**{
                k: v for k, v in ved_raw.items()
                if k in VedSettings.__dataclass_fields__
            }) if ved_raw else VedSettings()
            calc = VedCalculator(settings)

            for wi in wordstat_items:
                try:
                    # === ЭТАП 2: классификация ===
                    classification = self._stage2_classify(wi)

                    # Сохранить/обновить нишу
                    niche = Niche(
                        name_ru=wi.keyword,
                        name_en="",  # пока пусто; можно дополнить через AI в будущем
                        category=wi.category,
                        niche_type=classification["niche_type"],
                        is_seasonal=classification["is_seasonal"],
                        last_frequency=wi.frequency,
                        pain_points=niche_classifier.pain_points_as_json(
                            classification["pain_points"]
                        ),
                        created_at=datetime.now().isoformat(),
                    )
                    niche_id = db.save_niche(niche)

                    # Снимок частотности
                    db.save_demand_snapshot(DemandSnapshot(
                        niche_id=niche_id,
                        frequency=wi.frequency,
                        snapshot_date=datetime.now().date().isoformat(),
                    ))

                    niches_processed += 1

                    # === ЭТАП 3: Alibaba ===
                    ali_products, competition = self._stage3_alibaba(wi.keyword)

                    has_pain = len(classification["pain_points"]) > 0

                    for ap in ali_products:
                        # === ЭТАП 4: ВЭД-расчёт ===
                        # Нет данных о цене продажи в РФ для этого товара —
                        # для MVP используем price_usd_max * 2.5 как ориентир
                        # (можно улучшить отдельным парсером цен РФ).
                        price_cn = ap.price_usd_min or ap.price_usd_max
                        if price_cn <= 0:
                            continue

                        price_rf_guess = price_cn * calc.settings.usd_rate * 2.5

                        calc_res = calc.calculate(
                            price_cn_usd=price_cn,
                            price_rf_rub=price_rf_guess,
                            quantity=max(1, ap.moq or 1),
                            weight_kg_per_unit=ap.weight_kg or 0.5,
                            volume_cbm_per_unit=0.001,
                        )

                        # === ЭТАП 5: Вердикт ===
                        verdict = calc.verdict(
                            margin_percent=calc_res["margin_percent"],
                            margin_total_rub=calc_res["margin_total_rub"],
                            has_pain_points=has_pain,
                        )

                        product = Product(
                            niche_id=niche_id,
                            title_en=ap.title_en,
                            price_usd_min=ap.price_usd_min,
                            price_usd_max=ap.price_usd_max,
                            moq=ap.moq,
                            supplier_rating=ap.supplier_rating,
                            deals_count=ap.deals_count,
                            certificates=",".join(ap.certificates),
                            weight_kg=ap.weight_kg,
                            length_cm=ap.length_cm,
                            width_cm=ap.width_cm,
                            height_cm=ap.height_cm,
                            product_url=ap.product_url,
                            cost_total_rub=calc_res["cost_per_unit_rub"],
                            margin_percent=calc_res["margin_percent"],
                            margin_total_rub=calc_res["margin_total_rub"],
                            verdict=verdict,
                            competition_count=competition,
                            created_at=datetime.now().isoformat(),
                        )
                        db.save_product(product)
                        products_found += 1
                        if verdict == "ВЕЗЁМ":
                            profitable_count += 1

                except Exception as e:
                    logger.error(f"Niche '{wi.keyword}' failed: {e}", exc_info=True)
                    continue

            db.finish_run_log(
                run_id,
                status="done",
                niches_processed=niches_processed,
                products_found=products_found,
                profitable_count=profitable_count,
            )
            logger.info(
                f"=== Pipeline run #{run_id} DONE: niches={niches_processed}, "
                f"products={products_found}, profitable={profitable_count} ==="
            )
            return run_id

        except Exception as e:
            logger.error(f"Pipeline crashed: {e}", exc_info=True)
            db.finish_run_log(run_id, status="error", error_message=str(e))
            return run_id

    # ========= этапы =========

    def _stage1_wordstat(self):
        if not ENABLE_WORDSTAT:
            logger.info("Stage 1: Wordstat disabled, returning empty list")
            return []
        items = wordstat_parser.fetch_wordstat()
        items = [i for i in items if i.category in TARGET_CATEGORIES or not i.category]
        items.sort(key=lambda x: x.frequency, reverse=True)
        logger.info(f"Stage 1: Wordstat вернул {len(items)} ниш")
        return items

    def _stage2_classify(self, wordstat_item) -> dict:
        return niche_classifier.classify_niche(
            keyword=wordstat_item.keyword,
            category=wordstat_item.category,
            frequency=wordstat_item.frequency,
        )

    def _stage3_alibaba(self, keyword: str):
        if not ENABLE_ALIBABA:
            logger.info("Stage 3: Alibaba disabled")
            return [], 0
        return alibaba_parser.search_alibaba(keyword, self.max_products_per_niche)
