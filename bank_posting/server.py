"""
Standalone FastAPI application for SAP Bank Posting with Clearing.

Run directly:
    python bank_posting\\server.py --port 8001
Or using uvicorn:
    uvicorn bank_posting.server:app --reload --port 8001

Endpoints:
    GET  /                -> Redirect to Swagger UI (/docs)
    GET  /health          -> Service health check
    POST /api/bank/post   -> Post bank receipt with SAP invoice clearing
    POST /api/bank/upload -> Same, with the Excel file uploaded from the client
    POST /api/invoice/post  -> Post new customer invoices in SAP, document no. -> active_bt3i
    POST /api/clearing/post -> Clear one connection (active_cnx: bank receipt + invoices) in SAP
    GET  /docs            -> Interactive Swagger UI documentation
    GET  /redoc           -> ReDoc documentation
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import Depends, FastAPI, File, Form, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

try:
    from pydantic import BaseModel, ConfigDict, Field

    PYDANTIC_V2 = True
except ImportError:
    from pydantic import BaseModel, Field  # type: ignore

    ConfigDict = None  # type: ignore
    PYDANTIC_V2 = False

# Ensure project root is in sys.path for shared imports (logger_setup, db_manager, etc.)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bank_posting.auth import require_api_key  # noqa: E402
from bank_posting.clearing import process_clearing  # noqa: E402
from bank_posting.invoice_post import process_invoice_post  # noqa: E402
from bank_posting.posting import data_dir, logger, process_bank_posting, process_request  # noqa: E402


# ---------------------------------------------------------------------------
# Pydantic Schemas for Swagger / OpenAPI Documentation
# ---------------------------------------------------------------------------
class BankPostingItem(BaseModel):
    doc_date: Optional[str] = Field(
        default=None,
        description="Document date (YYYY-MM-DD or DD.MM.YYYY)",
        example="2026-11-15" if not PYDANTIC_V2 else None,
    )
    posting_date: Optional[str] = Field(
        default=None,
        description="Posting date (YYYY-MM-DD)",
        example="2026-11-15" if not PYDANTIC_V2 else None,
    )
    company_code: Optional[str] = Field(
        default=None,
        description="SAP Company Code (BUKRS)",
        example="DAT1" if not PYDANTIC_V2 else None,
    )
    currency: Optional[str] = Field(
        default=None,
        description="Currency key (WAERS)",
        example="INR" if not PYDANTIC_V2 else None,
    )
    customer: Optional[str] = Field(
        default=None,
        description="Customer account number (NEWKO_1)",
        example="30000001" if not PYDANTIC_V2 else None,
    )
    bank_gl: Optional[str] = Field(
        default=None,
        description="Bank G/L account (NEWKO_2)",
        example="410000" if not PYDANTIC_V2 else None,
    )
    amount: Optional[float] = Field(
        default=None,
        description="Receipt amount",
        example=1858500.0 if not PYDANTIC_V2 else None,
    )
    invoice_ref: Optional[str] = Field(
        default=None,
        description="Invoice reference to match and clear",
        example="0090000057" if not PYDANTIC_V2 else None,
    )
    utr: Optional[str] = Field(
        default=None,
        description="Bank UTR / transaction reference",
        example="UTR123456789" if not PYDANTIC_V2 else None,
    )
    item_text: Optional[str] = Field(
        default=None,
        description="Line item text",
        example="Bank receipt" if not PYDANTIC_V2 else None,
    )

    if PYDANTIC_V2:
        model_config = ConfigDict(extra="allow")
    else:

        class Config:
            extra = "allow"


class BankPostingRequest(BaseModel):
    file_type: Optional[str] = Field(
        default=None,
        description=(
            "'bank' (F-30 file: validate, post + clear in SAP, update DB) or "
            "'invoice' (FB70 file: validate customer + invoice in SAP, update DB). "
            "If omitted it is detected from the Excel header."
        ),
    )
    file_path: Optional[str] = Field(
        default=None,
        description=(
            "Excel file: an S3 location (s3://bucket/key or https://bucket.s3.amazonaws.com/key), "
            "or a path on the server. If a path does not exist on the server, a file with the "
            r"same name in the server data folder is used (e.g. C:\SAP\log\BANK.xlsx -> data/BANK.xlsx)."
        ),
        example="data/BANK EXCEL SHEET.xlsx" if not PYDANTIC_V2 else None,
    )
    sheet: Optional[str] = Field(
        default=None,
        description="Excel sheet name (defaults to first sheet)",
    )
    dry_run: bool = Field(
        default=False,
        description="Run validation against SAP open items without posting",
    )
    match_by: Optional[str] = Field(
        default=None,
        description=(
            "SAP field the invoice reference is matched on. Bank: XBLNR | ZUONR | BELNR "
            "(default from config, XBLNR). Invoice: ANY | XBLNR | ZUONR | BELNR (default ANY = XBLNR or ZUONR)."
        ),
    )
    company_code: Optional[str] = Field(
        default=None,
        description="Invoice files only: company code to check (the FB70 file has no company code column)",
    )
    check_open_items: Optional[bool] = Field(
        default=None,
        description="Validate against live SAP BSID open items before posting",
    )
    update_db: bool = Field(
        default=True,
        description="Update active_bt3i and active_bs tables with SAP document number",
    )
    items: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="Direct list of posting items instead of file_path",
    )
    tenant_id: Optional[str] = Field(
        default=None,
        description=(
            "Tenant. When set, file_path is read from the tenant S3 bucket: "
            "s3://dt-<tenant_id>-<env>/<folder>/<org_id>/<file name>"
        ),
    )
    org_id: Optional[str] = Field(
        default=None,
        description="Organisation id (folder inside the file folder: <folder>/<org_id>/<file>)",
    )

    if PYDANTIC_V2:
        model_config = ConfigDict(
            json_schema_extra={
                "example": {
                    "file_path": "C:\\SAP\\BANK EXCEL SHEET.xlsx",
                    "sheet": "F-30 Upload",
                    "dry_run": True,
                    "update_db": True,
                    "tenant_id": "DM00005",
                    "org_id": "OM0000000001",
                },
                "example_invoice": {
                    "file_type": "invoice",
                    "file_path": "SAP/Invoice Excel.xlsx",
                    "sheet": "FB70_Upload",
                    "company_code": "DAT1",
                    "dry_run": True,
                    "update_db": True,
                    "tenant_id": "DM00005",
                    "org_id": "OM0000000001",
                },
                "example_items": {
                    "dry_run": True,
                    "match_by": "XBLNR",
                    "check_open_items": True,
                    "update_db": True,
                    "items": [
                        {
                            "doc_date": "2026-11-15",
                            "posting_date": "2026-11-15",
                            "company_code": "DAT1",
                            "currency": "INR",
                            "customer": "30000001",
                            "bank_gl": "410000",
                            "amount": 1858500.0,
                            "invoice_ref": "0090000057",
                            "utr": "UTR123456789",
                            "item_text": "Bank receipt",
                        }
                    ],
                },
            }
        )
    else:

        class Config:
            schema_extra = {
                "example": {
                    "file_path": "C:\\SAP\\BANK EXCEL SHEET.xlsx",
                    "sheet": "F-30 Upload",
                    "dry_run": True,
                    "update_db": True,
                    "tenant_id": "DM00005",
                    "org_id": "OM0000000001",
                },
                "example_invoice": {
                    "file_type": "invoice",
                    "file_path": "SAP/Invoice Excel.xlsx",
                    "sheet": "FB70_Upload",
                    "company_code": "DAT1",
                    "dry_run": True,
                    "update_db": True,
                    "tenant_id": "DM00005",
                    "org_id": "OM0000000001",
                },
                "example_items": {
                    "dry_run": True,
                    "match_by": "XBLNR",
                    "check_open_items": True,
                    "update_db": True,
                    "items": [
                        {
                            "doc_date": "2026-11-15",
                            "posting_date": "2026-11-15",
                            "company_code": "DAT1",
                            "currency": "INR",
                            "customer": "30000001",
                            "bank_gl": "410000",
                            "amount": 1858500.0,
                            "invoice_ref": "0090000057",
                            "utr": "UTR123456789",
                            "item_text": "Bank receipt",
                        }
                    ],
                },
            }


class PostingItemResult(BaseModel):
    row: int = Field(description="Row index")
    status: str = Field(
        description="Status: VALID, INVALID, SUCCESS, POSTED_NOT_CLEARED, ERROR, NOT_POSTED"
    )
    company_code: Optional[str] = None
    customer: Optional[str] = None
    invoice_ref: Optional[str] = None
    utr: Optional[str] = None
    amount: Optional[float] = None
    currency: Optional[str] = None
    document_no: Optional[str] = None
    fiscal_year: Optional[str] = None
    clearing_doc: Optional[str] = None
    clearing_date: Optional[str] = None
    message: Optional[str] = None

    if PYDANTIC_V2:
        model_config = ConfigDict(extra="allow")
    else:

        class Config:
            extra = "allow"


class BankPostingResponse(BaseModel):
    status: str = Field(description="Execution status (completed / error)")
    batch_id: Optional[str] = Field(None, description="Batch execution identifier")
    dry_run: bool = Field(description="Whether this was a dry run")
    match_by: Optional[str] = Field(
        None, description="Matching rule used (XBLNR, ZUONR, BELNR)"
    )
    source: Optional[str] = Field(
        None, description="Source of items (file path or json)"
    )
    sap_posting: Optional[str] = Field(
        None, description="Status of SAP RFC posting connection"
    )
    total: int = Field(description="Total rows processed")
    summary: Dict[str, int] = Field(description="Count breakdown by row status")
    db_update: Dict[str, Any] = Field(description="Database document ID update summary")
    results: List[PostingItemResult] = Field(description="Per-row processing results")
    timestamp: Optional[str] = Field(None, description="ISO timestamp")

    if PYDANTIC_V2:
        model_config = ConfigDict(extra="allow")
    else:

        class Config:
            extra = "allow"


class InvoicePostItem(BaseModel):
    invoice_ref: str = Field(description="DataTwin invoice number (= active_bt3i.documentnumber), up to 25 characters")
    customer: Optional[str] = Field(default=None, description="SAP customer number (default: request level)")
    amount: float = Field(description="Gross invoice amount")
    doc_date: Optional[str] = Field(default=None, description="Invoice date YYYY-MM-DD (default: request level)")
    posting_date: Optional[str] = Field(default=None, description="Posting date (default: doc_date)")
    currency: Optional[str] = Field(default=None, description="Currency (default: request level)")
    company_code: Optional[str] = Field(default=None, description="SAP company code (default: request level)")
    gl_account: Optional[str] = Field(default=None, description="Revenue G/L account (default: request level)")
    tax_code: Optional[str] = Field(default=None, description="SAP tax code; empty = no tax (default: request level)")
    item_text: Optional[str] = Field(default=None, description="Line item text (50 characters)")
    baseline_date: Optional[str] = Field(default=None, description="Baseline date for payment (optional)")

    if PYDANTIC_V2:
        model_config = ConfigDict(extra="allow")
    else:

        class Config:
            extra = "allow"


_INVOICE_POST_EXAMPLE = {
    "org_id": "OM0000000001",
    "company_code": "DAT1",
    "gl_account": "40400000",
    "tax_code": "",
    "currency": "INR",
    "dry_run": True,
    "update_db": True,
    "items": [
        {
            "invoice_ref": "TMI3130009778",
            "customer": "30000001",
            "amount": 1165840.0,
            "doc_date": "2026-03-15",
            "posting_date": "2026-11-23",
            "item_text": "WPP MEDIA invoice TMI3130009778",
        }
    ],
}


class InvoicePostRequest(BaseModel):
    org_id: str = Field(description="Organisation (active_bt3i.orgid)")
    items: Optional[List[InvoicePostItem]] = Field(
        default=None, description="Invoices to post (or send an Excel file_path instead)"
    )
    file_path: Optional[str] = Field(
        default=None,
        description=(
            "FB70 Excel instead of items: S3 key in the tenant bucket (with tenant_id), "
            "s3://bucket/key, an S3 https URL, or a path on the server"
        ),
    )
    sheet: Optional[str] = Field(default=None, description="Excel sheet (default: first sheet)")
    tenant_id: Optional[str] = Field(
        default=None, description="Tenant: file_path is read from s3://dt-<tenant_id>-<env>/<file_path>"
    )
    company_code: Optional[str] = Field(default=None, description="Default SAP company code for all items")
    customer: Optional[str] = Field(default=None, description="Default SAP customer number")
    gl_account: Optional[str] = Field(default=None, description="Default revenue G/L account")
    tax_code: Optional[str] = Field(default=None, description="Default SAP tax code (empty = no tax)")
    currency: Optional[str] = Field(default=None, description="Default currency")
    doc_date: Optional[str] = Field(default=None, description="Default invoice date")
    posting_date: Optional[str] = Field(default=None, description="Default posting date")
    doc_type: Optional[str] = Field(default=None, description="SAP document type (default DR)")
    dry_run: bool = Field(default=False, description="true = checks + SAP simulation, nothing posted")
    update_db: bool = Field(default=True, description="Write the SAP document number to active_bt3i.documentid")

    if PYDANTIC_V2:
        model_config = ConfigDict(json_schema_extra={"example": _INVOICE_POST_EXAMPLE})
    else:

        class Config:
            schema_extra = {"example": _INVOICE_POST_EXAMPLE}


class ClearingInvoice(BaseModel):
    invoice_ref: str = Field(description="Invoice reference, matched on match_by (XBLNR / ZUONR / BELNR)")
    amount: float = Field(description="Amount to clear; must equal the open amount in SAP")


_CLEARING_EXAMPLE = {
    "org_id": "OM0000000001",
    "connection_id": "25677/20261117/30000",
    "company_code": "DAT1",
    "customer": "30000001",
    "bank_gl": "24500000",
    "currency": "INR",
    "posting_date": "2026-11-17",
    "amount": 30000.0,
    "utr": "PHDFCH01086920602",
    "match_by": "BELNR",
    "invoices": [
        {"invoice_ref": "1800000000", "amount": 10000.0},
        {"invoice_ref": "1800000007", "amount": 20000.0},
    ],
    "dry_run": True,
    "update_db": True,
}


class ClearingRequest(BaseModel):
    org_id: str = Field(description="Organisation (active_cnx.orgid)")
    connection_id: str = Field(description="Connection id (active_cnx.documentid)")
    company_code: Optional[str] = Field(default=None, description="SAP company code (BUKRS)")
    customer: Optional[str] = Field(default=None, description="SAP customer number (KUNNR)")
    bank_gl: Optional[str] = Field(default=None, description="Bank G/L account (HKONT)")
    currency: Optional[str] = Field(default=None, description="Currency (WAERS)")
    posting_date: Optional[str] = Field(default=None, description="Posting date (YYYY-MM-DD)")
    doc_date: Optional[str] = Field(default=None, description="Document date (default: posting_date)")
    value_date: Optional[str] = Field(default=None, description="Value date (optional)")
    amount: Optional[float] = Field(
        default=None, description="Bank receipt amount (default: the connection's bank row)"
    )
    utr: Optional[str] = Field(
        default=None, description="Bank reference / UTR (default: the connection's bank row reference)"
    )
    invoices: Optional[List[ClearingInvoice]] = Field(
        default=None,
        description="Invoices to clear (default: the connection's invoice rows, connectingdocumentnumber + documentvalue)",
    )
    match_by: Optional[str] = Field(
        default=None, description="SAP field the invoice_ref is matched on: XBLNR | ZUONR | BELNR (default from config)"
    )
    item_text: Optional[str] = Field(default=None, description="Bank line text (default: '<connection_id> <utr>')")
    doc_type: Optional[str] = Field(default=None, description="SAP document type (default DZ)")
    period: Optional[str] = Field(default=None, description="Posting period 1-16 (optional)")
    dry_run: bool = Field(default=False, description="true = validate only, nothing posted / updated")
    update_db: bool = Field(
        default=True, description="Write the SAP clearing document to active_cnx (cnx_clearing_column)"
    )

    if PYDANTIC_V2:
        model_config = ConfigDict(json_schema_extra={"example": _CLEARING_EXAMPLE})
    else:

        class Config:
            schema_extra = {"example": _CLEARING_EXAMPLE}


class ClearingResponse(BaseModel):
    status: str = Field(description="completed")
    batch_id: Optional[str] = None
    dry_run: bool
    result: str = Field(description="VALID, INVALID, SUCCESS, POSTED_NOT_CLEARED, ERROR, NOT_POSTED")
    message: Optional[str] = None
    org_id: Optional[str] = None
    connection_id: Optional[str] = None
    document_no: Optional[str] = Field(None, description="SAP posting document")
    fiscal_year: Optional[str] = None
    clearing_doc: Optional[str] = Field(None, description="SAP clearing document (written to active_cnx)")
    clearing_date: Optional[str] = None
    invoices: List[Dict[str, Any]] = Field(default_factory=list, description="Per-invoice SAP check")
    db_update: Dict[str, Any] = Field(default_factory=dict)

    if PYDANTIC_V2:
        model_config = ConfigDict(extra="allow")
    else:

        class Config:
            extra = "allow"


class HealthResponse(BaseModel):
    status: str = Field("ok", example="ok")
    service: str = Field("bank_posting", example="bank_posting")


class ErrorResponse(BaseModel):
    status: str = Field("error", example="error")
    message: str = Field(..., description="Error explanation")


# ---------------------------------------------------------------------------
# FastAPI Application
# ---------------------------------------------------------------------------
app = FastAPI(
    title="SAP Bank Posting & Clearing API",
    description=(
        "API that posts bank receipts in SAP S/4HANA and clears matching customer "
        "invoices in one step via SAP RFC function `Z_BANK_POST_CLEAR`, then writes "
        "the document numbers back to DataTwin tables (`active_bt3i` / `active_bs`)."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# Enable CORS for external web dashboards and Swagger interaction
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Windows paths pasted into JSON ("file_path": "sap\BANK.xlsx") are invalid JSON
# because of the single backslashes. If a JSON body does not parse, keep the
# escapes \\ \" \/ as they are and turn every other backslash into a literal one.
_BACKSLASH = re.compile(r'\\([\\"/])?')


def _escape_lone_backslashes(text: str) -> str:
    return _BACKSLASH.sub(lambda m: m.group(0) if m.group(1) else "\\\\", text)


class WindowsPathJSONMiddleware:
    """ASGI middleware: repairs JSON bodies that contain unescaped Windows paths."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        if b"json" not in headers.get(b"content-type", b""):
            return await self.app(scope, receive, send)

        chunks = []
        while True:
            message = await receive()
            if message["type"] != "http.request":
                return await self.app(scope, receive, send)  # client disconnected
            chunks.append(message.get("body", b""))
            if not message.get("more_body"):
                break
        body = b"".join(chunks)

        try:
            json.loads(body)
        except ValueError:
            try:
                fixed = _escape_lone_backslashes(body.decode("utf-8"))
                json.loads(fixed)
            except ValueError:
                pass  # still invalid: let FastAPI report the original error
            else:
                logger.info(
                    "[REQUEST] escaped single backslashes in JSON body (Windows path)"
                )
                body = fixed.encode("utf-8")

        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        return await self.app(scope, replay, send)


