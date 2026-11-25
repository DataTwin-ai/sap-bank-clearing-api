"""
1-minute SAP -> DataTwin sync, run by the bank API itself (no OS cron on the server).

Every SAP_SYNC_INTERVAL seconds (60) one background thread runs, as a separate process:

    python <SAP_SYNC_DIR>/cli.py delta --cron

which polls SAP BSID / BSAD / VBRK / VBRP, queues new and changed rows (each new SAP row gets a
passportid), checks or submits one ingestion job, and exits. The sync code (sap-extraction-api)
has its own modules named settings / db_manager / logger_setup / sap_odata_extractor, so it never
runs inside this process. A run starts only after the previous one ended.

uvicorn starts several workers; only the worker holding <data>/sap_sync/scheduler.lock runs the
loop. If that worker stops, another one takes the lock within a minute.

Environment:
    SAP_SYNC_ENABLED   true / false (default false, so a developer PC never syncs by accident;
                       deploy/deploy.sh turns it on in the container)
    SAP_SYNC_DIR       folder with the sync's cli.py (default <app>/sap_sync)
    SAP_SYNC_INTERVAL  seconds between run starts (default 60)
    SAP_SYNC_TIMEOUT   seconds before a hung run is stopped (default 900)
    SAP_EXTRACTION_CONFIG / SAP_EXTRACTION_BASE_CONFIG are passed on (sap-extraction-api/settings.py)

Logs: the sync writes <log>/sap_sync/cron.log itself, only when data moved or something failed.
Its one-line JSON result (same condition) and any crash output go to <log>/sap_sync/cron.out.
"""

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Optional, Tuple

from logger_setup import get_logger   # the bank API's daily log (log/log_YYYYMMDD.txt)

APP_ROOT = Path(__file__).resolve().parent
LOG_DIR = APP_ROOT / "log" / "sap_sync"
DATA_DIR = APP_ROOT / "data" / "sap_sync"
CRON_OUT_MAX_BYTES = 5 * 1024 * 1024
CRON_OUT_KEEP = 3

logger = get_logger("SAP_Pipeline.SapSync")

_stop = threading.Event()
_thread: Optional[threading.Thread] = None


