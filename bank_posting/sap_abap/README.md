# SAP side – Z_BANK_POST_CLEAR

Posts a bank receipt (Dr bank G/L, key 40) and clears the customer's open
invoice via `POSTING_INTERFACE_CLEARING` (FB05 incoming-payment logic).
Called from Python ([../posting.py](../posting.py)) through SAP's built-in
SOAP-RFC endpoint `/sap/bc/soap/rfc` – no OData / SEGW project needed.

## Objects in SAP (system A4H, client 200)

| Object | Name | Package |
|---|---|---|
| Function group | `ZBANK_POST` | `$TMP` |
| Function module (RFC-enabled) | `Z_BANK_POST_CLEAR` | `$TMP` |

Created and activated via ADT on 2026-11-15. Source: [Z_BANK_POST_CLEAR.abap](Z_BANK_POST_CLEAR.abap)
(ADT format – signature is in the code). `$TMP` is local: move to a Z package
with a transport (SE03 / ADT "Change Package Assignment") before moving to QA/PRD.

## Interface

| Import | Type | |
|---|---|---|
| IV_BUKRS | BUKRS | company code |
| IV_KUNNR | KUNNR | customer |
| IV_BLDAT / IV_BUDAT | BLDAT / BUDAT | document / posting date |
| IV_VALUT | VALUT | value date (optional) |
| IV_BLART | BLART | default `DZ` |
| IV_MONAT | MONAT | optional |
| IV_WAERS | WAERS | currency |
| IV_HKONT | HKONT | bank G/L account |
| IV_WRBTR | WRBTR | amount |
| IV_MATCH_BY | FIELDNAME | `XBLNR` / `ZUONR` / `BELNR` |
| IV_INVOICE_REF | CHAR20 | value to find the open item |
| IV_UTR | XBLNR1 | optional; duplicate check + header reference |
| IV_SGTXT | SGTXT | optional item text |

| Export | |
|---|---|
| EV_STATUS | `S` posted + cleared, `W` posted but clearing not confirmed, `E` error (nothing posted) |
| EV_BELNR / EV_GJAHR | posted document |
| EV_AUGBL / EV_AUGDT | clearing document / date (from `BSAD_VIEW`) |
| EV_MESSAGE | SAP message text |

## Logic

1. If `IV_UTR` given: reject if a non-reversed document with the same BLART/XBLNR exists.
2. Open item must exist in `BSID_VIEW` for company/customer/match field.
3. `POSTING_INTERFACE_START` (call transaction, mode N, sync update)
   → `POSTING_INTERFACE_CLEARING` (`EINGZAHL`, `FB05`) → `POSTING_INTERFACE_END`.
4. Read clearing doc from `BSAD_VIEW`.

## Tested

- SOAP call with unknown invoice → `E`, "No open item …" (no posting).
- Unsupported match field → `E`.
- Real posting **not yet tested** – needs a valid bank G/L for the company code.

## Z_BANK_POST_CLEAR_MULTI (connection clearing)

Same function group, RFC-enabled, `$TMP`. Created and activated via ADT on 2026-11-23.
Source: [Z_BANK_POST_CLEAR_MULTI.abap](Z_BANK_POST_CLEAR_MULTI.abap). Called by `POST /api/clearing/post`
([../clearing.py](../clearing.py), API guide [../CONNECTION_CLEARING_API.md](../CONNECTION_CLEARING_API.md)).

One bank receipt clears **several** invoices in one FB05 document (one `FTCLEAR` row per invoice).
Interface as `Z_BANK_POST_CLEAR`, except:

| Parameter | |
|---|---|
| IV_INVOICE_REFS (STRING) | invoice references separated by `,` (replaces IV_INVOICE_REF) |
| IV_ZUONR (DZUONR, optional) | assignment on the bank line (default: UTR, else first invoice) |
| EV_CLEARED (INT4) | number of invoices found cleared by EV_BELNR |

`EV_STATUS`: `S` all invoices cleared, `W` posted but not all found in `BSAD_VIEW`, `E` nothing posted.

Tested 2026-11-23 over SOAP-RFC: unsupported match field, empty list and unknown invoices all return `E`
without posting. A real posting has not been run yet.

## Z_INVOICE_POST (new customer invoice)

Same function group, RFC-enabled, `$TMP`. Created and activated via ADT on 2026-11-23.
Source: [Z_INVOICE_POST.abap](Z_INVOICE_POST.abap). Called by `POST /api/invoice/post`
([../invoice_post.py](../invoice_post.py)).

Tested 2026-11-23 in simulation only (nothing posted; BSID unchanged afterwards):

| Case | Result |
|---|---|
| Reference WEB000001 already posted | `D`, document 1800000002 |
| Customer not in KNB1 | `E` |
| G/L 800000 | `E` "G/L account 800000 DAT1 does not exist" |
| G/L 34400000 / 40010000 without tax code | `E` "requires a valid tax code" |
| Tax code A0 | `E` "Tax code A0 does not exist in company code DAT1": DAT1's tax procedure ITC3IN has only input codes S0–S4 |
| G/L 40400000 (Sales Offsetting Acct), no tax, 100 INR | `V` simulation passed |
| Posting date 2026-03-15 | `E` "Posting period 012 2025 is not open" |

Posts FB01 with `POSTING_INTERFACE_DOCUMENT`: line 1 key 01 customer (debit), line 2 key 50 revenue G/L
(credit), tax calculated by SAP when `IV_MWSKZ` is set. `IV_XBLNR` / `IV_ZUONR` / `IV_BKTXT` carry the
DataTwin invoice number.

`IV_SIMULATE = X` passes `I_XSIMU` to the posting interface, which replaces the save code `/11` by `BS`
(simulate): FB01 runs all checks and stops on the overview screen, nothing is saved (`EV_STATUS = V`).

`EV_STATUS`: `S` posted, `V` simulation passed, `D` reference already posted (`EV_BELNR` = that document),
`E` error, nothing posted.

## Authorisation for the calling user

`S_RFC` (function group `ZBANK_POST`), `S_TCODE` FB05,
`F_BKPF_BUK`, `F_BKPF_BLA`, `F_BKPF_KOA` (D, S) – activity 01.
