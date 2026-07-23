"""
NicheParser_China — Blueprint: Pipeline
Страница /pipeline — Kanban доска с ДВУМЯ вьюхами (переключатель сверху):

  1. «🤖 Kanban парсера» — 3 стадии автоматической обработки:
     - Идеи парсера      (гипотезы 0A + сигналы 0C, не в работе)
     - Анализ            (прогон агентов идёт или завершён без вердикта)
     - Готовые вердикты  (ВЕЗЁМ / ИЗУЧИТЬ, ждут решения пользователя)

  2. «👤 Мои сделки» — 4 стадии ручной работы пользователя после вердикта:
     - Взяла в работу    (outcome.status = "in_progress", ищет поставщика)
     - В пути            (outcome.notes содержит «оплачено» / «в пути»)
     - Получилось        (outcome.status = "success")
     - Не получилось     (outcome.status = "failed")

Товар может жить в одной вьюхе, потом переехать в другую по мере
прогресса. Отложенные (skipped) и НЕ ВЕЗЁМ — не показываем в основных
Kanban, они в архиве отдельно (можно раскрыть).

Wave-UX2 этап 5.5.
"""

from __future__ import annotations

import logging

from flask import Blueprint, render_template

from src.db import database as db

logger = logging.getLogger(__name__)

bp = Blueprint("pipeline", __name__, url_prefix="/pipeline")


@bp.route("/")
def index():
    """Страница /pipeline: два Kanban через переключатель."""
    products = db.get_top_products(limit=200)
    outcomes_map = db.get_product_outcomes_map()

    for p in products:
        p["outcome"] = outcomes_map.get(int(p.get("id") or 0))

    parser_cols, my_cols, archive = _split_two_kanbans(products)

    return render_template(
        "pipeline/index.html",
        parser_cols=parser_cols,
        my_cols=my_cols,
        archive=archive,
        totals={
            "parser": sum(len(v) for v in parser_cols.values()),
            "my": sum(len(v) for v in my_cols.values()),
            "archive": len(archive),
        },
    )


def _split_two_kanbans(
    products: list[dict],
) -> tuple[dict[str, list[dict]], dict[str, list[dict]], list[dict]]:
    """
    Раскидать товары по двум Kanban + архив.

    Логика приоритетов:
      1. Если outcome.status = success/failed → «Мои сделки» (готовые сделки).
      2. Если outcome.status = in_progress    → «Мои сделки» (в работе или в пути,
                                                 различается по notes).
      3. Если outcome.status = skipped        → архив (пользователь отложил).
      4. Если verdict = НЕ ВЕЗЁМ              → архив.
      5. Если verdict = ВЕЗЁМ/ИЗУЧИТЬ         → «Kanban парсера» → verdict.
      6. Есть alibaba_offers, но нет verdict  → «Kanban парсера» → analysis.
      7. Всё остальное                        → «Kanban парсера» → ideas.
    """
    parser_cols: dict[str, list[dict]] = {
        "ideas": [],       # ещё не проходили полный анализ
        "analysis": [],    # прогон агентов идёт или завершён без итога
        "verdict": [],     # готовый вердикт ВЕЗЁМ/ИЗУЧИТЬ
    }
    my_cols: dict[str, list[dict]] = {
        "working": [],     # взяла в работу — ищу поставщика / веду переговоры
        "shipping": [],    # оплачено / в пути
        "success": [],     # успешно доставлено и продано
        "failed": [],      # не получилось
    }
    archive: list[dict] = []  # skipped + НЕ ВЕЗЁМ

    for p in products:
        o = p.get("outcome") or {}
        status = (o.get("status") or "").lower()
        verdict = p.get("verdict") or ""

        if status == "success":
            my_cols["success"].append(p)
            continue
        if status == "failed":
            my_cols["failed"].append(p)
            continue
        if status == "in_progress":
            notes = (o.get("notes") or "").lower()
            if any(k in notes for k in ("оплач", "в пути", "shipping", "везу")):
                my_cols["shipping"].append(p)
            else:
                my_cols["working"].append(p)
            continue
        if status == "skipped":
            archive.append(p)
            continue

        # Ниже — без outcome, распределяем по вердикту
        if verdict == "НЕ ВЕЗЁМ":
            archive.append(p)
        elif verdict in ("ВЕЗЁМ", "ИЗУЧИТЬ"):
            parser_cols["verdict"].append(p)
        elif p.get("alibaba_offers") or p.get("ved_cost_per_unit_rub"):
            parser_cols["analysis"].append(p)
        else:
            parser_cols["ideas"].append(p)

    return parser_cols, my_cols, archive
