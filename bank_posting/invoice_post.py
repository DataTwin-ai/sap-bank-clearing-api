"""
New customer invoice in SAP -> SAP document number -> active_bt3i.documentid.

POST /api/invoice/post, per item:
    1. Format checks: company code, customer, dates, amount, currency, revenue G/L, reference.
    2. DB: the active_bt3i row (org + documentnumber = invoice_ref) must not have a documentid yet.
    3. SAP (OData, read-only): customer exists for the company code and is not blocked;
       if the reference is already in SAP (BSID / BSAD, XBLNR or ZUONR, debit), that
       document is linked instead of posting a second one (ALREADY_IN_SAP).
    4. dry_run = true : SAP simulation (Z_INVOICE_POST, IV_SIMULATE = X), nothing posted.
       dry_run = false: Z_INVOICE_POST posts Dr customer (key 01) / Cr revenue G/L (key 50).
    5. update_db: active_bt3i.documentid = SAP document number (only where it is still empty).

SAP reference fields: XBLNR holds up to 16 characters (and drives SAP's duplicate check),
ZUONR 18, BKTXT 25. Longer references are rejected.
"""

import re
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import psycopg2

from bank_posting.invoice import SAPInvoiceIndex
from bank_posting.posting import (
    SAPBankPostingClient,
    _blank,
    _text,
    alpha,
    load_bank_config,
    logger,
    parse_amount,
    parse_date,
)
from db_manager import get_db_credentials

REF_MAX = 25  # BKPF-BKTXT


