"""
NicheParser_China — Flask Web Application
Роуты: дашборд, карточка ниши/товара, история, настройки + JSON API.
"""

import json
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
    ENABLE_AVITO, ENABLE_WORDSTAT, ENABLE_ALIBABA,
    USE_MOCK_WORDSTAT, USE_MOCK_ALIBABA, USE_MOCK_AVITO,
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
from src.pipeline.agents.supplier_audit import audit_suppliers
from src.pipeline.agents.industry_explorer import explore_industry, explore_hs_category, INDUSTRIES
from src.pipeline.agents.hypothesis_critic import critique_hypotheses
from src.pipeline.agents.hypothesis_scorer import validate_and_score, recompute_with_dr
from src.pipeline.agents.import_detector import detect_import_signals
from src.pipeline.agents.tender_reader import read_tenders
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
# обязана содержать <input name="csrf_token" value="{{ csrf_token() }}".
# Таймаут 24 часа: иначе оставленная открытой страница «протухает» через час
# и пользователь видит 400 при нажатии «Найти товары» (по умолчанию у
# flask_wtf — 3600 сек). В dev-сценарии это раздражает; в проде безопасности
# никакой не теряем — токен всё ещё нужен и привязан к сессии.
app.config["WTF_CSRF_TIME_LIMIT"] = 60 * 60 * 24
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
_last_niche_run: dict = {"niche": "", "products": [], "finished_at": ""}


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
            supplier_score=int(p.get("supplier_score") or 0),
            supplier_risk_level=p.get("supplier_risk_level") or "",
            supplier_audit_recommendation=p.get("supplier_audit_recommendation") or "",
            supplier_audit_source=p.get("supplier_audit_source") or "",
            supplier_red_flags=json.dumps(p.get("supplier_red_flags") or [], ensure_ascii=False),
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
            "alibaba_mock": USE_MOCK_ALIBABA,
            "avito": ENABLE_AVITO,
            "avito_mock": USE_MOCK_AVITO,
        },
    }


@app.template_filter("ago")
def _filter_ago(iso_dt: Optional[str]) -> str:
    """ISO-строка → '5 мин назад' / 'сегодня 14:23' / '12.05.2026'."""
    if not iso_dt:
        return ""
    try:
        dt = datetime.fromisoformat(iso_dt)
    except (ValueError, TypeError):
        return ""
    now = datetime.now()
    delta = (now - dt).total_seconds()
    if delta < 60:
        return "только что"
    if delta < 3600:
        return f"{int(delta // 60)} мин назад"
    if delta < 86400 and dt.date() == now.date():
        return f"сегодня {dt.strftime('%H:%M')}"
    if delta < 172800 and (now.date() - dt.date()).days == 1:
        return f"вчера {dt.strftime('%H:%M')}"
    return dt.strftime("%d.%m.%Y %H:%M")


# ============ Pages ============

def _load_latest_import_signals() -> Optional[dict]:
    """
    Wave 6 — читаем последний прогон Agent 0C из БД + приклеиваем
    тендерные сигналы Agent 0D по этому же batch (LEFT JOIN по hs_code).
    Возвращаем ПЛОСКИЙ список сигналов, отсортированных по composite_score DESC.
    """
    batch_id = db.get_latest_import_signals_batch()
    if not batch_id:
        return None
    rows = db.get_import_signals_by_batch(batch_id)
    if not rows:
        return None

    # Свежие тендерные сигналы по этому же batch (по hs_code)
    tender_by_hs = db.get_tender_signals_for_import_batch(batch_id)
    for r in rows:
        r["tender"] = tender_by_hs.get(r.get("hs_code") or "")

    counts = {"hot": 0, "growing": 0, "medium": 0, "weak": 0}
    for r in rows:
        counts[r.get("classification") or "weak"] = counts.get(r.get("classification") or "weak", 0) + 1
    return {
        "batch_id": batch_id,
        "created_at": rows[0].get("created_at", ""),
        "period_current": rows[0].get("period_current", 0),
        "period_prev": rows[0].get("period_prev", 0),
        "total": len(rows),
        "signals": rows,
        "counts": counts,
        # Признак что хотя бы у одного сигнала есть тендерные данные
        "has_tender_data": any(r.get("tender") for r in rows),
    }


