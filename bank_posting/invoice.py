"""
Invoice file (FB70 layout) -> SAP validation -> DB update.

Columns (sheet FB70_Upload, row 1 = headers):
    BUSCS  transaction type ('Invoice')     ACCNT  customer account
    BLDAT  document date                    BUDAT  posting date
    WRBTR  invoice gross amount             WAERS  currency
    SGTXT  text                             HKONT  G/L account (offsetting line)
    WRBTR1 G/L line amount                  ZUONR  assignment / invoice reference
    KKBER  credit control area

Per row:
    1. Format checks (dates, amounts, currency, customer, G/L, reference)
    2. SAP checks (live, via OData ZGENERIC_DATA_SRV):
         - customer exists in KNA1, not blocked / flagged for deletion
         - customer is extended to the company code (KNB1), when a company code is known
         - invoice exists in SAP: customer line in BSID (open) or BSAD (cleared)
           whose reference (XBLNR and/or ZUONR) equals the file reference;
           currency and amount must match
    3. DB update (dry_run = false, update_db = true):
         active_bt3i.documentid = SAP invoice document number (BELNR)
         where active_bt3i.documentnumber = reference and documentid IS NULL
"""

import re
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from bank_posting.posting import (
    CustomerIndex,
    _text,
    alpha,
    configured_sap_tables,
    header_of,
    logger,
    parse_amount,
    parse_date,
)
from sap_odata_extractor import SAPODataExtractor

INVOICE_COLUMNS = {
    "BUSCS": "transaction_type",
    "ACCNT": "customer",
    "BLDAT": "doc_date",
    "BUDAT": "posting_date",
    "WRBTR": "amount",
    "WAERS": "currency",
    "SGTXT": "item_text",
    "HKONT": "gl_account",
    "WRBTR1": "gl_amount",
    "ZUONR": "invoice_ref",
    "KKBER": "credit_control_area",
}
OPTIONAL_COLUMNS = {"SGTXT", "KKBER", "WRBTR1"}
INVOICE_MATCH_FIELDS = ("ANY", "XBLNR", "ZUONR", "BELNR")


# ---------------------------------------------------------------------------
# Parsing / format validation
# ---------------------------------------------------------------------------
def invoice_items_from_rows(rows: List[tuple]) -> List[Dict[str, Any]]:
    if not rows:
        return []
    header = header_of(rows)
    col_idx = {INVOICE_COLUMNS[h]: i for i, h in enumerate(header) if h in INVOICE_COLUMNS}
    missing = [h for h, f in INVOICE_COLUMNS.items() if f not in col_idx and h not in OPTIONAL_COLUMNS]
    if missing:
        raise ValueError(f"Invoice Excel header is missing columns: {', '.join(missing)}")
    items = []
    for excel_row, values in enumerate(rows[1:], start=2):
        if all(_text(v) == "" for v in values):
            continue
        raw = {f: (values[i] if i < len(values) else None) for f, i in col_idx.items()}
        raw["row"] = excel_row
        items.append(raw)
    return items


def normalise_invoice(raw: Dict[str, Any], company_code: Optional[str]) -> Dict[str, Any]:
    return {
        "row": raw.get("row"),
        "transaction_type": _text(raw.get("transaction_type")),
        "company_code": (_text(raw.get("company_code")) or company_code or "").upper(),
        "customer": alpha(raw.get("customer")),
        "doc_date_raw": raw.get("doc_date"),
        "posting_date_raw": raw.get("posting_date"),
        "doc_date": parse_date(raw.get("doc_date")),
        "posting_date": parse_date(raw.get("posting_date")),
        "amount_raw": raw.get("amount"),
        "amount": parse_amount(raw.get("amount")),
        "gl_amount_raw": raw.get("gl_amount"),
        "gl_amount": parse_amount(raw.get("gl_amount")),
        "currency": _text(raw.get("currency")).upper(),
        "gl_account": alpha(raw.get("gl_account")),
        "invoice_ref": _text(raw.get("invoice_ref")),
        "item_text": _text(raw.get("item_text"))[:50],
        "credit_control_area": _text(raw.get("credit_control_area")),
    }


