"""Настройка логов с ротацией."""
import logging
import logging.handlers
import os


LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logger(log_dir: str) -> logging.Logger:
    os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger("tv_schedule_uploader")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    if not logger.handlers:
        path = os.path.join(log_dir, "app.log")
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
        logger.addHandler(handler)
    return logger