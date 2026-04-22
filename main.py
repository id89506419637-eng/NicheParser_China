"""
NicheParser_China — Main Entry Point
Инициализирует логирование, БД и запускает Flask-сервер.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import config  # noqa: E402
from src.db.database import init_db  # noqa: E402
from src.web.app import app  # noqa: E402


def setup_logging() -> None:
    """Console + rotating file (logs/app.log, 2 MB × 5 backups)."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if config.FLASK_DEBUG else logging.INFO)

    # убираем дубли хендлеров при повторном импорте
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    file_handler = RotatingFileHandler(
        config.LOGS_DIR / "app.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # Flask/werkzeug многословен — глушим до WARNING когда не debug
    if not config.FLASK_DEBUG:
        logging.getLogger("werkzeug").setLevel(logging.WARNING)


def main() -> None:
    setup_logging()
    log = logging.getLogger("main")

    log.info("Initializing database at %s", config.DB_PATH)
    init_db()

    log.info(
        "Starting NicheParser on http://%s:%s (debug=%s)",
        config.FLASK_HOST,
        config.FLASK_PORT,
        config.FLASK_DEBUG,
    )

    if config.FLASK_DEBUG:
        app.run(
            host=config.FLASK_HOST,
            port=config.FLASK_PORT,
            debug=True,
            use_reloader=False,  # reloader ломает pipeline daemon-thread
        )
    else:
        from waitress import serve
        serve(app, host=config.FLASK_HOST, port=config.FLASK_PORT, threads=8)


if __name__ == "__main__":
    main()
