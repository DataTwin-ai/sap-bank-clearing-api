"""
Logging for the SAP extraction service.

* All module loggers are children of "SAP_Pipeline"; handlers live on that parent.
* setup_logging("api") / setup_logging("cron") is called once by the entry point:
    - console handler (stdout) - picked up by systemd journal / docker logs
    - daily rotating file  <LOG_DIR>/<component>.log  (kept for log_retention_days)
  The API and the cron job write to different files, so they never rotate
  the same file from two processes.
* Settings: [logging] level, log_to_file, retention_days  (env: LOG_LEVEL, LOG_TO_FILE, LOG_RETENTION_DAYS)
"""

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Optional

import settings

ROOT_LOGGER = "SAP_Pipeline"
_FORMAT = logging.Formatter(
    fmt="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def setup_logging(component: str = "app", console: bool = True) -> Optional[Path]:
    """(Re)configures the shared logger. Returns the log file path, if any."""
    root = logging.getLogger(ROOT_LOGGER)
    level = getattr(logging, settings.get("logging", "level", "LOG_LEVEL", "INFO").upper(), logging.INFO)
    root.setLevel(level)
    root.propagate = False
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()

    if console:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(_FORMAT)
        root.addHandler(ch)

    log_file: Optional[Path] = None
    if settings.get_bool("logging", "log_to_file", "LOG_TO_FILE", True):
        settings.LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_file = settings.LOG_DIR / f"{component}.log"
        fh = TimedRotatingFileHandler(
            str(log_file),
            when="midnight",
            backupCount=settings.get_int("logging", "retention_days", "LOG_RETENTION_DAYS", 30),
            encoding="utf-8",
        )
        fh.setFormatter(_FORMAT)
        root.addHandler(fh)
    return log_file


def get_logger(name: str = ROOT_LOGGER) -> logging.Logger:
    """Module logger. Falls back to console-only output until setup_logging() runs."""
    root = logging.getLogger(ROOT_LOGGER)
    if not root.handlers:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(_FORMAT)
        root.addHandler(ch)
        root.setLevel(logging.INFO)
        root.propagate = False
    if name != ROOT_LOGGER and not name.startswith(ROOT_LOGGER + "."):
        name = f"{ROOT_LOGGER}.{name}"
    return logging.getLogger(name)
