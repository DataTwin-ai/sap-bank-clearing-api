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
    GET  /docs            -> Interactive Swagger UI documentation
    GET  /redoc           -> ReDoc documentation
"""

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, File, Form, Request, UploadFile, status
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

from bank_posting.posting import data_dir, logger, process_bank_posting  # noqa: E402


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
    file_path: Optional[str] = Field(
        default=None,
        description=(
            "Path to the Excel file. If the path does not exist on the server, a file with the "
            "same name in the server data folder is used (e.g. C:\SAP\log\BANK.xlsx -> data/BANK.xlsx)."
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
        default="XBLNR",
        description="SAP invoice matching field: XBLNR | ZUONR | BELNR",
        example="XBLNR" if not PYDANTIC_V2 else None,
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

    if PYDANTIC_V2:
        model_config = ConfigDict(
            json_schema_extra={
                "example": {
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
                }
            }
        )
    else:
        class Config:
            schema_extra = {
                "example": {
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
                }
            }


class PostingItemResult(BaseModel):
    row: int = Field(description="Row index")
    status: str = Field(description="Status: VALID, INVALID, SUCCESS, POSTED_NOT_CLEARED, ERROR, NOT_POSTED")
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
    match_by: Optional[str] = Field(None, description="Matching rule used (XBLNR, ZUONR, BELNR)")
    source: Optional[str] = Field(None, description="Source of items (file path or json)")
    sap_posting: Optional[str] = Field(None, description="Status of SAP RFC posting connection")
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
        500: {"model": ErrorResponse, "description": "Server or SAP error"},
    },
    tags=["Bank Posting"],
    summary="Post Bank Receipt with Clearing",
    description=(
        "Accepts either an Excel `file_path` on the server or a direct list of `items`. "
        "Validates line items against live SAP BSID open items, calls `Z_BANK_POST_CLEAR`, "
        "and updates the database tables with the resulting SAP document numbers."
    ),
)
async def bank_post(payload: BankPostingRequest):
    try:
        res = await run_in_threadpool(
            process_bank_posting,
            file_path=payload.file_path,
            items=payload.items,
            sheet=payload.sheet,
            dry_run=bool(payload.dry_run),
            match_by=payload.match_by,
            check_sap_open_items=payload.check_open_items,
            update_db=bool(payload.update_db),
        )
        return res
    except (ValueError, FileNotFoundError) as e:
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"status": "error", "message": str(e)})
    except Exception as e:
        logger.error(f"Bank posting failed: {e}")
        return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content={"status": "error", "message": str(e)})


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]")


@app.post(
    "/api/bank/upload",
    response_model=BankPostingResponse,
    responses={
        200: {"description": "Posting and validation results"},
        400: {"model": ErrorResponse, "description": "Validation or file error"},
        500: {"model": ErrorResponse, "description": "Server or SAP error"},
    },
    tags=["Bank Posting"],
    summary="Upload Excel, Validate, Post and Update DB",
    description=(
        "Upload the bank Excel file (17 columns, same as ZFI_BANK_PROGRAM). The file is saved in the "
        "server data folder, validated against live SAP open items, posted + cleared via "
        "`Z_BANK_POST_CLEAR` and the SAP document numbers are written to the database. "
        "Set `dry_run=true` to validate only."
    ),
)
async def bank_upload(
    file: UploadFile = File(..., description="Excel file (.xlsx)"),
    sheet: Optional[str] = Form(None, description="Sheet name (default: first sheet)"),
    dry_run: bool = Form(False, description="true = validate only, no SAP posting / DB update"),
    match_by: str = Form("XBLNR", description="XBLNR | ZUONR | BELNR"),
    update_db: bool = Form(True, description="Write SAP document numbers to active_bt3i / active_bs"),
):
    name = _SAFE_NAME.sub("_", Path(file.filename or "upload.xlsx").name)
    if not name.lower().endswith((".xlsx", ".xlsm")):
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST,
                            content={"status": "error", "message": "Only .xlsx / .xlsm files are supported"})
    folder = data_dir() / "uploads"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{datetime.now():%Y%m%d_%H%M%S}_{name}"
    target.write_bytes(await file.read())
    logger.info(f"[BANK UPLOAD] saved {target}")
    try:
        return await run_in_threadpool(
            process_bank_posting,
            file_path=str(target),
            sheet=sheet or None,
            dry_run=dry_run,
            match_by=match_by,
            update_db=update_db,
        )
    except (ValueError, FileNotFoundError) as e:
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"status": "error", "message": str(e)})
    except Exception as e:
        logger.error(f"Bank upload posting failed: {e}")
        return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content={"status": "error", "message": str(e)})


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bank Posting FastAPI Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host interface to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8001, help="Port to listen on (default: 8001)")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload on code changes")
    args = parser.parse_args()

    uvicorn.run("bank_posting.server:app", host=args.host, port=args.port, reload=args.reload, log_level="info")