app.add_middleware(WindowsPathJSONMiddleware)


@app.middleware("http")
async def access_log(request: Request, call_next):
    """One line per request: method, path, status, duration, client (no headers, no API key)."""
    started = time.perf_counter()
    client = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip() or (
        request.client.host if request.client else "-"
    )
    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            f"[HTTP] {request.method} {request.url.path} failed after "
            f"{time.perf_counter() - started:.1f}s (client {client})"
        )
        raise
    if request.url.path != "/health":
        logger.info(
            f"[HTTP] {request.method} {request.url.path} -> {response.status_code} "
            f"in {time.perf_counter() - started:.1f}s (client {client})"
        )
    return response


@app.get(
    "/",
    include_in_schema=False,
    summary="Root redirect to Swagger UI",
)
async def root(request: Request):
    """Redirects base URL to the interactive Swagger documentation.

    Honours the root path (e.g. /sap/bank behind nginx) so the redirect
    stays under the proxy prefix.
    """
    root_path = request.scope.get("root_path", "").rstrip("/")
    return RedirectResponse(url=f"{root_path}/docs")


@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["System"],
    summary="Service Health Check",
    description="Returns the status of the bank posting service.",
)
async def health():
    return {"status": "ok", "service": "bank_posting"}


@app.post(
    "/api/bank/post",
    response_model=BankPostingResponse,
    responses={
        200: {"description": "Posting and validation results"},
        400: {"model": ErrorResponse, "description": "Validation or file error"},
        401: {"description": "Missing or invalid X-API-Key"},
        500: {"model": ErrorResponse, "description": "Server or SAP error"},
    },
    dependencies=[Depends(require_api_key)],
    tags=["Bank Posting"],
    summary="Process Bank or Invoice File",
    description=(
        "Accepts an Excel `file_path` (S3 / server) or a list of `items`.\n\n"
        "* `file_type: bank` (F-30 layout): validates against live SAP open items (BSID), posts + clears via "
        "`Z_BANK_POST_CLEAR`, writes SAP document numbers to `active_bt3i` / `active_bs`.\n"
        "* `file_type: invoice` (FB70 layout): validates customer (KNA1/KNB1) and finds the invoice in SAP "
        "(BSID/BSAD), writes the SAP invoice document number to `active_bt3i`.\n\n"
        "`file_type` is detected from the Excel header when omitted. `dry_run: true` = validate only."
    ),
)
async def bank_post(payload: BankPostingRequest):
    try:
        res = await run_in_threadpool(
            process_request,
            file_type=payload.file_type,
            company_code=payload.company_code,
            file_path=payload.file_path,
            items=payload.items,
            sheet=payload.sheet,
            dry_run=bool(payload.dry_run),
            match_by=payload.match_by,
            check_sap_open_items=payload.check_open_items,
            update_db=bool(payload.update_db),
            tenant_id=payload.tenant_id,
            org_id=payload.org_id,
        )
        return res
    except (ValueError, FileNotFoundError) as e:
        logger.warning(f"[BANK POST] 400 Bad Request: {e}")
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"status": "error", "message": str(e)},
        )
    except Exception as e:
        logger.exception(f"Bank posting failed: {e}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"status": "error", "message": str(e)},
        )


