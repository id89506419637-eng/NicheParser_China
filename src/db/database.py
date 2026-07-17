"""
NicheParser_China — Database Layer
SQLite CRUD для ниш, товаров, истории спроса, настроек ВЭД и логов запусков.
"""

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from difflib import SequenceMatcher
from typing import List, Optional

from core.config import DB_PATH
from core.models import Niche, Product, DemandSnapshot, Hypothesis


# Стоп-слова и шумные префиксы при сравнении ниш на дубликаты.
# Цель: «Аппарат УЗИ портативный» и «Портативный УЗИ-аппарат» должны считаться
# одним и тем же. Не трогаем оригинальный name_ru — только нормализуем для сравнения.
_NICHE_NORM_STOPWORDS = {
    "и", "для", "на", "по", "от", "из", "с", "в", "к", "под", "над",
    "the", "a", "an", "for", "of",
}


def _normalize_niche_name(name: str) -> str:
    """
    Нормализация для DEDUP-сравнения ниш.
    - lowercase
    - удалить пунктуацию (-, /, и т.п.)
    - убрать стоп-слова
    - отсортировать оставшиеся слова (порядок не важен)
    - результат: каноническая «отпечатка» строки.
    """
    if not name:
        return ""
    s = name.lower().strip()
    s = re.sub(r"[^\w\sЀ-ӿ]+", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    words = [w for w in s.split() if w and w not in _NICHE_NORM_STOPWORDS]
    words.sort()
    return " ".join(words)


def _find_duplicate_niche_id(conn, niche_name: str, threshold: float = 0.85) -> Optional[int]:
    """
    Вернуть id существующей ниши, которая считается дубликатом для niche_name,
    или None если такой нет. Сначала точное совпадение, потом fuzzy ≥ threshold
    по нормализованной форме.
    """
    target = _normalize_niche_name(niche_name)
    if not target:
        return None

    exact = conn.execute(
        "SELECT id FROM niches WHERE name_ru = ?", (niche_name,)
    ).fetchone()
    if exact:
        return exact["id"]

    rows = conn.execute("SELECT id, name_ru FROM niches").fetchall()
    best_id: Optional[int] = None
    best_score = 0.0
    for r in rows:
        cand = _normalize_niche_name(r["name_ru"] or "")
        if not cand:
            continue
        if cand == target:
            return r["id"]
        ratio = SequenceMatcher(None, target, cand).ratio()
        if ratio > best_score:
            best_score = ratio
            best_id = r["id"]
    if best_score >= threshold:
        return best_id
    return None


@contextmanager
def get_connection():
    """Контекстный менеджер подключения к БД с автокоммитом и закрытием."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_column(conn, table: str, column: str, ddl: str) -> None:
    """Идемпотентный ADD COLUMN: проверяем PRAGMA, добавляем только если нет."""
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db() -> None:
    """Создать таблицы и первую запись настроек ВЭД, если БД пуста."""
    with get_connection() as conn:
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS niches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name_ru TEXT NOT NULL,
                name_en TEXT DEFAULT '',
                category TEXT DEFAULT '',
                niche_type TEXT DEFAULT '',
                is_seasonal INTEGER DEFAULT 0,
                last_frequency INTEGER DEFAULT 0,
                pain_points TEXT DEFAULT '[]',
                created_at TEXT NOT NULL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                niche_id INTEGER NOT NULL,
                title_en TEXT DEFAULT '',
                price_usd_min REAL DEFAULT 0,
                price_usd_max REAL DEFAULT 0,
                moq INTEGER DEFAULT 0,
                supplier_rating REAL DEFAULT 0,
                deals_count INTEGER DEFAULT 0,
                certificates TEXT DEFAULT '',
                weight_kg REAL DEFAULT 0,
                length_cm REAL DEFAULT 0,
                width_cm REAL DEFAULT 0,
                height_cm REAL DEFAULT 0,
                product_url TEXT DEFAULT '',
                cost_total_rub REAL DEFAULT 0,
                margin_percent REAL DEFAULT 0,
                margin_total_rub REAL DEFAULT 0,
                verdict TEXT DEFAULT '',
                competition_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY (niche_id) REFERENCES niches(id) ON DELETE CASCADE
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS demand_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                niche_id INTEGER NOT NULL,
                frequency INTEGER DEFAULT 0,
                snapshot_date TEXT NOT NULL,
                FOREIGN KEY (niche_id) REFERENCES niches(id) ON DELETE CASCADE
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS ved_settings (
                id INTEGER PRIMARY KEY,
                usd_rate REAL DEFAULT 0,
                cny_rate REAL DEFAULT 0,
                duty_percent REAL DEFAULT 10,
                vat_percent REAL DEFAULT 22,
                logistics_per_kg REAL DEFAULT 3,
                logistics_per_cbm REAL DEFAULT 350,
                bank_percent REAL DEFAULT 2,
                min_margin_percent REAL DEFAULT 50,
                min_margin_total_rub REAL DEFAULT 100000,
                updated_at TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS run_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT DEFAULT '',
                status TEXT DEFAULT 'running',
                niches_processed INTEGER DEFAULT 0,
                products_found INTEGER DEFAULT 0,
                profitable_count INTEGER DEFAULT 0,
                error_message TEXT DEFAULT ''
            )
        """)

        # Гипотезы от Agent 0A (Industry Explorer). Один прогон = один batch_id.
        # В Wave 5B сюда добавится critic_score / critic_reasons.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS hypotheses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL,
                industry TEXT NOT NULL,
                niche_name TEXT NOT NULL,
                pain TEXT DEFAULT '',
                china_solution TEXT DEFAULT '',
                why_free TEXT DEFAULT '',
                llm_confidence TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_hypotheses_batch ON hypotheses(batch_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_hypotheses_industry ON hypotheses(industry)")

        # Сигналы Agent 0C (Import Detector, Wave 6) — по HS-4 категории.
        # Каждый прогон детектора = новый batch_id (uuid), храним историю.
        # Не FK на что-то другое: это самостоятельный агент, гипотезы 0A/0B
        # к нему не привязаны напрямую (пользователь переходит через кнопку).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS import_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL,
                hs_code TEXT NOT NULL,
                category_name TEXT NOT NULL,
                value_current_usd REAL DEFAULT 0,
                delta_ru_percent REAL DEFAULT 0,
                delta_world_percent REAL DEFAULT 0,
                russia_specific_pp REAL DEFAULT 0,
                ru_share_of_world REAL DEFAULT 0,
                classification TEXT DEFAULT '',
                period_current INTEGER DEFAULT 0,
                period_prev INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_signals_batch ON import_signals(batch_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_signals_specific ON import_signals(russia_specific_pp DESC)")

        # Deal Readiness Check (Wave 5E) — ручной чеклист по 7 вопросам
        # для каждой гипотезы перед тем как она пойдёт в полный анализ.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS deal_readiness (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hypothesis_id INTEGER NOT NULL UNIQUE,
                q1_demo INTEGER DEFAULT 0,
                q2_warranty INTEGER DEFAULT 0,
                q3_prepay INTEGER DEFAULT 0,
                q4_term INTEGER DEFAULT 0,
                q5_legal INTEGER DEFAULT 0,
                q6_consumables INTEGER DEFAULT 0,
                q7_parallel INTEGER DEFAULT 0,
                notes TEXT DEFAULT '',
                filled_at TEXT NOT NULL,
                FOREIGN KEY (hypothesis_id) REFERENCES hypotheses(id) ON DELETE CASCADE
            )
        """)

        # Миграция: колонки, добавленные после первого релиза. SQLite не
        # поддерживает ADD COLUMN IF NOT EXISTS, поэтому проверяем вручную.
        _ensure_column(conn, "products", "avito_price_median",   "REAL DEFAULT 0")
        _ensure_column(conn, "products", "avito_listings_count", "INTEGER DEFAULT 0")
        _ensure_column(conn, "products", "verdict_reason",       "TEXT DEFAULT ''")
        _ensure_column(conn, "products", "verdict_source",       "TEXT DEFAULT ''")
        _ensure_column(conn, "products", "supplier_score",              "INTEGER DEFAULT 0")
        _ensure_column(conn, "products", "supplier_risk_level",         "TEXT DEFAULT ''")
        _ensure_column(conn, "products", "supplier_audit_recommendation", "TEXT DEFAULT ''")
        _ensure_column(conn, "products", "supplier_audit_source",       "TEXT DEFAULT ''")
        _ensure_column(conn, "products", "supplier_red_flags",          "TEXT DEFAULT '[]'")
        _ensure_column(conn, "hypotheses", "critic_score",   "INTEGER DEFAULT -1")
        _ensure_column(conn, "hypotheses", "critic_reasons", "TEXT DEFAULT '[]'")
        _ensure_column(conn, "hypotheses", "regulatory_risk", "TEXT DEFAULT ''")
        _ensure_column(conn, "hypotheses", "score_total",     "INTEGER DEFAULT -1")
        _ensure_column(conn, "hypotheses", "score_breakdown", "TEXT DEFAULT '{}'")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_hypotheses_score ON hypotheses(score_total DESC)")
        # Wave 6 расширение: 4-летний тренд импорта для стабильности сигнала
        _ensure_column(conn, "import_signals", "history_usd",           "TEXT DEFAULT '[]'")
        _ensure_column(conn, "import_signals", "trend_shape",           "TEXT DEFAULT ''")
        _ensure_column(conn, "import_signals", "trend_stability_label", "TEXT DEFAULT ''")
        # Композитный балл для единой сортировки (класс + форма тренда + сила сигнала)
        _ensure_column(conn, "import_signals", "composite_score",       "REAL DEFAULT 0")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_signals_composite ON import_signals(composite_score DESC)")

        # индексы для частых выборок
        cur.execute("CREATE INDEX IF NOT EXISTS idx_products_niche ON products(niche_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_products_verdict ON products(verdict)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_products_margin ON products(margin_percent DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_demand_niche_date ON demand_history(niche_id, snapshot_date)")

        # дефолтные настройки ВЭД
        cur.execute("SELECT COUNT(*) FROM ved_settings")
        if cur.fetchone()[0] == 0:
            cur.execute("""
                INSERT INTO ved_settings
                    (id, usd_rate, cny_rate, duty_percent, vat_percent,
                     logistics_per_kg, logistics_per_cbm, bank_percent,
                     min_margin_percent, min_margin_total_rub, updated_at)
                VALUES (1, 0, 0, 10, 22, 3, 350, 2, 50, 100000, ?)
            """, (datetime.now().isoformat(),))


# === Niches ===

def save_niche(niche: Niche) -> int:
    """
    Создать или обновить нишу с дедупликацией: точное совпадение name_ru ИЛИ
    fuzzy-совпадение нормализованной формы (≥85%). Это защищает от дублей,
    когда LLM в разных прогонах называет одну нишу чуть иначе
    («УЗИ-аппарат портативный» vs «Аппарат УЗИ портативный»).
    """
    with get_connection() as conn:
        cur = conn.cursor()
        existing_id = _find_duplicate_niche_id(conn, niche.name_ru)
        now = niche.created_at or datetime.now().isoformat()

        if existing_id:
            cur.execute("""
                UPDATE niches SET
                    name_en = ?, category = ?, niche_type = ?, is_seasonal = ?,
                    last_frequency = ?, pain_points = ?
                WHERE id = ?
            """, (
                niche.name_en, niche.category, niche.niche_type,
                int(niche.is_seasonal), niche.last_frequency, niche.pain_points,
                existing_id,
            ))
            return existing_id

        cur.execute("""
            INSERT INTO niches (name_ru, name_en, category, niche_type,
                is_seasonal, last_frequency, pain_points, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            niche.name_ru, niche.name_en, niche.category, niche.niche_type,
            int(niche.is_seasonal), niche.last_frequency, niche.pain_points, now,
        ))
        return cur.lastrowid


