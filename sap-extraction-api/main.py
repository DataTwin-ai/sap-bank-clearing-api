"""
SAP Extraction API (FastAPI)

Run:
    uvicorn main:app --host 127.0.0.1 --port 8002 --workers 1
    python main.py --port 8002

Docs: /docs (Swagger), /redoc

Endpoints
    GET       /health                        liveness (no SAP / DB calls)
    GET       /api/check                     SAP + DB connectivity
    GET|POST  /api/extract                   one SAP table -> active_<table>
    GET|POST  /api/sync-config               all [sap] tables -> DB -> bt3i -> ingestion
    GET|POST  /api/transform/bt3i            active_bsid -> active_bt3i
    GET       /api/tables                    active_* tables + row counts
    GET       /api/tables/{table_name}       preview rows of active_<table>
    GET       /api/sap/tables                SAP tables exposed by the OData service
    GET       /api/sap/metadata/{table_name} SAP DDIC columns
    GET       /api/delta/status              delta tables: queue counts, last poll, runs
    POST      /api/delta/poll                poll SAP now (worker does it every minute)
    POST      /api/delta/ingest              start an ingestion cycle (background)
    POST      /api/delta/retry?table=BSID    FAILED rows -> PENDING

Security: when API_KEY (env) or [api] api_key (config.ini) is set, every /api/*
call must send header  X-API-Key: <key>.
"""

import argparse
import hmac
import threading
from typing import Any, Dict, List, Optional, Union

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Path as PathParam, Query, Security
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field
from sqlalchemy import text

import settings
from logger_setup import get_logger, setup_logging

setup_logging("api")

import pipeline  # noqa: E402  (after logging setup)
from db_manager import get_active_table_name, get_db_credentials, get_engine, list_active_tables  # noqa: E402
from sap_odata_extractor import SAPODataExtractor, clean_table_name  # noqa: E402

logger = get_logger("SAP_Pipeline.API")

API_KEY = settings.get("api", "api_key", "API_KEY", "")
ROOT_PATH = settings.get("api", "root_path", "ROOT_PATH", "")
if not API_KEY:
    logger.warning("API_KEY is not set - /api/* endpoints are open. Set it before exposing the server.")

app = FastAPI(
    title="SAP Extraction API",
    description=(
        "Extracts SAP tables via OData (ZGENERIC_DATA_SRV) into PostgreSQL `active_<table>` "
        "and syncs open invoices from `active_bsid` into `active_bt3i`."
    ),
    version="1.0.0",
    root_path=ROOT_PATH,
)

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(key: Optional[str] = Security(_api_key_header)) -> None:
    if API_KEY and not (key and hmac.compare_digest(key, API_KEY)):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")


def _sap() -> SAPODataExtractor:
    return SAPODataExtractor()


def _run(fn, **kwargs) -> JSONResponse:
    """Runs a pipeline operation and maps errors to HTTP status codes."""
    try:
        res = fn(**kwargs)
    except pipeline.PipelineBusy as exc:
        return JSONResponse({"status": "busy", "message": str(exc)}, status_code=409)
    except ValueError as exc:
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=400)
    except Exception as exc:
        logger.exception(f"{fn.__name__} failed")
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=500)
    code = 500 if isinstance(res, dict) and res.get("status") == "failed" else 200
    return JSONResponse(jsonable_encoder(res), status_code=code)


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------
class ExtractRequest(BaseModel):
    table: str = Field("BSID", description="SAP table / view name", examples=["BSID"])
    rows: Optional[int] = Field(None, ge=0, description="Max rows; empty or 0 = all rows")
    mode: Optional[str] = Field(None, description="replace | append (default from config.ini)")
    save_csv: Optional[bool] = Field(None, description="Also write data/exports/<table>_latest.csv")
    sync_bt3i: bool = Field(False, description="After BSID load, sync invoices to active_bt3i")
    env: Optional[str] = Field(None, description="config.ini section (development / production)")


class SyncConfigRequest(BaseModel):
    tables: Optional[Union[List[str], str]] = Field(None, description="Override [sap] tables, e.g. ['BSID','KNA1']")
    rows: Optional[int] = Field(None, ge=0)
    mode: Optional[str] = None
    save_csv: Optional[bool] = None
    sync_bt3i: Optional[bool] = None
    ingestion_script: Optional[str] = None
    fail_fast: Optional[bool] = None
    env: Optional[str] = None


class TransformRequest(BaseModel):
    env: Optional[str] = None
    company_code: Optional[str] = None
    org_id: Optional[str] = None
    tenant_id: Optional[str] = None


def _tables(raw: Optional[Union[List[str], str]]) -> Optional[List[str]]:
    if raw is None:
        return None
    return settings.split_tables(",".join(raw) if isinstance(raw, list) else raw) or None


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url=f"{ROOT_PATH.rstrip('/')}/docs")