@app.post(
    "/api/invoice/post",
    response_model=BankPostingResponse,
    responses={
        200: {"description": "Per-invoice result (check results[].status)"},
        400: {"model": ErrorResponse, "description": "Bad request"},
        401: {"description": "Missing or invalid X-API-Key"},
        500: {"model": ErrorResponse, "description": "Server, database or SAP error"},
    },
    dependencies=[Depends(require_api_key)],
    tags=["Invoice Posting"],
    summary="Post New Customer Invoices in SAP",
    description=(
        "Posts each invoice in SAP via `Z_INVOICE_POST` (Dr customer, key 01 / Cr revenue G/L, key 50; "
        "tax calculated by SAP when `tax_code` is set) and writes the new SAP document number to "
        "`active_bt3i.documentid` (row: `orgid` + `documentnumber = invoice_ref`).\n\n"
        "An invoice whose reference is already in SAP is linked, not posted again (`ALREADY_IN_SAP`). "
        "`dry_run: true` runs the checks and an SAP simulation; nothing is posted.\n\n"
        "Status: VALID, INVALID, POSTED, ALREADY_IN_SAP, ERROR, NOT_POSTED."
    ),
)
async def invoice_post(payload: InvoicePostRequest):
    data = payload.model_dump() if PYDANTIC_V2 else payload.dict()
    try:
        return await run_in_threadpool(
            process_invoice_post, data, dry_run=bool(payload.dry_run), update_db=bool(payload.update_db)
        )
    except (ValueError, FileNotFoundError) as e:
        logger.warning(f"[INVOICE POST] 400 Bad Request: {e}")
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"status": "error", "message": str(e)},
        )
    except Exception as e:
        logger.exception(f"Invoice posting failed: {e}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"status": "error", "message": str(e)},
        )