def get_all_niches() -> List[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM niches ORDER BY last_frequency DESC"
        ).fetchall()
        return [_niche_row_to_dict(r) for r in rows]


def get_niche_by_id(niche_id: int) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM niches WHERE id = ?", (niche_id,)).fetchone()
        return _niche_row_to_dict(row) if row else None


def _niche_row_to_dict(row) -> dict:
    d = dict(row)
    d["is_seasonal"] = bool(d.get("is_seasonal", 0))
    # pain_points хранится как JSON-строка, возвращаем как список
    raw = d.get("pain_points") or "[]"
    try:
        d["pain_points_list"] = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        d["pain_points_list"] = []
    return d


# === Products ===

def save_product(product: Product) -> int:
    with get_connection() as conn:
        cur = conn.cursor()
        now = product.created_at or datetime.now().isoformat()
        cur.execute("""
            INSERT INTO products (niche_id, title_en, price_usd_min, price_usd_max,
                moq, supplier_rating, deals_count, certificates,
                weight_kg, length_cm, width_cm, height_cm, product_url,
                cost_total_rub, margin_percent, margin_total_rub,
                verdict, competition_count, created_at,
                avito_price_median, avito_listings_count,
                verdict_reason, verdict_source,
                supplier_score, supplier_risk_level,
                supplier_audit_recommendation, supplier_audit_source,
                supplier_red_flags)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            product.niche_id, product.title_en, product.price_usd_min,
            product.price_usd_max, product.moq, product.supplier_rating,
            product.deals_count, product.certificates,
            product.weight_kg, product.length_cm, product.width_cm,
            product.height_cm, product.product_url,
            product.cost_total_rub, product.margin_percent, product.margin_total_rub,
            product.verdict, product.competition_count, now,
            product.avito_price_median, product.avito_listings_count,
            product.verdict_reason, product.verdict_source,
            product.supplier_score, product.supplier_risk_level,
            product.supplier_audit_recommendation, product.supplier_audit_source,
            product.supplier_red_flags or "[]",
        ))
        return cur.lastrowid


def get_products_by_niche(niche_id: int) -> List[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM products WHERE niche_id = ? ORDER BY margin_percent DESC",
            (niche_id,)
        ).fetchall()
        return [_hydrate_product_row(dict(r)) for r in rows]


def _hydrate_product_row(d: dict) -> dict:
    """Дозаполнение полей продукта при чтении из БД (распаковка JSON-полей)."""
    try:
        d["supplier_red_flags"] = json.loads(d.get("supplier_red_flags") or "[]")
    except (json.JSONDecodeError, TypeError):
        d["supplier_red_flags"] = []
    return d


def get_top_products(limit: int = 10, filters: Optional[dict] = None) -> List[dict]:
    """
    Топ ниш по марже: одна строка на нишу — лучший оффер по margin_percent
    из её продуктов. Чтобы 8 «моделей» одной ниши (Model D521, F754, ...)
    не засоряли таблицу как разные товары.

    К каждой строке прикреплены:
      offers_count       — сколько всего офферов в этой нише после фильтров
      margin_percent_min — минимальная маржа среди офферов (для оценки разброса)
      margin_percent_max — максимальная (=margin_percent самой строки)
    """
    filters = filters or {}
    where = ["1=1"]
    params: list = []

    if filters.get("category"):
        where.append("n.category = ?")
        params.append(filters["category"])
    if filters.get("verdict"):
        where.append("p.verdict = ?")
        params.append(filters["verdict"])
    else:
        # По умолчанию НЕ ВЕЗЁМ не показываем — это шум, такие ниши
        # пользователь не повезёт. Виден только если явно отфильтровать.
        where.append("p.verdict != 'НЕ ВЕЗЁМ'")
    if filters.get("niche_type"):
        where.append("n.niche_type = ?")
        params.append(filters["niche_type"])
    if filters.get("seasonal") in (True, False):
        where.append("n.is_seasonal = ?")
        params.append(int(filters["seasonal"]))
    if filters.get("min_margin") is not None:
        where.append("p.margin_percent >= ?")
        params.append(float(filters["min_margin"]))

    # Окно по niche_id — ROW_NUMBER=1 даёт лучший оффер ниши; COUNT — все офферы
    # этой ниши, прошедшие фильтры. Доступно в SQLite ≥ 3.25.
    query = f"""
        SELECT * FROM (
            SELECT p.*, n.name_ru AS niche_name_ru, n.category AS niche_category,
                   n.niche_type, n.is_seasonal, n.pain_points,
                   ROW_NUMBER() OVER (
                       PARTITION BY p.niche_id ORDER BY p.margin_percent DESC
                   ) AS _rn,
                   COUNT(*)  OVER (PARTITION BY p.niche_id) AS offers_count,
                   MIN(p.margin_percent) OVER (PARTITION BY p.niche_id) AS margin_percent_min,
                   MAX(p.margin_percent) OVER (PARTITION BY p.niche_id) AS margin_percent_max
            FROM products p
            JOIN niches n ON n.id = p.niche_id
            WHERE {' AND '.join(where)}
        )
        WHERE _rn = 1
        ORDER BY margin_percent DESC
        LIMIT ?
    """
    params.append(limit)

    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d.pop("_rn", None)
            d["is_seasonal"] = bool(d.get("is_seasonal", 0))
            try:
                d["pain_points_list"] = json.loads(d.get("pain_points") or "[]")
            except (json.JSONDecodeError, TypeError):
                d["pain_points_list"] = []
            result.append(_hydrate_product_row(d))
        return result


def get_product_by_id(product_id: int) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute("""
            SELECT p.*, n.name_ru AS niche_name_ru, n.category AS niche_category,
                   n.niche_type, n.is_seasonal, n.pain_points
            FROM products p
            JOIN niches n ON n.id = p.niche_id
            WHERE p.id = ?
        """, (product_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["is_seasonal"] = bool(d.get("is_seasonal", 0))
        try:
            d["pain_points_list"] = json.loads(d.get("pain_points") or "[]")
        except (json.JSONDecodeError, TypeError):
            d["pain_points_list"] = []
        return _hydrate_product_row(d)


def delete_product(product_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM products WHERE id = ?", (product_id,))


def update_supplier_audit(product_id: int, *, score: int, risk_level: str,
                          recommendation: str, source: str, red_flags: list) -> None:
    """
    Обновить supplier-поля одного товара (после ручного запуска Agent 8 по
    кнопке «Найти поставщиков» на /product/<id>).
    """
    with get_connection() as conn:
        conn.execute("""
            UPDATE products SET
                supplier_score = ?,
                supplier_risk_level = ?,
                supplier_audit_recommendation = ?,
                supplier_audit_source = ?,
                supplier_red_flags = ?
            WHERE id = ?
        """, (
            int(score),
            risk_level or "",
            recommendation or "",
            source or "",
            json.dumps(red_flags or [], ensure_ascii=False),
            product_id,
        ))


# === Demand history ===

def save_demand_snapshot(snapshot: DemandSnapshot) -> int:
    with get_connection() as conn:
        cur = conn.cursor()
        date = snapshot.snapshot_date or datetime.now().date().isoformat()
        cur.execute("""
            INSERT INTO demand_history (niche_id, frequency, snapshot_date)
            VALUES (?, ?, ?)
        """, (snapshot.niche_id, snapshot.frequency, date))
        return cur.lastrowid


def get_demand_history(niche_id: int, days: int = 90) -> List[dict]:
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT snapshot_date, frequency
            FROM demand_history
            WHERE niche_id = ?
            ORDER BY snapshot_date DESC
            LIMIT ?
        """, (niche_id, days)).fetchall()
        return [dict(r) for r in rows]


