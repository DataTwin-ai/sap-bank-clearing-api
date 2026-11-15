"""
=============================================================================
Centralized Timestamped Logger for SAP Pipeline
=============================================================================
Features:
  1. Creates and manages the 'C:\\SAP\\log\\' directory.
  2. Generates a timestamped log file on every session/run:
     Example: C:\\SAP\\log\\log_20261110_133500.txt
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
LOG_DIR = BASE_DIR / "log"

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

    # Read configuration defaults
    config_file = BASE_DIR / "config.ini"
    prefix = "log"
    ext = "txt"

    if config_file.exists():
        try:
            cfg = configparser.ConfigParser()
            cfg.read(config_file, encoding="utf-8")
            prefix = cfg.get("environment", "log_file_prefix", fallback="log")
            ext = cfg.get("environment", "log_extension", fallback="txt")
        except Exception:
            pass

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