@app.post(
    "/api/clearing/post",
    response_model=ClearingResponse,
    responses={
        200: {"description": "Validation / posting result (check `result`)"},
        400: {"model": ErrorResponse, "description": "Bad request"},
        401: {"description": "Missing or invalid X-API-Key"},
        500: {"model": ErrorResponse, "description": "Server, database or SAP error"},
    },
    dependencies=[Depends(require_api_key)],
    tags=["Connection Clearing"],
    summary="Clear One Connection in SAP",
    description=(
        "Clears one DataTwin connection (`active_cnx`: one bank receipt + 1..n invoices) in SAP with "
        "**one** document via `Z_BANK_POST_CLEAR_MULTI` (Dr bank G/L, every invoice cleared), then writes "
        "the clearing document number to `active_cnx.clearingdocid` (config `cnx_clearing_column`) on "
        "every row of the connection.\n\n"
        "Invoices, amount and UTR default to the connection's rows. Bank amount must equal the sum of the "
        "invoices and each invoice must be open in SAP with exactly that amount (no partial clearing). "
        "`dry_run: true` = validate only."
    ),
)
async def clearing_post(payload: ClearingRequest):
    data = payload.model_dump() if PYDANTIC_V2 else payload.dict()
    try:
        return await run_in_threadpool(
            process_clearing,
            data,
            dry_run=bool(payload.dry_run),
            update_db=bool(payload.update_db),
            match_by=payload.match_by,
        )
    except ValueError as e:
        logger.warning(f"[CLEARING] 400 Bad Request: {e}")
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"status": "error", "message": str(e)},
        )
    except Exception as e:
        logger.exception(f"Connection clearing failed: {e}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"status": "error", "message": str(e)},
        )


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]")


