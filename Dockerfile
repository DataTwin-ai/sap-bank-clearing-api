# syntax=docker/dockerfile:1
# SAP Bank Posting & Clearing API - production image
FROM public.ecr.aws/docker/library/python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first so this layer is cached between code-only changes
COPY requirements.txt .
COPY sap-extraction-api/requirements.txt ./sap_sync_requirements.txt
RUN pip install -r requirements.txt -r sap_sync_requirements.txt

# Application code (config.ini is NOT baked in; it is mounted at runtime)
COPY main.py api.py logger_setup.py db_manager.py sap_odata_extractor.py sap_sync_scheduler.py ./
COPY bank_posting/ ./bank_posting/
# 1-minute SAP sync (run by sap_sync_scheduler.py as a separate process, see that file)
COPY sap-extraction-api/*.py sap-extraction-api/container.ini ./sap_sync/

# Run as non-root; log/ and data/ are bind-mounted from the host
RUN useradd --uid 1000 --create-home appuser \
    && mkdir -p /app/log /app/data \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# URL prefix when served behind a reverse proxy (e.g. /sap/bank); empty = served at /
ENV ROOT_PATH=""

# SAP sync: off unless the container is started with SAP_SYNC_ENABLED=true (deploy/deploy.sh does).
# It reads container.ini first, then the mounted config.ini (DB / SAP connection and passwords).
ENV SAP_SYNC_ENABLED="false" \
    SAP_SYNC_DIR="/app/sap_sync" \
    SAP_EXTRACTION_BASE_CONFIG="/app/sap_sync/container.ini" \
    SAP_EXTRACTION_CONFIG="/app/config.ini"

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2 --proxy-headers --forwarded-allow-ips='*' --root-path \"$ROOT_PATH\""]
