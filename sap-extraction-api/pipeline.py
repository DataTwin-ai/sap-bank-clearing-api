"""
SAP extraction pipeline (server version).

  SAP OData (ZGENERIC_DATA_SRV) -> PostgreSQL active_<table> -> active_bt3i sync
  -> optional external ingestion command.

Shared by the FastAPI app (main.py) and the cron entry point (cli.py).
Every run that writes to the database holds one lock file, so a cron run and
an API call can never load the same tables at the same time.
"""

import os
import shlex
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Dict, Iterator, List, Optional

import pandas as pd

import settings
from db_manager import get_active_table_name, save_dataframe_to_db
from logger_setup import get_logger
from sap_odata_extractor import SAPODataExtractor, clean_table_name

logger = get_logger("SAP_Pipeline.Pipeline")

DB_MODES = ("replace", "append")


class PipelineBusy(RuntimeError):
    """Another extraction / sync currently holds the pipeline lock."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def load_pipeline_config() -> Dict[str, Any]:
    """Pipeline defaults from config.ini [sap] (env vars override)."""
    rows = settings.get_int("sap", "default_max_rows", "SAP_DEFAULT_MAX_ROWS", 0)
    mode = settings.get("sap", "default_db_mode", "SAP_DEFAULT_DB_MODE", "replace").lower()
    return {
        "tables": settings.split_tables(settings.get("sap", "tables", "SAP_TABLES", "BSID")),
        "default_max_rows": rows if rows > 0 else None,
        "default_db_mode": mode if mode in DB_MODES else "replace",
        "save_csv": settings.get_bool("sap", "save_csv", "SAP_SAVE_CSV", False),
        "csv_retention_days": settings.get_int("sap", "csv_retention_days", "SAP_CSV_RETENTION_DAYS", 7),
        "run_bt3i_sync": settings.get_bool("sap", "run_bt3i_sync", "SAP_RUN_BT3I_SYNC", True),
        "ingestion_script": settings.get("sap", "ingestion_script", "SAP_INGESTION_SCRIPT", ""),
        "ingestion_timeout": settings.get_int("sap", "ingestion_timeout", "SAP_INGESTION_TIMEOUT", 1800),
        "fail_fast": settings.get_bool("sap", "fail_fast", "SAP_FAIL_FAST", True),
    }


# ---------------------------------------------------------------------------
# Lock (one DB-writing run at a time, across processes)
# ---------------------------------------------------------------------------
@contextmanager
def pipeline_lock(name: str = "pipeline") -> Iterator[None]:
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = settings.DATA_DIR / f"{name}.lock"
    fh = open(path, "a+")
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
        raise PipelineBusy("Another SAP extraction / sync is already running. Try again later.")
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid={os.getpid()} started={datetime.now().isoformat(timespec='seconds')}\n")
        fh.flush()
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------
def _save_csv(df: pd.DataFrame, table: str, retention_days: int) -> Dict[str, str]:
    """Writes <table>_latest.csv + a timestamped copy under data/exports and prunes old copies."""
    settings.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive = settings.EXPORT_DIR / f"{table}_{len(df)}_records_{stamp}.csv"
    latest = settings.EXPORT_DIR / f"{table}_latest.csv"
    df.to_csv(archive, index=False)
    df.to_csv(latest, index=False)

    if retention_days > 0:
        cutoff = (datetime.now() - timedelta(days=retention_days)).timestamp()
        for old in settings.EXPORT_DIR.glob(f"{table}_*_records_*.csv"):
            if old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True)
    return {"latest_csv": str(latest), "archive_csv": str(archive)}


def extract_table(
    table: str,
    max_rows: Optional[int] = None,
    db_mode: str = "replace",
    save_csv: bool = False,
    env: Optional[str] = None,
    odata: Optional[SAPODataExtractor] = None,
    csv_retention_days: int = 7,
) -> Dict[str, Any]:
    """Extracts one SAP table via OData and loads it into active_<table>. Raises on failure."""
    table = clean_table_name(table)
    if table in settings.delta_tables():
        raise ValueError(
            f"{table} is kept in sync by the delta poller (active_{table.lower()} has passportid); "
            "a full reload would drop it. Use `cli.py delta-poll` / POST /api/delta/poll instead."
        )
    if db_mode not in DB_MODES:
        raise ValueError(f"mode must be one of {DB_MODES}, got '{db_mode}'")
    target = get_active_table_name(table)
    limit = f"{max_rows} rows" if max_rows and max_rows > 0 else "ALL rows"
    logger.info(f"[EXTRACT] SAP '{table}' -> DB '{target}' ({limit}, mode={db_mode})")

    df = (odata or SAPODataExtractor()).fetch_table_data(table_name=table, max_rows=max_rows)
    if df.empty:
        raise RuntimeError(f"No records retrieved for SAP table '{table}'.")

    result: Dict[str, Any] = {
        "sap_table": table,
        "target_table": target,
        "rows_extracted": len(df),
        "columns_count": len(df.columns),
    }
    if save_csv:
        result["csv_files"] = _save_csv(df, table, csv_retention_days)

    db = save_dataframe_to_db(df=df, sap_table=table, mode=db_mode, env=env)
    result.update({
        "rows_inserted": db["rows_inserted"],
        "total_rows_in_db": db["total_rows_in_table"],
        "database": db.get("database"),
        "mode": db_mode,
        "deduplication": db.get("deduplication"),
        "elapsed_seconds": db.get("elapsed_seconds"),
    })
    return result


def run_bt3i_sync(
    env: Optional[str] = None,
    company_code: Optional[str] = None,
    org_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> Dict[str, Any]:
    """active_bsid -> active_bt3h (AR invoices). Name kept for config compatibility (run_bt3i_sync)."""
    from sync_bsid_to_bt3i import sync_bsid_to_bt3i
    return sync_bsid_to_bt3i(env=env, company_code=company_code, org_id=org_id, tenant_id=tenant_id)


def run_ingestion_script(
    script_cmd: str,
    synced_tables: List[str],
    env: Optional[str] = None,
    timeout: int = 1800,
) -> Dict[str, Any]:
    """
    Runs the configured downstream command after the DB load.
    A leading '*.py' is started with the same Python interpreter.
    The command runs in the project folder and gets SAP_SYNCED_TABLES / SAP_PIPELINE_ENV.
    """
    cmd = (script_cmd or "").strip()
    if not cmd:
        return {"status": "skipped", "reason": "No ingestion_script configured"}

    tokens = shlex.split(cmd, posix=(os.name != "nt"))
    if tokens and tokens[0].lower().endswith(".py"):
        tokens = [sys.executable] + tokens

    run_env = os.environ.copy()
    run_env["SAP_SYNCED_TABLES"] = ",".join(synced_tables)
    if env:
        run_env["SAP_PIPELINE_ENV"] = env

    logger.info(f"[INGESTION] Running: {' '.join(tokens)}")
    start = time.time()
    proc = subprocess.run(
        tokens, cwd=str(settings.PROJECT_ROOT), env=run_env,
        capture_output=True, text=True, check=False, timeout=timeout,
    )
    for line in (proc.stdout or "").strip().splitlines():
        logger.info(f"[INGESTION STDOUT] {line}")
    for line in (proc.stderr or "").strip().splitlines():
        logger.warning(f"[INGESTION STDERR] {line}")
    if proc.returncode != 0:
        raise RuntimeError(
            f"Ingestion script failed with exit code {proc.returncode}: "
            f"{(proc.stderr or proc.stdout or '').strip()[:500]}"
        )
    return {
        "status": "success",
        "command": tokens,
        "returncode": proc.returncode,
        "elapsed_seconds": round(time.time() - start, 2),
        "synced_tables": synced_tables,
    }


# ---------------------------------------------------------------------------
# Public operations (all take the lock)
# ---------------------------------------------------------------------------
def extract(
    table: str,
    max_rows: Optional[int] = None,
    db_mode: Optional[str] = None,
    save_csv: Optional[bool] = None,
    sync_bt3i: bool = False,
    env: Optional[str] = None,
) -> Dict[str, Any]:
    """Single table extraction (+ optional bt3i sync when the table is BSID)."""
    cfg = load_pipeline_config()
    with pipeline_lock():
        res = extract_table(
            table=table,
            max_rows=max_rows,
            db_mode=db_mode or cfg["default_db_mode"],
            save_csv=cfg["save_csv"] if save_csv is None else save_csv,
            env=env,
            csv_retention_days=cfg["csv_retention_days"],
        )
        res["status"] = "success"
        if sync_bt3i and res["sap_table"] == "BSID":
            try:
                res["bt3i_sync"] = run_bt3i_sync(env=env)
            except Exception as exc:
                logger.error(f"[BT3I ERROR] {exc}")
                res["status"] = "partial_success"
                res["bt3i_error"] = str(exc)
        res["timestamp"] = datetime.now().isoformat()
        return res


def transform_bt3i(
    env: Optional[str] = None,
    company_code: Optional[str] = None,
    org_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> Dict[str, Any]:
    with pipeline_lock():
        return run_bt3i_sync(env=env, company_code=company_code, org_id=org_id, tenant_id=tenant_id)


def run_configured_tables(
    tables: Optional[List[str]] = None,
    max_rows: Optional[int] = None,
    db_mode: Optional[str] = None,
    save_csv: Optional[bool] = None,
    run_bt3i: Optional[bool] = None,
    ingestion_script: Optional[str] = None,
    env: Optional[str] = None,
    fail_fast: Optional[bool] = None,
) -> Dict[str, Any]:
    """
    Config-driven sync (what cron runs):
      1. extract every table from [sap] tables (or `tables`) into active_<table>
      2. if all succeeded: active_bsid -> active_bt3i, then the ingestion command
    """
    cfg = load_pipeline_config()
    requested = [clean_table_name(t) for t in (tables or cfg["tables"]) if str(t).strip()]
    delta_managed = set(settings.delta_tables())
    skipped = [t for t in requested if t in delta_managed]
    target_tables = [t for t in requested if t not in delta_managed]
    if skipped:
        logger.info(f"[SYNC] Skipping {skipped}: kept in sync by the delta poller")
    if not target_tables:
        if skipped:
            return {"status": "success", "configured_tables": requested, "succeeded_tables": [],
                    "failed_tables": [], "skipped_delta_tables": skipped, "results": [],
                    "message": "All requested tables are handled by the delta poller",
                    "timestamp": datetime.now().isoformat()}
        raise ValueError("No SAP tables configured ([sap] tables / SAP_TABLES) and none supplied.")

    eff_rows = max_rows if max_rows is not None else cfg["default_max_rows"]
    eff_mode = db_mode or cfg["default_db_mode"]
    eff_csv = cfg["save_csv"] if save_csv is None else save_csv
    eff_bt3i = cfg["run_bt3i_sync"] if run_bt3i is None else run_bt3i
    eff_script = cfg["ingestion_script"] if ingestion_script is None else ingestion_script.strip()
    eff_fail_fast = cfg["fail_fast"] if fail_fast is None else fail_fast

    start = time.time()
    with pipeline_lock():
        logger.info(f"[SYNC START] tables={target_tables} mode={eff_mode} rows={eff_rows or 'ALL'}")
        odata = SAPODataExtractor()
        results: List[Dict[str, Any]] = []
        succeeded: List[str] = []
        failed: List[Dict[str, str]] = []

        for tbl in target_tables:
            try:
                results.append(extract_table(
                    table=tbl, max_rows=eff_rows, db_mode=eff_mode, save_csv=eff_csv,
                    env=env, odata=odata, csv_retention_days=cfg["csv_retention_days"],
                ))
                succeeded.append(tbl)
            except Exception as exc:
                logger.error(f"[SYNC ERROR] SAP table '{tbl}': {exc}")
                failed.append({"table": tbl, "error": str(exc)})
                if eff_fail_fast:
                    break

        summary: Dict[str, Any] = {
            "status": "success" if not failed else ("partial_success" if succeeded else "failed"),
            "configured_tables": target_tables,
            "skipped_delta_tables": skipped,
            "succeeded_tables": succeeded,
            "failed_tables": failed,
            "db_mode": eff_mode,
            "results": results,
        }

        if failed:
            logger.warning(f"[SYNC] Skipping bt3i sync / ingestion because {len(failed)} table(s) failed.")
        else:
            try:
                if eff_bt3i and "BSID" in succeeded:
                    summary["bt3i_sync"] = run_bt3i_sync(env=env)
                if eff_script:
                    summary["ingestion_script"] = run_ingestion_script(
                        eff_script, succeeded, env=env, timeout=cfg["ingestion_timeout"],
                    )
            except Exception as exc:
                logger.error(f"[SYNC ERROR] Downstream step failed: {exc}")
                summary["status"] = "partial_success"
                summary["downstream_error"] = str(exc)

        summary["elapsed_seconds"] = round(time.time() - start, 2)
        summary["timestamp"] = datetime.now().isoformat()
        logger.info(f"[SYNC END] status={summary['status']} elapsed={summary['elapsed_seconds']}s")
        return summary
