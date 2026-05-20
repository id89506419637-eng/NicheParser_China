"""
NicheParser_China — Flask Web Application
Роуты: дашборд, карточка ниши/товара, история, настройки + JSON API.
"""

import logging
import os
import threading
from datetime import datetime
from typing import Optional

from flask import (
    Flask, render_template, request, redirect, url_for,
    jsonify, flash, abort,
)
from flask_wtf.csrf import CSRFProtect

from core.config import (
    SECRET_KEY, TARGET_CATEGORIES, NICHE_TYPES, VERDICTS,
    ENABLE_AVITO, ENABLE_WORDSTAT, ENABLE_ALIBABA, USE_MOCK_WORDSTAT,
    FLASK_DEBUG,
)
from src.db import database as db
from src.pipeline.runner import PipelineRunner
from src.pipeline.agents.product_generator import generate_products
from src.pipeline.agents.demand_checker import check_demand
from src.pipeline.agents.niche_filter import filter_niches
from src.pipeline.agents.alibaba_finder import find_on_alibaba
from src.pipeline.agents.avito_finder import find_on_avito
from src.pipeline.agents.ved_runner import run_ved
from src.pipeline.agents.verdict_agent import issue_verdicts
from src.calculator.ved_calculator import VedCalculator, fetch_cbr_rates
from core.models import VedSettings, Niche, Product, DemandSnapshot

logger = logging.getLogger(__name__)

app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(__file__), "templates"),
    static_folder=os.path.join(os.path.dirname(__file__), "static"),
)
app.secret_key = SECRET_KEY

# CSRF-защита всех POST-форм. В шаблонах каждая <form method="POST">
# обязана содержать <input name="csrf_token" value="{{ csrf_token() }}">.
csrf = CSRFProtect(app)

# Cookie-флаги: HttpOnly блокирует кражу через document.cookie,
# SameSite=Lax — защита от CSRF поверх токена, Secure включается
# только вне debug (в проде по HTTPS).
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=not FLASK_DEBUG,
)


# === Глобальный лок: не даём запускать два пайплайна разом ===
_run_lock = threading.Lock()

# Последний прогон через /run-niche держится в памяти процесса для богатого
# детального отображения сверху (офферы Alibaba, объявления Авито, разбивка
# ВЭД, обоснование вердикта). В БД при этом сохраняется свёрнутая версия —
# одна ниша = один лучший оффер с ВЭД — она и идёт в общий «Топ» внизу.
_last_niche_run: dict = {"niche": "", "products": []}


def _persist_run(products: list) -> int:
    """
    После /run-niche сохраняем результат в общую БД, чтобы он попал в «Топ»
    и историю. Сохраняем ТОЛЬКО keep=True — дроп-товары видны лишь в свежем
    поиске сверху (как «отброшено Агентом 3»), в БД они только засоряли бы
    статистику.

    Структура: 1 LLM-продукт → 1 niche + 1 product (лучший оффер с ВЭД).
    Возвращает число реально сохранённых ниш.
    """
    saved = 0
    today = datetime.now().date().isoformat()

    for p in products:
        if not p.get("keep", True):
            continue
        # Без лучшего оффера ВЭД не считался — сохранять нечего
        ved_offer = p.get("ved_best_offer") or {}
        if not ved_offer:
            continue

        niche = Niche(
            name_ru=p.get("title_ru") or p.get("title_en") or "?",
            name_en=p.get("title_en") or "",
            category="",
            niche_type="",
            is_seasonal=False,
            last_frequency=int(p.get("frequency") or 0),
            pain_points="[]",
            created_at=datetime.now().isoformat(),
        )
        niche_id = db.save_niche(niche)

        db.save_demand_snapshot(DemandSnapshot(
            niche_id=niche_id,
            frequency=int(p.get("frequency") or 0),
            snapshot_date=today,
        ))

        # Берём именно тот оффер, на котором считался ВЭД (по url),
        # чтобы цена/MOQ/вес в БД соответствовали маржe.
        ali_offers = p.get("alibaba_offers") or []
        best = next(
            (o for o in ali_offers if o.get("product_url") == ved_offer.get("url")),
            ali_offers[0] if ali_offers else None,
        )
        if not best:
            continue

        product = Product(
            niche_id=niche_id,
            title_en=best.get("title_en") or p.get("title_en") or "",
            price_usd_min=float(best.get("price_usd_min") or 0),
            price_usd_max=float(best.get("price_usd_max") or 0),
            moq=int(best.get("moq") or 0),
            supplier_rating=float(best.get("supplier_rating") or 0),
            deals_count=int(best.get("deals_count") or 0),
            certificates=",".join(best.get("certificates") or []),
            weight_kg=float(best.get("weight_kg") or 0),
            product_url=best.get("product_url") or "",
            cost_total_rub=float(p.get("ved_cost_per_unit_rub") or 0),
            margin_percent=float(p.get("ved_margin_percent") or 0),
            margin_total_rub=float(p.get("ved_margin_per_moq_rub") or 0),
            verdict=p.get("verdict") or "",
            avito_price_median=float(p.get("avito_price_rub_median") or 0),
            avito_listings_count=int(p.get("avito_listings_count") or 0),
            verdict_reason=p.get("verdict_reason") or "",
            verdict_source=p.get("verdict_source") or "",
            competition_count=int(p.get("alibaba_competition") or 0),
            created_at=datetime.now().isoformat(),
        )
        db.save_product(product)
        saved += 1

    return saved


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

