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

## Authorisation for the calling user

`S_RFC` (function group `ZBANK_POST`), `S_TCODE` FB05,
`F_BKPF_BUK`, `F_BKPF_BLA`, `F_BKPF_KOA` (D, S) – activity 01.