def get_demand_timeline(limit_niches: int = 5) -> dict:
    """Временной ряд по top-N нишам (для графика динамики)."""
    with get_connection() as conn:
        top = conn.execute("""
            SELECT id, name_ru FROM niches
            ORDER BY last_frequency DESC LIMIT ?
        """, (limit_niches,)).fetchall()

        timeline = {}
        for n in top:
            rows = conn.execute("""
                SELECT snapshot_date, frequency
                FROM demand_history
                WHERE niche_id = ?
                ORDER BY snapshot_date ASC
            """, (n["id"],)).fetchall()
            timeline[n["name_ru"]] = [
                {"date": r["snapshot_date"], "frequency": r["frequency"]}
                for r in rows
            ]
        return timeline


# === VED Settings ===

def get_ved_settings() -> dict:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM ved_settings WHERE id = 1").fetchone()
        return dict(row) if row else {}


# Разрешённые колонки для UPDATE — защита от SQL-инъекции через имена столбцов.
_VED_SETTINGS_COLUMNS = frozenset({
    "usd_rate", "cny_rate", "duty_percent", "vat_percent",
    "logistics_per_kg", "logistics_per_cbm", "bank_percent",
    "min_margin_percent", "min_margin_total_rub", "updated_at",
})


def update_ved_settings(data: dict) -> None:
    # Отфильтровываем любые неожиданные ключи — в SQL попадают только свои.
    clean = {k: v for k, v in data.items() if k in _VED_SETTINGS_COLUMNS}
    if not clean:
        return
    clean["updated_at"] = datetime.now().isoformat()
    with get_connection() as conn:
        sets = ", ".join(f"{k} = ?" for k in clean)
        conn.execute(f"UPDATE ved_settings SET {sets} WHERE id = 1", list(clean.values()))