@app.get("/health", tags=["System"])
def health() -> Dict[str, Any]:
    cfg = pipeline.load_pipeline_config()
    return {
        "status": "ok",
        "service": "sap-extraction-api",
        "config_file_found": settings.CONFIG_FILE.exists(),
        "configured_tables": cfg["tables"],
    }


@app.get("/api/check", tags=["System"], dependencies=[Depends(require_api_key)])
def check(env: Optional[str] = None) -> JSONResponse:
    """Live connectivity test: SAP OData service and PostgreSQL."""
    out: Dict[str, Any] = {}
    try:
        out["sap"] = "ok" if _sap().test_connection() else "failed"
    except Exception as exc:
        out["sap"] = f"failed: {exc}"
    try:
        creds = get_db_credentials(env)
        with get_engine(env).connect() as conn:
            conn.execute(text("SELECT 1"))
        out["database"] = "ok"
        out["environment"] = creds["env"]
        out["database_name"] = creds["database"]
    except Exception as exc:
        out["database"] = f"failed: {exc}"
    ok = out.get("sap") == "ok" and out.get("database") == "ok"
    out["status"] = "ok" if ok else "error"
    return JSONResponse(out, status_code=200 if ok else 503)


# ---------------------------------------------------------------------------
# Extraction / sync (sync `def` -> FastAPI runs them in a worker thread)
# ---------------------------------------------------------------------------
@app.get("/api/extract", tags=["Extraction"], dependencies=[Depends(require_api_key)])
def extract_get(
    table: str = "BSID",
    rows: Optional[int] = Query(None, ge=0),
    mode: Optional[str] = None,
    save_csv: Optional[bool] = None,
    sync_bt3i: bool = False,
    env: Optional[str] = None,
):
    """GET /api/extract?table=BSID&rows=50&mode=replace&sync_bt3i=true"""
    return _run(pipeline.extract, table=table, max_rows=rows, db_mode=mode,
                save_csv=save_csv, sync_bt3i=sync_bt3i, env=env)


@app.post("/api/extract", tags=["Extraction"], dependencies=[Depends(require_api_key)])
def extract_post(body: ExtractRequest):
    return _run(pipeline.extract, table=body.table, max_rows=body.rows, db_mode=body.mode,
                save_csv=body.save_csv, sync_bt3i=body.sync_bt3i, env=body.env)


@app.get("/api/sync-config", tags=["Extraction"], dependencies=[Depends(require_api_key)])
def sync_config_get(
    tables: Optional[str] = Query(None, description="Comma separated, e.g. BSID,KNA1"),
    rows: Optional[int] = Query(None, ge=0),
    mode: Optional[str] = None,
    save_csv: Optional[bool] = None,
    sync_bt3i: Optional[bool] = None,
    fail_fast: Optional[bool] = None,
    env: Optional[str] = None,
):
    """Same as the cron job: every [sap] table -> DB -> bt3i sync -> ingestion command."""
    return _run(pipeline.run_configured_tables, tables=_tables(tables), max_rows=rows, db_mode=mode,
                save_csv=save_csv, run_bt3i=sync_bt3i, env=env, fail_fast=fail_fast)


@app.post("/api/sync-config", tags=["Extraction"], dependencies=[Depends(require_api_key)])
def sync_config_post(body: SyncConfigRequest = SyncConfigRequest()):
    return _run(pipeline.run_configured_tables, tables=_tables(body.tables), max_rows=body.rows,
                db_mode=body.mode, save_csv=body.save_csv, run_bt3i=body.sync_bt3i,
                ingestion_script=body.ingestion_script, env=body.env, fail_fast=body.fail_fast)


@app.get("/api/transform/bt3i", tags=["Extraction"], dependencies=[Depends(require_api_key)])
def transform_get(env: Optional[str] = None, company_code: Optional[str] = None,
                  org_id: Optional[str] = None, tenant_id: Optional[str] = None):
    """active_bsid -> active_bt3i (open AR invoices)."""
    return _run(pipeline.transform_bt3i, env=env, company_code=company_code,
                org_id=org_id, tenant_id=tenant_id)


@app.post("/api/transform/bt3i", tags=["Extraction"], dependencies=[Depends(require_api_key)])
def transform_post(body: TransformRequest = TransformRequest()):
    return _run(pipeline.transform_bt3i, **body.model_dump())


# ---------------------------------------------------------------------------
# Delta sync + ingestion queue
# ---------------------------------------------------------------------------
_ingest_thread: Optional[threading.Thread] = None


@app.get("/api/delta/status", tags=["Delta"], dependencies=[Depends(require_api_key)])
def delta_status(env: Optional[str] = None, runs: int = Query(10, ge=1, le=200)):
    """Per delta table: sap_status / ingestion_status counts, last poll, recent ingestion runs."""
    import delta
    return _run(delta.status, env=env, runs=runs)


