"""
Main entry point for the SAP Bank Clearing FastAPI Application.

Usage:
    uvicorn main:app --reload --port 8000
    python main.py --port 8000
"""

import argparse
import sys
from pathlib import Path

import uvicorn

# Ensure the repository root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bank_posting.server import app

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SAP Bank Posting & Clearing FastAPI Application")
    parser.add_argument("--host", default="0.0.0.0", help="Host interface (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="Port to run on (default: 8000)")
    parser.add_argument("--reload", action="store_true", help="Enable reload on changes")
    args = parser.parse_args()

    uvicorn.run("main:app", host=args.host, port=args.port, reload=args.reload, log_level="info")
