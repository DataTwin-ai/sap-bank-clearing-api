# Bank Posting with Clearing – Process Guide

This guide covers how a bank receipt is posted in SAP and the matching customer
invoice is cleared through the DataTwin API. It replaces the manual SAP GUI
program `ZFI_BANK_PROGRAM` (F-30 BDC from an Excel file).

---

## 1. What it does

For each bank receipt (one Excel row, or one JSON item):

1. Reads and cleans the data, including dates, amounts and customer numbers.
2. Validates the data.
3. Checks that the invoice is an **open item** in live SAP.
4. Posts the receipt in SAP and **clears the invoice** in the same step:
   - Dr Bank G/L (posting key 40)
   - Cr Customer (the open invoice is cleared)
5. Returns the **posting document number** and the **clearing document number**.
6. Writes the SAP document number back to `active_bt3i` / `active_bs` (`documentid`).

---

## 2. Architecture

```
 Caller (DataTwin / Postman / script)
        │  POST /api/bank/post  (file_path or items[])
        ▼
 Python  bank_posting/posting.py
   ├─ read Excel / JSON  → normalise
   ├─ validate
   ├─ open-item check ─────────► SAP OData ZGENERIC_DATA_SRV (read BSID)
   ├─ post each valid row ─────► SAP SOAP-RFC /sap/bc/soap/rfc
   │                               └─ FM Z_BANK_POST_CLEAR
   │                                    └─ POSTING_INTERFACE_CLEARING (FB05)
   └─ document_link.py ────────► PostgreSQL active_bt3i / active_bs (documentid)
        │
        ▼
 JSON response (one result per row)
```

| Component | Location |
|---|---|
| Main logic | [posting.py](posting.py) |
| DB write-back | [document_link.py](document_link.py) |
| Standalone API server | [server.py](server.py) (port 8001) |
| Same endpoint in main API | [../api.py](../api.py) (port 8000) |
| SAP function module source | [sap_abap/Z_BANK_POST_CLEAR.abap](sap_abap/Z_BANK_POST_CLEAR.abap) |
| Settings | [../config.ini](../config.ini) → `[sap]`, `[bank_posting]` |

### SAP objects (system A4H, client 200)

| Object | Name | Package |
|---|---|---|
| Function group | `ZBANK_POST` | `$TMP` |
| Function module (RFC-enabled) | `Z_BANK_POST_CLEAR` | `$TMP` |

Why SOAP-RFC and not OData: the standard posting/clearing OData services
(`FAR_PAYMENT_POST_SRV`, `FAR_MANUAL_CLEARING_SRV`) return "No System Alias" in
this system. The API user also cannot create SEGW projects. An RFC-enabled function
module behind SAP's built-in `/sap/bc/soap/rfc` endpoint needs no service registration.

---

## 3. How to run

```bash
# from C:\SAP
python bank_posting\server.py --port 8001      # standalone
python api.py                                  # or: main API on port 8000
python -m bank_posting.posting "C:\SAP\log\BANK EXCEL SHEET.xlsx" --dry-run   # CLI
```

**Always run with `"dry_run": true` first.** Dry run validates the data and checks open items without posting anything.

---

## 4. Input

### 4.1 Request

`POST /api/bank/post`

| Field | Required | Default | Meaning |
|---|---|---|---|
| `file_path` | one of | – | Excel path **on the API server machine** |
| `items` | one of | – | List of receipts as JSON (instead of a file) |
| `sheet` | – | first sheet | Excel sheet name |
| `dry_run` | – | `false` | `true` = validate only, no posting |
| `match_by` | – | `XBLNR` | How SAP finds the invoice: `XBLNR` (reference), `ZUONR` (assignment), `BELNR` (doc no) |
| `check_open_items` | – | `true` | Check the invoice in live SAP before posting |
| `update_db` | – | `true` | Write the document number back to PostgreSQL |

### 4.2 JSON item

```json
{
  "doc_date": "2026-11-15",
  "posting_date": "2026-11-15",
  "company_code": "DAT1",
  "currency": "INR",
  "customer": "30000001",
  "bank_gl": "24500000",
  "amount": 1858500.00,
  "invoice_ref": "0090000057",
  "utr": "DTUTR2026111501",
  "item_text": "Bank receipt",
  "doc_type": "DZ",
  "period": "",
  "value_date": null
}
```

| Field | Notes |
|---|---|
| `customer`, `bank_gl` | Numeric values are zero-padded automatically (`30000001` → `0030000001`) |
| `amount` | Number. Strings like `8,250.50` / `8250,50` / `8.250,50` are also accepted. |
| `invoice_ref` | Value searched in the `match_by` field of the open item |
| `utr` | Optional. Used as the header reference and for the duplicate check. **Send it** to avoid posting twice. |
| `doc_type` | Default `DZ` |
| `value_date` | Send only if the bank G/L shows the value-date field. Otherwise SAP fails with `BSEG-VALUT does not exist`. |

