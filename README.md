# SAP Bank Posting with Clearing API

FastAPI service that posts bank receipts in SAP S/4HANA **and clears the matching
customer invoice in one step**, then writes the SAP document number back to the
DataTwin PostgreSQL tables `active_bt3i` / `active_bs`.

```
Excel / JSON items ──► POST /api/bank/post
                         │
                         ├─ validate rows + match open items (SAP BSID via OData ZGENERIC_DATA_SRV)
                         ├─ post + clear in SAP   (RFC Z_BANK_POST_CLEAR via /sap/bc/soap/rfc)
                         └─ update active_bt3i / active_bs with document number (PostgreSQL)
```

## Repository layout

| Path | Purpose |
|---|---|
| `main.py` | Application entry point (`uvicorn main:app`, port 8000) |
| `api.py` | Compatibility entry point (`uvicorn api:app`) |
| `bank_posting/server.py` | FastAPI app, request/response schemas, endpoints |
| `bank_posting/posting.py` | Excel/JSON parsing, SAP validation, RFC posting |
| `bank_posting/document_link.py` | Writes SAP document numbers back to the DB |
| `bank_posting/sap_abap/` | ABAP source of `Z_BANK_POST_CLEAR` (install in SAP) |
| `db_manager.py` | PostgreSQL engine/credentials from `config.ini` or `DB_*` env vars |
| `sap_odata_extractor.py` | SAP OData client (reads `[sap]` from `config.ini` or `SAP_*` env vars) |
| `logger_setup.py` | Console + timestamped file logging into `log/` |
| `config.example.ini` | Template for `config.ini` (never commit the real one) |
| `Dockerfile`, `.dockerignore` | Container image |
| `buildspec.yml` | AWS CodeBuild: package source → S3 → deploy to EC2 via SSM |
| `deploy/deploy.sh` | Runs on EC2 (via SSM): download bundle, `docker build`, restart container |
| `deploy/iam/` | IAM policies for the CodeBuild role and the EC2 instance role |
| `deploy/nginx/sap-bank.conf` | nginx location for `https://demo.datatwin.ai/sap/bank` |

More detail:
[module usage](bank_posting/README.md) ·
[process guide](bank_posting/BANK_POSTING_PROCESS.md) ·
[clearing & DB update](bank_posting/CLEARING_AND_DB_UPDATE.md) ·
[SAP function module](bank_posting/sap_abap/)

## API

| Method | Path | Description |
|---|---|---|
| GET | `/` | Redirects to `/docs` |
| GET | `/health` | Liveness check → `{"status":"ok","service":"bank_posting"}` |
| POST | `/api/bank/post` | Validate / post / clear bank receipts |
| GET | `/docs`, `/redoc` | Swagger UI / ReDoc |

Request body for `POST /api/bank/post` (send **either** `items` **or** `file_path`):

| Field | Default | Meaning |
|---|---|---|
| `items` | – | List of posting rows (`doc_date`, `posting_date`, `company_code`, `currency`, `customer`, `bank_gl`, `amount`, `invoice_ref`, `utr`, `item_text`, …) |
| `file_path` | – | Excel file on the server (17 columns as `ZFI_BANK_PROGRAM`); in Docker use `/app/data/<file>.xlsx` |
| `sheet` | first sheet | Excel sheet name |
| `dry_run` | `false` | Validate against SAP only, do not post |
| `match_by` | `XBLNR` | Invoice match field: `XBLNR` \| `ZUONR` \| `BELNR` |
| `check_open_items` | from config | Validate against live SAP BSID before posting |
| `update_db` | `true` | Write document numbers to `active_bt3i` / `active_bs` |

Example (dry run):

```bash
curl -X POST http://localhost:8000/api/bank/post \
  -H "Content-Type: application/json" \
  -d '{"dry_run": true, "items": [{"company_code":"DAT1","customer":"30000001","bank_gl":"410000",
       "amount":1858500.0,"currency":"INR","invoice_ref":"0090000057","utr":"UTR123456789",
       "doc_date":"2026-11-15","posting_date":"2026-11-15"}]}'
```

Row statuses in the response: `VALID`, `INVALID`, `SUCCESS`, `POSTED_NOT_CLEARED`, `ERROR`, `NOT_POSTED`.

### Bank vs invoice files

`file_type` selects the flow; when omitted it is detected from the Excel header.

| `file_type` | Excel layout | SAP checks | DB update (`dry_run: false`) |
|---|---|---|---|
| `bank` | F-30, 17 columns (`NEWBS_2`, `NEWKO_2`, …) | format, open invoice in BSID (company, customer, reference, currency, amount); then post + clear via `Z_BANK_POST_CLEAR` | `active_bt3i` + `active_bs` `documentid` = new SAP document |
| `invoice` | FB70, 11 columns (`BUSCS`, `ACCNT`, `HKONT`, `ZUONR`, …) | format, customer in KNA1 (not blocked / deleted) and KNB1 for `company_code`, invoice found in BSID (open) or BSAD (cleared) by reference; currency and amount must match | `active_bt3i.documentid` = SAP invoice document number |

With `tenant_id` + `org_id`, `file_path` is read from S3:
`s3://dt-<tenant>-<env>/<org_id>/<file_path without drive>`.

```json
{ "file_type": "bank",    "file_path": "SAP/BANK EXCEL SHEET.xlsx", "sheet": "F-30 Upload",
  "dry_run": true, "match_by": "XBLNR", "tenant_id": "DM00005", "org_id": "OM0000000001" }

{ "file_type": "invoice", "file_path": "SAP/Invoice Excel.xlsx",    "sheet": "FB70_Upload",
  "company_code": "DAT1", "dry_run": true, "tenant_id": "DM00005", "org_id": "OM0000000001" }
```

## Configuration

All settings live in `config.ini` at the project root (copy `config.example.ini`):

| Section | Used for |
|---|---|
| `[environment]` | `current_env` (`development` / `production`), log settings |
| `[development]` / `[production]` | PostgreSQL host, port, db, user, password |
| `[defaults]` | `tenant_id`, `org_id` |
| `[sap]` | SAP host/port/client/user/password, OData service, TLS, timeouts |
| `[bank_posting]` | RFC name, default doc type, match field, open-item check |

SAP settings can also be given as environment variables (`SAP_HOST`, `SAP_PORT`,
`SAP_CLIENT`, `SAP_USER`, `SAP_PASSWORD`, …), which take precedence over `config.ini`.
Database settings work the same way: `APP_ENV`, `DB_HOST`, `DB_PORT`, `DB_NAME`,
`DB_USER`, `DB_PASSWORD`. If `DB_HOST` and `DB_PASSWORD` are set, `config.ini` is optional.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp config.example.ini config.ini                    # fill in DB and SAP credentials
uvicorn main:app --reload --port 8000               # or: python main.py
```

Standalone module server (port 8001): `python bank_posting/server.py --port 8001`.

## Run with Docker

```bash
docker build -t sap-bank-clearing-api .
docker run -d --name sap-bank-clearing-api -p 8000:8000 \
  -v "$PWD/config.ini:/app/config.ini:ro" \
  -v "$PWD/log:/app/log" \
  -v "$PWD/data:/app/data" \
  sap-bank-clearing-api
curl http://localhost:8000/health
```

`config.ini` is never baked into the image; it is mounted at runtime.

## Deployment (AWS, us-east-1)

No container registry is used; the image is built on the EC2 host.

```
GitHub main ──► CodeBuild sap-bank-clearing-api
                  ├─ py_compile check
                  ├─ tar source ──► s3://codepipeline-us-east-1-922151790333/sap-bank-clearing-api/releases/<commit>-<build>.tar.gz
                  └─ SSM Run Command ──► EC2 i-019cd9689708261b3 (development)
                                           ├─ download + extract bundle
                                           ├─ docker build -t sap-bank-clearing-api:<release>
                                           ├─ replace container on 127.0.0.1:8000 (ROOT_PATH=/sap/bank)
                                           └─ /health check, rollback to previous image on failure
```

Public URL: **https://demo.datatwin.ai/sap/bank/** (Swagger: `/sap/bank/docs`,
health: `/sap/bank/health`, posting: `POST /sap/bank/api/bank/post`). nginx on the
host proxies `/sap/bank/` to the container, which listens only on `127.0.0.1:8000`.

On the host: container `sap-bank-clearing-api`, files under `/opt/sap-bank-clearing-api/`
(`config.ini`, `log/`, `data/`, `releases/` — last 5 kept).

### One-time setup

1. **CodeBuild role** — allow SSM Run Command on the instance:
   ```bash
   aws iam put-role-policy --role-name codebuild-sap-bank-clearing-api-service-role \
     --policy-name sap-bank-clearing-api-deploy --policy-document file://deploy/iam/codebuild-policy.json
   ```
   (S3 upload to `codepipeline-us-east-1-*` is already in its base policy.)
2. **Docker on the instance** (Amazon Linux 2023):
   ```bash
   aws ssm send-command --region us-east-1 --instance-ids i-019cd9689708261b3 \
     --document-name AWS-RunShellScript \
     --parameters 'commands=["dnf install -y docker","systemctl enable --now docker"]'
   ```
3. **config.ini** — either place it on the host at `/opt/sap-bank-clearing-api/config.ini`,
   or store it in Parameter Store and grant the instance role read access:
   ```bash
   aws ssm put-parameter --region us-east-1 --name /sap-bank-clearing-api/config.ini \
     --type SecureString --value file://config.ini --overwrite
   aws iam put-role-policy --role-name DataTwinEC2Role \
     --policy-name sap-bank-clearing-api-config --policy-document file://deploy/iam/ec2-policy.json
   ```
   (The instance role already has S3 read via `AmazonS3FullAccess`.)
4. **nginx** — install [deploy/nginx/sap-bank.conf](deploy/nginx/sap-bank.conf) as
   `/etc/nginx/snippets/sap-bank.conf`, add `include /etc/nginx/snippets/sap-bank.conf;`
   inside the `demo.datatwin.ai` 443 server block, then `sudo nginx -t && sudo systemctl reload nginx`.
   Port 8000 does not need to be open in the security group.

### Operations

- Deploy: push to `main` (with a webhook) or *Start build* in CodeBuild.
- Build without deploying: set `DEPLOY_ENABLED=false` on the build.
- Roll back: re-run an older build, or on the host
  `docker rm -f sap-bank-clearing-api` and re-run with an older `sap-bank-clearing-api:<release>` image.
- Logs: `sudo docker logs -f sap-bank-clearing-api` or `/opt/sap-bank-clearing-api/log/`.
