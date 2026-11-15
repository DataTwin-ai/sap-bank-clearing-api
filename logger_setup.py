"""
=============================================================================
Centralized Timestamped Logger for SAP Pipeline
=============================================================================
Features:
  1. Writes to a 'log' folder in the server's current working directory
     (no static paths). Override with [environment] log_dir in config.ini or
     the LOG_DIR env var; relative values are resolved against the current
     working directory (in Docker: /app/log).
  2. Generates a timestamped log file on every session/run:
     Example: ./log/log_20261110_133500.txt
  3. Logs simultaneously to Terminal Console and Timestamped Log File.
  4. Automatically shared across app.py, db_manager.py, sync_bsid_to_bt3i.py, and api.py.
=============================================================================
"""

import os
import sys
import logging
import configparser
from datetime import datetime
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.ini"


def _read_env_config() -> configparser.SectionProxy | dict:
    if CONFIG_FILE.exists():
        try:
            cfg = configparser.ConfigParser()
            cfg.read(CONFIG_FILE, encoding="utf-8")
            if cfg.has_section("environment"):
                return cfg["environment"]
        except Exception:
            pass
    return {}


def _resolve_log_dir() -> Path:
    """Log folder: LOG_DIR env var > config.ini log_dir > './log' (current working dir)."""
    raw = os.environ.get("LOG_DIR") or _read_env_config().get("log_dir", "log") or "log"
    path = Path(raw).expanduser()
    return path if path.is_absolute() else Path.cwd() / path


LOG_DIR = _resolve_log_dir()

_CURRENT_LOG_FILE: Optional[Path] = None


def get_session_log_file() -> Path:
    """
    Returns the active session timestamped log file path,
    creating it if not already initialized.
    """
    global _CURRENT_LOG_FILE
    if _CURRENT_LOG_FILE is not None:
        return _CURRENT_LOG_FILE

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    env_cfg = _read_env_config()
    prefix = env_cfg.get("log_file_prefix", "log") or "log"
    ext = env_cfg.get("log_extension", "txt") or "txt"

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    _CURRENT_LOG_FILE = LOG_DIR / f"{prefix}_{timestamp}.{ext}"
    return _CURRENT_LOG_FILE


def get_logger(name: str = "SAP_Pipeline") -> logging.Logger:
    """
    Returns configured logger writing to both Console & Timestamped File.
    """
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    # Prevent duplicate handlers if called multiple times
    if not logger.handlers:
        formatter = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )

        # 1. Console Stream Handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

        # 2. Timestamped File Handler in 'log/' folder
        log_file = get_session_log_file()
        try:
            file_handler = logging.FileHandler(str(log_file), encoding="utf-8")
            file_handler.setLevel(logging.INFO)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
            logger.info(f"[LOG INIT] Session log file active at: {log_file.resolve()}")
        except Exception as e:
            logger.warning(f"Failed to create file handler for {log_file}: {e}")

    return logger
