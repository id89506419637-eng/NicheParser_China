"""
NicheParser_China — Flask Web Application
Роуты: дашборд, карточка ниши/товара, история, настройки + JSON API.
"""

import logging
import os
import threading

from flask import (
    Flask, render_template, request, redirect, url_for,
    jsonify, flash, abort,
)

from core.config import (
    SECRET_KEY, TARGET_CATEGORIES, NICHE_TYPES, VERDICTS,
    ENABLE_AVITO, ENABLE_WORDSTAT, ENABLE_ALIBABA, USE_MOCK_WORDSTAT,
)
from src.db import database as db
from src.pipeline.runner import PipelineRunner
from src.calculator.ved_calculator import VedCalculator, fetch_cbr_rates
from core.models import VedSettings

logger = logging.getLogger(__name__)

app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(__file__), "templates"),
    static_folder=os.path.join(os.path.dirname(__file__), "static"),
)
app.secret_key = SECRET_KEY


# === Глобальный лок: не даём запускать два пайплайна разом ===
_run_lock = threading.Lock()


@app.context_processor
def inject_globals():
    return {
        "target_categories": TARGET_CATEGORIES,
        "niche_types": NICHE_TYPES,
        "verdicts": VERDICTS,
        "features": {
            "wordstat": ENABLE_WORDSTAT,
            "wordstat_mock": USE_MOCK_WORDSTAT,
            "alibaba": ENABLE_ALIBABA,
            "avito": ENABLE_AVITO,
        },
    }


# ============ Pages ============

@app.route("/")
def dashboard():
    filters = _read_filters(request.args)
    top_products = db.get_top_products(limit=20, filters=filters)
    niches = db.get_all_niches()
    settings = db.get_ved_settings()
    demand_timeline = db.get_demand_timeline(limit_niches=5)
    active_run = db.get_active_run()

    total = len(niches)
    profitable = len([p for p in top_products if p.get("verdict") == "ВЕЗЁМ"])
    avg_margin = (
        sum(p.get("margin_percent", 0) for p in top_products) / len(top_products)
        if top_products else 0
    )

    return render_template(
        "dashboard.html",
        products=top_products,
        niches=niches,
        settings=settings,
        stats={
            "total_niches": total,
            "profitable": profitable,
            "avg_margin": round(avg_margin, 1),
            "usd_rate": settings.get("usd_rate", 0),
        },
        demand_timeline=demand_timeline,
        filters=filters,
        active_run=active_run,
    )


@app.route("/niche/<int:niche_id>")
def niche_detail(niche_id: int):
    niche = db.get_niche_by_id(niche_id)
    if not niche:
        abort(404)
    products = db.get_products_by_niche(niche_id)
    history = db.get_demand_history(niche_id, days=90)
    return render_template(
        "niche_detail.html",
        niche=niche,
        products=products,
        history=history,
    )


@app.route("/product/<int:product_id>")
def product_detail(product_id: int):
    product = db.get_product_by_id(product_id)
    if not product:
        abort(404)

    # Разбивка себестоимости для отображения
    settings_raw = db.get_ved_settings()
    ved_settings = VedSettings(**{
        k: v for k, v in settings_raw.items()
        if k in VedSettings.__dataclass_fields__
    })
    calc = VedCalculator(ved_settings)
    price_cn = product.get("price_usd_min") or product.get("price_usd_max") or 0
    price_rf_guess = price_cn * calc.settings.usd_rate * 2.5
    breakdown = calc.calculate(
        price_cn_usd=price_cn,
        price_rf_rub=price_rf_guess,
        quantity=max(1, product.get("moq") or 1),
        weight_kg_per_unit=product.get("weight_kg") or 0.5,
        volume_cbm_per_unit=0.001,
    )

    return render_template(
        "product_detail.html",
        product=product,
        breakdown=breakdown,
    )


@app.route("/history")
def history():
    runs = db.get_all_runs(limit=100)
    return render_template("history.html", runs=runs)


@app.route("/settings")
def settings_page():
    ved = db.get_ved_settings()
    return render_template("settings.html", settings=ved)


# ============ Actions ============

@app.route("/run", methods=["POST"])
def run_pipeline():
    """Запуск пайплайна в фоне. Возвращает JSON со статусом запуска."""
    if not _run_lock.acquire(blocking=False):
        flash("Анализ уже запущен — дождись завершения", "warning")
        return redirect(url_for("dashboard"))

    try:
        max_niches_raw = request.form.get("max_niches", "").strip()
        max_niches = int(max_niches_raw) if max_niches_raw.isdigit() else None

        def _worker():
            try:
                runner = PipelineRunner(max_niches=max_niches)
                runner.run()
            except Exception as e:
                logger.error(f"Pipeline worker crashed: {e}", exc_info=True)
            finally:
                _run_lock.release()

        threading.Thread(target=_worker, daemon=True).start()
        flash("Анализ запущен — обнови страницу через несколько минут", "success")

    except Exception as e:
        _run_lock.release()
        logger.error(f"Failed to start pipeline: {e}")
        flash(f"Не удалось запустить анализ: {e}", "error")

    return redirect(url_for("dashboard"))