def validate_invoice(item: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    errs: List[str] = []
    warns: List[str] = []
    if item["transaction_type"] and item["transaction_type"].lower() != "invoice":
        errs.append(f"BUSCS must be 'Invoice', got '{item['transaction_type']}'")
    if not item["customer"]:
        errs.append("Customer (ACCNT) is missing")
    if item["doc_date"] is None:
        errs.append(f"Invalid/missing document date (BLDAT): '{_text(item['doc_date_raw'])}'")
    if item["posting_date"] is None:
        errs.append(f"Invalid/missing posting date (BUDAT): '{_text(item['posting_date_raw'])}'")
    if item["amount"] is None or item["amount"] <= 0:
        errs.append(f"Invalid invoice amount (WRBTR): '{_text(item['amount_raw'])}'")
    if not re.fullmatch(r"[A-Z]{3}", item["currency"]):
        errs.append(f"Invalid currency (WAERS): '{item['currency']}'")
    if not item["gl_account"]:
        errs.append("G/L account (HKONT) is missing")
    if not item["invoice_ref"]:
        errs.append("Invoice reference (ZUONR) is missing")
    if item["gl_amount_raw"] not in (None, "") and item["gl_amount"] is None:
        errs.append(f"Invalid G/L amount (WRBTR1): '{_text(item['gl_amount_raw'])}'")
    if item["gl_amount"] is not None and item["amount"] is not None:
        if item["gl_amount"] > item["amount"]:
            errs.append(f"G/L amount {item['gl_amount']} is greater than invoice amount {item['amount']}")
        elif item["gl_amount"] != item["amount"]:
            warns.append(f"G/L amount {item['gl_amount']} differs from invoice amount {item['amount']} (tax?)")
    if item["company_code"] and not re.fullmatch(r"[A-Z0-9]{4}", item["company_code"]):
        errs.append(f"Invalid company code: '{item['company_code']}'")
    return errs, warns


# ---------------------------------------------------------------------------
# SAP lookups (fetched once per request)
# ---------------------------------------------------------------------------
class SAPInvoiceIndex:
    # An invoice check always needs all four tables, whatever [sap] tables lists.
    TABLES = ["KNA1", "KNB1", "BSID", "BSAD"]

    def __init__(self):
        x = SAPODataExtractor()
        self.customers = CustomerIndex(x)
        self.items: List[Dict[str, Any]] = []
        for table, state in (("BSID", "OPEN"), ("BSAD", "CLEARED")):
            for r in x.fetch_table_data(table).to_dict("records"):
                r["_state"] = state
                self.items.append(r)

    def check_customer(self, kunnr: str, bukrs: str) -> List[str]:
        return self.customers.check(kunnr, bukrs)

    def find_invoice(self, kunnr: str, bukrs: str, ref: str, match_by: str) -> List[Dict[str, Any]]:
        fields = ("xblnr", "zuonr") if match_by == "ANY" else (match_by.lower(),)
        want = alpha(ref) if match_by == "BELNR" else ref.strip().upper()
        out = []
        for r in self.items:
            if alpha(r.get("kunnr")) != kunnr:
                continue
            if bukrs and str(r.get("bukrs", "")).upper() != bukrs:
                continue
            if str(r.get("shkzg")) != "S":  # invoice = debit line on the customer
                continue
            for f in fields:
                cand = alpha(r.get(f)) if f == "belnr" else str(r.get(f) or "").strip().upper()
                if cand and cand == want:
                    out.append(r)
                    break
        return out


def check_invoice_in_sap(item: Dict[str, Any], index: SAPInvoiceIndex, match_by: str) -> Tuple[List[str], Dict[str, Any]]:
    errs = index.check_customer(item["customer"], item["company_code"])
    matches = index.find_invoice(item["customer"], item["company_code"], item["invoice_ref"], match_by)
    if not matches:
        where = f"company {item['company_code']}, " if item["company_code"] else ""
        errs.append(f"Invoice not found in SAP for {where}customer {item['customer']} "
                    f"with {'XBLNR/ZUONR' if match_by == 'ANY' else match_by} = '{item['invoice_ref']}'")
        return errs, {}

    docs = sorted({(str(m.get("bukrs")), str(m.get("belnr")), str(m.get("gjahr"))) for m in matches})
    if len(docs) > 1:
        errs.append(f"Reference '{item['invoice_ref']}' matches {len(docs)} SAP documents: "
                    + ", ".join(f"{b}/{d}/{y}" for b, d, y in docs))

    currencies = {str(m.get("waers", "")).upper() for m in matches}
    if item["currency"] and currencies != {item["currency"]}:
        errs.append(f"Currency mismatch: file {item['currency']} vs SAP {', '.join(sorted(currencies))}")
    sap_amt = sum((Decimal(str(m.get("wrbtr") or 0)) for m in matches), Decimal("0")).quantize(Decimal("0.01"))
    if item["amount"] is not None and sap_amt != item["amount"]:
        errs.append(f"Amount mismatch: file {item['amount']} vs SAP {sap_amt}")

    m = matches[0]
    info = {
        "company_code": str(m.get("bukrs")),
        "document_no": str(m.get("belnr")),
        "fiscal_year": str(m.get("gjahr")),
        "sap_status": m["_state"],
        "clearing_doc": _text(m.get("augbl")) or None,
        "clearing_date": _text(m.get("augdt")) or None,
        "sap_amount": float(sap_amt),
    }
    return errs, info


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------
def process_invoice_file(
    sheet_rows: Optional[List[tuple]] = None,
    items: Optional[List[Dict[str, Any]]] = None,
    source: str = "json",
    dry_run: bool = False,
    match_by: Optional[str] = None,
    update_db: bool = True,
    company_code: Optional[str] = None,
) -> Dict[str, Any]:
    match_by = (match_by or "ANY").upper()
    if match_by not in INVOICE_MATCH_FIELDS:
        raise ValueError(f"match_by for invoices must be one of {INVOICE_MATCH_FIELDS}")

    if sheet_rows is not None:
        raw_items = invoice_items_from_rows(sheet_rows)
    elif items:
        raw_items = [{**it, "row": it.get("row", i)} for i, it in enumerate(items, start=1)]
    else:
        raise ValueError("Provide either 'file_path' or 'items'")

    batch_id = f"INV{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:4].upper()}"
    logger.info(f"[INVOICE] {batch_id}: {len(raw_items)} row(s), dry_run={dry_run}, match_by={match_by}")

    try:
        index = SAPInvoiceIndex()
    except Exception as e:
        raise RuntimeError(f"Could not read customers / invoices from SAP: {e}") from e

    results: List[Dict[str, Any]] = []
    seen = set()
    for raw in raw_items:
        item = normalise_invoice(raw, company_code)
        errs, warns = validate_invoice(item)
        key = (item["company_code"], item["customer"], item["invoice_ref"].upper())
        if not errs and key in seen:
            errs.append("Duplicate row in this file for the same customer/invoice")
        seen.add(key)

        extra: Dict[str, Any] = {}
        if not errs:
            sap_errs, extra = check_invoice_in_sap(item, index, match_by)
            errs.extend(sap_errs)

        results.append({
            "row": item["row"],
            "status": "INVALID" if errs else "VALID",
            "company_code": extra.get("company_code") or item["company_code"] or None,
            "customer": item["customer"],
            "invoice_ref": item["invoice_ref"],
            "amount": float(item["amount"]) if item["amount"] is not None else None,
            "currency": item["currency"],
            "gl_account": item["gl_account"] or None,
            "document_no": extra.get("document_no") if not errs else None,
            "fiscal_year": extra.get("fiscal_year") if not errs else None,
            "sap_status": extra.get("sap_status"),
            "clearing_doc": extra.get("clearing_doc"),
            "clearing_date": extra.get("clearing_date"),
            "message": "; ".join(errs) if errs else "; ".join(["Found in SAP"] + warns),
            **({"warnings": warns} if warns else {}),
        })

    db_update: Dict[str, Any] = {"skipped": "dry_run" if dry_run else "disabled"}
    if not dry_run and update_db:
        linked = [{"assignment": r["invoice_ref"], "document_no": r["document_no"]}
                  for r in results if r["status"] == "VALID" and r["document_no"]]
        if linked:
            try:
                from bank_posting.document_link import link_document_ids
                db_update = link_document_ids(linked, invoices=True, bank=False)
            except Exception as e:
                logger.error(f"[INVOICE] documentid update failed: {e}")
                db_update = {"error": str(e)}
        else:
            db_update = {"skipped": "no valid invoices"}

    counts: Dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    return {
        "status": "completed",
        "file_type": "invoice",
        "batch_id": batch_id,
        "dry_run": dry_run,
        "match_by": match_by,
        "source": source,
        "sap_posting": "not applicable (invoice validation)",
        "sap_tables": {"used": SAPInvoiceIndex.TABLES, "ignored": configured_sap_tables()[1]},
        "total": len(results),
        "summary": counts,
        "db_update": db_update,
        "results": results,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }
