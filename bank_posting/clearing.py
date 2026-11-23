"""
Connection clearing: one DataTwin connection -> one SAP clearing document.

A connection (public.active_cnx, all rows with the same orgid + documentid) links
one bank receipt (source = 'Bank') with 1..n invoices (source = 'Invoice').

Steps (POST /api/clearing/post):
    1. Read the connection from active_cnx. Invoices, bank amount and UTR that the
       request leaves out are taken from it.
    2. Validate: dates, company code, customer, bank G/L, currency,
       bank amount = sum of the invoice amounts (no partial clearing).
    3. Connection checks: it exists for the org and is not cleared yet; the
       clearing column exists (when update_db is true).
    4. SAP checks (BSID / BSAD via ZGENERIC_DATA_SRV): every invoice is an open item
       of the customer, same currency, open amount = invoice amount.
    5. dry_run = false: RFC Z_BANK_POST_CLEAR_MULTI posts the bank line (Dr bank G/L)
       and clears every invoice in one FB05 document. Posting document = clearing document.
    6. update_db = true: active_cnx.<cnx_clearing_column> = clearing document,
       on every row of the connection where the column is still empty.

Nothing is posted in SAP when a check fails.
"""

import re
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

import psycopg2
from psycopg2 import sql

from bank_posting.posting import (
    MATCH_FIELDS,
    OpenItemIndex,
    SAPBankPostingClient,
    _blank,
    _text,
    alpha,
    configured_sap_tables,
    is_true,
    load_bank_config,
    logger,
    org_settings,
    parse_amount,
    parse_date,
    sap_customer_for,
)
from db_manager import get_db_credentials

CNX_SCHEMA = "public"
CNX_TABLE = "active_cnx"
REF_MAX_LEN = {"XBLNR": 16, "ZUONR": 18, "BELNR": 10}
_COLUMN_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")


# ---------------------------------------------------------------------------
# Database: public.active_cnx
# ---------------------------------------------------------------------------
def _connect(env: Optional[str] = None):
    c = get_db_credentials(env)
    return psycopg2.connect(host=c["host"], port=c["port"], user=c["user"],
                            password=c["password"], dbname=c["database"])


def _check_column_name(column: str) -> str:
    if not _COLUMN_NAME.fullmatch(column or ""):
        raise ValueError(f"[bank_posting] cnx_clearing_column '{column}' is not a valid column name")
    return column


def read_connection(org_id: str, connection_id: str, column: str,
                    env: Optional[str] = None) -> Dict[str, Any]:
    """Rows of one connection plus whether the clearing column exists."""
    column = _check_column_name(column)
    conn = _connect(env)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass(%s) "
                "AND attname = %s AND attnum > 0 AND NOT attisdropped",
                (f"{CNX_SCHEMA}.{CNX_TABLE}", column),
            )
            has_col = cur.fetchone() is not None
            q = sql.SQL(
                "SELECT source, sno, connectingdocumentnumber, documentvalue, documentstatus, {col}, customername "
                "FROM {tbl} WHERE orgid = %s AND documentid = %s "
                "ORDER BY source DESC, length(sno), sno"
            ).format(
                col=sql.Identifier(column) if has_col else sql.SQL("NULL"),
                tbl=sql.Identifier(CNX_SCHEMA, CNX_TABLE),
            )
            cur.execute(q, (org_id, connection_id))
            rows = [
                {"source": r[0], "sno": r[1], "ref": _text(r[2]),
                 "amount": parse_amount(r[3]), "status": r[4], "clearing_doc": _text(r[5]),
                 "customer_name": " ".join(_text(r[6]).split())}
                for r in cur.fetchall()
            ]
    finally:
        conn.close()
    return {
        "column_exists": has_col,
        "rows": rows,
        "invoices": [r for r in rows if (r["source"] or "").lower() == "invoice"],
        "bank": [r for r in rows if (r["source"] or "").lower() == "bank"],
        "cleared_by": sorted({r["clearing_doc"] for r in rows if r["clearing_doc"]}),
        "customer_names": sorted({r["customer_name"] for r in rows if r["customer_name"]}),
    }


