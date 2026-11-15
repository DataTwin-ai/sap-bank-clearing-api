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
RUN pip install -r requirements.txt

# Application code (config.ini is NOT baked in; it is mounted at runtime)
COPY main.py api.py logger_setup.py db_manager.py sap_odata_extractor.py ./
COPY bank_posting/ ./bank_posting/

# Run as non-root; log/ and data/ are bind-mounted from the host
RUN useradd --uid 1000 --create-home appuser \
    && mkdir -p /app/log /app/data \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# URL prefix when served behind a reverse proxy (e.g. /sap/bank); empty = served at /
ENV ROOT_PATH=""

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2 --proxy-headers --forwarded-allow-ips='*' --root-path \"$ROOT_PATH\""]
