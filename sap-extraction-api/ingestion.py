"""
Ingestion queue runner.

PENDING rows of a delta table (see delta.py) are sent to the Data Modelling &
Ingestion API, one run at a time:

  1. resume a RUNNING run left behind by a stopped worker (wait for its job)
  2. claim PENDING rows            -> IN_PROGRESS, ingestion_run_id = run_id
  3. staging table active_<t>_delta = claimed rows only (passportid + SAP columns)
  4. POST {base_url}/api/ingest/table {db, table_name: active_<t>_delta, tenant_id, org_id,
                                        transaction_id: run_id}
  5. poll GET /api/jobs/{job_id} until the job finishes
  6. success -> rows DONE; failure -> retry_count + 1, back to PENDING (FAILED after max_retries)

A new run starts only after the previous one finished: a file lock in this process
and a unique index on sap_ingestion_run (status = 'RUNNING') in the database.
"""

import json
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import requests

import settings
from db_manager import get_active_table_name, get_db_credentials
from delta import RUN_TABLE, _connect, data_columns, ensure_control_tables, q
from logger_setup import get_logger
from pipeline import pipeline_lock
from sap_odata_extractor import clean_table_name

logger = get_logger("SAP_Pipeline.Ingestion")

SUCCESS_STATES = {"completed", "complete", "success", "succeeded", "done", "finished"}
FAILED_STATES = {"failed", "failure", "error", "cancelled", "canceled", "aborted"}


def ingestion_settings() -> Dict[str, Any]:
    return {
        "enabled": settings.get_bool("ingestion", "enabled", "INGESTION_ENABLED", True),
        "base_url": settings.get("ingestion", "base_url", "INGESTION_BASE_URL", "").rstrip("/"),
        "db": settings.get("ingestion", "db", "INGESTION_DB", ""),
        "batch_size": max(1, settings.get_int("ingestion", "batch_size", "INGESTION_BATCH_SIZE", 5000)),
        "job_poll_seconds": max(2, settings.get_int("ingestion", "job_poll_seconds", "INGESTION_JOB_POLL_SECONDS", 10)),
        "job_timeout_minutes": max(1, settings.get_int("ingestion", "job_timeout_minutes", "INGESTION_JOB_TIMEOUT_MINUTES", 30)),
        "request_timeout": max(5, settings.get_int("ingestion", "request_timeout", "INGESTION_REQUEST_TIMEOUT", 120)),
        "max_retries": max(1, settings.get_int("ingestion", "max_retries", "INGESTION_MAX_RETRIES", 3)),
        "tenant_id": settings.get("defaults", "tenant_id", "TENANT_ID", "DM00005"),
        "org_id": settings.get("defaults", "org_id", "ORG_ID", "OM0000000001"),
        "product_name": settings.get("ingestion", "product_name", "INGESTION_PRODUCT_NAME", ""),
        "user_id": settings.get("ingestion", "user_id", "INGESTION_USER_ID", ""),
        "save_location": settings.get("ingestion", "save_location", "INGESTION_SAVE_LOCATION", ""),
    }


_IDENT = re.compile(r"[a-z_][a-z0-9_]*")


def staging_columns(sap_table: str) -> List[Tuple[Tuple[str, ...], str]]:
    """
    SAP columns copied to the staging table as ((source columns), staging name) pairs
    ([ingestion] columns_bsid = bukrs, kunnr, ...).
      "bukrs_ana as bukrs"           renames a column, e.g. so VBRP items carry their VBRK header's bukrs
      "netwr + mwsbk as gross_value" adds numeric columns (empty = 0), e.g. gross = net + tax
    The ingestion service adds every unmapped source column to the target table, so send only the
    columns the target process needs. Empty = all columns.
    """
    t = sap_table.lower()
    raw = settings.get("ingestion", f"columns_{t}", f"INGESTION_COLUMNS_{t.upper()}", "")
    out = []
    for item in raw.replace(";", ",").split(","):
        item = " ".join(item.lower().split())
        if not item:
            continue
        expr, sep, alias = item.rpartition(" as ")
        if not sep:
            expr, alias = item, item
        src = tuple(c.strip() for c in expr.split("+"))
        if not all(_IDENT.fullmatch(c) for c in src + (alias,)):
            raise ValueError(f"[ingestion] columns_{t}: cannot read '{item}' "
                             f"(use 'column', 'column as name' or 'col1 + col2 as name')")
        out.append((src, alias))
    return out