# === Run Logs ===

def create_run_log() -> int:
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO run_logs (started_at, status) VALUES (?, 'running')",
            (datetime.now().isoformat(),),
        )
        return cur.lastrowid


def finish_run_log(run_id: int, status: str, niches_processed: int = 0,
                   products_found: int = 0, profitable_count: int = 0,
                   error_message: str = "") -> None:
    with get_connection() as conn:
        conn.execute("""
            UPDATE run_logs SET finished_at = ?, status = ?, niches_processed = ?,
                products_found = ?, profitable_count = ?, error_message = ?
            WHERE id = ?
        """, (datetime.now().isoformat(), status, niches_processed,
              products_found, profitable_count, error_message, run_id))


def get_run_log(run_id: int) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM run_logs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None


def get_all_runs(limit: int = 50) -> List[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM run_logs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_active_run() -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM run_logs WHERE status = 'running' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def count_runs() -> int:
    """
    Оценка количества прогонов: количество уникальных «минут» в created_at
    у продуктов. Один прогон укладывается в одну минуту, поэтому это даёт
    адекватный счётчик независимо от того, через какой endpoint он запущен.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(DISTINCT substr(created_at, 1, 16)) AS n FROM products"
        ).fetchone()
        return int(row["n"] or 0)


def get_runs_grouped(limit_runs: int = 30) -> List[dict]:
    """
    Группирует продукты по «прогонам» (уникальные минуты в created_at) и
    возвращает последние limit_runs прогонов с их товарами.

    Возвращает: [{run_at: '2026-05-23T14:23', products: [...], wins: 2,
                  total: 5, niches: [...]}, ...] от свежего к старому.
    """
    with get_connection() as conn:
        # последние N прогонов
        runs = conn.execute("""
            SELECT substr(created_at, 1, 16) AS run_at,
                   COUNT(*) AS total,
                   SUM(CASE WHEN verdict = 'ВЕЗЁМ' THEN 1 ELSE 0 END) AS wins
            FROM products
            GROUP BY substr(created_at, 1, 16)
            ORDER BY run_at DESC
            LIMIT ?
        """, (limit_runs,)).fetchall()

        result: List[dict] = []
        for r in runs:
            run_at = r["run_at"]
            # Для каждого продукта подтаскиваем хронологически предыдущий
            # прогон ТОЙ ЖЕ ниши — чтобы посчитать дельты маржи/прибыли/спроса.
            # Это даёт «вот эта ниша в прошлый раз была вот такой».
            rows = conn.execute("""
                SELECT p.*, n.name_ru AS niche_name_ru, n.niche_type, n.is_seasonal,
                       n.pain_points, n.last_frequency,
                       (SELECT p2.margin_percent FROM products p2
                          WHERE p2.niche_id = p.niche_id
                            AND p2.created_at < p.created_at
                          ORDER BY p2.created_at DESC LIMIT 1) AS prev_margin_percent,
                       (SELECT p2.margin_total_rub FROM products p2
                          WHERE p2.niche_id = p.niche_id
                            AND p2.created_at < p.created_at
                          ORDER BY p2.created_at DESC LIMIT 1) AS prev_margin_total_rub,
                       (SELECT p2.avito_price_median FROM products p2
                          WHERE p2.niche_id = p.niche_id
                            AND p2.created_at < p.created_at
                          ORDER BY p2.created_at DESC LIMIT 1) AS prev_avito_price_median,
                       (SELECT substr(p2.created_at, 1, 16) FROM products p2
                          WHERE p2.niche_id = p.niche_id
                            AND p2.created_at < p.created_at
                          ORDER BY p2.created_at DESC LIMIT 1) AS prev_run_at
                FROM products p
                JOIN niches n ON n.id = p.niche_id
                WHERE substr(p.created_at, 1, 16) = ?
                ORDER BY p.margin_percent DESC
            """, (run_at,)).fetchall()
            products = []
            for prow in rows:
                d = dict(prow)
                d["is_seasonal"] = bool(d.get("is_seasonal", 0))
                try:
                    d["pain_points_list"] = json.loads(d.get("pain_points") or "[]")
                except (json.JSONDecodeError, TypeError):
                    d["pain_points_list"] = []
                products.append(_hydrate_product_row(d))
            result.append({
                "run_at": run_at,
                "total": int(r["total"] or 0),
                "wins": int(r["wins"] or 0),
                "products": products,
                "niches": sorted({p["niche_name_ru"] for p in products if p.get("niche_name_ru")}),
            })
        return result


# === Hypotheses (Agent 0A: Industry Explorer) ===

def save_hypotheses(hypotheses: List[Hypothesis]) -> List[int]:
    """Сохранить пачку гипотез одного прогона. Возвращает список их id."""
    if not hypotheses:
        return []
    ids: List[int] = []
    with get_connection() as conn:
        cur = conn.cursor()
        for h in hypotheses:
            now = h.created_at or datetime.now().isoformat()
            cur.execute("""
                INSERT INTO hypotheses (batch_id, industry, niche_name, pain,
                    china_solution, why_free, llm_confidence,
                    critic_score, critic_reasons, regulatory_risk,
                    score_total, score_breakdown, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                h.batch_id, h.industry, h.niche_name, h.pain,
                h.china_solution, h.why_free, h.llm_confidence,
                int(h.critic_score), h.critic_reasons or "[]", h.regulatory_risk or "",
                int(h.score_total), h.score_breakdown or "{}", now,
            ))
            ids.append(cur.lastrowid)
        return ids