@app.route("/settings/update", methods=["POST"])
def update_settings():
    """Обновление параметров ВЭД с валидацией."""
    try:
        data = {
            "usd_rate": _float_field("usd_rate", min_val=0),
            "cny_rate": _float_field("cny_rate", min_val=0),
            "duty_percent": _float_field("duty_percent", min_val=0, max_val=100),
            "vat_percent": _float_field("vat_percent", min_val=0, max_val=100),
            "logistics_per_kg": _float_field("logistics_per_kg", min_val=0),
            "logistics_per_cbm": _float_field("logistics_per_cbm", min_val=0),
            "bank_percent": _float_field("bank_percent", min_val=0, max_val=100),
            "min_margin_percent": _float_field("min_margin_percent", min_val=0, max_val=100),
            "min_margin_total_rub": _float_field("min_margin_total_rub", min_val=0),
        }
        db.update_ved_settings(data)
        flash("Настройки обновлены", "success")
    except ValueError as e:
        flash(f"Ошибка валидации: {e}", "error")

    return redirect(url_for("settings_page"))


@app.route("/settings/refresh_rates", methods=["POST"])
def refresh_rates():
    """Принудительно обновить курсы с ЦБ РФ."""
    rates = fetch_cbr_rates(force=True)
    if rates:
        db.update_ved_settings({
            "usd_rate": rates.get("USD", 0),
            "cny_rate": rates.get("CNY", 0),
        })
        flash(f"Курсы обновлены: USD={rates.get('USD', 0):.2f}, CNY={rates.get('CNY', 0):.2f}", "success")
    else:
        flash("Не удалось получить курсы ЦБ", "error")
    return redirect(url_for("settings_page"))


@app.route("/product/<int:product_id>/delete", methods=["POST"])
def delete_product(product_id: int):
    db.delete_product(product_id)
    flash("Товар удалён", "success")
    return redirect(url_for("dashboard"))


# ============ JSON API ============

@app.route("/api/status")
def api_status():
    niches = db.get_all_niches()
    products = db.get_top_products(limit=1000)
    settings = db.get_ved_settings()
    active_run = db.get_active_run()
    return jsonify({
        "total_niches": len(niches),
        "total_products": len(products),
        "profitable_products": len([p for p in products if p.get("verdict") == "ВЕЗЁМ"]),
        "usd_rate": settings.get("usd_rate", 0),
        "cny_rate": settings.get("cny_rate", 0),
        "active_run": active_run,
    })


@app.route("/api/products")
def api_products():
    filters = _read_filters(request.args)
    limit = min(int(request.args.get("limit", 50)), 500)
    return jsonify(db.get_top_products(limit=limit, filters=filters))


@app.route("/api/demand_timeline")
def api_demand_timeline():
    return jsonify(db.get_demand_timeline(limit_niches=5))


@app.route("/api/runs/<int:run_id>")
def api_run(run_id: int):
    run = db.get_run_log(run_id)
    if not run:
        abort(404)
    return jsonify(run)


# ============ Helpers ============

def _float_field(name: str, min_val=None, max_val=None) -> float:
    raw = request.form.get(name, "").strip().replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"поле '{name}' должно быть числом")
    if min_val is not None and value < min_val:
        raise ValueError(f"поле '{name}' должно быть ≥ {min_val}")
    if max_val is not None and value > max_val:
        raise ValueError(f"поле '{name}' должно быть ≤ {max_val}")
    return value


def _read_filters(args) -> dict:
    filters = {}
    if args.get("category") in TARGET_CATEGORIES:
        filters["category"] = args["category"]
    if args.get("verdict") in VERDICTS:
        filters["verdict"] = args["verdict"]
    if args.get("niche_type") in NICHE_TYPES:
        filters["niche_type"] = args["niche_type"]
    if args.get("seasonal") in ("1", "true", "yes"):
        filters["seasonal"] = True
    elif args.get("seasonal") in ("0", "false", "no"):
        filters["seasonal"] = False
    try:
        if args.get("min_margin"):
            filters["min_margin"] = float(args["min_margin"])
    except ValueError:
        pass
    return filters


@app.errorhandler(404)
def not_found(_):
    return render_template("404.html"), 404
