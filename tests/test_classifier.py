"""
Тесты AI-классификатора: парсинг JSON-блока, нормализация niche_type и pain_points.
Реальные вызовы к OpenRouter не делаем — проверяем только чистую логику.
"""

from __future__ import annotations

from src.analytics import niche_classifier as nc


def test_parse_json_block_clean():
    txt = '{"niche_type": "ДЕФИЦИТ", "is_seasonal": false, "pain_points": ["a"]}'
    assert nc._parse_json_block(txt) == {
        "niche_type": "ДЕФИЦИТ",
        "is_seasonal": False,
        "pain_points": ["a"],
    }


def test_parse_json_block_with_markdown_fence():
    txt = '```json\n{"niche_type": "ИННОВАЦИЯ", "pain_points": []}\n```'
    result = nc._parse_json_block(txt)
    assert result == {"niche_type": "ИННОВАЦИЯ", "pain_points": []}


def test_parse_json_block_with_bare_fence():
    txt = '```\n{"niche_type": "ВЫСОКИЙ СПРОС"}\n```'
    assert nc._parse_json_block(txt) == {"niche_type": "ВЫСОКИЙ СПРОС"}


def test_parse_json_block_with_preamble():
    """AI иногда пишет пояснение перед JSON — надо выдрать блок."""
    txt = 'Вот мой анализ:\n{"niche_type": "СЛАБАЯ НИША", "pain_points": []}\nКонец.'
    result = nc._parse_json_block(txt)
    assert result == {"niche_type": "СЛАБАЯ НИША", "pain_points": []}


def test_parse_json_block_returns_none_for_garbage():
    assert nc._parse_json_block("совсем не json, вообще") is None
    assert nc._parse_json_block("{сломанный json") is None
    assert nc._parse_json_block("") is None


def test_normalize_keeps_valid_niche_type():
    out = nc._normalize({
        "niche_type": "ДЕФИЦИТ",
        "is_seasonal": True,
        "pain_points": ["боль 1", "боль 2"],
        "reasoning": "причина",
    })
    assert out["niche_type"] == "ДЕФИЦИТ"
    assert out["is_seasonal"] is True
    assert out["pain_points"] == ["боль 1", "боль 2"]
    assert out["reasoning"] == "причина"


def test_normalize_lowercase_niche_type_is_uppercased():
    out = nc._normalize({"niche_type": "дефицит"})
    assert out["niche_type"] == "ДЕФИЦИТ"


def test_normalize_partial_match_resolves():
    """AI прислал 'большой высокий спрос' → распознаём 'ВЫСОКИЙ СПРОС'."""
    out = nc._normalize({"niche_type": "БОЛЬШОЙ ВЫСОКИЙ СПРОС"})
    assert out["niche_type"] == "ВЫСОКИЙ СПРОС"


def test_normalize_unknown_niche_type_falls_back_to_vysokiy_spros():
    out = nc._normalize({"niche_type": "какая-то левая категория"})
    assert out["niche_type"] == "ВЫСОКИЙ СПРОС"


def test_normalize_missing_fields_use_safe_defaults():
    out = nc._normalize({})
    assert out["niche_type"] == "ВЫСОКИЙ СПРОС"
    assert out["is_seasonal"] is False
    assert out["pain_points"] == []
    assert out["reasoning"] == ""


def test_normalize_pain_points_not_list_wrapped():
    out = nc._normalize({"niche_type": "ДЕФИЦИТ", "pain_points": "одна строка"})
    assert out["pain_points"] == ["одна строка"]


def test_normalize_pain_points_trimmed_and_limited():
    """Пустые строки отбрасываются, лишние — обрезаются до 5."""
    out = nc._normalize({
        "niche_type": "ДЕФИЦИТ",
        "pain_points": ["a", "  ", "b", "", "c", "d", "e", "f", "g"],
    })
    assert out["pain_points"] == ["a", "b", "c", "d", "e"]


def test_classify_niche_without_api_key_returns_fallback(monkeypatch):
    """Без OPENROUTER_API_KEY → fallback, а не исключение."""
    monkeypatch.setattr(nc, "OPENROUTER_API_KEY", "")
    result = nc.classify_niche("тестовый ключ", "Медтехника и медоборудование", 1000)
    assert result["niche_type"] in ("ВЫСОКИЙ СПРОС",)
    assert result["pain_points"] == []
    assert "нет OPENROUTER_API_KEY" in result["reasoning"]


def test_classify_niche_http_error_returns_fallback(monkeypatch):
    monkeypatch.setattr(nc, "OPENROUTER_API_KEY", "fake-key")

    class Resp:
        status_code = 500
        text = "internal error"

    monkeypatch.setattr(nc.requests, "post", lambda *a, **kw: Resp())
    result = nc.classify_niche("x", "y", 0)
    assert result["niche_type"] == "ВЫСОКИЙ СПРОС"
    assert "HTTP 500" in result["reasoning"]


def test_classify_niche_success_path(monkeypatch):
    monkeypatch.setattr(nc, "OPENROUTER_API_KEY", "fake-key")

    class Resp:
        status_code = 200
        def json(self):
            return {
                "choices": [{
                    "message": {
                        "content": (
                            '```json\n'
                            '{"niche_type": "ДЕФИЦИТ", "is_seasonal": false, '
                            '"pain_points": ["нет поставок EU", "санкции"], '
                            '"reasoning": "ушли западные бренды"}\n'
                            '```'
                        )
                    }
                }]
            }

    monkeypatch.setattr(nc.requests, "post", lambda *a, **kw: Resp())
    result = nc.classify_niche("станок ЧПУ", "Промышленное оборудование и станки", 3400)
    assert result["niche_type"] == "ДЕФИЦИТ"
    assert result["is_seasonal"] is False
    assert result["pain_points"] == ["нет поставок EU", "санкции"]
    assert "западные" in result["reasoning"]


def test_pain_points_as_json_roundtrip():
    pains = ["дефицит запчастей", "большие сроки доставки"]
    blob = nc.pain_points_as_json(pains)
    assert '"дефицит запчастей"' in blob  # без \uXXXX
    import json
    assert json.loads(blob) == pains
