# SAP Extraction API

Pulls SAP tables through the OData service `ZGENERIC_DATA_SRV` into PostgreSQL (`active_<table>`),
then syncs open AR invoices from `active_bsid` into `active_bt3i`.
It runs in two ways that share the same code:

| Mode | Entry point | Use |
|---|---|---|
| API | `uvicorn main:app` (FastAPI) | On-demand extraction, previews, Swagger at `/docs` |
| Cron | `deploy/run_cron.sh sync` → `cli.py` | Scheduled full sync |
| Delta cron | `deploy/run_cron.sh delta --cron` (`/etc/cron.d`, `DELTA_MODE=cron`) | Every minute: new / changed SAP rows → `active_<table>` → ingestion API |
| Delta worker | `cli.py worker` (systemd) | Same as the delta cron as a long-running loop; use one of the two |

A file lock (`data/pipeline.lock`) lets only one DB-writing run happen at a time.
If a second run starts, the API returns **409** and the cron run exits with **75**.

## Delta sync (every minute, only new records)

Tables listed in `[delta] tables` (e.g. `BSID`) are not reloaded in full. The worker:

1. Fetches the SAP table every `poll_seconds` (60). The OData service cannot filter, so it compares
   the full snapshot with `active_<table>` using the SAP DDIC key.
2. A new key is inserted with a new **passportid** (`PPSAP0126270000001`, sequence in
   `MASTEROBJECTSEQUENCINGV2`), `sap_status = NEW`, `ingestion_status = PENDING`.
   A changed row gets `CHANGED` + `PENDING`.
   A row missing from SAP for 2 polls (e.g. a cleared invoice) gets `REMOVED`. It is kept and not sent to ingestion.
3. When no ingestion run is active, it claims the `PENDING` rows (`IN_PROGRESS`).
   It copies them to `active_<table>_delta` (passportid + SAP columns) and calls the ingestion API:
   `POST /api/ingest/table {db, table_name: "active_bsid_delta", tenant_id, org_id, transaction_id: <run_id>}`.
4. It polls `GET /api/jobs/{job_id}` until the job ends.
   - Success → rows `DONE`.
   - Failure → back to `PENDING`, then `FAILED` after `max_retries`.
   - Only after this does the next run start. Rows that arrive in the meantime wait as `PENDING`.

Control columns added to `active_<table>`:

| Column | Meaning |
|---|---|
| `passportid` | Our ID for the SAP record; never changes |
| `sap_key_hash` / `row_hash` | Identity / change detection |
| `sap_status` | `ACTIVE`, `NEW`, `CHANGED`, `REMOVED` |
| `ingestion_status` | `PENDING`, `IN_PROGRESS`, `DONE`, `FAILED` (the queue) |
| `ingestion_run_id`, `retry_count`, `error_message`, `ingested_at` | Ingestion trail |
| `first_seen_at`, `last_seen_at`, `changed_at`, `removed_at`, `missing_count` | SAP trail |

Supporting tables:
- `sap_extraction_state`: last poll and counts per table.
- `sap_ingestion_run`: one row per ingestion run; `run_id` = `transaction_id` sent to the API.

On Windows, run `start_worker.bat` (keeps a console open; log in `log\worker.log`).
The ingestion service must accept `process_code`. If it doesn't, or doesn't answer, nothing is sent and rows stay `PENDING`.

Setup per table (one time). The current table is kept as `active_<table>_bak_<timestamp>`:
- Rows whose document is already in the ingestion target (`active_bt3h` / `active_bt3i`, same company code + document number, any theme) become `DONE` and are not ingested.
- All other SAP rows become `PENDING` and are ingested once, also rows that were already in the old `active_<table>`.
- Set `[ingestion] process_code_<table>` before running it: the target table is found from it.

```bash
python cli.py delta-init BSID
sudo cp deploy/sap-extraction-worker.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now sap-extraction-worker
python cli.py delta-status
```

Useful queries:

```sql
-- the queue
SELECT passportid, bukrs, belnr, kunnr, dmbtr, sap_status, ingestion_status, retry_count, error_message
FROM active_bsid WHERE ingestion_status <> 'DONE' ORDER BY first_seen_at;
-- open items only (what SAP BSID shows now)
SELECT * FROM active_bsid WHERE sap_status <> 'REMOVED';
-- recent ingestion runs
SELECT run_id, status, job_status, record_count, started_at, finished_at, error
FROM sap_ingestion_run ORDER BY started_at DESC LIMIT 20;
```

## Files

```
main.py                FastAPI app
cli.py                 cron / command line
pipeline.py            extract -> DB -> bt3i sync -> ingestion command, lock
delta.py               1-minute delta sync, passportid, sap_status
ingestion.py           ingestion queue -> Data Modelling & Ingestion API
worker.py              long-running loop (poll + ingestion) for systemd
sap_odata_extractor.py SAP OData client
db_manager.py          PostgreSQL load (active_<table>, de-duplication)
sync_bsid_to_bt3i.py   active_bsid -> active_bt3h
context.py             CTXSAP context-id sequencing
settings.py            config.ini + environment variables, paths
logger_setup.py        log/api.log, log/cron.log (rotated daily)
config.example.ini     template for config.ini
deploy/                systemd unit, cron wrapper, crontab example
```

