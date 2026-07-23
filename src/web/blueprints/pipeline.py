"""
NicheParser_China — Blueprint: Pipeline
Страница /pipeline — Kanban доска «Твой процесс».

6 колонок:
  1. Идеи        — гипотезы 0A + сигналы 0C которые ещё не в работе
  2. Анализ      — товары где прогон агентов идёт/завершён без вердикта
  3. Вердикт     — товары с готовым ВЕЗЁМ/ИЗУЧИТЬ
  4. Поставщик   — outcome.status = "in_progress" (взяла в работу)
  5. В пути      — outcome.notes содержит «оплачено» или отдельный статус
  6. Итог        — outcome.status = "success" или "failed"

Одна карточка = один товар с полной историей от идеи до фактической маржи.

Wave-UX2 этап 5. Пока — заглушка, наполнение в следующем коммите.
"""

from __future__ import annotations

import logging

from flask import Blueprint, render_template

from src.db import database as db

logger = logging.getLogger(__name__)

bp = Blueprint("pipeline", __name__, url_prefix="/pipeline")


@bp.route("/")
def index():
    """Kanban-доска: 6 колонок со стадиями сделки."""
    # Пока — плоский список для отрисовки первого этапа Kanban.
    # На этапе 5 распределим по колонкам через outcome.status.
    products = db.get_top_products(limit=200)
    outcomes_map = db.get_product_outcomes_map()

    for p in products:
        p["outcome"] = outcomes_map.get(int(p.get("id") or 0))

    columns = _split_by_stage(products)

    return render_template(
        "pipeline/index.html",
        columns=columns,
    )


def _split_by_stage(products: list[dict]) -> dict[str, list[dict]]:
    """
    Раскидать товары по 6 колонкам pipeline по правилам:
      - outcome.status приоритетен если задан
      - иначе смотрим на verdict
      - иначе — «Идеи»
    """
    columns: dict[str, list[dict]] = {
        "ideas": [],       # ещё не проверяли или отброшено фильтром
        "analysis": [],    # прогон запущен, вердикта нет
        "verdict": [],     # готовый вердикт ВЕЗЁМ/ИЗУЧИТЬ, ещё не взяла
        "supplier": [],    # взяла в работу, ищет/проверяет поставщика
        "shipping": [],    # оплачено, товар в пути
        "done": [],        # success/failed
    }

    for p in products:
        o = p.get("outcome") or {}
        status = (o.get("status") or "").lower()
        verdict = p.get("verdict") or ""

        if status in ("success", "failed"):
            columns["done"].append(p)
        elif status == "in_progress":
            # взяла в работу — по заметкам может быть уже в пути,
            # пока не различаем shipping/supplier (расширим на этапе 5)
            notes = (o.get("notes") or "").lower()
            if "оплач" in notes or "в пути" in notes or "shipping" in notes:
                columns["shipping"].append(p)
            else:
                columns["supplier"].append(p)
        elif status == "skipped":
            columns["ideas"].append(p)  # отложила — возвращаем в idea backlog
        elif verdict in ("ВЕЗЁМ", "ИЗУЧИТЬ"):
            columns["verdict"].append(p)
        elif verdict == "НЕ ВЕЗЁМ":
            columns["ideas"].append(p)  # не подошло — в архив идей
        else:
            columns["analysis"].append(p)

    return columns
