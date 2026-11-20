"""
Central settings for the SAP extraction service.

Nothing here depends on the machine the code runs on:
  * config.ini is looked up at $SAP_EXTRACTION_CONFIG, else <project>/config.ini
  * relative folders (log/, data/) are resolved against the project folder,
    not the current working directory, so cron and systemd behave the same
  * every value can be overridden by an environment variable
"""

import configparser
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_FILE = Path(os.getenv("SAP_EXTRACTION_CONFIG") or PROJECT_ROOT / "config.ini").expanduser()

_TRUE = ("true", "1", "yes", "y", "on")


@lru_cache(maxsize=1)
def load_config() -> configparser.ConfigParser:
    """Parsed config.ini; empty when the file does not exist (env vars only)."""
    cfg = configparser.ConfigParser(interpolation=None)   # passwords may contain '%'
    if CONFIG_FILE.exists():
        cfg.read(CONFIG_FILE, encoding="utf-8")
    return cfg


def get(section: str, key: str, env: Optional[str] = None, default: str = "") -> str:
    """Environment variable `env` wins, then config.ini [section] key, then default."""
    if env and os.getenv(env, "").strip():
        return os.getenv(env, "").strip()
    cfg = load_config()
    if cfg.has_option(section, key):
        return cfg.get(section, key).strip()
    return default


def get_bool(section: str, key: str, env: Optional[str] = None, default: bool = False) -> bool:
    return get(section, key, env, "true" if default else "false").lower() in _TRUE


def get_int(section: str, key: str, env: Optional[str] = None, default: int = 0) -> int:
    try:
        return int(get(section, key, env, str(default)) or default)
    except ValueError:
        return default


def resolve_path(raw: str) -> Path:
    """Absolute paths are kept; relative ones are placed under the project folder."""
    path = Path(raw).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def split_tables(raw: str) -> List[str]:
    """'BSID, KNA1;BKPF' -> ['BSID', 'KNA1', 'BKPF'] (upper-case, de-duplicated)."""
    seen, out = set(), []
    for tok in re.split(r"[\s,;]+", raw or ""):
        name = tok.strip().upper()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def delta_tables() -> List[str]:
    """SAP tables kept in sync by the 1-minute delta poller ([delta] tables / DELTA_TABLES)."""
    return split_tables(get("delta", "tables", "DELTA_TABLES", ""))


# Folders -------------------------------------------------------------------
LOG_DIR = resolve_path(get("paths", "log_dir", "LOG_DIR", "log"))
DATA_DIR = resolve_path(get("paths", "data_dir", "DATA_DIR", "data"))
EXPORT_DIR = DATA_DIR / "exports"