## Configuration

1. `cp config.example.ini config.ini` and fill in the DB and SAP values. Never commit `config.ini`.
2. Optionally keep the secrets out of the file and set environment variables in `.env` instead
   (the systemd unit and the cron wrapper both load it):
   ```
   APP_ENV=production
   DB_PASSWORD=...
   SAP_PASSWORD=...
   API_KEY=<long random string>
   ```
   Environment variables always win over `config.ini`. The full list is in `config.example.ini`.
3. To keep `config.ini` somewhere else, set `SAP_EXTRACTION_CONFIG=/etc/sap-extraction/config.ini`.

Relative paths (`log/`, `data/`) are resolved against the project folder.
That keeps cron (which starts in `$HOME`) and systemd writing to the same place.

## Install on the server (Linux)

```bash
sudo useradd --system --create-home sapapp
sudo mkdir -p /opt/sap-extraction-api && sudo chown sapapp: /opt/sap-extraction-api
# copy this folder to /opt/sap-extraction-api, then as sapapp:
cd /opt/sap-extraction-api
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.ini config.ini && chmod 600 config.ini   # edit it
chmod +x deploy/run_cron.sh

.venv/bin/python cli.py check            # SAP + DB must both be "ok"
```

### API (systemd)

```bash
sudo cp deploy/sap-extraction-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sap-extraction-api
curl http://127.0.0.1:8002/health
```

The service listens on `127.0.0.1:8002` with one worker.
To expose it, put nginx in front and set `root_path` (`ROOT_PATH`) to the URL prefix, e.g. `/sap/extract`:

```nginx
location /sap/extract/ {
    proxy_pass http://127.0.0.1:8002/;
    proxy_read_timeout 1800s;   # full extractions can take minutes
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

### Cron

```bash
crontab -e      # as sapapp; paste a line from deploy/crontab.example
```

```
*/30 * * * * /opt/sap-extraction-api/deploy/run_cron.sh sync >> /opt/sap-extraction-api/log/cron.out 2>&1
```

- `log/cron.log` holds the detailed log and `log/cron.out` the JSON result of each run.
- Exit codes: `0` ok, `1` failed, `2` partial, `75` skipped because the previous run is still going.
- Cron uses the server timezone (EC2: UTC).

## API

Send header `X-API-Key: <key>` on every `/api/*` call when `API_KEY` is set.

| Method | Path | What it does |
|---|---|---|
| GET | `/health` | Liveness, no SAP/DB calls |
| GET | `/api/check` | SAP + DB connectivity |
| GET/POST | `/api/extract` | One table → `active_<table>` |
| GET/POST | `/api/sync-config` | All `[sap] tables` → DB → bt3i → ingestion (same as cron) |
| GET/POST | `/api/transform/bt3i` | `active_bsid` → `active_bt3i` |
| GET | `/api/tables` | `active_*` tables with row counts |
| GET | `/api/tables/{name}?limit=10` | Preview rows |
| GET | `/api/sap/tables?filter=BS` | Tables exposed by the OData service |
| GET | `/api/sap/metadata/{name}` | SAP DDIC columns |
| GET | `/api/delta/status` | Delta tables: status counts, last poll, recent runs |
| POST | `/api/delta/poll?table=BSID` | Poll SAP now |
| POST | `/api/delta/ingest` | Start an ingestion cycle in the background |
| POST | `/api/delta/retry?table=BSID` | `FAILED` rows → `PENDING` |

Examples:

```bash
curl -H "X-API-Key: $API_KEY" "http://127.0.0.1:8002/api/extract?table=BSID&sync_bt3i=true"

curl -X POST -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
     -d '{"tables": ["BSID", "KNA1"], "sync_bt3i": true}' \
     http://127.0.0.1:8002/api/sync-config
```

## CLI

```bash
python cli.py sync [--tables BSID,KNA1] [--rows N] [--mode replace|append] [--no-bt3i]
python cli.py extract BSID [--rows 100] [--csv] [--sync-bt3i]
python cli.py sync-bt3i [--company-code DAT1]
python cli.py check | list-tables | sap-tables --filter BS | sap-metadata BSID

python cli.py delta-init BSID [--all-done]   # one time per delta table
python cli.py worker                         # loop (systemd)
python cli.py delta [--cron]                 # one poll + one ingestion cycle (1-minute cron: --cron)
python cli.py delta-poll [BSID] | ingest [--no-wait] | delta-status | delta-retry BSID
```

Add `--env development` to any command to use another DB section and `-v` to see the log on screen.

## Changes from the Windows version (`C:\SAP`)

- `api.py` (Starlette) is now `main.py` (FastAPI). Routes are unchanged except `/api/bank/post`,
  which belongs to the separate bank-clearing service.
- `app.py` is now `pipeline.py` + `cli.py`. These were removed:
  - the SAP GUI extractor (Windows COM only)
  - the SFTP uploader (placeholder settings, never used)
  - `mock` / CSV fallback data
  - the hard-coded SAP password
- Removed the `C:\SAP`, `sap_extractor/.env` and current-directory path lookups.
  - CSVs go to `data/exports/` and are off by default.
  - Logs go to `log/` with daily rotation.
- Table names are validated before they go into OData filters.
- `/api/*` endpoints can be protected with an API key.