@app.post(
    "/api/bank/upload",
    response_model=BankPostingResponse,
    responses={
        200: {"description": "Posting and validation results"},
        400: {"model": ErrorResponse, "description": "Validation or file error"},
        401: {"description": "Missing or invalid X-API-Key"},
        500: {"model": ErrorResponse, "description": "Server or SAP error"},
    },
    dependencies=[Depends(require_api_key)],
    tags=["Bank Posting"],
    summary="Upload Bank or Invoice Excel, Validate and Update DB",
    description=(
        "Upload a bank (F-30) or invoice (FB70) Excel file. The file is saved in the server data folder "
        "and processed exactly like `POST /api/bank/post`. Set `dry_run=true` to validate only."
    ),
)
async def bank_upload(
    file: UploadFile = File(..., description="Excel file (.xlsx)"),
    file_type: Optional[str] = Form(
        None, description="bank | invoice (default: detected from header)"
    ),
    sheet: Optional[str] = Form(None, description="Sheet name (default: first sheet)"),
    dry_run: bool = Form(
        False, description="true = validate only, no SAP posting / DB update"
    ),
    match_by: Optional[str] = Form(
        None,
        description="Bank: XBLNR | ZUONR | BELNR. Invoice: ANY | XBLNR | ZUONR | BELNR",
    ),
    update_db: bool = Form(
        True, description="Write SAP document numbers to active_bt3i / active_bs"
    ),
    company_code: Optional[str] = Form(
        None, description="Invoice files: company code to check"
    ),
):
    name = _SAFE_NAME.sub("_", Path(file.filename or "upload.xlsx").name)
    if not name.lower().endswith((".xlsx", ".xlsm")):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "status": "error",
                "message": "Only .xlsx / .xlsm files are supported",
            },
        )
    folder = data_dir() / "uploads"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{datetime.now():%Y%m%d_%H%M%S}_{name}"
    target.write_bytes(await file.read())
    logger.info(f"[BANK UPLOAD] saved {target}")
    try:
        return await run_in_threadpool(
            process_request,
            file_type=file_type or None,
            file_path=str(target),
            sheet=sheet or None,
            dry_run=dry_run,
            match_by=match_by or None,
            update_db=update_db,
            company_code=company_code or None,
        )
    except (ValueError, FileNotFoundError) as e:
        logger.warning(f"[BANK POST] 400 Bad Request: {e}")
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"status": "error", "message": str(e)},
        )
    except Exception as e:
        logger.exception(f"Bank upload posting failed: {e}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"status": "error", "message": str(e)},
        )


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bank Posting FastAPI Server")
    parser.add_argument(
        "--host", default="0.0.0.0", help="Host interface to bind (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port", type=int, default=8001, help="Port to listen on (default: 8001)"
    )
    parser.add_argument(
        "--reload", action="store_true", help="Enable auto-reload on code changes"
    )
    args = parser.parse_args()

    uvicorn.run(
        "bank_posting.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