### 4.3 Excel layout

The layout is the same as the old `ZFI_BANK_PROGRAM`. Row 1 holds the headers, and data starts at row 2.

| Header | Used as |
|---|---|
| BLDAT | document date |
| BLART | document type |
| BUKRS | company code |
| BUDAT | posting date |
| MONAT | period |
| WAERS | currency |
| NEWBS_1 / NEWKO_1 / WRBTR_1 | customer key / **customer** / customer amount |
| ZFBDT | (ignored) |
| ZUONR_1 | **invoice reference** |
| SGTXT_1 | (ignored) |
| NEWBS_2 / NEWKO_2 / WRBTR_2 | must be 40 / **bank G/L** / **bank amount** |
| ZUONR_2 / SGTXT_2 | – / item text |

Rows with only notes or text (for example a "description" row under the data) come back as `INVALID`. Remove them from the sheet.

---

## 5. Validation

### 5.1 Python (before SAP is called)

| Check | Error example |
|---|---|
| Dates valid | `Invalid/missing document date: 'posting date bank'` |
| Company code (4 chars), currency (3 letters) | `Invalid company code: 'STATIC'` |
| Customer, bank G/L, invoice ref present | `Invoice reference is missing - nothing to clear` |
| Amount > 0 and parseable | `Invalid bank amount: 'Bank Amount'` |
| Customer amount = bank amount (Excel) | `Customer amount 8250.50 != bank amount ... (Dr must equal Cr)` |
| Bank posting key = 40 | `Bank line posting key must be 40` |
| No duplicate row in the same batch | `Duplicate row in this batch ...` |
| **Open item exists in SAP** | `No open item in SAP for company 2024, customer CUST02 with ZUONR = 'INV-90001'` |
| Currency matches the open item | `Currency mismatch: bank EUR vs open item INR` |
| **Amount = open amount** | `Amount mismatch: bank 1000000.00 vs open item(s) 188800.00` |

Only **exact amount** matches are posted. Partial payments and residual items are not handled yet.

### 5.2 SAP (inside `Z_BANK_POST_CLEAR`)

1. **Duplicate UTR:** if a non-reversed document with the same doc type and reference (UTR) already exists, the row is rejected.
2. **Open item:** the open item must still exist in `BSID_VIEW`.
3. **FI checks:** standard SAP FB05 checks (period open, account valid, balance zero, field status, authorisation).
4. **Clearing confirmation:** after posting, the FM reads `BSAD_VIEW` to confirm that the invoice is cleared by the new document.

---

## 6. SAP posting logic (`Z_BANK_POST_CLEAR`)

```
POSTING_INTERFACE_START    (call transaction, mode N, synchronous update)
POSTING_INTERFACE_CLEARING (procedure EINGZAHL = incoming payment, tcode FB05)
   header : BLDAT, BUDAT, BLART, BUKRS, WAERS, XBLNR (=UTR or invoice ref), MONAT
   line   : key 40, bank G/L, amount, assignment, text [, value date]
   clear  : account type D, customer, company code, select by XBLNR|ZUONR|BELNR = invoice ref
POSTING_INTERFACE_END
→ read posted document number, then the clearing document from BSAD_VIEW
```

Returned status:

| EV_STATUS | API status | Meaning |
|---|---|---|
| `S` | `SUCCESS` | Posted and invoice cleared |
| `W` | `POSTED_NOT_CLEARED` | Document posted but clearing not found. Check FBL5N. |
| `E` | `ERROR` | Nothing posted. Reason in `message`. |

---

## 7. Output

```json
{
  "status": "completed",
  "batch_id": "BNK20261115131441-28F7",
  "dry_run": false,
  "sap_posting": "ok",
  "total": 1,
  "summary": {"SUCCESS": 1},
  "db_update": {"invoices_updated": 0, "bank_updated": 0},
  "results": [{
    "row": 1,
    "status": "SUCCESS",
    "company_code": "DAT1",
    "customer": "0030000001",
    "invoice_ref": "0090000057",
    "utr": "DTUTR2026111501",
    "amount": 1858500.0,
    "currency": "INR",
    "document_no": "1900000003",
    "fiscal_year": "2026",
    "clearing_doc": "1900000003",
    "clearing_date": "2026-11-15",
    "message": "Document 1900000003 posted and cleared XBLNR 0090000057"
  }]
}
```

| status | Meaning |
|---|---|
| `VALID` / `INVALID` | Dry-run result |
| `SUCCESS` | Posted and cleared |
| `POSTED_NOT_CLEARED` | Posted, clearing not confirmed |
| `ERROR` | SAP rejected the row |
| `NOT_POSTED` | FM not reachable |

---

## 8. Database write-back