def write_clearing_doc(org_id: str, connection_id: str, clearing_doc: str, column: str,
                       env: Optional[str] = None) -> Dict[str, Any]:
    """active_cnx.<column> = clearing_doc on every row of the connection where it is still empty."""
    column = _check_column_name(column)
    q = sql.SQL(
        "UPDATE {tbl} SET {col} = %s "
        "WHERE orgid = %s AND documentid = %s AND ({col} IS NULL OR {col} = '')"
    ).format(tbl=sql.Identifier(CNX_SCHEMA, CNX_TABLE), col=sql.Identifier(column))
    conn = _connect(env)
    try:
        with conn.cursor() as cur:
            cur.execute(q, (clearing_doc, org_id, connection_id))
            n = cur.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    logger.info(f"[CLEARING] {CNX_TABLE}.{column} = {clearing_doc} on {n} row(s) of {org_id} / {connection_id}")
    return {"table": CNX_TABLE, "column": column, "value": clearing_doc, "rows_updated": n}


# ---------------------------------------------------------------------------
# Request -> item
# ---------------------------------------------------------------------------
def normalise_clearing(p: Dict[str, Any], cfg: Dict[str, Any], org: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Request values; company code, customer, bank G/L, currency, document type fall back to [org:<org_id>]."""
    org = org or {}

    def pick(k: str) -> Any:
        return p.get(k) if not _blank(p.get(k)) else org.get(k)

    posting = parse_date(p.get("posting_date"))
    doc_raw = p.get("doc_date") if not _blank(p.get("doc_date")) else p.get("posting_date")
    # bank G/Ls with field status "value date required" (e.g. G005): value_date_required = true in [org:..]
    value_raw = p.get("value_date") if not _blank(p.get("value_date")) else (
        p.get("posting_date") if is_true(org.get("value_date_required")) else None)
    return {
        "org_id": _text(p.get("org_id")),
        "connection_id": _text(p.get("connection_id")),
        "company_code": _text(pick("company_code")).upper(),
        "customer": alpha(pick("customer")),
        "bank_gl": alpha(pick("bank_gl")),
        "currency": _text(pick("currency")).upper(),
        "posting_date_raw": p.get("posting_date"),
        "posting_date": posting,
        "doc_date_raw": doc_raw,
        "doc_date": parse_date(doc_raw),
        "value_date": parse_date(value_raw),
        "doc_type": _text(pick("doc_type")).upper() or cfg["default_doc_type"],
        "house_bank": _text(pick("house_bank")).upper(),
        "house_bank_account": _text(pick("house_bank_account")).upper(),
        "period": _text(p.get("period")),
        "amount_raw": p.get("amount"),
        "amount": parse_amount(p.get("amount")),
        "utr": _text(p.get("utr")),
        "item_text": _text(p.get("item_text"))[:50],
        "invoices": [
            {"invoice_ref": _text(i.get("invoice_ref")), "amount_raw": i.get("amount"),
             "amount": parse_amount(i.get("amount"))}
            for i in (p.get("invoices") or [])
            if isinstance(i, dict)
        ],
    }


def fill_from_connection(item: Dict[str, Any], cnx: Dict[str, Any], match_by: str,
                         sap_doc_ids: Optional[Callable[[List[str]], Dict[str, List[str]]]] = None
                         ) -> Tuple[List[str], List[str]]:
    """
    Take invoices / amount / UTR from active_cnx when the request leaves them out.
    match_by BELNR: the connection holds DataTwin invoice numbers, so each one is replaced by
    its SAP document number from active_bt3i.documentid (set by POST /api/invoice/post).
    """
    filled: List[str] = []
    errs: List[str] = []
    if not item["invoices"]:
        item["invoices"] = [
            {"invoice_ref": r["ref"], "amount_raw": r["amount"], "amount": r["amount"]}
            for r in cnx["invoices"]
        ]
        filled.append("invoices")
        if match_by == "BELNR" and item["invoices"] and sap_doc_ids is not None:
            ids = sap_doc_ids([i["invoice_ref"] for i in item["invoices"]])
            for inv in item["invoices"]:
                ref = inv["invoice_ref"]
                found = ids.get(ref.upper()) or []
                inv["connection_ref"] = ref
                if len(found) == 1:
                    inv["invoice_ref"] = found[0]
                elif not found:
                    inv["unmapped"] = True
                    errs.append(f"Invoice '{ref}' has no SAP document number in active_bt3i.documentid: "
                                f"post it first (POST /api/invoice/post) or send 'invoices'")
                else:
                    inv["unmapped"] = True
                    errs.append(f"Invoice '{ref}' has several SAP document numbers in active_bt3i "
                                f"({', '.join(found)}): send 'invoices'")
            filled.append("sap_document_numbers")
    if _blank(item["amount_raw"]) or not item["utr"]:
        if len(cnx["bank"]) != 1:
            errs.append(
                f"Connection has {len(cnx['bank'])} bank row(s); send 'amount' and 'utr' in the request"
            )
        else:
            bank = cnx["bank"][0]
            if _blank(item["amount_raw"]):
                item["amount_raw"], item["amount"] = bank["amount"], bank["amount"]
                filled.append("amount")
            if not item["utr"]:
                item["utr"] = bank["ref"]
                filled.append("utr")
    return filled, errs


def validate_clearing(item: Dict[str, Any], match_by: str) -> List[str]:
    errs: List[str] = []
    if item["posting_date"] is None:
        errs.append(f"Invalid/missing posting date: '{_text(item['posting_date_raw'])}'")
    if item["doc_date"] is None:
        errs.append(f"Invalid document date: '{_text(item['doc_date_raw'])}'")
    if not re.fullmatch(r"[A-Z0-9]{4}", item["company_code"]):
        errs.append(f"Invalid company code: '{item['company_code']}'")
    if not re.fullmatch(r"[A-Z]{3}", item["currency"]):
        errs.append(f"Invalid currency: '{item['currency']}'")
    if not item["customer"]:
        errs.append("Customer is missing")
    if not item["bank_gl"]:
        errs.append("Bank G/L account is missing")
    if item["amount"] is None or item["amount"] <= 0:
        errs.append(f"Invalid bank amount: '{_text(item['amount_raw'])}'")
    if len(item["utr"]) > 18:
        errs.append(f"UTR '{item['utr']}' is {len(item['utr'])} characters; SAP allows 18 (assignment)")
    if item["period"] and not (item["period"].isdigit() and 1 <= int(item["period"]) <= 16):
        errs.append(f"Invalid posting period: '{item['period']}'")

    invoices = item["invoices"]
    if not invoices:
        errs.append("No invoices: send 'invoices' or link invoices to the connection in active_cnx")
        return errs
    seen = set()
    total = Decimal("0")
    amounts_ok = True
    for n, inv in enumerate(invoices, start=1):
        ref = inv["invoice_ref"]
        if not ref:
            errs.append(f"Invoice {n}: invoice_ref is missing")
            amounts_ok = False
            continue
        if "," in ref:
            errs.append(f"Invoice '{ref}': ',' is not allowed in an invoice reference")
        if not inv.get("unmapped") and len(ref) > REF_MAX_LEN[match_by]:
            errs.append(f"Invoice '{ref}': {match_by} allows {REF_MAX_LEN[match_by]} characters")
        key = alpha(ref) if match_by == "BELNR" else ref.upper()
        if key in seen:
            errs.append(f"Invoice '{ref}' is listed twice")
        seen.add(key)
        if inv["amount"] is None or inv["amount"] <= 0:
            errs.append(f"Invoice '{ref}': invalid amount '{_text(inv['amount_raw'])}'")
            amounts_ok = False
        else:
            total += inv["amount"]
    if item["amount"] is not None and amounts_ok and total != item["amount"]:
        errs.append(
            f"Bank amount {item['amount']} != sum of invoice amounts {total} "
            f"(difference {item['amount'] - total}); partial clearing is not supported"
        )
    return errs


def check_invoices_in_sap(item: Dict[str, Any], index: OpenItemIndex, match_by: str) -> List[str]:
    """Per invoice: open in BSID for company + customer, same currency, open amount = invoice amount."""
    errs: List[str] = []
    if index.customers is not None:
        errs.extend(index.customers.check(item["customer"], item["company_code"]))
    bukrs, kunnr = item["company_code"], item["customer"]
    for inv in item["invoices"]:
        ref = inv["invoice_ref"]
        matches = index.find(bukrs, kunnr, match_by, ref)
        if not matches:
            cleared = index.find_cleared(bukrs, kunnr, match_by, ref)
            if cleared:
                by = sorted({f"{_text(m.get('belnr'))} by {_text(m.get('augbl'))} on {_text(m.get('augdt'))}"
                             for m in cleared})
                inv["sap_status"] = "ALREADY_CLEARED"
                inv["message"] = f"Already cleared in SAP: document {', '.join(by)}"
            else:
                inv["sap_status"] = "NOT_FOUND"
                inv["message"] = (f"No open item in SAP for company {bukrs}, customer {kunnr} "
                                  f"with {match_by} = '{ref}'")
            errs.append(f"Invoice '{ref}': {inv['message']}")
            continue

        problems = []
        currencies = {str(m.get("waers", "")).upper() for m in matches}
        if currencies != {item["currency"]}:
            problems.append(f"currency {', '.join(sorted(currencies))} in SAP, {item['currency']} in request")
        open_amt = sum(
            (Decimal(str(m.get("wrbtr") or 0)) * (1 if str(m.get("shkzg")) == "S" else -1) for m in matches),
            Decimal("0"),
        ).quantize(Decimal("0.01"))
        if inv["amount"] is not None and open_amt != inv["amount"]:
            problems.append(f"open amount in SAP {open_amt}, request {inv['amount']}")
        inv["open_amount"] = float(open_amt)
        inv["open_items"] = [
            {"belnr": _text(m.get("belnr")), "gjahr": _text(m.get("gjahr")), "buzei": _text(m.get("buzei")),
             "xblnr": _text(m.get("xblnr")), "zuonr": _text(m.get("zuonr")),
             "amount": float(m.get("wrbtr") or 0), "dc": m.get("shkzg")}
            for m in matches
        ]
        if problems:
            inv["sap_status"] = "MISMATCH"
            inv["message"] = "; ".join(problems)
            errs.append(f"Invoice '{ref}': {inv['message']}")
        else:
            inv["sap_status"] = "OPEN"
            inv["message"] = "Open in SAP, amount matches"
    return errs


# ---------------------------------------------------------------------------
# SAP posting (RFC Z_BANK_POST_CLEAR_MULTI)
# ---------------------------------------------------------------------------
class SAPClearingClient(SAPBankPostingClient):
    def __init__(self, cfg: Dict[str, Any]):
        super().__init__({**cfg, "rfc_function": cfg["clearing_rfc_function"]})

    def post_connection(self, item: Dict[str, Any], match_by: str) -> Dict[str, Any]:
        def d(v: Optional[date]) -> Optional[str]:
            return v.isoformat() if v else None

        utr = item["utr"]
        params = {
            "IV_BUKRS": item["company_code"],
            "IV_KUNNR": item["customer"],
            "IV_BLDAT": d(item["doc_date"]),
            "IV_BUDAT": d(item["posting_date"]),
            "IV_VALUT": d(item["value_date"]),
            "IV_BLART": item["doc_type"],
            "IV_MONAT": item["period"],
            "IV_WAERS": item["currency"],
            "IV_HKONT": item["bank_gl"],
            "IV_WRBTR": str(item["amount"]),
            "IV_MATCH_BY": match_by,
            "IV_INVOICE_REFS": ",".join(i["invoice_ref"] for i in item["invoices"]),
            # XBLNR (16) holds the UTR and drives SAP's duplicate check; ZUONR (18) always gets it
            "IV_UTR": utr if utr and len(utr) <= 16 else None,
            "IV_ZUONR": utr or None,
            "IV_SGTXT": item["item_text"] or f"{item['connection_id']} {utr}".strip()[:50],
            "IV_HBKID": item["house_bank"] or None,
            "IV_HKTID": item["house_bank_account"] or None,
        }
        try:
            resp = self._call(self.fm, params)
        except Exception as e:
            return {"Status": "E", "Message": str(e)}
        out = {el.tag: (el.text or "").strip() for el in resp}
        augdt = out.get("EV_AUGDT", "")
        return {
            "Status": out.get("EV_STATUS"),
            "Belnr": out.get("EV_BELNR") or None,
            "Gjahr": out.get("EV_GJAHR") if out.get("EV_GJAHR") not in ("", "0000") else None,
            "Augbl": out.get("EV_AUGBL") or None,
            "Augdt": augdt if augdt and not augdt.startswith("0000") else None,
            "Cleared": int(out.get("EV_CLEARED") or 0),
            "Message": out.get("EV_MESSAGE"),
        }


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------
def process_clearing(payload: Dict[str, Any], dry_run: bool = False, update_db: bool = True,
                     match_by: Optional[str] = None) -> Dict[str, Any]:
    cfg = load_bank_config()
    column = _check_column_name(cfg["cnx_clearing_column"])
    match_by = (match_by or cfg["clearing_match_by"]).upper()
    if match_by not in MATCH_FIELDS:
        raise ValueError(f"match_by must be one of {MATCH_FIELDS}")

    org = org_settings(_text(payload.get("org_id")))
    from_config = sorted(k for k in ("company_code", "customer", "bank_gl", "currency", "doc_type",
                                     "house_bank", "house_bank_account")
                         if _blank(payload.get(k)) and org.get(k))
    if _blank(payload.get("value_date")) and is_true(org.get("value_date_required")):
        from_config.append("value_date (= posting_date)")
    item = normalise_clearing(payload, cfg, org)
    if not item["org_id"] or not item["connection_id"]:
        raise ValueError("org_id and connection_id (active_cnx.documentid) are required")

    started = datetime.now()
    batch_id = f"CLR{started:%Y%m%d%H%M%S}-{uuid.uuid4().hex[:4].upper()}"
    log = f"[CLEARING] {batch_id}"
    logger.info(
        f"{log}: start, org {item['org_id']}, connection {item['connection_id']}, dry_run={dry_run}, "
        f"update_db={update_db}, match_by={match_by}, company={item['company_code']} customer={item['customer']} "
        f"bank_gl={item['bank_gl']} posting_date={item['posting_date']} amount={item['amount']} {item['currency']} "
        f"utr={item['utr'] or '-'} invoices={[(i['invoice_ref'], str(i['amount'])) for i in item['invoices']] or 'from connection'}"
    )

    # 1. connection
    try:
        cnx = read_connection(item["org_id"], item["connection_id"], column)
    except Exception as e:
        logger.exception(f"{log}: could not read {CNX_TABLE}: {e}")
        raise RuntimeError(f"Could not read {CNX_TABLE}: {e}") from e
    logger.info(f"{log}: connection rows={len(cnx['rows'])} (invoice {len(cnx['invoices'])}, bank {len(cnx['bank'])}), "
                f"{column} exists={cnx['column_exists']}, already cleared by={cnx['cleared_by'] or '-'}, "
                f"customer names={cnx['customer_names'] or '-'}")
    if from_config:
        logger.info(f"{log}: from config [org:{item['org_id']}]: {', '.join(from_config)}")

    errs: List[str] = []
    warnings: List[str] = []
    filled: List[str] = []
    customer_from: Optional[str] = None

    # SAP customer: request > [org:..] customer > [customers:..] mapping of the connection's customer name
    if not item["customer"] and cnx["rows"]:
        names = cnx["customer_names"]
        if len(names) == 1:
            sap_no = sap_customer_for(item["org_id"], names[0])
            if sap_no:
                item["customer"] = alpha(sap_no)
                customer_from = f"config [customers:{item['org_id']}] '{names[0]}' = {item['customer']}"
                logger.info(f"{log}: customer {item['customer']} from {customer_from}")
            else:
                errs.append(f"No SAP customer for '{names[0]}': add '{names[0]} = <SAP customer>' under "
                            f"[customers:{item['org_id']}] in config.ini, or send 'customer'")
        elif len(names) > 1:
            errs.append(f"Connection has several customer names ({', '.join(names)}): send 'customer'")

    if not cnx["rows"]:
        errs.append(f"Connection '{item['connection_id']}' not found in {CNX_TABLE} for org {item['org_id']}")
    else:
        from bank_posting.invoice_post import bt3i_document_ids

        filled, fill_errs = fill_from_connection(
            item, cnx, match_by, sap_doc_ids=lambda refs: bt3i_document_ids(item["org_id"], refs)
        )
        errs.extend(fill_errs)
        if filled:
            logger.info(
                f"{log}: from connection {filled}: amount={item['amount']} utr={item['utr']} invoices="
                + ", ".join(f"{i.get('connection_ref', i['invoice_ref'])}->{i['invoice_ref']} {i['amount']}"
                            for i in item["invoices"])
            )
        if cnx["cleared_by"]:
            errs.append(f"Connection is already cleared in SAP: {column} = {', '.join(cnx['cleared_by'])}")
    if update_db and not cnx["column_exists"]:
        errs.append(
            f"Column {CNX_TABLE}.{column} does not exist, so the clearing document cannot be saved. "
            f"Create it first (ALTER TABLE public.{CNX_TABLE} ADD COLUMN {column} text) "
            f"or send update_db = false"
        )

    # 2. format + amounts
    errs.extend(validate_clearing(item, match_by))
    if item["utr"] and 16 < len(item["utr"]) <= 18:
        warnings.append(
            f"UTR '{item['utr']}' is longer than 16 characters: saved in the assignment (ZUONR) only, "
            f"SAP's duplicate check by UTR is skipped"
        )

    # 3. SAP open items
    sap_tables: List[str] = []
    if not errs:
        sap_tables = configured_sap_tables()[0]
        if "BSAD" not in sap_tables:
            sap_tables.append("BSAD")
        try:
            index = OpenItemIndex(sap_tables)
        except Exception as e:
            logger.exception(f"{log}: could not read {', '.join(sap_tables)} from SAP: {e}")
            raise RuntimeError(f"Could not read {', '.join(sap_tables)} from SAP: {e}") from e
        errs.extend(check_invoices_in_sap(item, index, match_by))
        for inv in item["invoices"]:
            logger.info(f"{log}: SAP check {inv['invoice_ref']}: {inv.get('sap_status')} - {inv.get('message')}")

    result = "INVALID" if errs else "VALID"
    message = "; ".join(errs) if errs else "Validation passed"
    logger.info(f"{log}: checks {result} - {message}" + (f" | warnings: {'; '.join(warnings)}" if warnings else ""))
    sap_status = "skipped (dry_run)" if dry_run else ("skipped (invalid)" if errs else None)
    posted: Dict[str, Any] = {}

    # 4. post + clear
    if not dry_run and not errs:
        client = SAPClearingClient(cfg)
        ok, msg = client.available()
        if not ok:
            logger.warning(f"{log}: {msg}")
            result, message, sap_status = "NOT_POSTED", msg, msg
        else:
            sap_status = "ok"
            logger.info(f"{log}: POSTING in SAP ({client.fm}) amount={item['amount']} {item['currency']} "
                        f"invoices={[i['invoice_ref'] for i in item['invoices']]}")
            posted = client.post_connection(item, match_by)
            result = {"S": "SUCCESS", "W": "POSTED_NOT_CLEARED"}.get(posted.get("Status"), "ERROR")
            message = posted.get("Message") or ("Posted" if result == "SUCCESS" else "Unknown SAP error")
            logger.info(f"{log} SAP: {result} document={posted.get('Belnr')}/{posted.get('Gjahr')} "
                        f"clearing={posted.get('Augbl')} cleared={posted.get('Cleared')} - {message}")

    clearing_doc = posted.get("Augbl") or posted.get("Belnr")

    # 5. write the clearing document to active_cnx
    db_update: Dict[str, Any] = {"skipped": "dry_run" if dry_run else "disabled"}
    if not dry_run and update_db:
        if result in ("SUCCESS", "POSTED_NOT_CLEARED") and clearing_doc:
            try:
                db_update = write_clearing_doc(item["org_id"], item["connection_id"], clearing_doc, column)
            except Exception as e:
                logger.exception(f"{log}: SAP document {clearing_doc} posted, "
                                 f"but {CNX_TABLE}.{column} update failed: {e}")
                db_update = {"error": str(e), "sap_document": clearing_doc}
        else:
            db_update = {"skipped": "nothing posted"}

    logger.info(f"{log}: done in {(datetime.now() - started).total_seconds():.1f}s, result={result}, "
                f"clearing_doc={clearing_doc or '-'}, db={db_update}")
    return {
        "status": "completed",
        "batch_id": batch_id,
        "dry_run": dry_run,
        "result": result,
        "message": message,
        "org_id": item["org_id"],
        "connection_id": item["connection_id"],
        "match_by": match_by,
        "company_code": item["company_code"],
        "customer": item["customer"],
        "bank_gl": item["bank_gl"],
        "currency": item["currency"],
        "amount": float(item["amount"]) if item["amount"] is not None else None,
        "utr": item["utr"] or None,
        "posting_date": item["posting_date"].isoformat() if item["posting_date"] else None,
        "value_date": item["value_date"].isoformat() if item["value_date"] else None,
        "invoices": [
            {k: v for k, v in {**inv, "amount": float(inv["amount"]) if inv["amount"] is not None else None}.items()
             if k != "amount_raw"}
            for inv in item["invoices"]
        ],
        "filled_from_connection": filled,
        "from_config": from_config,
        **({"customer_from": customer_from} if customer_from else {}),
        "connection": {
            "rows": len(cnx["rows"]),
            "invoice_rows": len(cnx["invoices"]),
            "bank_rows": len(cnx["bank"]),
            "status": sorted({r["status"] for r in cnx["rows"] if r["status"]}),
            "clearing_column": column,
            "column_exists": cnx["column_exists"],
        },
        "document_no": posted.get("Belnr"),
        "fiscal_year": posted.get("Gjahr"),
        "clearing_doc": clearing_doc,
        "clearing_date": posted.get("Augdt"),
        "cleared_invoices": posted.get("Cleared") if posted else None,
        "sap_posting": sap_status,
        "sap_tables": sap_tables,
        "db_update": db_update,
        **({"warnings": warnings} if warnings else {}),
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }
