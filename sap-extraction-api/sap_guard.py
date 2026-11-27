"""
SAP login guard: stop calling SAP after repeated login failures.

SAP locks a user after a few wrong-password logons. If the password in config.ini is
wrong (for example after SAP changed it), the 1-minute worker would otherwise keep
trying and lock the user. After [sap] max_auth_failures (default 2) consecutive HTTP 401
answers, every SAP call is refused locally - no request leaves this server - until:

  * the SAP credentials in config.ini / environment change (picked up automatically,
    also by a running worker), or
  * someone runs `python cli.py sap-unblock`.

State lives in <data_dir>/sap_auth_guard.json and is shared by the worker, cron runs and
the API. It stores a hash of the credentials, never the password.
"""

import hashlib
import json
import os
from datetime import datetime
from typing import Any, Dict, Optional

import settings
from logger_setup import get_logger

logger = get_logger("SAP_Pipeline.SAPGuard")

GUARD_FILE = settings.DATA_DIR / "sap_auth_guard.json"


class SAPAuthBlocked(RuntimeError):
    """SAP calls are paused after repeated login failures."""


def max_failures() -> int:
    return max(1, settings.get_int("sap", "max_auth_failures", "SAP_MAX_AUTH_FAILURES", 2))


def fingerprint(cfg: Dict[str, Any]) -> str:
    raw = "|".join(str(cfg.get(k, "")) for k in ("host", "port", "client", "user", "password"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _load() -> Dict[str, Any]:
    try:
        return json.loads(GUARD_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(state: Dict[str, Any]) -> None:
    GUARD_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = GUARD_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, GUARD_FILE)


def check(cfg: Dict[str, Any]) -> None:
    """Raises SAPAuthBlocked when SAP calls are paused for these credentials."""
    st = _load()
    if not st.get("blocked"):
        return
    if st.get("fingerprint") != fingerprint(cfg):
        logger.warning("[SAP GUARD] SAP credentials changed - SAP calls resumed")
        _save({"failures": 0, "blocked": False, "resumed_at": datetime.now().isoformat(timespec="seconds"),
               "resumed_by": "credentials changed"})
        return
    raise SAPAuthBlocked(
        f"SAP calls paused since {st.get('blocked_at')} after {st.get('failures')} failed logins as "
        f"'{cfg.get('user')}' (HTTP 401). Fix the password in config.ini (resumes automatically) "
        f"or run: python cli.py sap-unblock"
    )


def record_auth_failure(cfg: Dict[str, Any], detail: str = "") -> None:
    st = _load()
    fp = fingerprint(cfg)
    failures = (st.get("failures", 0) if st.get("fingerprint") == fp else 0) + 1
    st = {"failures": failures, "fingerprint": fp, "user": cfg.get("user"), "blocked": False,
          "last_failure_at": datetime.now().isoformat(timespec="seconds"), "last_error": detail[:200]}
    if failures >= max_failures():
        st["blocked"] = True
        st["blocked_at"] = st["last_failure_at"]
        logger.critical(f"[SAP GUARD] {failures} failed SAP logins as '{cfg.get('user')}' - all SAP calls "
                        "paused to avoid locking the user. Update the password in config.ini or run "
                        "`python cli.py sap-unblock`.")
    else:
        logger.error(f"[SAP GUARD] SAP login failed ({failures}/{max_failures()}) as '{cfg.get('user')}'")
    _save(st)


def record_success(cfg: Dict[str, Any]) -> None:
    if GUARD_FILE.exists():
        st = _load()
        if st.get("failures") or st.get("blocked"):
            _save({"failures": 0, "blocked": False, "fingerprint": fingerprint(cfg),
                   "resumed_at": datetime.now().isoformat(timespec="seconds"), "resumed_by": "successful login"})


def unblock() -> Dict[str, Any]:
    _save({"failures": 0, "blocked": False, "resumed_at": datetime.now().isoformat(timespec="seconds"),
           "resumed_by": "sap-unblock"})
    logger.warning("[SAP GUARD] SAP calls resumed manually (sap-unblock)")
    return {"status": "success", "message": "SAP calls resumed. The next call is a real login attempt."}


def status() -> Dict[str, Any]:
    st = _load()
    return {k: v for k, v in st.items() if k != "fingerprint"} or {"failures": 0, "blocked": False}
