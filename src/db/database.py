"""
NicheParser_China — Database Layer
SQLite CRUD для ниш, товаров, истории спроса, настроек ВЭД и логов запусков.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import List, Optional

from core.config import DB_PATH
from core.models import Niche, Product, DemandSnapshot


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
    """Создать или обновить нишу по уникальному name_ru. Возвращает id."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM niches WHERE name_ru = ?", (niche.name_ru,))
        row = cur.fetchone()

        now = niche.created_at or datetime.now().isoformat()

        if row:
            niche_id = row["id"]
            cur.execute("""
                UPDATE niches SET
                    name_en = ?, category = ?, niche_type = ?, is_seasonal = ?,
                    last_frequency = ?, pain_points = ?
                WHERE id = ?
            """, (
                niche.name_en, niche.category, niche.niche_type,
                int(niche.is_seasonal), niche.last_frequency, niche.pain_points,
                niche_id,
            ))
            return niche_id

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
                verdict, competition_count, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            product.niche_id, product.title_en, product.price_usd_min,
            product.price_usd_max, product.moq, product.supplier_rating,
            product.deals_count, product.certificates,
            product.weight_kg, product.length_cm, product.width_cm,
            product.height_cm, product.product_url,
            product.cost_total_rub, product.margin_percent, product.margin_total_rub,
            product.verdict, product.competition_count, now,
        ))
        return cur.lastrowid


def get_products_by_niche(niche_id: int) -> List[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM products WHERE niche_id = ? ORDER BY margin_percent DESC",
            (niche_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_top_products(limit: int = 10, filters: Optional[dict] = None) -> List[dict]:
    """Топ товаров по марже с опциональными фильтрами."""
    filters = filters or {}
    where = ["1=1"]
    params: list = []

    if filters.get("category"):
        where.append("n.category = ?")
        params.append(filters["category"])
    if filters.get("verdict"):
        where.append("p.verdict = ?")
        params.append(filters["verdict"])
    if filters.get("niche_type"):
        where.append("n.niche_type = ?")
        params.append(filters["niche_type"])
    if filters.get("seasonal") in (True, False):
        where.append("n.is_seasonal = ?")
        params.append(int(filters["seasonal"]))
    if filters.get("min_margin") is not None:
        where.append("p.margin_percent >= ?")
        params.append(float(filters["min_margin"]))

    query = f"""
        SELECT p.*, n.name_ru AS niche_name_ru, n.category AS niche_category,
               n.niche_type, n.is_seasonal, n.pain_points
        FROM products p
        JOIN niches n ON n.id = p.niche_id
        WHERE {' AND '.join(where)}
        ORDER BY p.margin_percent DESC
        LIMIT ?
    """
    params.append(limit)

    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["is_seasonal"] = bool(d.get("is_seasonal", 0))
            try:
                d["pain_points_list"] = json.loads(d.get("pain_points") or "[]")
            except (json.JSONDecodeError, TypeError):
                d["pain_points_list"] = []
            result.append(d)
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
        return d


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


def update_ved_settings(data: dict) -> None:
    data = dict(data)  # копия
    data["updated_at"] = datetime.now().isoformat()
    with get_connection() as conn:
        sets = ", ".join(f"{k} = ?" for k in data)
        conn.execute(f"UPDATE ved_settings SET {sets} WHERE id = 1", list(data.values()))


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
