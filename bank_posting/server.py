"""
Standalone API server for bank posting.

    python bank_posting\\server.py --port 8001

POST /api/bank/post   -> see README.md for payload
GET  /health
"""

import argparse
import sys
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bank_posting.posting import logger, process_bank_posting  # noqa: E402


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "bank_posting"})


async def bank_post(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "message": "Request body must be JSON"}, status_code=400)

    try:
        res = await run_in_threadpool(
            process_bank_posting,
            file_path=body.get("file_path"),
            items=body.get("items"),
            sheet=body.get("sheet"),
            dry_run=bool(body.get("dry_run", False)),
            match_by=body.get("match_by"),
            check_sap_open_items=body.get("check_open_items"),
            update_db=bool(body.get("update_db", True)),
        )
        return JSONResponse(res)
    except (ValueError, FileNotFoundError) as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)
    except Exception as e:
        logger.error(f"Bank posting failed: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


app = Starlette(routes=[
    Route("/health", health, methods=["GET"]),
    Route("/api/bank/post", bank_post, methods=["POST"]),
])


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Bank posting API server")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8001)
    a = ap.parse_args()
    uvicorn.run(app, host=a.host, port=a.port, log_level="info")