def update_hypothesis_critic(hyp_id: int, critic_score: int, critic_reasons: list) -> None:
    """Обновить результаты Agent 0B для одной гипотезы."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE hypotheses SET critic_score = ?, critic_reasons = ? WHERE id = ?",
            (int(critic_score), json.dumps(critic_reasons or [], ensure_ascii=False), hyp_id),
        )


def get_hypothesis_by_id(hyp_id: int) -> Optional[dict]:
    """Получить одну гипотезу по id (для перехода в Этап 5)."""
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM hypotheses WHERE id = ?", (hyp_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["critic_reasons_list"] = json.loads(d.get("critic_reasons") or "[]")
        except (json.JSONDecodeError, TypeError):
            d["critic_reasons_list"] = []
        try:
            d["score_breakdown_dict"] = json.loads(d.get("score_breakdown") or "{}")
        except (json.JSONDecodeError, TypeError):
            d["score_breakdown_dict"] = {}
        return d


def get_hypotheses_by_batch(batch_id: str) -> List[dict]:
    """
    Гипотезы одной пачки + подгруженные DR-чеклисты (LEFT JOIN).
    Каждая гипотеза получает поля:
      critic_reasons_list — распарсенный JSON
      score_breakdown_dict — распарсенный JSON (для UI)
      deal_readiness — None если чеклист не заполнен, иначе dict с q1..q7
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM hypotheses WHERE batch_id = ? ORDER BY id ASC",
            (batch_id,),
        ).fetchall()
        if not rows:
            return []

        hyp_ids = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(hyp_ids))
        dr_rows = conn.execute(
            f"SELECT * FROM deal_readiness WHERE hypothesis_id IN ({placeholders})",
            hyp_ids,
        ).fetchall()
        dr_by_hid = {row["hypothesis_id"]: dict(row) for row in dr_rows}

        result = []
        for r in rows:
            d = dict(r)
            try:
                d["critic_reasons_list"] = json.loads(d.get("critic_reasons") or "[]")
            except (json.JSONDecodeError, TypeError):
                d["critic_reasons_list"] = []
            try:
                d["score_breakdown_dict"] = json.loads(d.get("score_breakdown") or "{}")
            except (json.JSONDecodeError, TypeError):
                d["score_breakdown_dict"] = {}

            dr = dr_by_hid.get(r["id"])
            if dr:
                dr["yes_count"] = sum(int(dr.get(k, 0)) for k, _ in DR_QUESTIONS)
            d["deal_readiness"] = dr  # None если не заполнен
            result.append(d)
        return result


def get_hypothesis_batches(limit: int = 20) -> List[dict]:
    """
    Список последних прогонов Agent 0A: один batch = одна индустрия за один раз.
    Возвращает [{batch_id, industry, created_at, count}], от свежего к старому.
    """
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT batch_id, industry, MIN(created_at) AS created_at, COUNT(*) AS count
            FROM hypotheses
            GROUP BY batch_id
            ORDER BY MIN(created_at) DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def update_hypothesis_score(hyp_id: int, score_total: int, score_breakdown: dict) -> None:
    """Обновить итоговый балл и разбивку (после пересчёта от Deal Readiness)."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE hypotheses SET score_total = ?, score_breakdown = ? WHERE id = ?",
            (int(score_total), json.dumps(score_breakdown, ensure_ascii=False), hyp_id),
        )


# === Deal Readiness (Wave 5E) ===

DR_QUESTIONS = (
    ("q1_demo",        "Можно объяснить ценность за 30 секунд БЕЗ физического показа"),
    ("q2_warranty",    "Есть понятный сценарий гарантии / замены"),
    ("q3_prepay",      "Клиент платит предоплату охотно (не сопротивляется)"),
    ("q4_term",        "Срок поставки 30-45 дней приемлем для этой категории"),
    ("q5_legal",       "Юридическая чистота: нет нарушения торговой марки"),
    ("q6_consumables", "Есть расходники / повторные покупки (доп. ценность)"),
    ("q7_parallel",    "Можно сделать 5-10 параллельных объявлений по товару"),
)


def save_deal_readiness(hyp_id: int, answers: dict, notes: str = "") -> None:
    """Сохранить чеклист Deal Readiness для гипотезы (UPSERT)."""
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO deal_readiness (hypothesis_id, q1_demo, q2_warranty, q3_prepay,
                q4_term, q5_legal, q6_consumables, q7_parallel, notes, filled_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(hypothesis_id) DO UPDATE SET
                q1_demo = excluded.q1_demo,
                q2_warranty = excluded.q2_warranty,
                q3_prepay = excluded.q3_prepay,
                q4_term = excluded.q4_term,
                q5_legal = excluded.q5_legal,
                q6_consumables = excluded.q6_consumables,
                q7_parallel = excluded.q7_parallel,
                notes = excluded.notes,
                filled_at = excluded.filled_at
        """, (
            hyp_id,
            int(answers.get("q1_demo", 0)),
            int(answers.get("q2_warranty", 0)),
            int(answers.get("q3_prepay", 0)),
            int(answers.get("q4_term", 0)),
            int(answers.get("q5_legal", 0)),
            int(answers.get("q6_consumables", 0)),
            int(answers.get("q7_parallel", 0)),
            notes,
            datetime.now().isoformat(),
        ))


