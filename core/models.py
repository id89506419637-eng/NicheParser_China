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

    # Контекст с Авито (для прозрачности расчёта в истории)
    avito_price_median: float = 0.0    # медианная цена продажи в РФ
    avito_listings_count: int = 0       # объявлений в выдаче (насыщение рынка)

    # Обоснование вердикта от Агента 7 (или арифметики, если LLM упал)
    verdict_reason: str = ""
    verdict_source: str = ""            # 'llm' | 'arithmetic'

    # Скоринг поставщика от Агента 8
    supplier_score: int = 0              # композитный балл 0–100
    supplier_risk_level: str = ""         # 'низкий' / 'средний' / 'высокий'
    supplier_audit_recommendation: str = ""  # что проверить вручную
    supplier_audit_source: str = ""       # 'llm' / 'arithmetic' / 'skip'
    supplier_red_flags: str = ""          # JSON-массив строк с конкретными флагами

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
class Hypothesis:
    """
    Гипотеза о свободной нише от Agent 0A (Industry Explorer).
    Это «сырое» предположение от LLM ДО проверки парсером и до Deal Readiness.
    В Wave 5B будет дополняться полями critic_score / critic_reasons.
    В Wave 5C — полным 7-факторным скорингом.
    """
    id: Optional[int] = None
    batch_id: str = ""               # uuid одного прогона: «вот эти 30 гипотез сгенерены вместе»
    industry: str = ""               # «стройка-отделка» / «мебельное производство» / ...
    niche_name: str = ""             # «Безвоздушные покрасочные станции для фасадов»
    pain: str = ""                   # «Маляры красят валиками, медленно и дорого по труду»
    china_solution: str = ""         # «Аппараты Wagner-клонов от $800»
    why_free: str = ""               # «В РФ только европейские, $5000+»
    llm_confidence: str = ""         # «высокая» / «средняя» / «низкая»
    # Agent 0B (Critic) — Wave 5B
    critic_score: int = -1           # 0-5, чем выше тем меньше серьёзных контраргументов; -1 = не проверено
    critic_reasons: str = ""         # JSON-массив строк с конкретными контраргументами
    # Wave 5C — 7-факторный скоринг
    score_total: int = -1            # 0-100, итоговый балл; -1 = не считано
    score_breakdown: str = ""        # JSON: {demand, china, avito, economy, demo, ops, critic, validation_data}
    created_at: str = ""


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
