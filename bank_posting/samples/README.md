# Sample payloads

All samples have `"dry_run": true`: they run every check (invoice posting also runs an SAP
simulation) and post nothing. Set `"dry_run": false` only when you want a real SAP document.

| File | Endpoint | What it does |
|---|---|---|
| [invoice_post.json](invoice_post.json) | `POST /api/invoice/post` | New customer invoice in SAP, number → `active_bt3i.documentid` |
| [invoice_link_existing.json](invoice_link_existing.json) | `POST /api/bank/post` | Invoice already in SAP: find it, number → `active_bt3i.documentid` |
| [clearing_minimal.json](clearing_minimal.json) | `POST /api/clearing/post` | Clear a connection; invoices, amount, UTR come from `active_cnx` |
| [clearing_explicit.json](clearing_explicit.json) | `POST /api/clearing/post` | Clear a connection with the invoices given in the request |
| [bank_file.json](bank_file.json) | `POST /api/bank/post` | Bank Excel from the tenant S3 bucket (one invoice per row) |

Field reference: [../CONNECTION_CLEARING_API.md](../CONNECTION_CLEARING_API.md).

## Run

Set the key once per shell (never put it in a file in git):

```bash
export API_KEY='<api key>'
```

| Where | Base URL |
|---|---|
| Your PC, local server | `http://127.0.0.1:8000` (LAN: `http://192.168.29.8:8000`) |
| EC2 host shell (container behind nginx) | `http://127.0.0.1:8000` |
| Anywhere, through nginx | `https://demo.datatwin.ai/sap/bank` |

```bash
BASE=http://127.0.0.1:8000
cd <folder with the samples>

curl -s -X POST "$BASE/api/invoice/post"  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" -d @invoice_post.json      | python3 -m json.tool
curl -s -X POST "$BASE/api/clearing/post" -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" -d @clearing_minimal.json  | python3 -m json.tool
curl -s -X POST "$BASE/api/clearing/post" -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" -d @clearing_explicit.json | python3 -m json.tool
curl -s -X POST "$BASE/api/bank/post"     -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" -d @invoice_link_existing.json | python3 -m json.tool
curl -s -X POST "$BASE/api/bank/post"     -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" -d @bank_file.json         | python3 -m json.tool
```

Every response has a `batch_id` (`INP…` invoice posting, `CLR…` clearing, `BNK…` / `INV…` bank and invoice files).
Use it to find the request in the log.

## On the EC2 server (i-019cd9689708261b3)

| What | Path |
|---|---|
| Config (SAP / DB passwords, API keys) | `/opt/sap-bank-clearing-api/config.ini` |
| Logs | `/opt/sap-bank-clearing-api/log/log_YYYYMMDD.txt` (one file per day, server time = UTC, kept 30 days) |
| Logs before this version | `/opt/sap-bank-clearing-api/log/log_YYYYMMDD_HHMMSS.txt` (one file per process start) |
| Samples of the running release | `/opt/sap-bank-clearing-api/releases/<release>/bank_posting/samples/` |
| Container | `sap-bank-clearing-api` (inside: `/app`, logs `/app/log`) |

```bash
# which release is running, and its samples
REL=$(ls -1t /opt/sap-bank-clearing-api/releases | head -1); echo "$REL"
cd /opt/sap-bank-clearing-api/releases/$REL/bank_posting/samples

# is the API up?
curl -s http://127.0.0.1:8000/health
sudo docker ps --filter name=sap-bank-clearing-api

# today's log, live
sudo tail -f /opt/sap-bank-clearing-api/log/log_$(date -u +%Y%m%d).txt

# one request, by batch_id from the response
sudo grep 'CLR20261123191449-5826' /opt/sap-bank-clearing-api/log/log_*.txt

# all invoice postings / clearings / SAP calls / errors today
LOG=/opt/sap-bank-clearing-api/log/log_$(date -u +%Y%m%d).txt
sudo grep -E '\[INVOICE POST\]|\[CLEARING\]' $LOG
sudo grep '\[SAP RFC\]' $LOG
sudo grep -E '\[(ERROR|WARNING)\]' $LOG
sudo grep '\[HTTP\]' $LOG            # one line per request: path, status, duration, client

# container output (same lines + uvicorn start-up / crash messages)
sudo docker logs --tail 200 sap-bank-clearing-api
```

## What the log shows for one clearing

```
[HTTP] POST /api/clearing/post -> 200 in 4.1s (client 10.0.1.25)
[CLEARING] CLR…: start, org OM0000000001, connection DTTEST/20261123/150, dry_run=False, …
[CLEARING] CLR…: connection rows=3 (invoice 2, bank 1), clearingdocid exists=True, already cleared by=-
[CLEARING] CLR…: from connection ['invoices', 'sap_document_numbers', 'amount', 'utr']: … DTTEST-INV-01->1800000003 100.00 …
[CLEARING] CLR…: SAP check 1800000003: OPEN - Open in SAP, amount matches
[CLEARING] CLR…: checks VALID - Validation passed
[CLEARING] CLR…: POSTING in SAP (Z_BANK_POST_CLEAR_MULTI) amount=150.00 INR invoices=['1800000003', '1800000004']
[SAP RFC] Z_BANK_POST_CLEAR_MULTI: HTTP 200 in 2.3s
[CLEARING] CLR… SAP: SUCCESS document=1400000012/2026 clearing=1400000012 cleared=2 - …
[CLEARING] active_cnx.clearingdocid = 1400000012 on 3 row(s) of OM0000000001 / DTTEST/20261123/150
[CLEARING] CLR…: done in 4.0s, result=SUCCESS, clearing_doc=1400000012, db={…}
```

(Document numbers above are examples.)