def get_deal_readiness(hyp_id: int) -> Optional[dict]:
    """Прочитать DR-чеклист по гипотезе. Возвращает None если не заполнен."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM deal_readiness WHERE hypothesis_id = ?", (hyp_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["yes_count"] = sum(int(d.get(k, 0)) for k, _ in DR_QUESTIONS)
        d["questions"] = DR_QUESTIONS
        return d


# === Import Signals (Agent 0C, Wave 6) ===

def save_import_signals(signals: list, batch_id: str) -> int:
    """
    Сохранить пачку сигналов детектора одного прогона.
    Принимает список ImportSignal dataclass либо dict — берём поля по имени.
    """
    if not signals:
        return 0
    now = datetime.now().isoformat()
    with get_connection() as conn:
        cur = conn.cursor()
        for s in signals:
            get = (lambda k, d=None: getattr(s, k, d)) if hasattr(s, 'hs_code') else (lambda k, d=None: s.get(k, d))
            history = get('history_usd', []) or []
            cur.execute("""
                INSERT INTO import_signals (
                    batch_id, hs_code, category_name, value_current_usd,
                    delta_ru_percent, delta_world_percent, russia_specific_pp,
                    ru_share_of_world, classification,
                    period_current, period_prev,
                    history_usd, trend_shape, trend_stability_label,
                    composite_score,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                batch_id, get('hs_code'), get('category_name'),
                float(get('value_current_usd') or 0),
                float(get('delta_ru_percent') or 0),
                float(get('delta_world_percent') or 0),
                float(get('russia_specific_pp') or 0),
                float(get('ru_share_of_world') or 0),
                get('classification') or '',
                int(get('period_current') or 0), int(get('period_prev') or 0),
                json.dumps(list(history), ensure_ascii=False),
                get('trend_shape') or '',
                get('trend_stability_label') or '',
                float(get('composite_score') or 0),
                now,
            ))
        return len(signals)


def get_latest_import_signals_batch() -> Optional[str]:
    """Вернуть batch_id последнего прогона детектора или None если пусто."""
    with get_connection() as conn:
        row = conn.execute("""
            SELECT batch_id FROM import_signals
            ORDER BY created_at DESC LIMIT 1
        """).fetchone()
        return row["batch_id"] if row else None


def get_import_signals_by_batch(batch_id: str) -> list[dict]:
    """Прочитать все сигналы одного прогона, отсортированные по composite_score DESC
    (единый рейтинг от лучшего к худшему). Распаковывает history_usd."""
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT * FROM import_signals
            WHERE batch_id = ?
            ORDER BY composite_score DESC, russia_specific_pp DESC
        """, (batch_id,)).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try:
                d["history_usd_list"] = json.loads(d.get("history_usd") or "[]")
            except (json.JSONDecodeError, TypeError):
                d["history_usd_list"] = []
            result.append(d)
        return result
