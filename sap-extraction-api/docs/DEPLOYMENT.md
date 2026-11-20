# sap-extraction-api — Deployment Guide

As of 2026-11-20

SAP customer items (BSID open, BSAD cleared) and billing documents (VBRK header, VBRP items) reach `active_bt3h` / `active_bt3i` through a queue: a worker polls SAP every 60 seconds, queues new and changed rows, and sends them to the Data Modelling & Ingestion API with a fixed target (process code, transaction type, pinned theme).

```
SAP BSID / BSAD / VBRK / VBRP ──(worker, every 60 s)──► active_bsid / active_bsad / active_vbrk / active_vbrp
                                                         (queue: PENDING → IN_PROGRESS → DONE / FAILED)
                                                   │  one ingestion run at a time, tables in config order
                                                   ▼
                       Ingestion API  POST /api/ingest/table  (process_code, transaction_type, theme_name)
                                                   ▼
                                  active_bt3h (headers)  /  active_bt3i (VBRP items)
```

| SAP table | Queue table | Target | Theme |
| --- | --- | --- | --- |
| BSID | `active_bsid` | `active_bt3h`, `processcode = 'BT3H'`, `transactiontype = 'Invoice'` | `SAP_BSID_INV` |
| BSAD | `active_bsad` | `active_bt3h`, `processcode = 'BT3H'`, `transactiontype = 'CUSTOMER_ACCOUNT'` | `SAP_BSAD_CA` |
| VBRK | `active_vbrk` | `active_bt3h`, `processcode = 'BT3H'`, `transactiontype = 'SalesInvoice'` (one row per billing document) | `SAP_VBRK_SI` |
| VBRP | `active_vbrp` | `active_bt3i`, `processcode = 'BT3I'`, `transactiontype = 'SalesInvoice'` (one row per billing item) | `SAP_VBRP_SI` |

A billing document's header (VBRK) and items (VBRP) have the same `documentnumber` (VBELN) and the same context ID (`grouptransactionref`). The header's `linkref` is the accounting document (BELNR), which is the `documentnumber` of the BSID / BSAD row of the same invoice.

## 1. Before you deploy

All four must be true, or the worker keeps rows PENDING and sends nothing.

| # | Prerequisite | How to check |
| --- | --- | --- |
| 1 | The ingestion service at `https://lv.datatwin.ai/data-model-ingestion` runs repo `DataTwin-ai/data_modelling-ingestion`, branch `feature/dual-source-api-incremental-export`: commits `4d98030` (caller-routed process / theme) and `40e2acc` (SNO from the theme as the row key, one context ID for a header in `active_bt3h` and its items in `active_bt3i`) | `curl -s https://lv.datatwin.ai/data-model-ingestion/openapi.json` lists `process_code`, `transaction_type`, `theme_name` in `TableIngestRequest` |
| 2 | Themes `SAP_BSID_INV`, `SAP_BSAD_CA`, `SAP_VBRK_SI`, `SAP_VBRP_SI` exist in the target DB | Run `deploy/sql/bt3h_sap_themes.sql` and `deploy/sql/sales_invoice_themes.sql` (already done on dev `dm00005`) |
| 3 | `active_bt3h` has the 5 extra columns | Run `deploy/sql/bt3h_columns.sql` (already done on dev `dm00005`) |
| 4 | The server can reach SAP OData and PostgreSQL (hosts and ports in `config.ini` `[sap]` and `[development]` / `[production]`), and the ingestion service can reach the same PostgreSQL | `install.sh` step 4 runs `cli.py check` |

The ingestion service reads the staging tables `active_bsid_delta` / `active_bsad_delta` from the DB named in the request (`dm00005`), so it must point at the same PostgreSQL server as `config.ini`.

## 2. Files to deploy

Target folder on the server: `/opt/sap-extraction-api` (the systemd units expect this path).