[document_link.py](document_link.py) uses the same matching rules as the AR Inbox
`connect_document_id` flow. It runs only for rows that are `SUCCESS` or `POSTED_NOT_CLEARED`.

| Table | Filter | Match | Set |
|---|---|---|---|
| `active_bt3i` | `transactiontype = 'Invoice'` and `documentid IS NULL` | `documentnumber` = invoice_ref | `documentid` = SAP document no |
| `active_bs` | `transactiontype = 'Bank'` and `documentid IS NULL` | `transactionref` = invoice_ref **or** UTR | `documentid` = SAP document no |

- Only the `documentid` column is updated, by primary key. Existing values are never overwritten.
- **Limitation:** invoices that came from the SAP sync (`source_table = 'active_bsid'`) already have `documentid` set to the invoice number (BELNR), so they are skipped. This is why the first live posting updated 0 rows. **Open decision:** overwrite `documentid` for these rows, or add separate `clearing_doc` / `clearing_date` columns (recommended).

---

## 9. First live posting (2026-11-15)

| Item | Value |
|---|---|
| Company code | DAT1 |
| Customer | 0030000001 |
| Invoice cleared | 4800000007 (XBLNR 0090000057), 1,858,500.00 INR |
| Bank G/L | 24500000. Taken from earlier DZ receipts 1900000000–02 in DAT1. |
| UTR | DTUTR2026111501 |
| **Posting / clearing document** | **1900000003 / 2026**, cleared on 2026-11-15 |
| Verified | Invoice no longer in BSID (open items) |
| DB update | 0 rows (see section 8) |

The first attempt failed with `Field BSEG-VALUT does not exist in dynpro SAPMF05A 0300`
and posted nothing. It was fixed by sending the value date only when it is given explicitly.

### Second live posting – real DB data, with DB write-back (2026-11-15)

Source data was taken from PostgreSQL and validated against SAP:

| Item | Value | Source |
|---|---|---|
| Invoice | 1800000001, 10,000 INR, WPP MEDIA | `active_bt3i` (Invoice, documentid NULL) = SAP open item DAT1 / 0030000001 |
| Bank receipt | FHDFCH01086920901, 13-11-2026, credit 40,000 | `active_bs.transactionref` = invoice `linkref` |
| match_by | BELNR | |
| **Posting / clearing document** | **1900000004 / 2026**, cleared on 2026-11-13 | |
| DB update | `active_bt3i` 1800000001 → documentid 1900000004; `active_bs` FHDFCH01086920901 → documentid 1900000004 | |

The bank receipt (40,000) also covers JM3304800001 and JM3304800002 (15,000 each).
Those invoices are not open items in SAP, so only 10,000 was posted. Because the same
UTR is now used in SAP, the duplicate check will reject further postings with this UTR.

**Issue found after the run:** the match was coincidental. The SAP customer 0030000001 is
"TEST Customer_02", not WPP MEDIA. The DB invoice is not from SAP (`source_table` NULL).
Reverse 1900000004 (FBRA → FB08) and reset the two `documentid` values. See
[CLEARING_AND_DB_UPDATE.md](CLEARING_AND_DB_UPDATE.md) section 5.3.

Fix made during this run: the `BELNR` match padded the invoice number to 20 characters
(`00000000001800000001`). It now converts to a 10-character BELNR.

**To reverse a test posting:** in SAP, run **FBRA** (reset cleared items) and then **FB08** (reverse), for document 1900000003 / DAT1 / 2026.

---

## 10. Troubleshooting

| Message | Cause / fix |
|---|---|
| `No open item in SAP ...` | Wrong customer, company code or reference, or `match_by` points to the wrong field. Check FBL5N. |
| `Amount mismatch ...` | The bank amount must equal the open amount (partial payments are not supported). |
| `Field BSEG-VALUT does not exist ...` | Don't send `value_date` for this bank G/L. |
| `Field BSEG-HKONT does not exist ...` | In the FM, change `BSEG-HKONT` to `RF05A-NEWKO`. |
| `Posting period ... is not open` | Open the period in OB52. |
| `UTR ... already posted in document ...` | Duplicate. This receipt is already in SAP. |
| `SAP function Z_BANK_POST_CLEAR not available` | FM inactive or missing, or `/sap/bc/soap/rfc` disabled, or the user has no `S_RFC` authorisation. |
| `Excel file not found on API server` | `file_path` must exist on the machine running the API. |

---

## 11. Open points

1. **Transport:** the SAP objects are in `$TMP`. Move them to a Z package with a transport before QA/PRD.
2. **DB write-back** for SAP-sourced invoices (see section 8).
3. **Bank G/L per company code:** confirm with the FI team. Only DAT1 → 24500000 has been used so far.
4. **Partial payment / residual items / bank charges:** not supported yet.
5. **Authorisation:** for production, use a dedicated technical user with `S_RFC` (ZBANK_POST), FB05, `F_BKPF_*`.
