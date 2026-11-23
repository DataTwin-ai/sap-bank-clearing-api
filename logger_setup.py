"""
=============================================================================
Logging for the SAP bank posting API
=============================================================================
  * One log file per day:  <log_dir>/log_YYYYMMDD.txt   (e.g. log/log_20261123.txt)
    All processes (uvicorn workers, CLI scripts) append to the same file; it
    switches to a new file at midnight (server time).
  * Files older than [environment] log_retention_days (default 30) are deleted.
  * Every line also goes to the console (docker logs).
  * Folder: LOG_DIR env var > [environment] log_dir in config.ini > ./log.
    Relative values are resolved against the current working directory
    (in Docker: /app/log, mounted from /opt/sap-bank-clearing-api/log).
  * Level: [environment] log_level (default INFO).
=============================================================================
"""

import configparser
import logging
import os
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Union

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.ini"
FORMAT = "%(asctime)s [%(levelname)s] [%(process)d] [%(name)s] %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"


def _read_env_config() -> Union[configparser.SectionProxy, dict]:
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


class DailyFileHandler(logging.Handler):
    """
    Appends to <directory>/<prefix>_<YYYYMMDD>.<ext> and moves to the next day's file at
    midnight. Files are never renamed, so several processes can share them safely.
    """

    def __init__(self, directory: Path, prefix: str = "log", ext: str = "txt", keep_days: int = 30):
        super().__init__()
        self.directory = Path(directory)
        self.prefix = prefix
        self.ext = ext
        self.keep_days = keep_days
        self._day: Optional[str] = None
        self._stream = None

    def path_for(self, day: str) -> Path:
        return self.directory / f"{self.prefix}_{day}.{self.ext}"

    def _open(self, day: str) -> None:
        if self._stream:
            self._stream.close()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._stream = open(self.path_for(day), "a", encoding="utf-8")
        self._day = day
        self._delete_old()

    def _delete_old(self) -> None:
        if self.keep_days <= 0:
            return
        cutoff = (datetime.now() - timedelta(days=self.keep_days)).strftime("%Y%m%d")
        for f in self.directory.glob(f"{self.prefix}_*.{self.ext}"):
            day = f.stem[len(self.prefix) + 1:]
            if len(day) == 8 and day.isdigit() and day < cutoff:
                try:
                    f.unlink()
                except OSError:
                    pass  # another process deleted it first

    def emit(self, record: logging.LogRecord) -> None:
        try:
            day = datetime.fromtimestamp(record.created).strftime("%Y%m%d")
            if day != self._day:
                self._open(day)
            self._stream.write(self.format(record) + "\n")
            self._stream.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        self.acquire()
        try:
            if self._stream:
                self._stream.close()
                self._stream = None
        finally:
            self.release()
        super().close()


_LOCK = threading.Lock()
_FILE_HANDLER: Optional[DailyFileHandler] = None
_CONSOLE_HANDLER: Optional[logging.Handler] = None


def _level() -> int:
    name = str(_read_env_config().get("log_level", "INFO") or "INFO").upper()
    return getattr(logging, name, logging.INFO)


def _handlers():
    """One console handler and one file handler, shared by every logger of this process."""
    global _FILE_HANDLER, _CONSOLE_HANDLER
    with _LOCK:
        if _CONSOLE_HANDLER is None:
            formatter = logging.Formatter(fmt=FORMAT, datefmt=DATEFMT)
            _CONSOLE_HANDLER = logging.StreamHandler(sys.stdout)
            _CONSOLE_HANDLER.setFormatter(formatter)
            env = _read_env_config()
            try:
                keep = int(env.get("log_retention_days", 30) or 30)
            except ValueError:
                keep = 30
            try:
                _FILE_HANDLER = DailyFileHandler(
                    LOG_DIR,
                    prefix=env.get("log_file_prefix", "log") or "log",
                    ext=env.get("log_extension", "txt") or "txt",
                    keep_days=keep,
                )
                _FILE_HANDLER.setFormatter(formatter)
            except Exception as e:  # logging must never stop the API
                print(f"Log file handler not available ({LOG_DIR}): {e}", file=sys.stderr)
                _FILE_HANDLER = None
        return _CONSOLE_HANDLER, _FILE_HANDLER


def get_session_log_file() -> Path:
    """Today's log file (kept for older callers)."""
    _, fh = _handlers()
    day = datetime.now().strftime("%Y%m%d")
    return fh.path_for(day) if fh else LOG_DIR / f"log_{day}.txt"


def get_logger(name: str = "SAP_Pipeline") -> logging.Logger:
    """Logger writing to the console and to the daily log file."""
    logger = logging.getLogger(name)
    logger.setLevel(_level())
    logger.propagate = False
    if not logger.handlers:
        console, file_handler = _handlers()
        logger.addHandler(console)
        if file_handler:
            logger.addHandler(file_handler)
    return logger
