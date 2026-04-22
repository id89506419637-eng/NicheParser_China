"""
NicheParser_China — Data Models
Dataclass-модели для ниш, товаров, истории спроса и настроек ВЭД.
"""

from dataclasses import dataclass, field
from typing import Optional, List


@dataclass
class Niche:
    """Товарная ниша (семантическое ядро, не конкретный товар)."""
    id: Optional[int] = None
    name_ru: str = ""
    name_en: str = ""
    category: str = ""
    niche_type: str = ""           # ДЕФИЦИТ / ИННОВАЦИЯ / СЛАБАЯ НИША / ВЫСОКИЙ СПРОС / ОБЪЁМНЫЙ ТОВАР
    is_seasonal: bool = False
    last_frequency: int = 0        # последняя частотность из Wordstat
    pain_points: str = ""          # JSON-массив болей предпринимателей (от AI)
    created_at: str = ""


@dataclass
class Product:
    """Конкретный товар с Alibaba, привязанный к нише."""
    id: Optional[int] = None
    niche_id: int = 0
    title_en: str = ""
    price_usd_min: float = 0.0
    price_usd_max: float = 0.0
    moq: int = 0
    supplier_rating: float = 0.0
    deals_count: int = 0
    certificates: str = ""         # CSV: "CE,ISO,RoHS"
    weight_kg: float = 0.0         # на единицу
    length_cm: float = 0.0
    width_cm: float = 0.0
    height_cm: float = 0.0
    product_url: str = ""

    # Считаемые поля
    cost_total_rub: float = 0.0        # себестоимость 1 единицы в рублях
    margin_percent: float = 0.0        # маржа в % на единицу
    margin_total_rub: float = 0.0      # абсолютная маржа на партию MOQ
    verdict: str = ""                   # ВЕЗЁМ / ИЗУЧИТЬ / НЕ ВЕЗЁМ

    # Метаданные
    competition_count: int = 0          # всего товаров на Alibaba по запросу
    created_at: str = ""


@dataclass
class DemandSnapshot:
    """Снимок частотности Wordstat на дату (для графика динамики)."""
    id: Optional[int] = None
    niche_id: int = 0
    frequency: int = 0
    snapshot_date: str = ""


@dataclass
class VedSettings:
    """Настройки ВЭД-калькулятора."""
    id: Optional[int] = None
    usd_rate: float = 0.0
    cny_rate: float = 0.0
    duty_percent: float = 10.0
    vat_percent: float = 22.0
    logistics_per_kg: float = 3.0        # USD/kg (средний тариф)
    logistics_per_cbm: float = 350.0     # USD/cbm
    bank_percent: float = 2.0
    min_margin_percent: float = 50.0
    min_margin_total_rub: float = 100_000.0
    updated_at: str = ""


@dataclass
class RunLog:
    """Лог одного запуска пайплайна."""
    id: Optional[int] = None
    started_at: str = ""
    finished_at: str = ""
    status: str = "running"              # running / done / error
    niches_processed: int = 0
    products_found: int = 0
    profitable_count: int = 0
    error_message: str = ""


@dataclass
class WordstatItem:
    """Сырой результат Wordstat по одному ключу."""
    keyword: str = ""
    frequency: int = 0
    category: str = ""


@dataclass
class AlibabaProduct:
    """Сырой результат парсинга одной карточки Alibaba."""
    title_en: str = ""
    price_usd_min: float = 0.0
    price_usd_max: float = 0.0
    moq: int = 0
    supplier_rating: float = 0.0
    deals_count: int = 0
    certificates: List[str] = field(default_factory=list)
    weight_kg: float = 0.0
    length_cm: float = 0.0
    width_cm: float = 0.0
    height_cm: float = 0.0
    product_url: str = ""
