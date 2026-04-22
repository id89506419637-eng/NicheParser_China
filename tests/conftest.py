"""
Общая настройка pytest: путь до корня проекта + временная БД для тестов БД.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """
    Подсовывает временный DB_PATH во все модули, что его уже импортировали.
    Возвращает путь к временной БД.
    """
    from core import config as cfg
    from src.db import database as db_mod

    tmp_file = tmp_path / "test_niches.db"
    monkeypatch.setattr(cfg, "DB_PATH", tmp_file)
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_file)

    db_mod.init_db()
    yield tmp_file


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """
    На всякий случай ломаем реальные вызовы requests.get/post для всех тестов.
    Тесты, которым нужен HTTP — сами мокают свою функцию.
    """
    def _fail(*args, **kwargs):
        raise RuntimeError(
            "Network access blocked in tests — mock the client in the specific test."
        )

    import requests
    monkeypatch.setattr(requests, "get", _fail)
    monkeypatch.setattr(requests, "post", _fail)
    yield
