# AR Inbox → SAP: invoice posting and connection clearing

This page is for the team that calls the API from AR Inbox.

| Step | Call | What SAP does | What is written in our DB |
|---|---|---|---|
| 1. Invoice | `POST /api/invoice/post` | Posts a **new** customer invoice and returns its document number | `active_bt3i.documentid` = SAP document number |
| 2. Clearing | `POST /api/clearing/post` | Posts the bank receipt and clears **all** invoices of the connection in **one** document | `active_cnx.clearingdocid` = SAP clearing document number |

Step 2 finds each invoice in SAP through the `documentid` written in step 1, so run step 1 for every
invoice of a connection first.

Base URL: `https://demo.datatwin.ai/sap/bank` (live) or `http://<host>:8000` (local).
Every call needs the header `X-API-Key: <key>`. Swagger: `<base URL>/docs`.

Always call with `"dry_run": true` first. A dry run runs every check (step 1 also runs an SAP
simulation), posts nothing and writes nothing.

---

## 1. Invoice: post in SAP, store the document number

### Request

```json
POST /api/invoice/post
{
  "org_id": "OM0000000001",
  "company_code": "DAT1",
  "gl_account": "40400000",
  "tax_code": "",
  "currency": "INR",
  "dry_run": true,
  "update_db": true,
  "items": [
    {
      "invoice_ref": "TMI3130009778",
      "customer": "30000001",
      "amount": 1165840.00,
      "doc_date": "2026-03-15",
      "posting_date": "2026-11-23",
      "item_text": "WPP MEDIA invoice TMI3130009778"
    }
  ]
}
```

`company_code`, `customer`, `gl_account`, `tax_code`, `currency`, `doc_date`, `posting_date` can be given
once at request level or per item (the item value wins).

| Field | Required | Meaning |
|---|---|---|
| `org_id` | yes | `active_bt3i.orgid` |
| `items[].invoice_ref` | yes | DataTwin invoice number = `active_bt3i.documentnumber`. Up to 25 characters (see below) |
| `items[].amount` | yes | Gross amount |
| `customer` | yes | **SAP** customer number (not the DataTwin customer name) |
| `company_code` | yes | SAP company code |
| `gl_account` | yes | Revenue G/L account |
| `currency` | yes | Currency |
| `doc_date` | yes | Invoice date `YYYY-MM-DD` |
| `posting_date` | no | Default `doc_date`. The SAP period must be open: send today's date for old invoices (a doc date of 2026-03-15 falls in the closed period 012/2025) |
| `tax_code` | no | SAP tax code. Empty = no tax. When set, SAP calculates the tax out of the gross amount |
| `item_text` | no | Line text (50 characters) |
| `baseline_date`, `period`, `doc_type` | no | Defaults: from payment terms, from posting date, `DR` |
| `dry_run` | no | Default `false` |
| `update_db` | no | Default `true` |