def _enabled() -> bool:
    return os.getenv("SAP_SYNC_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, "") or default))
    except ValueError:
        return default


def _sync_dir() -> Path:
    return Path(os.getenv("SAP_SYNC_DIR") or APP_ROOT / "sap_sync")


def _try_lock() -> Optional[IO]:
    """Non-blocking exclusive lock, held while this process lives. None = another worker has it."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(DATA_DIR / "scheduler.lock", "a+")
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def _append_output(text: str) -> None:
    """Appends to cron.out, keeping cron.out.1 .. cron.out.<KEEP> when it grows past the limit."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out = LOG_DIR / "cron.out"
    try:
        if out.exists() and out.stat().st_size > CRON_OUT_MAX_BYTES:
            for i in range(CRON_OUT_KEEP - 1, 0, -1):
                src = LOG_DIR / f"cron.out.{i}"
                if src.exists():
                    src.replace(LOG_DIR / f"cron.out.{i + 1}")
            out.replace(LOG_DIR / "cron.out.1")
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(text if text.endswith("\n") else text + "\n")
    except OSError as exc:
        logger.warning(f"[SAP SYNC] could not write {out}: {exc}")


HEARTBEAT = "last_run.json"


def _write_heartbeat(started: float, exit_code: int, stdout: str) -> None:
    """data/sap_sync/last_run.json: when the last run started / ended and what it did (read by /api/sap-sync/status)."""
    result = None
    for line in reversed((stdout or "").strip().splitlines()):
        try:
            result = json.loads(line)
            break
        except ValueError:
            continue
    doc = {
        "started_at": datetime.fromtimestamp(started, timezone.utc).isoformat(timespec="seconds"),
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seconds": round(time.time() - started, 1),
        "exit_code": exit_code,
        "pid": os.getpid(),
        # the sync prints a result only when data moved or something failed
        "result": result,
    }
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = DATA_DIR / (HEARTBEAT + ".tmp")
        tmp.write_text(json.dumps(doc, default=str), encoding="utf-8")
        tmp.replace(DATA_DIR / HEARTBEAT)
        if result is not None:   # keep the last run that moved data too
            (DATA_DIR / "last_activity.json").write_text(json.dumps(doc, default=str), encoding="utf-8")
    except OSError as exc:
        logger.warning(f"[SAP SYNC] could not write heartbeat: {exc}")


def read_heartbeat() -> dict:
    """Last run and last run that moved data, as written by the running scheduler (any worker can read)."""
    out = {}
    for key, name in (("last_run", HEARTBEAT), ("last_activity", "last_activity.json")):
        try:
            out[key] = json.loads((DATA_DIR / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            out[key] = None
    return out


def status(runs: int = 10) -> dict:
    """
    For GET /api/sap-sync/status: is the 1-minute loop alive, what each SAP table did in its last
    poll, the ingestion queue, and the last ingestion runs (same DB as the bank API).
    """
    interval = _int_env("SAP_SYNC_INTERVAL", 60)
    beat = read_heartbeat()
    last = beat.get("last_run") or {}
    age = None
    if last.get("finished_at"):
        age = round((datetime.now(timezone.utc) - datetime.fromisoformat(last["finished_at"])).total_seconds())
    out = {
        "enabled": _enabled(),
        "interval_seconds": interval,
        # alive = a run finished within the last 3 intervals
        "alive": age is not None and age <= 3 * interval,
        "seconds_since_last_run": age,
        "last_run": last or None,
        "last_run_with_data": beat.get("last_activity"),
    }
    try:
        from sqlalchemy import text
        from db_manager import get_engine
        with get_engine().connect() as conn:
            tables = {}
            for r in conn.execute(text(
                    "SELECT sap_table, target_table, last_poll_at, last_success_at, sap_count, last_new, "
                    "last_changed, last_removed, last_error FROM public.sap_extraction_state ORDER BY sap_table")).mappings():
                info = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()}
                target = r["target_table"]
                if target and target.replace("_", "").isalnum():
                    q = conn.execute(text(f'SELECT ingestion_status, count(*) AS n FROM public."{target}" '
                                          "WHERE ingestion_status <> 'DONE' GROUP BY 1")).mappings()
                    info["queue"] = {row["ingestion_status"]: row["n"] for row in q}
                tables[r["sap_table"]] = info
            out["tables"] = tables
            out["ingestion_runs"] = [
                {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()}
                for r in conn.execute(text(
                    "SELECT run_id, sap_table, status, record_count, job_status, left(error, 300) AS error, "
                    "started_at, finished_at FROM public.sap_ingestion_run ORDER BY started_at DESC LIMIT :n"),
                    {"n": max(1, min(runs, 100))}).mappings()]
    except Exception as exc:   # status must answer even when the DB is down
        out["db_error"] = str(exc)[:300]
    return out


def run_once(timeout: Optional[int] = None) -> int:
    """One sync cycle in a child process. Returns its exit code (124 = stopped after timeout)."""
    started = time.time()
    rc, stdout = _run_child(timeout)
    _write_heartbeat(started, rc, stdout)
    return rc


def _run_child(timeout: Optional[int]) -> Tuple[int, str]:
    cli = _sync_dir() / "cli.py"
    timeout = timeout or _int_env("SAP_SYNC_TIMEOUT", 900)
    try:
        proc = subprocess.run(
            [sys.executable, str(cli), "delta", "--cron"],
            cwd=str(cli.parent), env=os.environ.copy(),
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        msg = f"SAP sync run stopped after {timeout}s (no result)"
        logger.error(f"[SAP SYNC] {msg}")
        _append_output(f'{{"at":"{time.strftime("%Y-%m-%dT%H:%M:%S")}","status":"failed","error":"{msg}"}}')
        if exc.stderr:
            _append_output(exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode(errors="replace"))
        return 124, ""
    output = (proc.stdout or "") + (proc.stderr or "")
    if output.strip():
        _append_output(output.strip())
    # 0 ok, 75 = previous run still busy; 1 / 2 are logged by the sync itself (cron.log)
    if proc.returncode not in (0, 2, 75) and not (proc.stdout or "").strip():
        logger.error(f"[SAP SYNC] run exited with code {proc.returncode} - see {LOG_DIR / 'cron.out'}")
    return proc.returncode, proc.stdout or ""


def _loop(interval: int) -> None:
    lock = None
    try:
        while not _stop.is_set():
            if lock is None:
                lock = _try_lock()
                if lock is None:          # another uvicorn worker runs the sync
                    _stop.wait(interval)
                    continue
                logger.info(f"[SAP SYNC] started in worker pid {os.getpid()}: every {interval}s, "
                            f"{_sync_dir() / 'cli.py'} delta --cron")
            started = time.time()
            try:
                run_once()
            except Exception:
                logger.exception("[SAP SYNC] run failed to start")
            _stop.wait(max(1.0, interval - (time.time() - started)))
    finally:
        if lock is not None:
            lock.close()


def start() -> bool:
    """Starts the background loop when SAP_SYNC_ENABLED is on. Safe to call more than once."""
    global _thread
    if not _enabled():
        logger.info("[SAP SYNC] off (SAP_SYNC_ENABLED is not true)")
        return False
    cli = _sync_dir() / "cli.py"
    if not cli.exists():
        logger.error(f"[SAP SYNC] not started: {cli} does not exist")
        return False
    if _thread is not None and _thread.is_alive():
        return True
    _stop.clear()
    _thread = threading.Thread(target=_loop, args=(_int_env("SAP_SYNC_INTERVAL", 60),),
                               name="sap-sync", daemon=True)
    _thread.start()
    return True


def stop() -> None:
    """Stops the loop after the current run (a running child process finishes on its own)."""
    _stop.set()