| File / folder | Purpose |
| --- | --- |
| `cli.py` | Command line + cron entry point (`worker`, `delta-poll`, `ingest`, `delta-status`, `check`, …) |
| `worker.py` | The 60-second loop: delta poll + background ingestion cycle |
| `delta.py` | Polls SAP, compares with the DB, maintains the PENDING queue, passportids |
| `ingestion.py` | Claims PENDING rows, builds the staging table, calls the ingestion API, marks DONE / FAILED |
| `main.py` | FastAPI app (port 8002, local only): health, status, manual triggers |
| `pipeline.py`, `sync_bsid_to_bt3i.py` | Full loads and the direct `active_bsid → active_bt3h` fallback |
| `sap_odata_extractor.py`, `db_manager.py`, `context.py`, `settings.py`, `logger_setup.py` | SAP OData client, DB access, ID sequences, settings, logging |
| `requirements.txt` | Python packages |
| `config.ini` | **Copied separately** (contains SAP and DB passwords) |
| `config.example.ini` | Template with every setting explained |
| `deploy/install.sh` | Server setup: venv, packages, permissions, connectivity check, systemd services |
| `deploy/sap-extraction-worker.service` | systemd unit for the worker (always on) |
| `deploy/sap-extraction-api.service` | systemd unit for the API |
| `deploy/run_cron.sh`, `deploy/crontab.example` | Cron wrapper and the optional / fallback cron lines |
| `deploy/sql/bt3h_sap_themes.sql`, `deploy/sql/sales_invoice_themes.sql`, `deploy/sql/bt3h_columns.sql` | One-time DB objects (section 1) |
| `docs/` | This guide and the earlier direct-sync guide |

Not deployed: `config.ini.bak_*`, `sync_bsid_to_bt3i.py.bak_*`, `log/`, `data/`, `start_worker.bat` (Windows only), `__pycache__/`.

## 3. Build the bundle (workstation)

```powershell
cd C:\sap-extraction-api
python deploy\build_bundle.py
# -> dist\sap-extraction-api-<YYYYMMDD-HHMM>.tar.gz  (config.ini is never inside)
```

## 4. First deployment (server)

```bash
# from the workstation
scp dist/sap-extraction-api-<stamp>.tar.gz config.ini <user>@<server>:/tmp/

# on the server
sudo mkdir -p /opt && cd /opt
sudo tar -xzf /tmp/sap-extraction-api-<stamp>.tar.gz          # creates /opt/sap-extraction-api
sudo install -m 600 /tmp/config.ini /opt/sap-extraction-api/config.ini
rm /tmp/config.ini
cd /opt/sap-extraction-api
sudo bash deploy/install.sh
```

`install.sh` creates the user `sapapp`, the virtualenv and `log/` + `data/`, runs `cli.py check` (SAP + DB), shows `delta-status`, installs both systemd services and starts them. It stops before starting anything if the connectivity check fails.

`config.ini` on the server: `current_env` picks the DB section (`development` = `dm00005` on the dev DB server). `[ingestion] base_url` must be the deployed ingestion URL. No local paths are needed: `log` and `data` are relative to the project folder.

## 5. Delta tables (once per database)

The queue tables must exist before the worker starts. On the dev DB `dm00005` all four are already initialised (2026-11-20: `active_bsid` 66 rows, `active_bsad` 54, `active_vbrk` 76, `active_vbrp` 105). On any other database:

```bash
cd /opt/sap-extraction-api
sudo -u sapapp .venv/bin/python cli.py delta-init BSID --all-done   # existing SAP rows = DONE (not re-sent)
sudo -u sapapp .venv/bin/python cli.py delta-init BSAD              # existing rows = PENDING (sent once)
sudo -u sapapp .venv/bin/python cli.py delta-init VBRK
sudo -u sapapp .venv/bin/python cli.py delta-init VBRP
sudo systemctl restart sap-extraction-worker
```

Use `--all-done` when those rows already exist in `active_bt3h`; leave it out to ingest the current SAP rows once.

## 6. Verify

```bash
curl -s http://127.0.0.1:8002/health                                   # API up
systemctl status sap-extraction-worker --no-pager                      # worker active
tail -f /opt/sap-extraction-api/log/worker.log                         # a poll line every 60 s
sudo -u sapapp /opt/sap-extraction-api/.venv/bin/python /opt/sap-extraction-api/cli.py delta-status
```

```sql
-- queue: PENDING should drain to DONE within a few minutes
SELECT ingestion_status, count(*) FROM active_bsid GROUP BY 1;
SELECT ingestion_status, count(*) FROM active_bsad GROUP BY 1;

-- last ingestion runs
SELECT run_id, sap_table, status, record_count, left(error, 120), started_at
FROM sap_ingestion_run ORDER BY started_at DESC LIMIT 10;

-- rows that arrived in active_bt3h / active_bt3i
SELECT themename, transactiontype, count(*), max(modifieddatetime)
FROM active_bt3h WHERE themename IN ('SAP_BSID_INV', 'SAP_BSAD_CA', 'SAP_VBRK_SI') GROUP BY 1, 2;
SELECT themename, transactiontype, count(*), max(modifieddatetime)
FROM active_bt3i WHERE themename = 'SAP_VBRP_SI' GROUP BY 1, 2;

-- one billing document: header + items with the same context ID
SELECT 'header' AS part, h.documentnumber, h.sno, NULL AS itemcode, h.totalbasevalue AS value, h.grouptransactionref
FROM active_bt3h h WHERE h.themename = 'SAP_VBRK_SI' AND h.documentnumber = '0090000050'
UNION ALL
SELECT 'item', i.documentnumber, i.sno, i.itemcode, i.basevalue, i.grouptransactionref
FROM active_bt3i i WHERE i.themename = 'SAP_VBRP_SI' AND i.documentnumber = '0090000050';
```

End-to-end test: post an invoice in SAP. Within about a minute it is PENDING in `active_bsid`, and a few minutes later it is in `active_bt3h` with a `CTXSAP…` context ID.

## 7. Updating

```bash
# workstation: python deploy\build_bundle.py ; scp the new .tar.gz to /tmp
cd /opt && sudo tar -xzf /tmp/sap-extraction-api-<stamp>.tar.gz   # config.ini, log/, data/ are kept
cd /opt/sap-extraction-api && sudo bash deploy/install.sh         # reinstalls packages, restarts services
```

The worker can be restarted at any time: an unfinished ingestion run is picked up again, rows are neither lost nor sent twice.

## 8. Operations

| Task | Command |
| --- | --- |
| Queue counts, last poll, recent runs | `cli.py delta-status` |
| Re-queue rows that failed 3 times | `cli.py delta-retry BSID` (or `BSAD`) |
| Poll SAP now (no ingestion) | `cli.py delta-poll BSID BSAD` |
| Run one ingestion cycle now | `cli.py ingest` |
| Worker logs | `log/worker.log`, `journalctl -u sap-extraction-worker -f` |
| API logs | `log/api.log`, `journalctl -u sap-extraction-api -f` |

Run every `cli.py` command as `sudo -u sapapp /opt/sap-extraction-api/.venv/bin/python /opt/sap-extraction-api/cli.py …`.

Queue states: **PENDING** (waiting) → **IN_PROGRESS** (sent) → **DONE**. A failed run puts its rows back to PENDING; after 3 failures they are **FAILED** until `delta-retry`. Rows that disappear from SAP for 2 polls are **REMOVED** and never sent.

## 9. Rollback

```bash
sudo systemctl stop sap-extraction-worker sap-extraction-api
cd /opt && sudo tar -xzf /tmp/sap-extraction-api-<previous-stamp>.tar.gz
cd /opt/sap-extraction-api && sudo bash deploy/install.sh
```

Stopping the worker is always safe: rows stay PENDING and are sent when it starts again.

## 10. Adding another SAP table

No code change:

1. `cli.py delta-init <TABLE>` (add `--all-done` to skip the current rows).
2. `config.ini`: add the table to `[delta] tables` (an item table after its header table), and add `process_code_<table>`, `transaction_type_<table>`, `theme_name_<table>`, `columns_<table>` under `[ingestion]`. `columns_<table>` accepts `column as name` to rename a column.
3. Create the theme in `theme_mapping` (copy `deploy/sql/sales_invoice_themes.sql`). Map `SNO` to the source line number (or the constant `1` for a header table): it is part of the row key, so a changed SAP row updates its stored row.
4. `sudo systemctl restart sap-extraction-worker`.

## 11. Fallback while the ingestion API is not ready

If prerequisite 1 is not met yet, `active_bt3h` can be fed directly from `active_bsid` (BSID only, theme `IT_Invoice`). Enable the `*/5 … sync-bt3i` line in `deploy/crontab.example` (`sudo -u sapapp crontab -e`); details in `docs/BT3H_SYNC_DEPLOYMENT.md`. Remove that cron line as soon as the worker ingests BSID, otherwise the same invoice appears twice in `active_bt3h`.

## 12. Known limitations

- BSAD context IDs are not yet linked to the context ID the direct sync gave the same invoice (the older rows have no financial year). This goes away once the direct sync is switched off.
- `documentnumber` holds the SAP document number (BELNR); the invoice reference (XBLNR) is in `documentref`, because XBLNR is often empty or repeated. The AR Inbox query on `active_bt3h` should read `documentref` for the invoice number and `totalbasevalue` / `totaldocumentvalue` for amounts.
- The worker polls each whole table (BSID, BSAD, VBRK, VBRP) every minute (the OData service has no delta filter). This is fine at the current size (tens to hundreds of rows); large tables need the planned watermark pull.
- VBRK billing documents (`SalesInvoice`) and the BSID / BSAD rows of the same invoice have different context IDs. They are linked by header `linkref` (BELNR) = BSID `documentnumber`.
- The context ID is reused only when the header row was ingested first. If a VBRK run fails while the VBRP run of the same cycle succeeds, those items get their own context ID.