@app.post("/api/delta/poll", tags=["Delta"], dependencies=[Depends(require_api_key)])
def delta_poll(table: Optional[str] = Query(None, description="Default: all [delta] tables"),
               env: Optional[str] = None):
    """Poll SAP now (the worker does this every minute)."""
    import delta
    if table:
        return _run(delta.poll_table, sap_table=table, env=env)
    return _run(delta.poll_all, env=env)


@app.post("/api/delta/ingest", tags=["Delta"], dependencies=[Depends(require_api_key)])
def delta_ingest(env: Optional[str] = None):
    """Start one ingestion cycle in the background (409 if one is already running)."""
    import ingestion
    global _ingest_thread
    if _ingest_thread is not None and _ingest_thread.is_alive():
        return JSONResponse({"status": "busy", "message": "An ingestion cycle is already running"}, status_code=409)

    def _go():
        try:
            ingestion.run_ingestion(env=env)
        except pipeline.PipelineBusy as exc:
            logger.info(f"Ingestion not started: {exc}")
        except Exception:
            logger.exception("Background ingestion failed")

    _ingest_thread = threading.Thread(target=_go, name="api-ingestion", daemon=True)
    _ingest_thread.start()
    return JSONResponse({"status": "started", "message": "Check GET /api/delta/status for the run"},
                        status_code=202)


@app.post("/api/delta/retry", tags=["Delta"], dependencies=[Depends(require_api_key)])
def delta_retry(table: str = Query(..., description="SAP table, e.g. BSID"), env: Optional[str] = None):
    """Move FAILED rows back to PENDING (retry_count reset)."""
    import delta
    return _run(delta.retry_failed, sap_table=table, env=env)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
@app.get("/api/tables", tags=["Database"], dependencies=[Depends(require_api_key)])
def tables(env: Optional[str] = None):
    try:
        found = list_active_tables(env=env)
        return {"status": "success", "count": len(found), "tables": found}
    except Exception as exc:
        logger.error(f"Listing active tables failed: {exc}")
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=500)


@app.get("/api/tables/{table_name}", tags=["Database"], dependencies=[Depends(require_api_key)])
def table_preview(
    table_name: str = PathParam(..., description="SAP name (BSID) or DB name (active_bsid)"),
    limit: int = Query(10, ge=1, le=1000),
    env: Optional[str] = None,
):
    target = get_active_table_name(table_name)
    try:
        with get_engine(env).connect() as conn:
            exists = conn.execute(
                text("SELECT to_regclass(:t) IS NOT NULL"), {"t": f'public."{target}"'}
            ).scalar()
            if not exists:
                return JSONResponse({"status": "error", "message": f"Table '{target}' not found"}, status_code=404)
            total = conn.execute(text(f'SELECT COUNT(*) FROM public."{target}"')).scalar()
            res = conn.execute(text(f'SELECT * FROM public."{target}" LIMIT :n'), {"n": limit})
            cols = list(res.keys())
            data = [dict(zip(cols, [None if v is None else str(v) for v in r])) for r in res.fetchall()]
        return {"status": "success", "target_table": target, "total_rows": total,
                "sample_limit": limit, "columns": cols, "sample_data": data}
    except Exception as exc:
        logger.error(f"Preview of {target} failed: {exc}")
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=500)


# ---------------------------------------------------------------------------
# SAP dictionary
# ---------------------------------------------------------------------------
@app.get("/api/sap/tables", tags=["SAP"], dependencies=[Depends(require_api_key)])
def sap_tables(filter: Optional[str] = Query(None, description="Name contains, e.g. BS"),
               top: int = Query(100, ge=1, le=5000)):
    try:
        found = _sap().list_available_tables(name_filter=filter, top=top)
        clean = [{"table_name": t.get("TableName", ""), "description": t.get("Description", ""),
                  "max_batch_size": t.get("MaxBatchSize", 5000)} for t in found]
        return {"status": "success", "count": len(clean), "tables": clean}
    except Exception as exc:
        logger.error(f"Listing SAP tables failed: {exc}")
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=500)


@app.get("/api/sap/metadata/{table_name}", tags=["SAP"], dependencies=[Depends(require_api_key)])
def sap_metadata(table_name: str):
    try:
        name = clean_table_name(table_name)
        cols = _sap().get_table_metadata(name)
        return {"status": "success", "table": name, "columns_count": len(cols), "columns": cols}
    except ValueError as exc:
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=400)
    except Exception as exc:
        logger.error(f"SAP metadata for {table_name} failed: {exc}")
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=500)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SAP Extraction API")
    parser.add_argument("--host", default=settings.get("api", "host", "API_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=settings.get_int("api", "port", "API_PORT", 8002))
    args = parser.parse_args()
    uvicorn.run("main:app", host=args.host, port=args.port, workers=1, log_level="info")