def _hydrate_industry_run_from_db() -> dict:
    """
    Если `_last_industry_run` пуст (после рестарта сервера) — подгружаем
    последний batch гипотез из БД. Нормализуем под ту же структуру, что
    создаёт explore_industry_route, чтобы шаблон рендерил одинаково.
    Возвращаем dict с {industry, industry_label, hypotheses, batch_id,
    finished_at} либо пустой dict если в БД ничего нет.
    """
    batches = db.get_hypothesis_batches(limit=1)
    if not batches:
        return {"industry": "", "industry_label": "", "hypotheses": [],
                "batch_id": "", "finished_at": ""}

    batch = batches[0]
    rows = db.get_hypotheses_by_batch(batch["batch_id"])
    hypotheses = [
        {
            "id": r["id"],
            "niche_name": r["niche_name"],
            "pain": r["pain"],
            "china_solution": r["china_solution"],
            "why_free": r["why_free"],
            "llm_confidence": r["llm_confidence"],
            "critic_score": r["critic_score"],
            "critic_reasons": r["critic_reasons_list"],
            "regulatory_risk": r.get("regulatory_risk") or "",
            "score_total": r["score_total"],
            "score_breakdown": r["score_breakdown_dict"],
            "deal_readiness": r["deal_readiness"],
        }
        for r in rows
    ]
    # Сортируем как в свежем прогоне — по убыванию балла
    hypotheses.sort(key=lambda x: -(x.get("score_total") or -1))

    industry_label = INDUSTRIES.get(batch["industry"], {}).get("label", batch["industry"])
    return {
        "industry": batch["industry"],
        "industry_label": industry_label,
        "hypotheses": hypotheses,
        "batch_id": batch["batch_id"],
        "finished_at": batch["created_at"],
    }


def _dashboard_context(extra: Optional[dict] = None) -> dict:
    """Собирает контекст дашборда. Используется обычным GET и страницей с генерацией."""
    filters = _read_filters(request.args)
    top_products = db.get_top_products(limit=20, filters=filters)
    niches = db.get_all_niches()
    settings = db.get_ved_settings()
    demand_timeline = db.get_demand_timeline(limit_niches=5)
    active_run = db.get_active_run()

    unique_niches = len(niches)
    profitable = len([p for p in top_products if p.get("verdict") == "ВЕЗЁМ"])
    total_runs = db.count_runs()
    # 10 последних прогонов на странице — больше скроллить тяжело.
    # Старше — пока никуда не показываем, но в БД остаются (пагинацию
    # сделаем когда реально понадобится копаться в архиве).
    runs_history = db.get_runs_grouped(limit_runs=10)

    # Если в памяти процесса пусто (свежий старт сервера) — подгружаем
    # последний batch гипотез из БД. Иначе после рестарта дашборд бы
    # показывал «нет гипотез», хотя в БД они есть.
    industry_run = _last_industry_run
    if not industry_run.get("hypotheses"):
        industry_run = _hydrate_industry_run_from_db()

    ctx = {
        "products": top_products,
        "niches": niches,
        "settings": settings,
        "stats": {
            "unique_niches": unique_niches,
            "total_runs": total_runs,
            "profitable": profitable,
            "usd_rate": settings.get("usd_rate", 0),
        },
        "demand_timeline": demand_timeline,
        "runs_history": runs_history,
        "filters": filters,
        "active_run": active_run,
        # Agent 0A — Industry Explorer
        "industries": [
            {"key": k, "label": v["label"]} for k, v in INDUSTRIES.items()
        ],
        "industry_run": industry_run,
        # Wave 5E — список вопросов для DR-чеклиста (rendered в шаблоне)
        "dr_questions": db.DR_QUESTIONS,
        # Wave 6 — сигналы Agent 0C (Import Detector). Подгружаем последний
        # прогон из БД; None если детектор ещё ни разу не запускался.
        "import_signals": _load_latest_import_signals(),
        # Подмешиваем последний поиск через форму ниши, чтобы результаты не
        # пропадали при следующих переходах/запросах. Хранится в памяти
        # процесса (см. _last_niche_run).
        "generated_products": _last_niche_run["products"],
        "generated_niche": _last_niche_run["niche"],
        "generated_finished_at": _last_niche_run.get("finished_at", ""),
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

def _run_full_pipeline(niche: str) -> Optional[str]:
    """
    Прогнать строку ниши через все 8 агентов. Обновляет _last_niche_run и
    сохраняет в БД. Возвращает None при успехе или строку с ошибкой при провале.
    Вызывается из /run-niche (юзер ввёл строку) и /take-hypothesis/<id>
    (юзер выбрал гипотезу из Agent 0A).
    """
    try:
        products = generate_products(niche)
    except Exception as e:
        logger.error(f"Agent 1 unexpected error: {type(e).__name__}: {e}")
        return "Не удалось сгенерировать товары — проверь логи"

    if not products:
        return (
            "AI не вернул товары. Возможные причины: пустой OPENROUTER_API_KEY, "
            "лимит free-модели или неожиданный формат ответа. Смотри logs/."
        )

    _run_agents_2_to_8(products)

    _last_niche_run["niche"] = niche
    _last_niche_run["products"] = products
    _last_niche_run["finished_at"] = datetime.now().isoformat()

    try:
        saved = _persist_run(products)
        if saved:
            logger.info(f"pipeline: '{niche}' — сохранено {saved} ниш в БД")
    except Exception as e:
        logger.error(f"_persist_run failed for '{niche}': {type(e).__name__}: {e}")

    return None


def _run_agents_2_to_8(products: list) -> None:
    """Прогон Агентов 2-8 по уже сгенерированному списку товаров (мутирует)."""
    try:
        check_demand(products)
    except Exception as e:
        logger.error(f"Agent 2 unexpected error: {e}")
        for p in products:
            p.setdefault("frequency", 0)

    try:
        filter_niches(products)
    except Exception as e:
        logger.error(f"Agent 3 unexpected error: {e}")
        for p in products:
            p.setdefault("keep", True)
            p.setdefault("filter_reason", "фильтр упал")

    try:
        find_on_alibaba(products, top_per_query=5)
    except Exception as e:
        logger.error(f"Agent 4 unexpected error: {e}")
        for p in products:
            p.setdefault("alibaba_offers", [])
            p.setdefault("alibaba_min_usd", 0.0)
            p.setdefault("alibaba_min_moq", 0)

    try:
        find_on_avito(products, top_per_query=10)
    except Exception as e:
        logger.error(f"Agent 6 unexpected error: {e}")
        for p in products:
            p.setdefault("avito_offers", [])
            p.setdefault("avito_price_rub_median", 0.0)
            p.setdefault("avito_listings_count", 0)

    try:
        run_ved(products)
    except Exception as e:
        logger.error(f"Agent 5 unexpected error: {e}")

    try:
        issue_verdicts(products)
    except Exception as e:
        logger.error(f"Agent 7 unexpected error: {e}")
        for p in products:
            p.setdefault("verdict", "ИЗУЧИТЬ")
            p.setdefault("verdict_reason", "вердикт-агент упал")
            p.setdefault("verdict_source", "arithmetic")

    # ВАЖНО: Agent 8 (supplier audit) НЕ запускается в общем пайплайне.
    # Правильная цепочка: гипотеза → ниша → выбор конкретного товара → ТОЛЬКО
    # ТОГДА аудит поставщика. Иначе система делает дорогую LLM-работу по
    # поставщикам для товаров, которые юзер ещё не решила везти.
    # Триггер аудита — кнопка «Найти поставщиков» на /product/<id>.
    for p in products:
        p.setdefault("supplier_score", 0)
        p.setdefault("supplier_red_flags", [])
        p.setdefault("supplier_risk_level", "")
        p.setdefault("supplier_audit_recommendation", "")
        p.setdefault("supplier_audit_source", "")


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

    err = _run_full_pipeline(niche)
    if err:
        flash(err, "error")
        return redirect(url_for("dashboard"))

    return render_template("dashboard.html", **_dashboard_context())


@app.route("/take-hypothesis/<int:hyp_id>", methods=["POST"])
def take_hypothesis(hyp_id: int):
    """
    Wave 5D — пользователь выбрал гипотезу из Agent 0A и хочет полный анализ.
    Берём niche_name гипотезы и прогоняем через тот же 8-агентный пайплайн,
    что и /run-niche.
    """
    h = db.get_hypothesis_by_id(hyp_id)
    if not h:
        flash("Гипотеза не найдена — возможно удалена", "error")
        return redirect(url_for("dashboard"))

    niche = h["niche_name"]
    logger.info(f"/take-hypothesis: {hyp_id} → '{niche}' (industry={h['industry']})")
    err = _run_full_pipeline(niche)
    if err:
        flash(err, "error")
        return redirect(url_for("dashboard"))

    flash(f"Гипотеза «{niche}» взята в работу — прогнан полный пайплайн", "success")
    return render_template("dashboard.html", **_dashboard_context())


@app.route("/save-deal-readiness/<int:hyp_id>", methods=["POST"])
def save_deal_readiness_route(hyp_id: int):
    """
    Wave 5E — пользователь заполнил/изменил DR-чеклист по 7 вопросам.
    Сохраняем в БД, пересчитываем factors.demo и factors.ops + score_total,
    обновляем гипотезу в БД и в памяти (для немедленного показа на дашборде).
    """
    h = db.get_hypothesis_by_id(hyp_id)
    if not h:
        flash("Гипотеза не найдена", "error")
        return redirect(url_for("dashboard"))

    answers = {key: 1 if request.form.get(key) else 0 for key, _ in db.DR_QUESTIONS}
    notes = (request.form.get("dr_notes") or "").strip()[:1000]

    db.save_deal_readiness(hyp_id, answers, notes)

    # Пересчёт скоринга: берём текущий breakdown из БД и накладываем DR
    breakdown = h.get("score_breakdown_dict") or {}
    if breakdown.get("factors"):
        new_breakdown = recompute_with_dr(breakdown, int(h.get("critic_score", -1)), answers)
        db.update_hypothesis_score(hyp_id, int(new_breakdown["total"]), new_breakdown)
    else:
        new_breakdown = breakdown

    # Обновляем in-memory представление: меняем deal_readiness + score + сортируем
    yes_count = sum(answers.values())
    updated_dr = dict(answers)
    updated_dr.update({"notes": notes, "yes_count": yes_count})
    for item in _last_industry_run.get("hypotheses", []):
        if item.get("id") == hyp_id:
            item["deal_readiness"] = updated_dr
            if new_breakdown:
                item["score_total"] = int(new_breakdown.get("total", item.get("score_total", -1)))
                item["score_breakdown"] = new_breakdown
            break
    _last_industry_run.get("hypotheses", []).sort(
        key=lambda x: -(x.get("score_total") or -1)
    )

    flash(f"Deal Readiness сохранён: {yes_count}/7. Скоринг обновлён.", "success")
    return render_template("dashboard.html", **_dashboard_context())


# Память процесса для последнего прогона Agent 0A — чтобы свежие гипотезы
# показывались на дашборде сразу без обращения к БД.
_last_industry_run: dict = {"industry": "", "industry_label": "", "hypotheses": [],
                            "batch_id": "", "finished_at": ""}


@app.route("/explore-industry", methods=["POST"])
def explore_industry_route():
    """
    Agent 0A: пользователь выбирает индустрию → LLM генерит 30-40 гипотез
    о свободных нишах. Гипотезы сохраняются в БД и в памяти процесса.
    """
    industry_key = (request.form.get("industry") or "").strip()
    if industry_key not in INDUSTRIES:
        flash("Выбери одну из доступных индустрий", "warning")
        return redirect(url_for("dashboard"))

    logger.info(f"/explore-industry: запуск Agent 0A по '{industry_key}'")
    hypotheses, batch_id, source = explore_industry(industry_key)

    if source != "llm" or not hypotheses:
        flash("Agent 0A не смог сгенерировать гипотезы (проверь OPENROUTER_API_KEY и интернет)", "error")
        return redirect(url_for("dashboard"))

    # Agent 0B: критика гипотез (обязательно по плану v3). Мутирует объекты на месте.
    try:
        critique_hypotheses(hypotheses)
    except Exception as e:
        logger.error(f"Agent 0B failed: {type(e).__name__}: {e}")

    # Wave 5C — валидация + 7-факторный скоринг. Мутирует + сортирует по баллу.
    try:
        validate_and_score(hypotheses)
    except Exception as e:
        logger.error(f"validate_and_score failed: {type(e).__name__}: {e}")

    # Сохраняем в БД (с critic + score полями) и забираем присвоенные id
    hyp_ids = []
    try:
        hyp_ids = db.save_hypotheses(hypotheses)
    except Exception as e:
        logger.error(f"save_hypotheses failed: {type(e).__name__}: {e}")

    # В память процесса — для немедленного показа на дашборде
    _last_industry_run["industry"] = industry_key
    _last_industry_run["industry_label"] = INDUSTRIES[industry_key]["label"]
    _last_industry_run["hypotheses"] = [
        {
            "id": hyp_ids[i] if i < len(hyp_ids) else None,
            "niche_name": h.niche_name, "pain": h.pain,
            "china_solution": h.china_solution, "why_free": h.why_free,
            "llm_confidence": h.llm_confidence,
            "critic_score": h.critic_score,
            "critic_reasons": json.loads(h.critic_reasons or "[]"),
            "regulatory_risk": h.regulatory_risk or "",
            "score_total": h.score_total,
            "score_breakdown": json.loads(h.score_breakdown or "{}"),
            "deal_readiness": None,  # ещё не заполнен пользователем
        }
        for i, h in enumerate(hypotheses)
    ]
    _last_industry_run["batch_id"] = batch_id
    _last_industry_run["finished_at"] = datetime.now().isoformat()

    flash(f"Agent 0A+0B+скоринг: {len(hypotheses)} гипотез по «{INDUSTRIES[industry_key]['label']}»", "success")
    return render_template("dashboard.html", **_dashboard_context())


@app.route("/run-import-detector", methods=["POST"])
def run_import_detector_route():
    """
    Wave 6 — Agent 0C: Import Detector.
    Тянет статистику UN Comtrade по ~60 категориям (Китай→РФ + Китай→мир),
    считает Δ и «специфически российский сигнал», сохраняет в БД.
    Синхронный — прогон 10-30 сек (4 запроса к Comtrade). На фронте лоадер.
    """
    import uuid
    logger.info("Agent 0C: старт детектора импорт-сигналов")
    try:
        signals = detect_import_signals()
    except Exception as e:
        logger.error(f"Agent 0C failed: {type(e).__name__}: {e}")
        flash("Детектор упал — проверь логи. Возможно UN Comtrade недоступен.", "error")
        return redirect(url_for("dashboard"))

    if not signals:
        flash("Детектор не получил данных из UN Comtrade (пусто)", "warning")
        return redirect(url_for("dashboard"))

    batch = uuid.uuid4().hex[:12]
    try:
        db.save_import_signals(signals, batch)
    except Exception as e:
        logger.error(f"save_import_signals failed: {type(e).__name__}: {e}")

    hot = sum(1 for s in signals if s.classification == "hot")
    growing = sum(1 for s in signals if s.classification == "growing")
    flash(
        f"Agent 0C: {len(signals)} категорий проверено, {hot} 🔥HOT + {growing} 🟢растущих",
        "success",
    )
    return redirect(url_for("dashboard"))


@app.route("/run-tender-reader", methods=["POST"])
def run_tender_reader_route():
    """
    Wave 6 — Agent 0D: Tender Reader.
    Прогоняет последний batch сигналов Agent 0C через веб-поиск zakupki.gov.ru.
    Для каждой категории тянет свежие тендеры за 90 дней, считает счётчик +
    цены + активность. Занимает ~1-2 минуты (60 категорий × ~1-2 сек + паузы).
    """
    import uuid
    logger.info("Agent 0D: старт tender reader")

    import_batch_id = db.get_latest_import_signals_batch()
    if not import_batch_id:
        flash("Сначала запусти детектор импорта (Agent 0C) — тендерам нужен список категорий", "warning")
        return redirect(url_for("dashboard"))

    categories = db.get_import_signals_by_batch(import_batch_id)
    if not categories:
        flash("Batch импорт-сигналов пуст", "warning")
        return redirect(url_for("dashboard"))

    try:
        signals = read_tenders(categories, days_window=90, sleep_between=0.6)
    except Exception as e:
        logger.error(f"Agent 0D failed: {type(e).__name__}: {e}")
        flash(f"Tender Reader упал: {type(e).__name__}. Проверь VPN split-tunnel для zakupki.gov.ru", "error")
        return redirect(url_for("dashboard"))

    if not signals:
        flash("Agent 0D не вернул сигналов", "warning")
        return redirect(url_for("dashboard"))

    tender_batch = uuid.uuid4().hex[:12]
    try:
        db.save_tender_signals(signals, tender_batch, import_batch_id)
    except Exception as e:
        logger.error(f"save_tender_signals failed: {type(e).__name__}: {e}")

    errs = sum(1 for s in signals if s.error)
    high = sum(1 for s in signals if s.tender_activity == "high")
    medium = sum(1 for s in signals if s.tender_activity == "medium")
    if errs == len(signals):
        flash(
            f"Agent 0D: все {errs} запросов упали — вероятно zakupki.gov.ru недоступен. "
            "Проверь что VPN в split-tunnel режиме (сайт режет иностранные IP через Qrator)",
            "error",
        )
    else:
        flash(
            f"Agent 0D: {len(signals)} категорий, {high} 🔥high + {medium} 🟢medium активности" +
            (f", {errs} ошибок" if errs else ""),
            "success",
        )
    return redirect(url_for("dashboard"))


@app.route("/take-category/<hs_code>", methods=["POST"])
def take_category_route(hs_code: str):
    """
    Wave 6 — мост Agent 0C → Agent 0A.
    Пользователь нажал «Взять категорию в работу» на карточке сигнала.
    Дёргаем explore_hs_category(), который просит LLM сгенерировать
    конкретные подниши ВНУТРИ выбранной HS-4 категории (не всей индустрии,
    а именно подтипов товаров в группе).

    Название категории и контекст рынка берём из последнего batch'а
    Agent 0C в БД — сигнал должен там быть.
    """
    hs_code = (hs_code or "").strip()
    if not hs_code:
        flash("Не указан HS-код категории", "error")
        return redirect(url_for("dashboard"))

    latest_batch = db.get_latest_import_signals_batch()
    if not latest_batch:
        flash("Нет свежего прогона детектора — сначала запусти его", "warning")
        return redirect(url_for("dashboard"))

    signals = db.get_import_signals_by_batch(latest_batch)
    signal = next((s for s in signals if s.get("hs_code") == hs_code), None)
    if not signal:
        flash(f"Категория HS {hs_code} не найдена в последнем прогоне", "error")
        return redirect(url_for("dashboard"))

    category_name = signal.get("category_name") or f"HS {hs_code}"
    # Дадим LLM короткий контекст сигнала — растёт / стабильно / хайп
    context_parts = [
        f"Китайский экспорт в РФ за {signal.get('period_current')}: "
        f"${(signal.get('value_current_usd') or 0)/1e6:.0f}M",
        f"ΔРФ={signal.get('delta_ru_percent'):+.0f}%, "
        f"ΔМир={signal.get('delta_world_percent'):+.0f}%, "
        f"специфически-российский сигнал {signal.get('russia_specific_pp'):+.0f} п.п.",
    ]
    if signal.get("trend_stability_label"):
        context_parts.append(f"Форма 4-летнего тренда: {signal['trend_stability_label']}")
    market_context = "\n".join(context_parts)

    logger.info(f"/take-category: HS {hs_code} ({category_name}) → Agent 0A")
    hypotheses, batch_id, source = explore_hs_category(hs_code, category_name, market_context)

    if source != "llm" or not hypotheses:
        flash(f"LLM не смог сгенерировать подниши для HS {hs_code}", "error")
        return redirect(url_for("dashboard"))

    # Agent 0B (Critic) + Wave 5C scoring — как в обычном explore_industry_route
    try:
        critique_hypotheses(hypotheses)
    except Exception as e:
        logger.error(f"Agent 0B failed: {type(e).__name__}: {e}")
    try:
        validate_and_score(hypotheses)
    except Exception as e:
        logger.error(f"validate_and_score failed: {type(e).__name__}: {e}")

    hyp_ids: list = []
    try:
        hyp_ids = db.save_hypotheses(hypotheses)
    except Exception as e:
        logger.error(f"save_hypotheses failed: {type(e).__name__}: {e}")

    industry_key = f"hs-{hs_code}"
    _last_industry_run["industry"] = industry_key
    _last_industry_run["industry_label"] = f"{category_name} (HS {hs_code})"
    _last_industry_run["hypotheses"] = [
        {
            "id": hyp_ids[i] if i < len(hyp_ids) else None,
            "niche_name": h.niche_name, "pain": h.pain,
            "china_solution": h.china_solution, "why_free": h.why_free,
            "llm_confidence": h.llm_confidence,
            "critic_score": h.critic_score,
            "critic_reasons": json.loads(h.critic_reasons or "[]"),
            "regulatory_risk": h.regulatory_risk or "",
            "score_total": h.score_total,
            "score_breakdown": json.loads(h.score_breakdown or "{}"),
            "deal_readiness": None,
        }
        for i, h in enumerate(hypotheses)
    ]
    _last_industry_run["batch_id"] = batch_id
    _last_industry_run["finished_at"] = datetime.now().isoformat()

    flash(
        f"Agent 0A по HS {hs_code} «{category_name}»: {len(hypotheses)} подниш сгенерировано",
        "success",
    )
    return redirect(url_for("dashboard"))


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


@app.route("/product/<int:product_id>/audit-suppliers", methods=["POST"])
def audit_suppliers_route(product_id: int):
    """
    Ручной триггер Agent 8 для одного выбранного товара (Wave 5D gate).
    Запускается ТОЛЬКО по кнопке на /product/<id> — никогда автоматически,
    чтобы не упахивать LLM на товарах, которые юзер не решила везти.
    """
    product = db.get_product_by_id(product_id)
    if not product:
        abort(404)

    # Собираем псевдо-оффер из полей сохранённого Product — Agent 8 ждёт
    # формат словарей пайплайна: список товаров, у каждого alibaba_offers
    # и ved_best_offer.url для выбора нужного оффера.
    certs_list = []
    raw_certs = product.get("certificates") or ""
    if raw_certs:
        certs_list = [c.strip() for c in raw_certs.split(",") if c.strip()]

    offer = {
        "title_en": product.get("title_en") or "",
        "price_usd_min": float(product.get("price_usd_min") or 0),
        "price_usd_max": float(product.get("price_usd_max") or 0),
        "moq": int(product.get("moq") or 0),
        "supplier_rating": float(product.get("supplier_rating") or 0),
        "deals_count": int(product.get("deals_count") or 0),
        "certificates": certs_list,
        "weight_kg": float(product.get("weight_kg") or 0),
        "product_url": product.get("product_url") or "",
    }
    pseudo = [{
        "title_en": product.get("title_en") or "",
        "title_ru": product.get("niche_name_ru") or "",
        "keep": True,
        "alibaba_offers": [offer],
        "ved_best_offer": {"url": offer["product_url"]},
        "verdict": product.get("verdict") or "",
        "frequency": int(product.get("last_frequency") or 0),
    }]

    try:
        audit_suppliers(pseudo)
    except Exception as e:
        logger.error(f"Agent 8 (manual) failed for product {product_id}: {type(e).__name__}: {e}")
        flash("Аудит поставщика упал — проверь логи", "error")
        return redirect(url_for("product_detail", product_id=product_id))

    p = pseudo[0]
    db.update_supplier_audit(
        product_id,
        score=int(p.get("supplier_score") or 0),
        risk_level=p.get("supplier_risk_level") or "",
        recommendation=p.get("supplier_audit_recommendation") or "",
        source=p.get("supplier_audit_source") or "",
        red_flags=p.get("supplier_red_flags") or [],
    )
    flash(
        f"Аудит поставщика: {p.get('supplier_score', 0)}/100, риск «{p.get('supplier_risk_level', '—')}»",
        "success",
    )
    return redirect(url_for("product_detail", product_id=product_id))


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
