@echo off
rem SAP BSID delta worker (Windows): polls SAP every minute and sends new rows to ingestion.
rem Keep this window open. Log: log\worker.log   Stop: Ctrl+C
rem Before starting: the ingestion service (python api.py on port 8050) must be running.
title SAP Delta Worker
cd /d "%~dp0"
python cli.py -v worker
pause
