"""
NicheParser_China — Blueprint: Products
Страница /products — master-detail список всех проверенных товаров.

Master: слева таблица товаров (сортировка/фильтры/поиск).
Detail: справа slide-over с деталями по 6 функциональным шагам.

Wave-UX2 этап 4: главный экран для «работы с товарами». Дашборд `/` пока
остаётся как есть (переходный период), постепенно переезжаем сюда.
"""

from __future__ import annotations

import logging
from typing import Optional

from flask import Blueprint, render_template, request, abort

from src.db import database as db
from src.analytics.certification_classifier import classify_certification

logger = logging.getLogger(__name__)

bp = Blueprint("products", __name__, url_prefix="/products")


@bp.route("/")
def index():
    """
    Страница списка всех товаров.
    Filters через GET-параметры: ?verdict=ВЕЗЁМ&min_margin=50 и т.п.
    """
    filters = {
        "verdict": (request.args.get("verdict") or "").strip() or None,
        "category": (request.args.get("category") or "").strip() or None,
        "min_margin": _parse_float(request.args.get("min_margin")),
    }
    filters = {k: v for k, v in filters.items() if v is not None}

    products = db.get_top_products(limit=100, filters=filters)

    # Обогащаем сертификацией + outcomes (как в основном дашборде)
    outcomes_map = db.get_product_outcomes_map()
    for p in products:
        p["certification"] = classify_certification(
            title=p.get("niche_name_ru") or p.get("title_en") or "",
            category=p.get("niche_category") or "",
        ).as_dict()
        p["outcome"] = outcomes_map.get(int(p.get("id") or 0))

    # Стат-полоска сверху
    verdicts_count = {"ВЕЗЁМ": 0, "ИЗУЧИТЬ": 0, "НЕ ВЕЗЁМ": 0}
    for p in products:
        v = p.get("verdict") or ""
        if v in verdicts_count:
            verdicts_count[v] += 1

    return render_template(
        "products/index.html",
        products=products,
        filters=filters,
        verdicts_count=verdicts_count,
        total=len(products),
    )


def _parse_float(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None
