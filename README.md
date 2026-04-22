# NicheParser_China

Автоматический поиск товарных ниш для импорта из Китая в Россию:
Wordstat → классификация → Alibaba → ВЭД-расчёт → вердикт «везём / изучить / не везём».

## Быстрый старт

```bash
# 1. Установить зависимости
pip install -r requirements.txt

# 2. Установить браузер для Playwright (разово, ~150 МБ)
playwright install chromium

# 3. Скопировать .env и заполнить ключи
cp .env.example .env
# отредактируй .env

# 4. Запуск
python main.py
# открой http://127.0.0.1:5000
```

## Структура

| Путь | Назначение |
|------|-----------|
| [main.py](main.py) | Entry point (Flask) |
| [core/config.py](core/config.py) | Настройки, флаги модулей |
| [core/models.py](core/models.py) | Dataclass-модели |
| [src/db/database.py](src/db/database.py) | SQLite CRUD |
| [src/parsers/](src/parsers/) | Wordstat, Alibaba, Avito (заглушка) |
| [src/calculator/ved_calculator.py](src/calculator/ved_calculator.py) | Расчёт себестоимости импорта |
| [src/analytics/niche_classifier.py](src/analytics/niche_classifier.py) | AI-классификация типа ниши |
| [src/pipeline/runner.py](src/pipeline/runner.py) | Оркестратор 5 этапов |
| [src/web/app.py](src/web/app.py) | Flask-роуты |
| [docs/SPEC.md](docs/SPEC.md) | Техническое задание v5.1 |
| [tests/](tests/) | Pytest |

## Переменные окружения (`.env`)

| Ключ | Обязательно | Описание |
|------|:---:|-----------|
| `SECRET_KEY` | ✅ | Flask session secret |
| `OPENROUTER_API_KEY` | ✅ | Ключ для AI-классификации |
| `YANDEX_OAUTH_TOKEN` | ⚠️ | Wordstat. Без него используется mock |
| `FLASK_DEBUG` | — | `1` для dev-режима (по умолчанию `0`) |
| `FLASK_HOST`, `FLASK_PORT` | — | По умолчанию `127.0.0.1:5000` |
| `USE_MOCK_WORDSTAT` | — | `1` = использовать mock-данные вместо API |
| `ENABLE_AVITO` | — | `0` по умолчанию (заглушка) |

## Режим dev vs prod

Определяется переменной `FLASK_DEBUG` в `.env`:

| FLASK_DEBUG | Сервер | Назначение |
|:---:|--------|-----------|
| `1` | Flask dev-server | Локальная разработка, авто-перезагрузки нет (чтобы не ломать pipeline-поток) |
| `0` | Waitress (WSGI) | Прод-ready для Windows/Linux, многопоточный |

Команда запуска одинакова в обоих случаях:

```bash
python main.py
```

## Тесты

```bash
pytest -v
```

## Статус модулей

| Модуль | Статус |
|--------|--------|
| Wordstat | ✅ API + mock |
| Alibaba | ✅ Playwright + stealth |
| Avito | ⏸ заглушка (ждём API-доступ) |
| ВЭД-калькулятор | ✅ |
| AI-классификатор | ✅ OpenRouter/Gemini |
| Дашборд | ✅ Flask + Jinja2 + тёмная тема |

## Дисклеймер

Расчёты себестоимости **приблизительные**. Точный код ТН ВЭД, пошлина
и логистика всегда уточняются у таможенного брокера и логистической компании
перед реальной закупкой.