# ---------------------------------------------------------------------------
# Request -> items
# ---------------------------------------------------------------------------
def normalise_invoice_post(raw: Dict[str, Any], defaults: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    def pick(k: str) -> Any:
        return raw.get(k) if not _blank(raw.get(k)) else defaults.get(k)

    doc_raw = pick("doc_date")
    post_raw = pick("posting_date") if not _blank(pick("posting_date")) else doc_raw
    return {
        "row": raw.get("row"),
        "invoice_ref": _text(raw.get("invoice_ref")),
        "company_code": _text(pick("company_code")).upper(),
        "customer": alpha(pick("customer")),
        "gl_account": alpha(pick("gl_account")),
        "tax_code": _text(pick("tax_code")).upper(),
        "currency": _text(pick("currency")).upper(),
        "amount_raw": raw.get("amount"),
        "amount": parse_amount(raw.get("amount")),
        "doc_date_raw": doc_raw,
        "doc_date": parse_date(doc_raw),
        "posting_date_raw": post_raw,
        "posting_date": parse_date(post_raw),
        "baseline_date": parse_date(pick("baseline_date")),
        "period": _text(pick("period")),
        "doc_type": _text(pick("doc_type")).upper() or cfg["invoice_doc_type"],
        "item_text": _text(raw.get("item_text"))[:50],
    }


def validate_invoice_post(item: Dict[str, Any]) -> List[str]:
    errs: List[str] = []
    ref = item["invoice_ref"]
    if not ref:
        errs.append("invoice_ref is missing")
    elif len(ref) > REF_MAX:
        errs.append(f"invoice_ref '{ref}' is {len(ref)} characters; SAP allows {REF_MAX}")
    if not re.fullmatch(r"[A-Z0-9]{4}", item["company_code"]):
        errs.append(f"Invalid company code: '{item['company_code']}'")
    if not item["customer"]:
        errs.append("customer (SAP customer number) is missing")
    if not item["gl_account"]:
        errs.append("gl_account (revenue G/L) is missing")
    if item["tax_code"] and not re.fullmatch(r"[A-Z0-9]{1,2}", item["tax_code"]):
        errs.append(f"Invalid tax code: '{item['tax_code']}'")
    if not re.fullmatch(r"[A-Z]{3}", item["currency"]):
        errs.append(f"Invalid currency: '{item['currency']}'")
    if item["amount"] is None or item["amount"] <= 0:
        errs.append(f"Invalid amount: '{_text(item['amount_raw'])}'")
    if item["doc_date"] is None:
        errs.append(f"Invalid/missing doc_date: '{_text(item['doc_date_raw'])}'")
    if item["posting_date"] is None:
        errs.append(f"Invalid posting_date: '{_text(item['posting_date_raw'])}'")
    if item["period"] and not (item["period"].isdigit() and 1 <= int(item["period"]) <= 16):
        errs.append(f"Invalid posting period: '{item['period']}'")
    return errs


# ---------------------------------------------------------------------------
# Database: active_bt3i
# ---------------------------------------------------------------------------
def bt3i_document_ids(org_id: str, refs: List[str], env: Optional[str] = None) -> Dict[str, List[str]]:
    """documentnumber (upper-cased) -> documentids already set ([] = row exists, no documentid yet)."""
    if not refs:
        return {}
    c = get_db_credentials(env)
    conn = psycopg2.connect(host=c["host"], port=c["port"], user=c["user"],
                            password=c["password"], dbname=c["database"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT upper(trim(documentnumber)), "
                "       array_remove(array_agg(DISTINCT nullif(trim(documentid), '')), NULL) "
                "FROM public.active_bt3i "
                "WHERE orgid = %s AND transactiontype = 'Invoice' AND upper(trim(documentnumber)) = ANY(%s) "
                "GROUP BY 1",
                (org_id, [r.upper() for r in refs]),
            )
            return {r[0]: list(r[1] or []) for r in cur.fetchall()}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# SAP posting (RFC Z_INVOICE_POST)
# ---------------------------------------------------------------------------
class SAPInvoicePostClient(SAPBankPostingClient):
    def __init__(self, cfg: Dict[str, Any]):
        super().__init__({**cfg, "rfc_function": cfg["invoice_rfc_function"]})

    def post(self, item: Dict[str, Any], simulate: bool = False) -> Dict[str, Any]:  # type: ignore[override]
        def d(v: Optional[date]) -> Optional[str]:
            return v.isoformat() if v else None

        ref = item["invoice_ref"]
        params = {
            "IV_BUKRS": item["company_code"],
            "IV_KUNNR": item["customer"],
            "IV_BLDAT": d(item["doc_date"]),
            "IV_BUDAT": d(item["posting_date"]),
            "IV_BLART": item["doc_type"],
            "IV_MONAT": item["period"],
            "IV_WAERS": item["currency"],
            "IV_WRBTR": str(item["amount"]),
            "IV_HKONT": item["gl_account"],
            "IV_MWSKZ": item["tax_code"],
            "IV_XBLNR": ref if len(ref) <= 16 else None,
            "IV_ZUONR": ref if len(ref) <= 18 else None,
            "IV_BKTXT": ref,
            "IV_SGTXT": item["item_text"],
            "IV_ZFBDT": d(item["baseline_date"]),
            "IV_SIMULATE": "X" if simulate else None,
        }
        try:
            resp = self._call(self.fm, params)
        except Exception as e:
            return {"Status": "E", "Message": str(e)}
        out = {el.tag: (el.text or "").strip() for el in resp}
        return {
            "Status": out.get("EV_STATUS"),
            "Belnr": out.get("EV_BELNR") or None,
            "Gjahr": out.get("EV_GJAHR") if out.get("EV_GJAHR") not in ("", "0000") else None,
            "Message": out.get("EV_MESSAGE"),
        }


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------
def process_invoice_post(payload: Dict[str, Any], dry_run: bool = False, update_db: bool = True) -> Dict[str, Any]:
    cfg = load_bank_config()
    org_id = _text(payload.get("org_id"))
    if not org_id:
        raise ValueError("org_id is required")
    raw_items = [i for i in (payload.get("items") or []) if isinstance(i, dict)]
    if not raw_items:
        raise ValueError("items is empty")
    defaults = {k: payload.get(k) for k in ("company_code", "customer", "gl_account", "tax_code", "currency",
                                            "doc_date", "posting_date", "baseline_date", "period", "doc_type")}

    started = datetime.now()
    batch_id = f"INP{started:%Y%m%d%H%M%S}-{uuid.uuid4().hex[:4].upper()}"
    log = f"[INVOICE POST] {batch_id}"
    logger.info(f"{log}: start, org {org_id}, {len(raw_items)} item(s), dry_run={dry_run}, update_db={update_db}")

    items = [normalise_invoice_post({**r, "row": r.get("row", n)}, defaults, cfg)
             for n, r in enumerate(raw_items, start=1)]
    for i in items:
        logger.info(
            f"{log} row {i['row']} input: ref={i['invoice_ref']} company={i['company_code']} "
            f"customer={i['customer']} amount={i['amount']} {i['currency']} gl={i['gl_account']} "
            f"tax={i['tax_code'] or '-'} doc_date={i['doc_date']} posting_date={i['posting_date']} type={i['doc_type']}"
        )

    try:
        linked_ids = bt3i_document_ids(org_id, [i["invoice_ref"] for i in items if i["invoice_ref"]])
    except Exception as e:
        raise RuntimeError(f"Could not read active_bt3i: {e}") from e

    results: List[Dict[str, Any]] = []
    todo: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    seen = set()
    index: Optional[SAPInvoiceIndex] = None

    for item in items:
        errs = validate_invoice_post(item)
        warns: List[str] = []
        key = (item["company_code"], item["invoice_ref"].upper())
        if not errs and key in seen:
            errs.append("Duplicate invoice_ref in this request")
        seen.add(key)

        ids = linked_ids.get(item["invoice_ref"].upper())
        if ids:
            errs.append(f"active_bt3i already has SAP documentid {', '.join(ids)} for '{item['invoice_ref']}'")
        elif ids is None and item["invoice_ref"]:
            warns.append(f"No active_bt3i invoice row for org {org_id} / '{item['invoice_ref']}': "
                         f"the SAP document number cannot be stored")
        if len(item["invoice_ref"]) > 16:
            warns.append("invoice_ref is longer than 16 characters: not stored in the SAP reference (XBLNR), "
                         "SAP's duplicate check by reference is skipped")

        res: Dict[str, Any] = {
            "row": item["row"],
            "status": "INVALID",
            "invoice_ref": item["invoice_ref"],
            "company_code": item["company_code"],
            "customer": item["customer"],
            "gl_account": item["gl_account"],
            "tax_code": item["tax_code"] or None,
            "amount": float(item["amount"]) if item["amount"] is not None else None,
            "currency": item["currency"],
            "document_no": None,
            "fiscal_year": None,
            "message": "",
        }

        if not errs:
            if index is None:
                try:
                    index = SAPInvoiceIndex()
                except Exception as e:
                    raise RuntimeError(f"Could not read customers / invoices from SAP: {e}") from e
            errs.extend(index.check_customer(item["customer"], item["company_code"]))
            existing = index.find_invoice(item["customer"], item["company_code"], item["invoice_ref"], "ANY")
            if not errs and existing:
                docs = sorted({(_text(m.get("belnr")), _text(m.get("gjahr"))) for m in existing})
                sap_amt = sum((Decimal(str(m.get("wrbtr") or 0)) for m in existing), Decimal("0")).quantize(Decimal("0.01"))
                if len(docs) > 1:
                    errs.append(f"Reference is already in SAP in {len(docs)} documents: "
                                + ", ".join(f"{b}/{y}" for b, y in docs))
                elif item["amount"] is not None and sap_amt != item["amount"]:
                    errs.append(f"Reference is already in SAP (document {docs[0][0]}/{docs[0][1]}) "
                                f"with amount {sap_amt}, request {item['amount']}")
                else:
                    res.update(status="ALREADY_IN_SAP", document_no=docs[0][0], fiscal_year=docs[0][1],
                               message=f"Already in SAP as document {docs[0][0]}/{docs[0][1]}: linked, not posted again")

        if errs:
            res["message"] = "; ".join(errs)
        elif res["status"] != "ALREADY_IN_SAP":
            res.update(status="VALID", message="Checks passed")
            todo.append((item, res))
        if warns:
            res["warnings"] = warns
        results.append(res)
        logger.info(f"{log} row {res['row']} checks: {res['status']} - {res['message']}"
                    + (f" | warnings: {'; '.join(warns)}" if warns else ""))

    # SAP: simulate (dry run) or post
    sap_status = "nothing to post"
    if todo:
        client = SAPInvoicePostClient(cfg)
        ok, msg = client.available()
        if not ok:
            logger.warning(f"{log}: {msg}")
            sap_status = msg
            for _, res in todo:
                if dry_run:
                    res["warnings"] = res.get("warnings", []) + [f"SAP simulation skipped: {msg}"]
                else:
                    res.update(status="NOT_POSTED", message=msg)
        else:
            sap_status = "simulated" if dry_run else "ok"
            for item, res in todo:
                logger.info(f"{log} row {res['row']}: {'simulating' if dry_run else 'POSTING'} in SAP "
                            f"({client.fm}) ref={item['invoice_ref']} amount={item['amount']} {item['currency']}")
                d = client.post(item, simulate=dry_run)
                st = d.get("Status")
                if st == "V":
                    res.update(status="VALID", message=d.get("Message") or "Simulation passed")
                elif st == "S":
                    res.update(status="POSTED", document_no=d.get("Belnr"), fiscal_year=d.get("Gjahr"),
                               message=d.get("Message") or "Posted")
                elif st == "D":
                    res.update(status="ALREADY_IN_SAP", document_no=d.get("Belnr"), fiscal_year=d.get("Gjahr"),
                               message=d.get("Message") or "Already posted")
                else:
                    res.update(status="INVALID" if dry_run else "ERROR",
                               message=d.get("Message") or "Unknown SAP error")
                logger.info(f"{log} row {res['row']} SAP: {res['status']} "
                            f"document={res['document_no']}/{res['fiscal_year']} - {res['message']}")

    # DB: active_bt3i.documentid
    db_update: Dict[str, Any] = {"skipped": "dry_run" if dry_run else "disabled"}
    if not dry_run and update_db:
        linked = [{"assignment": r["invoice_ref"], "document_no": r["document_no"]}
                  for r in results if r["status"] in ("POSTED", "ALREADY_IN_SAP") and r["document_no"]]
        if linked:
            try:
                from bank_posting.document_link import link_document_ids
                db_update = link_document_ids(linked, invoices=True, bank=False, org_id=org_id)
                logger.info(f"{log}: active_bt3i.documentid updated on {db_update.get('invoices_updated')} row(s): "
                            + ", ".join(f"{m['documentnumber']}={m['documentid']}"
                                        for m in db_update.get("invoice_matches", [])))
            except Exception as e:
                logger.exception(f"{log}: SAP document(s) {[x['document_no'] for x in linked]} exist, "
                                 f"but active_bt3i.documentid update failed: {e}")
                db_update = {"error": str(e), "sap_documents": [x["document_no"] for x in linked]}
        else:
            db_update = {"skipped": "no SAP documents"}

    counts: Dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    logger.info(f"{log}: done in {(datetime.now() - started).total_seconds():.1f}s, {counts}, sap={sap_status}")
    return {
        "status": "completed",
        "batch_id": batch_id,
        "dry_run": dry_run,
        "org_id": org_id,
        "sap_posting": sap_status,
        "total": len(results),
        "summary": counts,
        "db_update": db_update,
        "results": results,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }
