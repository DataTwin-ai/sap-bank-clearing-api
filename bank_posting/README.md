# Bank Posting with Clearing (API)

Replaces the `ZFI_BANK_PROGRAM` F-30 BDC (Excel upload in SAP GUI) with an API.
Python reads the Excel / JSON, validates it against live SAP open items, and
calls RFC function `Z_BANK_POST_CLEAR` via SAP SOAP-RFC (`/sap/bc/soap/rfc`),
which posts the bank receipt and clears the customer invoice.

```
bank_posting/
├── posting.py        # Excel/JSON read, validation, open-item check, SAP call
├── document_link.py  # writes SAP document no. back to active_bt3i / active_bs
├── server.py         # standalone API server (port 8001)
├── __init__.py
└── sap_abap/         # SAP side: function module source + notes
```

Shared from the project root: `config.ini` (`[sap]` connection, `[bank_posting]`
settings), `logger_setup.py`, `sap_odata_extractor.py`.

## Run

| How | Command |
|---|---|
| Standalone server | `python bank_posting\server.py --port 8001` |
| Swagger UI | `http://localhost:8001/docs` |
| ReDoc | `http://localhost:8001/redoc` |
| Main API (already wired) | `python api.py` → same endpoint on port 8000 |
| CLI | `python -m bank_posting.posting "data/BANK EXCEL SHEET.xlsx" --dry-run` (from the project folder) |

## Request

`POST /api/bank/post`

```json
{
  "file_path": "data/BANK EXCEL SHEET.xlsx",
  "dry_run": true,
  "match_by": "XBLNR",
  "check_open_items": true
}
```

Or send rows directly instead of `file_path`:

```json
{
  "dry_run": false,
  "match_by": "XBLNR",
  "items": [{
    "doc_date": "2026-11-15", "posting_date": "2026-11-15",
    "company_code": "DAT1", "currency": "INR",
    "customer": "30000001", "bank_gl": "410000",
    "amount": 1858500.00, "invoice_ref": "0090000057",
    "utr": "UTR123456789", "item_text": "Bank receipt"
  }]
}
```

`file_path` must exist on the machine running the API.
`match_by`: how SAP finds the invoice – `XBLNR` (reference), `ZUONR` (assignment), `BELNR` (doc no).

## Response (per row)

| status | meaning |
|---|---|
| `VALID` / `INVALID` | dry_run result |
| `SUCCESS` | posted and invoice cleared (`document_no`, `clearing_doc`, `clearing_date`) |
| `POSTED_NOT_CLEARED` | document posted, clearing not confirmed |
| `ERROR` | SAP rejected the row |
| `NOT_POSTED` | `Z_BANK_POST_CLEAR` not available / SAP not reachable |

## DB update after posting

For every row with status `SUCCESS` / `POSTED_NOT_CLEARED`, `document_link.py`
sets `documentid = <SAP document_no>` (only where `documentid IS NULL`):

| Table | Match |
|---|---|
| `active_bt3i` (transactiontype = Invoice) | `documentnumber` = invoice_ref |
| `active_bs` (transactiontype = Bank) | `transactionref` = invoice_ref or utr |

Disable with `"update_db": false`. Result is returned in `db_update`.

## Status

- Dry run: working against live SAP (BSID open items via `ZGENERIC_DATA_SRV`).
- `Z_BANK_POST_CLEAR` is created and active in SAP (A4H/200, package `$TMP`);
  Python → SOAP → FM call verified with error cases.
- First real posting still to be done with a valid bank G/L → see [sap_abap/README.md](sap_abap/README.md).