def _staging_expr(src: Tuple[str, ...], alias: str) -> str:
    if len(src) == 1:
        return f"t.{q(src[0])}" if src[0] == alias else f"t.{q(src[0])} AS {q(alias)}"
    total = " + ".join(f"COALESCE(NULLIF(TRIM(t.{q(c)}::text), '')::numeric, 0)" for c in src)
    return f"({total}) AS {q(alias)}"


def customer_name_column(sap_table: str) -> str:
    """
    Customer number column of a delta table whose name from the customer master (active_kna1.name1,
    loaded by `cli.py extract KNA1`) is added to the staging table as customer_name
    ([ingestion] customer_name_vbrk = kunrg). SAP documents carry only the customer number; the
    theme maps customer_name to SenderName. Empty = no name column.
    """
    t = sap_table.lower()
    return settings.get("ingestion", f"customer_name_{t}", f"INGESTION_CUSTOMER_NAME_{t.upper()}", "").strip().lower()


def target_for(sap_table: str) -> Dict[str, str]:
    """
    Target process for a delta table, so the ingestion API skips AI classification:
      [ingestion] process_code_bsid = BT3I / transaction_type_bsid = Invoice / theme_name_bsid =
    (env: INGESTION_PROCESS_CODE_BSID, ...). Generic process_code / transaction_type / theme_name
    keys apply to tables without their own entry. Empty = let the AI decide.
    """
    t = sap_table.lower()
    out = {}
    for key in ("process_code", "transaction_type", "theme_name"):
        val = settings.get("ingestion", f"{key}_{t}", f"INGESTION_{key.upper()}_{t.upper()}", "") \
            or settings.get("ingestion", key, f"INGESTION_{key.upper()}", "")
        if val:
            out[key] = val
    return out


class IngestionError(RuntimeError):
    pass