Where `invoice_ref` goes in SAP: header reference `XBLNR` (if ≤ 16 characters, also SAP's duplicate check),
assignment `ZUONR` (if ≤ 18), header text `BKTXT` (always, ≤ 25).

### What SAP posts

| Line | Posting key | Account | Amount |
|---|---|---|---|
| 1 | 01 | Customer 30000001 | Dr 1,165,840.00 |
| 2 | 50 | Revenue G/L 40400000 | Cr 1,165,840.00 (net + tax lines if `tax_code` is set) |

### Checks (nothing is posted if one fails)

1. Fields above; no duplicate `invoice_ref` in the request.
2. `active_bt3i` row for `org_id` + `invoice_ref` has no `documentid` yet (otherwise: already linked).
   No row at all → warning, the invoice is posted but the number cannot be stored.
3. SAP: customer exists for the company code and is not blocked.
4. SAP: if the reference is already in SAP for this customer (XBLNR or ZUONR), that document is **linked**
   instead of posting a second one (`ALREADY_IN_SAP`).
5. Dry run: SAP simulation of the posting (accounts, tax code, field status, period). Real run: posting.

### Response

```json
{
  "status": "completed",
  "batch_id": "INP20261123191812-1A2B",
  "dry_run": false,
  "org_id": "OM0000000001",
  "summary": { "POSTED": 1 },
  "results": [
    { "row": 1, "status": "POSTED", "invoice_ref": "TMI3130009778", "document_no": "1800000010",
      "fiscal_year": "2026", "message": "Invoice document 1800000010/2026 posted" }
  ],
  "db_update": { "invoices_updated": 1, "invoice_matches": [ { "documentnumber": "TMI3130009778", "documentid": "1800000010" } ] }
}
```

(Example values; the document number comes from SAP.)

| `results[].status` | Meaning | Posted? | `documentid` written? |
|---|---|---|---|
| `VALID` | Dry run passed (incl. SAP simulation) | no | no |
| `INVALID` | A check failed, reason in `message` | no | no |
| `POSTED` | New SAP invoice, number in `document_no` | yes | yes |
| `ALREADY_IN_SAP` | Reference already in SAP, that document is used | no | yes |
| `ERROR` | SAP refused the posting, SAP message in `message` | no | no |
| `NOT_POSTED` | SAP function not reachable | no | no |

### From an Excel file in S3 instead of `items`

Upload the FB70 Excel to the tenant bucket and send its key:

```json
POST /api/invoice/post
{
  "org_id": "OM0000000001",
  "tenant_id": "DM00005",
  "file_path": "AR Inbox Post ERP/OM0000000001/invoice_fb70_sample.xlsx",
  "sheet": "FB70_Upload",
  "company_code": "DAT1",
  "gl_account": "40400000",
  "dry_run": true
}
```

| `file_path` form | File read from |
|---|---|
| key + `tenant_id` | `s3://dt-<tenant_id lower-case><env suffix>/<file_path>`, e.g. `s3://dt-dm00005-dev/AR Inbox Post ERP/OM0000000001/invoice.xlsx` (dev suffix `-dev`) |
| `s3://bucket/key` or `https://bucket.s3.amazonaws.com/key` | that object |
| other text | a file on the API server (or the same file name in its `data/` folder) |

The file is downloaded to the server's `data/s3/` folder and read; each row becomes one item.

| Column | Item field | Required |
|---|---|---|
| ZUONR | `invoice_ref` | yes |
| ACCNT | `customer` (SAP number) | yes |
| WRBTR | `amount` (gross) | yes |
| WAERS | `currency` | yes |
| BLDAT / BUDAT | `doc_date` / `posting_date` | yes |
| HKONT | `gl_account` (empty cell → request value) | yes (column) |
| BUSCS | must be `Invoice` | yes (column) |
| BUKRS, MWSKZ | `company_code`, `tax_code` (empty → request value) | no |
| SGTXT | `item_text` | no |
| WRBTR1 | only compared with WRBTR (warning if different and no tax code) | no |

A bank-layout file (NEWKO_2 / WRBTR_2) is rejected with 400. A key that does not exist returns 400 `S3 file not found: s3://…`.
Sample: [samples/invoice_fb70_sample.xlsx](samples/invoice_fb70_sample.xlsx), [samples/invoice_post_s3.json](samples/invoice_post_s3.json).

### Invoice already posted in SAP by someone else

`POST /api/bank/post` with `"file_type": "invoice"` only **finds** the invoice in SAP and stores its number;
it never posts. Same fields as above plus `match_by` (`ANY` / `XBLNR` / `ZUONR` / `BELNR`).

---

## 2. Connection clearing

A connection is all rows of `public.active_cnx` with the same `orgid` + `documentid`:
one `source = 'Bank'` row (the receipt) and one or more `source = 'Invoice'` rows.

### Request: minimal (after step 1)

```json
POST /api/clearing/post
{
  "org_id": "OM0000000001",
  "connection_id": "5677/20260703/1110380",
  "company_code": "DAT1",
  "customer": "30000001",
  "bank_gl": "24500000",
  "currency": "INR",
  "posting_date": "2026-11-23",
  "dry_run": true
}
```

The API takes from `active_cnx`:
- invoices: the invoice rows (`connectingdocumentnumber`, `documentvalue`); each number is replaced by its SAP
  document number from `active_bt3i.documentid` (`match_by` = `BELNR`, the default);
- amount and UTR: the bank row (`documentvalue`, `connectingdocumentnumber`).

The response lists what was taken in `filled_from_connection`, and `invoices[].connection_ref` shows the
DataTwin number next to the SAP number.

### Request: everything explicit

```json
{
  "org_id": "OM0000000001",
  "connection_id": "25677/20261117/30000",
  "company_code": "DAT1",
  "customer": "30000001",
  "bank_gl": "24500000",
  "currency": "INR",
  "posting_date": "2026-11-23",
  "amount": 11000,
  "utr": "TESTUTR0001",
  "match_by": "BELNR",
  "invoices": [
    { "invoice_ref": "1800000000", "amount": 10000 },
    { "invoice_ref": "1800000002", "amount": 1000 }
  ],
  "dry_run": true,
  "update_db": true
}
```

### Fields

| Field | Required | Default | Meaning |
|---|---|---|---|
| `org_id` | yes | | `active_cnx.orgid` |
| `connection_id` | yes | | `active_cnx.documentid` |
| `company_code` | yes | | SAP company code |
| `customer` | yes | | SAP customer number. All invoices must belong to this customer |
| `bank_gl` | yes | | Bank G/L account (debit line, posting key 40) |
| `currency` | yes | | Currency of the receipt and of every invoice |
| `posting_date` | yes | | `YYYY-MM-DD`. The SAP period must be open |
| `doc_date` | no | `posting_date` | Document date |
| `amount` | no | bank row | Receipt amount. Must equal the sum of `invoices[].amount` |
| `utr` | no | bank row | Bank reference. ≤ 16 characters: SAP header reference + duplicate check. 17–18: assignment only, with a warning. More: rejected |
| `invoices[]` | no | invoice rows | `invoice_ref` + `amount`. Each amount must equal the open amount in SAP |
| `match_by` | no | `BELNR` | SAP field that holds `invoice_ref`: `BELNR` (SAP document number), `XBLNR` or `ZUONR` |
| `item_text` | no | `<connection_id> <utr>` | Text on the bank line |
| `doc_type` | no | `DZ` | SAP document type |
| `period`, `value_date` | no | | Only if needed |
| `dry_run` | no | `false` | `true` = check only |
| `update_db` | no | `true` | Write the clearing document to `active_cnx` |

### Checks (in this order, nothing is posted if one fails)

1. The connection exists for the org, and `clearingdocid` is still empty on all its rows (no double clearing).
2. `update_db: true` → the column `active_cnx.clearingdocid` exists.
3. Invoices taken from the connection have an SAP document number in `active_bt3i.documentid`.
4. Dates, company code, currency, customer, bank G/L, UTR length, no duplicate invoices.
5. **Bank amount = sum of the invoice amounts.** Partial clearing (`partially_cleared`) is rejected.
6. SAP (BSID / BSAD): every invoice is an open item of this customer in this company code, same currency,
   open amount = invoice amount. An invoice that is already cleared is reported with its clearing document.
7. In SAP (`Z_BANK_POST_CLEAR_MULTI`): UTR not posted before, every invoice still open.

### What SAP posts (`dry_run: false`)

One FB05 document (incoming payment):

| Line | Posting key | Account | Amount |
|---|---|---|---|
| 1 | 40 | Bank G/L 24500000 | Dr 11,000 |
| 2 | 15 (generated) | Customer 30000001 | Cr 11,000, clears 1800000000 and 1800000002 |

The posting document is also the clearing document (`AUGBL` of every invoice).

### Response (real dry run)

```json
{
  "status": "completed",
  "batch_id": "CLR20261123191449-5826",
  "dry_run": true,
  "result": "VALID",
  "message": "Validation passed",
  "org_id": "OM0000000001",
  "connection_id": "25677/20261117/30000",
  "document_no": null,
  "clearing_doc": null,
  "invoices": [
    { "invoice_ref": "1800000000", "amount": 10000.0, "open_amount": 10000.0, "sap_status": "OPEN",
      "message": "Open in SAP, amount matches" },
    { "invoice_ref": "1800000002", "amount": 1000.0, "open_amount": 1000.0, "sap_status": "OPEN",
      "message": "Open in SAP, amount matches" }
  ],
  "filled_from_connection": [],
  "connection": { "rows": 3, "invoice_rows": 2, "bank_rows": 1, "clearing_column": "clearingdocid", "column_exists": false },
  "sap_posting": "skipped (dry_run)",
  "db_update": { "skipped": "dry_run" }
}
```

After a real posting, `result` is `SUCCESS`, and `document_no` / `clearing_doc` / `clearing_date` hold the SAP
document. `db_update` then shows `{"table": "active_cnx", "column": "clearingdocid", "value": "<doc>", "rows_updated": 3}`.

| `result` | Meaning | Posted in SAP? | DB written? |
|---|---|---|---|
| `VALID` | Dry run, all checks passed | no | no |
| `INVALID` | A check failed; reason in `message` and `invoices[].message` | no | no |
| `SUCCESS` | Posted, every invoice cleared | yes | yes |
| `POSTED_NOT_CLEARED` | Posted, but not every invoice was found cleared; check FBL5N | yes | yes (prevents a second posting) |
| `ERROR` | SAP refused the posting; SAP message in `message` | no | no |
| `NOT_POSTED` | The SAP function is not reachable | no | no |

HTTP status for both calls: `200` for all of the above; `401` wrong or missing API key;
`422` required field missing; `500` SAP or database not reachable.

### What is written

```sql
UPDATE public.active_cnx SET clearingdocid = '<clearing_doc>'
 WHERE orgid = '<org_id>' AND documentid = '<connection_id>'
   AND (clearingdocid IS NULL OR clearingdocid = '');
```

Every row of the connection (bank and invoices) gets the same number. A value that is already there is never overwritten.

---

## 3. Bank receipts

| Case | Call |
|---|---|
| The receipt is matched to invoices in a connection (`active_cnx`) | `POST /api/clearing/post` (section 2), one call per connection |
| A bank Excel (F-30 layout) or a JSON list, **one receipt clears one invoice** per row | `POST /api/bank/post` |

`/api/bank/post` with a file in S3:

```json
{
  "file_type": "bank",
  "tenant_id": "DM00005",
  "org_id": "OM0000000002",
  "file_path": "AR Inbox Post ERP/OM0000000002/bank_CTXBS0126270000028_1795429634553.xlsx",
  "sheet": "FB70_Upload",
  "dry_run": true,
  "update_db": true
}
```

The same S3 rules apply as for invoices. Excel columns per row (F-30 layout): BLDAT, BLART, BUKRS, BUDAT, MONAT, WAERS,
NEWBS_1 / NEWKO_1 (customer) / WRBTR_1, ZUONR_1 (invoice reference), NEWBS_2 (40) / NEWKO_2 (bank G/L) / WRBTR_2 (amount),
ZUONR_2 (UTR), SGTXT_1 / SGTXT_2. The same with JSON `items`:

```json
{
  "org_id": "OM0000000001",
  "match_by": "BELNR",
  "dry_run": true,
  "items": [
    { "doc_date": "2026-11-23", "posting_date": "2026-11-23", "company_code": "DAT1", "currency": "INR",
      "customer": "30000001", "bank_gl": "24500000", "amount": 10000.00,
      "invoice_ref": "1800000000", "utr": "UTR0001", "item_text": "Bank receipt" }
  ]
}
```

Result per row: SAP posting = clearing document in `results[].document_no`, written to `active_bt3i.documentid`
(invoice) and `active_bs.documentid` (bank row whose `transactionref` = UTR).

## 4. Before the first real run

| # | Who | What | Status |
|---|---|---|---|
| 1 | SAP | `Z_INVOICE_POST` and `Z_BANK_POST_CLEAR_MULTI` | active in A4H client 200 (`$TMP`); other systems need a transport |
| 2 | Finance / SAP | Revenue G/L and tax code for DAT1. The revenue accounts 34400000 and 40010000 require a tax code, but DAT1's tax procedure (ITC3IN) has no output tax code, so SAP rejects both. 40400000 (Sales Offsetting Acct) works without tax (tested in simulation) | **open** |
| 3 | DB team | `ALTER TABLE public.active_cnx ADD COLUMN clearingdocid text;` (also `inactive_cnx` if rows are moved there) | **open** |
| 4 | Finance | SAP customer number, company code, revenue G/L and tax code per invoice / org | **open**: AR Inbox must send them |
| 5 | DevOps | Deploy this API version to the live server | **open** |

## 5. Undo

| What | SAP | DB |
|---|---|---|
| Wrong invoice (step 1) | **FB08** reverse the invoice document (only while it is not cleared) | `UPDATE active_bt3i SET documentid = NULL WHERE orgid = '<org>' AND documentnumber = '<invoice_ref>' AND documentid = '<doc>';` |
| Wrong clearing (step 2) | **FBRA** for the clearing document, "Resetting and reversing" | `UPDATE public.active_cnx SET clearingdocid = NULL WHERE orgid = '<org>' AND documentid = '<connection_id>' AND clearingdocid = '<doc>';` |
