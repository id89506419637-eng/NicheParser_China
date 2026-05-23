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
from core.models import Niche, Product, DemandSnapshot


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
            rows = conn.execute("""
                SELECT p.*, n.name_ru AS niche_name_ru, n.niche_type, n.is_seasonal,
                       n.pain_points
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