class IngestionClient:
    def __init__(self, cfg: Optional[Dict[str, Any]] = None):
        self.cfg = cfg or ingestion_settings()
        if not self.cfg["base_url"]:
            raise IngestionError("Ingestion base_url is not configured ([ingestion] base_url / INGESTION_BASE_URL)")
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})

    def submit_table(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /api/ingest/table. The service processes the table inside this request
        (AI mapping + upsert), so the read timeout is the whole job timeout."""
        url = f"{self.cfg['base_url']}/api/ingest/table"
        resp = self.session.post(url, json=payload,
                                 timeout=(self.cfg["request_timeout"], self.cfg["job_timeout_minutes"] * 60))
        if resp.status_code >= 400:
            raise IngestionError(f"POST /api/ingest/table -> HTTP {resp.status_code}: {resp.text[:500]}")
        try:
            return resp.json() if resp.content else {}
        except ValueError:
            return {"raw": resp.text[:500]}

    def missing_fields(self, fields: List[str]) -> Optional[List[str]]:
        """
        Request fields the service does not accept (from its OpenAPI schema), [] if all are
        supported, None if the service did not answer. An old service would silently ignore
        process_code and let the AI route BSID to GLE, so nothing is sent in that case.
        """
        try:
            resp = self.session.get(f"{self.cfg['base_url']}/openapi.json", timeout=(5, 30))
            props = resp.json()["components"]["schemas"]["TableIngestRequest"]["properties"]
        except Exception as exc:
            logger.warning(f"[INGEST] ingestion service not answering at {self.cfg['base_url']}: {exc}")
            return None
        return [f for f in fields if f not in props]

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        resp = self.session.get(f"{self.cfg['base_url']}/api/jobs/{job_id}", timeout=30)
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise IngestionError(f"GET /api/jobs/{job_id} -> HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        return data.get("job", data) if isinstance(data, dict) else {"raw": data}


def _job_state(job: Optional[Dict[str, Any]]) -> str:
    """SUCCESS / FAILED / RUNNING from an ingestion job document."""
    if not job:
        return "RUNNING"
    st = str(job.get("status") or "").strip().lower()
    if st in SUCCESS_STATES:
        return "SUCCESS"
    if st in FAILED_STATES:
        return "FAILED"
    return "RUNNING"


def _logged_outcome(conn, run_id: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    """
    The ingestion service writes one transaction_log row per transaction_id (same database).
    Used when the HTTP response was lost (timeout, worker restart): SUCCESS / FAILED, or None if unknown.
    """
    cur = conn.cursor()
    try:
        cur.execute("""SELECT status, processcode, themename, transactiontype FROM public.transaction_log
                       WHERE transactionid = %s ORDER BY id DESC LIMIT 1""", (run_id,))
        row = cur.fetchone()
        conn.commit()
    except psycopg2.Error:
        conn.rollback()
        return None
    if not row:
        return None
    doc = {"status": row[0], "processcode": row[1], "themename": row[2], "transactiontype": row[3],
           "source": "transaction_log"}
    st = str(row[0] or "").strip().lower()
    if st in SUCCESS_STATES:
        return "SUCCESS", doc
    if st in FAILED_STATES:
        return "FAILED", doc
    return None


def _compact(job: Optional[Dict[str, Any]]) -> Optional[str]:
    """Job document for the run table, without the (large) captured log."""
    if job is None:
        return None
    small = {k: (v[:2000] if isinstance(v, str) else v) for k, v in job.items() if k not in ("log", "logs")}
    return json.dumps(small, default=str)


def _wait_for_job(client: IngestionClient, job_id: str, started_at: datetime,
                  first: Optional[Dict[str, Any]] = None) -> Tuple[str, Optional[Dict[str, Any]], Optional[str]]:
    cfg = client.cfg
    deadline = started_at.timestamp() + cfg["job_timeout_minutes"] * 60
    job = first
    last_status = None
    while True:
        state = _job_state(job) if job is not None else "RUNNING"
        if state != "RUNNING":
            return state, job, (job or {}).get("error")
        if time.time() > deadline:
            return "FAILED", job, f"Timed out after {cfg['job_timeout_minutes']} min waiting for job {job_id}"
        time.sleep(cfg["job_poll_seconds"])
        try:
            job = client.get_job(job_id)
        except (requests.RequestException, IngestionError) as exc:
            logger.warning(f"[INGEST] job {job_id}: status check failed ({exc}); retrying")
            continue
        if job and job.get("status") != last_status:
            last_status = job.get("status")
            logger.info(f"[INGEST] job {job_id}: status={job.get('status')} stage={job.get('stage')} "
                        f"progress={job.get('progress')}")


def _finalize(conn, run: Dict[str, Any], outcome: str, job: Optional[Dict[str, Any]], error: Optional[str],
              max_retries: int) -> Dict[str, Any]:
    cur = conn.cursor()
    target, run_id = run["target_table"], run["run_id"]
    if outcome == "SUCCESS":
        cur.execute(f"""UPDATE public.{q(target)} SET ingestion_status = 'DONE', ingested_at = now(),
                        error_message = NULL
                        WHERE ingestion_run_id = %s AND ingestion_status = 'IN_PROGRESS'""", (run_id,))
    else:
        cur.execute(f"""UPDATE public.{q(target)} SET retry_count = retry_count + 1, error_message = %s,
                        ingestion_status = CASE WHEN retry_count + 1 >= %s THEN 'FAILED' ELSE 'PENDING' END
                        WHERE ingestion_run_id = %s AND ingestion_status = 'IN_PROGRESS'""",
                    ((error or "ingestion failed")[:1000], max_retries, run_id))
    rows = cur.rowcount
    cur.execute(f"""UPDATE public.{RUN_TABLE} SET status = %s, job_status = %s, error = %s,
                    finished_at = now(), result = %s::jsonb WHERE run_id = %s""",
                (outcome, (job or {}).get("status"), error, _compact(job), run_id))
    conn.commit()
    level = logger.info if outcome == "SUCCESS" else logger.error
    level(f"[INGEST] run {run_id} {outcome}: {rows} rows {'DONE' if outcome == 'SUCCESS' else 're-queued/failed'}"
          + (f" - {error}" if error else ""))
    return {"run_id": run_id, "sap_table": run["sap_table"], "status": outcome, "records": rows,
            "job_id": run.get("job_id"), "job_status": (job or {}).get("status"), "error": error}


def _resume_running(conn, client: IngestionClient) -> Optional[Dict[str, Any]]:
    """Finishes a run that a previous (stopped) worker left in RUNNING state."""
    cur = conn.cursor()
    cur.execute(f"""SELECT run_id, sap_table, target_table, staging_table, job_id, started_at
                    FROM public.{RUN_TABLE} WHERE status = 'RUNNING' LIMIT 1""")
    row = cur.fetchone()
    conn.commit()
    if not row:
        return None
    run = dict(zip(["run_id", "sap_table", "target_table", "staging_table", "job_id", "started_at"], row))
    logger.info(f"[INGEST] resuming run {run['run_id']} (job {run['job_id']})")
    logged = _logged_outcome(conn, run["run_id"])
    if logged:
        return _finalize(conn, run, logged[0], logged[1],
                         None if logged[0] == "SUCCESS" else f"transaction_log status {logged[1]['status']}",
                         client.cfg["max_retries"])
    if not run["job_id"]:
        return _finalize(conn, run, "FAILED", None, "Worker stopped before the ingestion API answered",
                         client.cfg["max_retries"])
    started = run["started_at"]
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    try:
        first = client.get_job(run["job_id"])
    except (requests.RequestException, IngestionError):
        first = None
    outcome, job, error = _wait_for_job(client, run["job_id"], started, first)
    return _finalize(conn, run, outcome, job, error, client.cfg["max_retries"])


def _run_one(conn, client: IngestionClient, sap_table: str, env: Optional[str], wait: bool) -> Optional[Dict[str, Any]]:
    cfg = client.cfg
    target = get_active_table_name(sap_table)
    staging = f"{target}_delta"
    run_id = f"TR_SAP_{sap_table}_{datetime.now():%Y%m%d%H%M%S}_{uuid.uuid4().hex[:6].upper()}"
    cur = conn.cursor()

    if not _has_column(cur, target, "passportid"):
        conn.commit()
        logger.warning(f"[INGEST] {target} is not a delta table; skipped")
        return None

    try:
        cur.execute(f"""INSERT INTO public.{RUN_TABLE} (run_id, sap_table, target_table, staging_table, status)
                        VALUES (%s, %s, %s, %s, 'RUNNING')""", (run_id, sap_table, target, staging))
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        return {"sap_table": sap_table, "status": "busy", "message": "Another ingestion run is RUNNING"}

    cur.execute(f"""UPDATE public.{q(target)} SET ingestion_status = 'IN_PROGRESS', ingestion_run_id = %s,
                    error_message = NULL
                    WHERE sap_key_hash IN (
                        SELECT sap_key_hash FROM public.{q(target)} WHERE ingestion_status = 'PENDING'
                        ORDER BY first_seen_at, passportid LIMIT %s FOR UPDATE SKIP LOCKED)
                    RETURNING passportid""", (run_id, cfg["batch_size"]))
    passports = sorted(r[0] for r in cur.fetchall())
    if not passports:
        conn.rollback()
        return None

    cols = data_columns(cur, target)
    wanted = staging_columns(sap_table)
    if wanted:
        missing = sorted({c for src, _ in wanted for c in src if c not in cols})
        if missing:
            logger.warning(f"[INGEST] {sap_table}: configured columns not in {target}: {missing}")
        select = [(src, alias) for src, alias in wanted if all(c in cols for c in src)]
    else:
        select = [((c,), c) for c in cols]
    parts = ["t.passportid"] + [_staging_expr(src, alias) for src, alias in select]

    name_join, name_col = "", customer_name_column(sap_table)
    if name_col:
        if name_col in cols and _has_column(cur, "active_kna1", "kunnr") and _has_column(cur, "active_kna1", "name1"):
            name_join = (f"LEFT JOIN (SELECT DISTINCT ON (kunnr) kunnr, NULLIF(TRIM(name1::text), '') AS name1 "
                         f"FROM public.active_kna1 WHERE kunnr IS NOT NULL ORDER BY kunnr) k "
                         f"ON k.kunnr::text = t.{q(name_col)}::text")
            parts.append("k.name1 AS customer_name")
        else:
            logger.warning(f"[INGEST] {sap_table}: customer_name sent empty ({name_col} not in {target} "
                           f"or active_kna1 missing - run: python cli.py extract KNA1)")
            parts.append("NULL::text AS customer_name")

    select_sql = ", ".join(parts)
    cur.execute(f"DROP TABLE IF EXISTS public.{q(staging)}")
    cur.execute(f"""CREATE TABLE public.{q(staging)} AS
                    SELECT {select_sql} FROM public.{q(target)} t {name_join}
                    WHERE t.ingestion_run_id = %s AND t.ingestion_status = 'IN_PROGRESS'
                    ORDER BY t.first_seen_at, t.passportid""", (run_id,))
    cur.execute(f"UPDATE public.{RUN_TABLE} SET record_count = %s WHERE run_id = %s", (len(passports), run_id))
    cur.execute(f"SELECT started_at FROM public.{RUN_TABLE} WHERE run_id = %s", (run_id,))
    started = cur.fetchone()[0]
    conn.commit()
    run = {"run_id": run_id, "sap_table": sap_table, "target_table": target, "staging_table": staging}
    logger.info(f"[INGEST] run {run_id}: {len(passports)} rows -> {staging} ({passports[0]} .. {passports[-1]})")

    payload = {
        "db": cfg["db"] or get_db_credentials(env)["database"],
        "table_name": staging,
        "tenant_id": cfg["tenant_id"],
        "org_id": cfg["org_id"],
        "transaction_id": run_id,
    }
    for opt in ("product_name", "user_id", "save_location"):
        if cfg[opt]:
            payload[opt] = cfg[opt]
    payload.update(target_for(sap_table))   # e.g. process_code=BT3I, transaction_type=Invoice

    try:
        resp = client.submit_table(payload)
    except (requests.RequestException, IngestionError) as exc:
        # The response may be lost although the service finished: trust its transaction_log
        logged = _logged_outcome(conn, run_id)
        if logged and logged[0] == "SUCCESS":
            logger.warning(f"[INGEST] run {run_id}: no HTTP answer ({exc}) but transaction_log says Success")
            return _finalize(conn, run, "SUCCESS", logged[1], None, cfg["max_retries"])
        reason = (f"No response from the ingestion API within {cfg['job_timeout_minutes']} min"
                  if isinstance(exc, requests.Timeout) else f"Submit failed: {exc}")
        return _finalize(conn, run, "FAILED", logged[1] if logged else None, reason, cfg["max_retries"])

    state = _job_state(resp) if resp.get("status") else None
    job_id = resp.get("job_id")
    if job_id and state == "RUNNING":
        # asynchronous variant: the API queued a job -> follow it
        run["job_id"] = str(job_id)
        cur.execute(f"UPDATE public.{RUN_TABLE} SET job_id = %s, job_status = %s WHERE run_id = %s",
                    (run["job_id"], resp.get("status"), run_id))
        conn.commit()
        logger.info(f"[INGEST] run {run_id}: queued as job {job_id}")
        if not wait:
            return {"run_id": run_id, "sap_table": sap_table, "status": "SUBMITTED",
                    "records": len(passports), "job_id": job_id}
        outcome, job, error = _wait_for_job(client, str(job_id), started, resp)
        return _finalize(conn, run, outcome, job, error, cfg["max_retries"])

    # synchronous variant: HTTP 200 = processed; a failed status / error in the body = failed
    run["job_id"] = str(job_id or resp.get("transaction_id") or run_id)
    inner = resp.get("data") if isinstance(resp.get("data"), dict) else {}
    if state is None and inner.get("status"):
        state = _job_state(inner)
    if state is None and resp.get("success") is False:
        state = "FAILED"
    logged = _logged_outcome(conn, run_id)
    if logged:
        resp = dict(resp, transaction_log=logged[1])
        if state is None:
            state = logged[0]
    if state == "RUNNING":   # finished request with an unknown status text
        return _finalize(conn, run, "FAILED", resp, f"Unexpected ingestion status: {inner.get('status') or resp.get('status')}",
                         cfg["max_retries"])
    if state == "FAILED" or (state is None and resp.get("error")):
        return _finalize(conn, run, "FAILED", resp, str(resp.get("error") or resp.get("message") or resp)[:1000],
                         cfg["max_retries"])
    return _finalize(conn, run, "SUCCESS", resp, None, cfg["max_retries"])


def _has_column(cur, table: str, column: str) -> bool:
    cur.execute("""SELECT 1 FROM information_schema.columns
                   WHERE table_schema = 'public' AND table_name = %s AND column_name = %s""", (table, column))
    return cur.fetchone() is not None


def run_ingestion(env: Optional[str] = None, tables: Optional[List[str]] = None, wait: bool = True) -> Dict[str, Any]:
    """
    One ingestion cycle: resume an unfinished run, then one run per delta table that has PENDING rows.
    Raises PipelineBusy when another ingestion cycle holds the lock.
    """
    cfg = ingestion_settings()
    if not cfg["enabled"]:
        return {"status": "disabled"}
    client = IngestionClient(cfg)
    wanted = sorted({k for t in (tables or settings.delta_tables()) for k in target_for(t)})
    missing = client.missing_fields(wanted) if wanted else []
    if missing is None or missing:
        msg = (f"Ingestion service at {cfg['base_url']} is not answering" if missing is None else
               f"Ingestion service at {cfg['base_url']} does not accept {missing} (old version - restart it)")
        logger.error(f"[INGEST] {msg}; nothing sent, rows stay PENDING")
        return {"status": "failed", "runs": [], "message": msg + "; nothing sent, rows stay PENDING"}
    with pipeline_lock("ingestion"):
        conn = _connect(env)
        try:
            cur = conn.cursor()
            ensure_control_tables(cur)
            conn.commit()
            runs: List[Dict[str, Any]] = []
            resumed = _resume_running(conn, client)
            if resumed:
                runs.append(resumed)
            for t in tables or settings.delta_tables():
                r = _run_one(conn, client, clean_table_name(t), env, wait)
                if r:
                    runs.append(r)
                    if r.get("status") in ("busy", "SUBMITTED"):
                        break
        finally:
            conn.close()
    failed = [r for r in runs if r.get("status") == "FAILED"]
    return {
        "status": "failed" if failed and len(failed) == len(runs) else ("partial_success" if failed else "success"),
        "runs": runs,
        "message": None if runs else "Nothing to ingest",
    }