def _dashboard_context(extra: Optional[dict] = None) -> dict:
    """Собирает контекст дашборда. Используется обычным GET и страницей с генерацией."""
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

    ctx = {
        "products": top_products,
        "niches": niches,
        "settings": settings,
        "stats": {
            "total_niches": total,
            "profitable": profitable,
            "avg_margin": round(avg_margin, 1),
            "usd_rate": settings.get("usd_rate", 0),
        },
        "demand_timeline": demand_timeline,
        "filters": filters,
        "active_run": active_run,
        # Подмешиваем последний поиск через форму ниши, чтобы результаты не
        # пропадали при следующих переходах/запросах. Хранится в памяти
        # процесса (см. _last_niche_run).
        "generated_products": _last_niche_run["products"],
        "generated_niche": _last_niche_run["niche"],
    }
    if extra:
        ctx.update(extra)
    return ctx


@app.route("/")
def dashboard():
    return render_template("dashboard.html", **_dashboard_context())


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

@app.route("/run-niche", methods=["POST"])
def run_niche():
    """Агент 1: по нише от пользователя получить 5–10 B2B-товаров через LLM."""
    niche = (request.form.get("niche") or "").strip()
    if not niche:
        flash("Введи нишу — например, «станки» или «медоборудование»", "warning")
        return redirect(url_for("dashboard"))

    if len(niche) > 80:
        flash("Слишком длинная ниша — сократи до 80 символов", "warning")
        return redirect(url_for("dashboard"))

    try:
        products = generate_products(niche)
    except Exception as e:
        logger.error(f"Agent 1 unexpected error: {type(e).__name__}: {e}")
        flash("Не удалось сгенерировать товары — проверь логи", "error")
        return redirect(url_for("dashboard"))

    if not products:
        flash(
            "AI не вернул товары. Возможные причины: пустой OPENROUTER_API_KEY, "
            "лимит free-модели или неожиданный формат ответа. Смотри logs/.",
            "error",
        )
        return redirect(url_for("dashboard"))

    # Агент 2 — обогащаем частотностью из Wordstat (mock, пока нет YANDEX_OAUTH_TOKEN).
    # Не отсеивает; просто добавляет каждой записи поле 'frequency'.
    try:
        products = check_demand(products)
    except Exception as e:
        logger.error(f"Agent 2 unexpected error: {e}")
        # Не валим страницу — просто покажем без частотности
        for p in products:
            p.setdefault("frequency", 0)

    # Агент 3 — LLM-фильтр перегретого ритейла. Не выкидывает из списка,
    # размечает каждый продукт keep=True/False + filter_reason.
    try:
        products = filter_niches(products)
    except Exception as e:
        logger.error(f"Agent 3 unexpected error: {e}")
        for p in products:
            p.setdefault("keep", True)
            p.setdefault("filter_reason", "фильтр упал")

    # Агент 4 — Alibaba. Только для keep=True. В mock-режиме быстро,
    # в реале — 5–15с на товар.
    try:
        products = find_on_alibaba(products, top_per_query=5)
    except Exception as e:
        logger.error(f"Agent 4 unexpected error: {e}")
        for p in products:
            p.setdefault("alibaba_offers", [])
            p.setdefault("alibaba_min_usd", 0.0)
            p.setdefault("alibaba_min_moq", 0)

    # Агент 6 — Avito. Тянем медианную цену продажи в РФ для замены
    # эвристики ×2.5 в ВЭД-расчёте. Идёт ДО Агента 5, чтобы тот мог
    # использовать реальный price_rf_rub. В mock-режиме мгновенно.
    try:
        products = find_on_avito(products, top_per_query=10)
    except Exception as e:
        logger.error(f"Agent 6 unexpected error: {e}")
        for p in products:
            p.setdefault("avito_offers", [])
            p.setdefault("avito_price_rub_median", 0.0)
            p.setdefault("avito_listings_count", 0)

    # Агент 5 — ВЭД-расчёт. Берёт лучший оффер Alibaba + медиану Авито
    # и считает себестоимость и маржу. Если Авито пуст — fallback на эвристику.
    try:
        products = run_ved(products)
    except Exception as e:
        logger.error(f"Agent 5 unexpected error: {e}")

    # Агент 7 — LLM-вердикт. По полному пакету данных каждому товару
    # присваивается ВЕЗЁМ / ИЗУЧИТЬ / НЕ ВЕЗЁМ + обоснование. Если LLM
    # упал — fallback на арифметику по тем же порогам.
    try:
        products = issue_verdicts(products)
    except Exception as e:
        logger.error(f"Agent 7 unexpected error: {e}")
        for p in products:
            p.setdefault("verdict", "ИЗУЧИТЬ")
            p.setdefault("verdict_reason", "вердикт-агент упал")
            p.setdefault("verdict_source", "arithmetic")

    # Запомнили результат в памяти процесса для богатого детального вида
    # сверху (с офферами/разбивкой/обоснованием) — это нужно прямо сейчас,
    # пока пользователь смотрит на страницу.
    _last_niche_run["niche"] = niche
    _last_niche_run["products"] = products

    # И параллельно сохраняем сжатую версию (одна niche + один лучший оффер)
    # в БД — чтобы результат попал в общий «Топ товаров» внизу и в /history.
    # Падение сохранения не должно ломать показ страницы.
    try:
        saved = _persist_run(products)
        if saved:
            logger.info(f"/run-niche: '{niche}' — сохранено {saved} ниш в БД")
    except Exception as e:
        logger.error(f"_persist_run failed for '{niche}': {type(e).__name__}: {e}")

    return render_template("dashboard.html", **_dashboard_context())


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
                logger.error(f"Pipeline worker crashed: {e}")
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
