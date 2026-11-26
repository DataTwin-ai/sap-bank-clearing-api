"""
SAP auto-match: bank receipts -> SAP open invoices -> connection -> clearing.

POST /api/clearing/match
    1. Bank receipts of the org: public.active_bs, credit > 0, not posted in SAP yet
       (documentid empty). Optional date range / bank_refs. A receipt that already has a
       connection in active_cnx is reported (CONNECTED) and not matched again.
    2. SAP open invoices: live BSID (customer items, no special G/L), customer names from KNA1,
       only in the org's company codes:
         [org:<org_id>] company_codes in config.ini, else the bukrs the SAP extraction stored
         for the org (active_bt3h / active_bt3i), else every company code.
       Invoices that are already in an open connection of the org are left out.
    3. Per receipt, with the bank narration (transactiondescription + remittername):
         evidence  = an SAP customer name ([customers:<org_id>] names too) or an invoice
                     reference (BELNR / XBLNR / ZUONR / VBELN) found in the narration
         amount    = one invoice, or 2..max_invoices invoices of ONE customer, adding up to
                     exactly the bank amount; same currency; invoice posted on/before the bank date
       Result per receipt:
         MATCHED      evidence + amount, one answer only               -> can be connected and cleared
         REVIEW       evidence + amount, several answers (e.g. invoices with the same amount)
         AMOUNT_ONLY  an invoice has the amount, nothing in the narration names it: never connected
         NO_MATCH     nothing fits
         CONNECTED    the receipt already has a connection
    4. dry_run = false and create_connections = true: MATCHED receipts are written to active_cnx in
       the AR Inbox layout (invoice rows with the SAP document number + customer account, one bank
       row; documentid = bank transactionnumber).
    5. clear = true: the /api/clearing/post logic for each MATCHED receipt (company code, customer and
       invoices sent explicitly). dry_run = true validates against SAP with the connection as it would
       be written; dry_run = false posts, and only for receipts listed in bank_refs.

dry_run = true (default): nothing is written to the database and nothing is posted in SAP.
"""

import re
import uuid
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Tuple

from psycopg2 import sql

from bank_posting.clearing import CNX_SCHEMA, CNX_TABLE, _check_column_name, _connect, process_clearing
from bank_posting.posting import (
    OpenItemIndex,
    _plain_config,
    _org_section,
    _text,
    alpha,
    configured_sap_tables,
    load_bank_config,
    logger,
    org_settings,
    parse_date,
    sap_defaults,
)

BANK_TABLE = "active_bs"
CENT = Decimal("0.01")
MARKER = "SAP_AUTO_MATCH"          # active_cnx.linkreftype of the rows this module writes
MAX_INVOICES_LIMIT = 10
SEARCH_BUDGET = 200_000            # subset-search steps per customer, then the receipt goes to REVIEW
MIN_REF_LEN = 5                    # shorter references (e.g. '1', 'TEST') prove nothing

# words that do not identify a customer (legal forms, fillers, bank truncations of them)
STOPWORDS = {
    "PRIVATE", "PRIVA", "PRIVAT", "PVT", "PTE", "LIMITED", "LIMITE", "LIMIT", "LIMI", "LTD", "LT",
    "CO", "COMPANY", "CORP", "CORPORATION", "INC", "LLP", "LLC", "PLC", "GMBH", "AG", "SA",
    "AND", "THE", "OF", "M", "S", "MS",
}


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------
def _tokens(text: Any) -> List[str]:
    return [t for t in re.split(r"[^A-Z0-9]+", str(text or "").upper()) if t]


def name_tokens(name: Any) -> List[str]:
    """Tokens that identify a customer name: no legal forms, at least 2 characters."""
    return [t for t in _tokens(name) if t not in STOPWORDS and len(t) >= 2]


NAME_NONE, NAME_WEAK, NAME_STRONG = 0, 1, 2


def name_match(name: Any, text_tokens: Set[str], text_list: List[str]) -> int:
    """
    How well the narration names this customer:
      NAME_STRONG  every identifying token (2 or more) is a word of the narration
      NAME_WEAK    the name has ONE identifying token (4+ characters) and it is a word of the narration
                   (e.g. SHANKAR: common words / first names, so never enough on its own)
      NAME_NONE    otherwise
    Bank narrations cut long names, so the LAST token may appear shortened (4+ characters and at least
    half of it), right after the token before it: MEDIACOM COMMUNICATI.
    """
    toks = name_tokens(name)
    if not toks or (len(toks) == 1 and len(toks[0]) < 4):
        return NAME_NONE
    for i, t in enumerate(toks):
        if t in text_tokens:
            continue
        if i == len(toks) - 1 and i > 0:
            prev = toks[i - 1]
            if any(text_list[p - 1] == prev and len(w) >= max(4, (len(t) + 1) // 2) and t.startswith(w)
                   for p, w in enumerate(text_list) if p > 0):
                continue
        return NAME_NONE
    return NAME_STRONG if len(toks) >= 2 else NAME_WEAK


def _ref_keys(value: Any) -> Set[str]:
    """
    Forms of a reference that are compared with the narration: as is, and without leading zeros.
    A reference needs a digit and 5+ characters ('TESTING', '1' prove nothing).
    """
    v = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
    out: Set[str] = set()
    if not re.search(r"\d", v):
        return out
    if len(v) >= MIN_REF_LEN:
        out.add(v)
    if v.isdigit():
        s = v.lstrip("0")
        if len(s) >= 6:
            out.add(s)
    return out


def _text_ref_keys(tokens: List[str]) -> Set[str]:
    """Words of the narration and every digit run inside them (with and without leading zeros)."""
    out: Set[str] = set()
    for t in tokens:
        out.add(t)
        for run in re.findall(r"\d+", t):
            out.add(run)
            if run.lstrip("0"):
                out.add(run.lstrip("0"))
    return out


def _dec(v: Any) -> Optional[Decimal]:
    try:
        return Decimal(str(v)).quantize(CENT) if v not in (None, "") else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Database reads
# ---------------------------------------------------------------------------
def org_company_codes(org_id: str, env: Optional[str] = None) -> Tuple[Optional[List[str]], str]:
    """
    SAP company codes of the org: [org:<org_id>] company_codes, else the bukrs stored by the SAP
    extraction for the org (active_bt3h / active_bt3i) plus [org:] company_code, else None (= all).
    """
    org = org_settings(org_id)
    raw = org.get("company_codes", "")
    if raw.strip():
        codes = sorted({c.strip().upper() for c in re.split(r"[,\s;]+", raw) if c.strip()})
        return codes, f"config [org:{org_id}] company_codes"
    conn = _connect(env)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT upper(trim(bukrs::text)) FROM public.active_bt3h "
                "WHERE orgid = %s AND coalesce(trim(bukrs::text), '') <> '' "
                "UNION SELECT DISTINCT upper(trim(bukrs::text)) FROM public.active_bt3i "
                "WHERE orgid = %s AND coalesce(trim(bukrs::text), '') <> ''",
                (org_id, org_id),
            )
            codes = {r[0] for r in cur.fetchall() if r[0]}
    finally:
        conn.close()
    if org.get("company_code"):
        codes.add(org["company_code"].strip().upper())
    if codes:
        return sorted(codes), "DataTwin tables (bukrs stored by the SAP extraction for this org)"
    return None, "all company codes (none configured or stored for this org)"


def load_bank_receipts(org_id: str, date_from: Optional[date] = None, date_to: Optional[date] = None,
                       bank_refs: Optional[List[str]] = None, env: Optional[str] = None) -> List[Dict[str, Any]]:
    """Credit rows of active_bs for the org that are not posted in SAP yet (documentid empty)."""
    where = ["orgid = %s", "coalesce(credit, 0) > 0", "coalesce(trim(documentid), '') = ''"]
    args: List[Any] = [org_id]
    if date_from:
        where.append("transactiondate >= %s")
        args.append(date_from)
    if date_to:
        where.append("transactiondate <= %s")
        args.append(date_to)
    refs = sorted({r.strip().upper() for r in (bank_refs or []) if r and r.strip()})
    if refs:
        where.append("(upper(trim(transactionref)) = ANY(%s) OR upper(trim(transactionnumber)) = ANY(%s) "
                     "OR upper(trim(utr)) = ANY(%s))")
        args += [refs, refs, refs]
    conn = _connect(env)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT accountnumber, transactiondate, sno, credit, transactioncurrency, transactionref, "
                "utr, transactionnumber, transactiondescription, remittername, valuedate "
                f"FROM public.{BANK_TABLE} WHERE " + " AND ".join(where)
                + " ORDER BY transactiondate, length(sno), sno",
                args,
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        narration = " ".join(x for x in (_text(r[8]), _text(r[9])) if x)
        out.append({
            "account": _text(r[0]), "date": r[1], "sno": _text(r[2]), "amount": _dec(r[3]),
            "currency": _text(r[4]).upper(), "utr": _text(r[5]) or _text(r[6]),
            "transaction_number": _text(r[7]), "narration": narration, "value_date": r[10],
        })
    return out


def request_receipts(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Bank receipts sent in the request (bank_items) instead of read from active_bs: to try the matching
    before the bank statement is loaded. Checked with dry_run only - nothing is written for them.
    """
    out = []
    for n, it in enumerate(items, start=1):
        if not isinstance(it, dict):
            raise ValueError(f"bank_items[{n}] must be an object")
        day = parse_date(it.get("date"))
        amount = _dec(it.get("amount"))
        utr = _text(it.get("utr"))
        if day is None or amount is None or amount <= 0 or not utr:
            raise ValueError(f"bank_items[{n}] needs date (YYYY-MM-DD), amount > 0 and utr")
        out.append({
            "account": _text(it.get("account")), "date": day, "sno": str(n), "amount": amount,
            "currency": _text(it.get("currency")).upper(), "utr": utr,
            "transaction_number": _text(it.get("transaction_number")) or f"REQUEST/{day:%Y%m%d}/{n}",
            "narration": " ".join(_text(it.get("narration")).split()), "value_date": day,
        })
    return out


def load_connections(org_id: str, column: str, env: Optional[str] = None) -> Dict[str, Any]:
    """
    Connections of the org in active_cnx:
      by_id       documentid -> {"cleared_by": [...], "rows": n}
      bank_refs   bank row reference (upper) -> documentid
      open_invoices  invoice references in connections that are not cleared yet:
                     {(ref, customer account or '')}  (ref = SAP number without leading zeros)
    """
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
            cur.execute(
                sql.SQL("SELECT documentid, source, connectingdocumentnumber, customeraccount, {clr} "
                        "FROM {tbl} WHERE orgid = %s").format(
                    clr=sql.SQL("coalesce(trim({}), '')").format(sql.Identifier(column)) if has_col
                    else sql.Literal(""),
                    tbl=sql.Identifier(CNX_SCHEMA, CNX_TABLE)),
                (org_id,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    by_id: Dict[str, Dict[str, Any]] = {}
    bank_refs: Dict[str, str] = {}
    open_inv: Set[Tuple[str, str]] = set()
    for doc_id, source, ref, account, cleared in rows:
        doc_id = _text(doc_id)
        e = by_id.setdefault(doc_id, {"cleared_by": set(), "rows": 0})
        e["rows"] += 1
        if cleared:
            e["cleared_by"].add(cleared)
    for doc_id, source, ref, account, cleared in rows:
        doc_id, ref, src = _text(doc_id), _text(ref).upper(), _text(source).lower()
        if src == "bank" and ref:
            bank_refs.setdefault(ref, doc_id)
        elif src == "invoice" and ref and not by_id[doc_id]["cleared_by"]:
            key = ref.lstrip("0") if ref.isdigit() else ref
            open_inv.add((key, alpha(account) if _text(account) else ""))
    return {"column_exists": has_col,
            "by_id": {k: {"cleared_by": sorted(v["cleared_by"]), "rows": v["rows"]} for k, v in by_id.items()},
            "bank_refs": bank_refs, "open_invoices": open_inv}


# ---------------------------------------------------------------------------
# SAP open invoices
# ---------------------------------------------------------------------------
def open_invoices(index: OpenItemIndex, company_codes: Optional[List[str]],
                  connected: Set[Tuple[str, str]]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """
    BSID customer lines grouped per (company code, customer, document number) the way the clearing
    checks them (debit - credit). Left out: special G/L items, other company codes, documents already
    in an open connection, a number in several fiscal years, mixed currencies, net amount <= 0.
    """
    skipped: Dict[str, int] = defaultdict(int)
    groups: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    allowed = set(company_codes) if company_codes else None
    for r in index.rows:
        bukrs = _text(r.get("bukrs")).upper()
        if _text(r.get("umskz")):
            skipped["special G/L item (down payment etc.)"] += 1
            continue
        if allowed is not None and bukrs not in allowed:
            skipped["other company code"] += 1
            continue
        groups[(bukrs, alpha(r.get("kunnr")), alpha(r.get("belnr")))].append(r)

    names = index.customers.kna1 if index.customers is not None and index.customers.kna1 else {}
    out: List[Dict[str, Any]] = []
    for (bukrs, kunnr, belnr), lines in groups.items():
        short = belnr.lstrip("0")
        if (short, kunnr) in connected or (short, "") in connected:
            skipped["already in an open connection"] += 1
            continue
        years = {_text(x.get("gjahr")) for x in lines}
        currencies = {_text(x.get("waers")).upper() for x in lines}
        if len(years) > 1:
            skipped["same document number in several fiscal years"] += 1
            continue
        if len(currencies) != 1:
            skipped["mixed currencies"] += 1
            continue
        net = sum(((Decimal(str(x.get("wrbtr") or 0)) * (1 if _text(x.get("shkzg")) == "S" else -1))
                   for x in lines), Decimal("0")).quantize(CENT)
        if net <= 0:
            skipped["credit or zero balance"] += 1
            continue
        first = min(lines, key=lambda x: _text(x.get("buzei")))
        refs: Set[str] = set()
        for x in lines:
            refs |= _ref_keys(x.get("belnr")) | _ref_keys(x.get("xblnr")) | _ref_keys(x.get("vbeln"))
            z = _text(x.get("zuonr"))
            if z and alpha(z) != kunnr:          # ZUONR often holds the customer number
                refs |= _ref_keys(z)
        cust = names.get(kunnr) or {}
        out.append({
            "company_code": bukrs,
            "customer": kunnr,
            "customer_name": " ".join(_text(cust.get("name1")).split()),
            "belnr": belnr,
            "gjahr": next(iter(years)),
            "xblnr": _text(first.get("xblnr")),
            "zuonr": _text(first.get("zuonr")),
            "doc_type": _text(first.get("blart")),
            "posting_date": parse_date(first.get("budat")),
            "currency": next(iter(currencies)),
            "amount": net,
            "refs": refs,
        })
    out.sort(key=lambda i: (i["posting_date"] or date.max, i["belnr"]))
    return out, dict(skipped)


def subsets_with_sum(items: List[Dict[str, Any]], target: Decimal, max_size: int,
                     limit: int = 20) -> Tuple[List[List[Dict[str, Any]]], bool]:
    """
    Sets of 1..max_size items whose amounts add up to target. Items are in posting-date order,
    so the first set found takes the oldest invoices. Returns (sets, search_complete).
    """
    found: List[List[Dict[str, Any]]] = []
    steps = 0
    cut = False
    pool = [i for i in items if i["amount"] <= target]

    def walk(start: int, chosen: List[Dict[str, Any]], total: Decimal) -> None:
        nonlocal steps, cut
        for j in range(start, len(pool)):
            if len(found) >= limit or steps >= SEARCH_BUDGET:
                cut = True
                return
            steps += 1
            s = total + pool[j]["amount"]
            if s == target:
                found.append(chosen + [pool[j]])
            elif s < target and len(chosen) + 1 < max_size:
                walk(j + 1, chosen + [pool[j]], s)

    walk(0, [], Decimal("0"))
    return found, not cut


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
def _invoice_view(i: Dict[str, Any]) -> Dict[str, Any]:
    return {"belnr": i["belnr"], "gjahr": i["gjahr"], "xblnr": i["xblnr"] or None, "doc_type": i["doc_type"],
            "posting_date": i["posting_date"].isoformat() if i["posting_date"] else None,
            "amount": float(i["amount"]), "currency": i["currency"]}


def _option(invoices: List[Dict[str, Any]], evidence: List[str]) -> Dict[str, Any]:
    first = invoices[0]
    return {
        "company_code": first["company_code"],
        "customer": first["customer"],
        "customer_name": first["customer_name"],
        "invoices": [_invoice_view(i) for i in invoices],
        "invoice_total": float(sum((i["amount"] for i in invoices), Decimal("0"))),
        "evidence": evidence,
    }


def configured_names(org_id: str) -> Dict[str, str]:
    """[customers:<org_id>] name -> SAP customer (10 digits)."""
    cfg = _plain_config()
    sec = _org_section(cfg, "customers", org_id)
    return {k: alpha(v) for k, v in cfg[sec].items() if v.strip()} if sec else {}


def match_receipt(bank: Dict[str, Any], invoices: List[Dict[str, Any]], by_customer: Dict[Tuple[str, str], List[Dict[str, Any]]],
                  config_names: Dict[str, str], bank_currency: str, max_invoices: int) -> Dict[str, Any]:
    """Find the invoices one bank receipt pays (see module docstring for the rules)."""
    amount: Decimal = bank["amount"]
    words = _tokens(bank["narration"])
    word_set = set(words)
    ref_words = _text_ref_keys(words)

    def usable(i: Dict[str, Any]) -> bool:
        return i["currency"] == bank_currency and (i["posting_date"] is None or i["posting_date"] <= bank["date"])

    # evidence: customers named in the narration (SAP name, config name), invoices referenced in it
    named: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    strength: Dict[Tuple[str, str], int] = defaultdict(int)
    for key, items in by_customer.items():
        nm = items[0]["customer_name"]
        level = name_match(nm, word_set, words) if nm else NAME_NONE
        if level:
            named[key].append(f"customer name '{nm}' in the bank narration"
                              + (" (one word only)" if level == NAME_WEAK else ""))
            strength[key] = max(strength[key], level)
    for cname, kunnr in config_names.items():
        level = name_match(cname, word_set, words)
        if level:
            for key in by_customer:
                if key[1] == kunnr:
                    named[key].append(f"config [customers] name '{cname}' (= {kunnr}) in the bank narration"
                                      + (" (one word only)" if level == NAME_WEAK else ""))
                    strength[key] = max(strength[key], level)
    referenced = [i for i in invoices if i["refs"] & ref_words]

    options: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, ...]] = set()
    cut = False

    def add(sel: List[Dict[str, Any]], evidence: List[str], strong: bool) -> None:
        key = tuple(sorted(f"{i['company_code']}/{i['customer']}/{i['belnr']}" for i in sel))
        if key not in seen:
            seen.add(key)
            options.append({**_option(sel, evidence), "strong": strong})

    # 1. invoices whose reference is in the narration (per customer). Strong, unless the narration
    #    names a DIFFERENT customer.
    ref_groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for i in referenced:
        ref_groups[(i["company_code"], i["customer"])].append(i)
    for key, items in ref_groups.items():
        items = [i for i in items if usable(i)]
        sets, complete = subsets_with_sum(items, amount, max_invoices)
        cut = cut or not complete
        for s in sets:
            ev = [f"invoice {i['belnr']}{' / ' + i['xblnr'] if i['xblnr'] else ''} referenced in the bank narration"
                  for i in s] + named.get(key, [])
            if named and key not in named:
                ev.append("but the narration names another customer")
            add(s, ev, strong=not named or key in named)

    # 2. customers named in the narration: their open invoices that add up to the amount
    if not options:
        for key, evidence in named.items():
            items = [i for i in by_customer[key] if usable(i)]
            sets, complete = subsets_with_sum(items, amount, max_invoices)
            cut = cut or not complete
            for s in sets:
                add(s, list(evidence), strong=strength[key] >= NAME_STRONG)

    if options:
        # fewest invoices first (one invoice with exactly the amount beats several), then the oldest
        options.sort(key=lambda o: len(o["invoices"]))
        best = options[0]
        tail = f"invoice total = bank amount {amount} {bank_currency}"
        if len(options) == 1 and not cut and best["strong"]:
            return {"status": "MATCHED", "confidence": "HIGH", "match": best,
                    "reason": "; ".join(best["evidence"]) + f"; {tail}"}
        if len(options) == 1 and not cut:
            return {"status": "REVIEW", "confidence": "MEDIUM", "suggested": best, "options": options,
                    "option_count": 1,
                    "reason": "; ".join(best["evidence"]) + f"; {tail} - the evidence is not enough on its own: "
                              "check it and connect it in AR Inbox, or send it to /api/clearing/post"}
        return {"status": "REVIEW", "confidence": "MEDIUM", "suggested": best, "options": options[:5],
                "option_count": len(options) if not cut else f"{len(options)}+",
                "reason": (f"{len(options)}{'+' if cut else ''} invoice sets add up to "
                           f"{amount} {bank_currency}; the suggestion takes the fewest, then the oldest invoices - pick one and "
                           f"connect it in AR Inbox, or send it to /api/clearing/post")}

    # 3. nothing in the narration: single invoices with exactly the amount (never connected)
    same = [i for i in invoices if i["amount"] == amount and usable(i)] if not named and not referenced else []
    if same:
        return {"status": "AMOUNT_ONLY", "confidence": "LOW", "options": [_option([i], ["amount only"]) for i in same[:5]],
                "option_count": len(same),
                "reason": ("open invoice(s) with the same amount, but the bank narration names neither the customer "
                           "nor the invoice: not connected")}

    # 4. no match: say why
    if referenced and not named:
        refs = ", ".join(sorted({f"{i['company_code']}/{i['belnr']} ({i['amount']} {i['currency']})"
                                 for i in referenced}))
        return {"status": "NO_MATCH", "confidence": None,
                "reason": f"invoice(s) {refs} referenced in the bank narration, but they do not add up to "
                          f"{amount} {bank_currency} (partial payments are not cleared)"}
    if named:
        lines = []
        for key in named:
            items = by_customer[key]
            ok = [i for i in items if usable(i)]
            total = sum((i["amount"] for i in ok), Decimal("0"))
            lines.append(f"{items[0]['customer_name']} ({key[0]} / {key[1]}): {len(ok)} usable open invoice(s), "
                         f"total {total} {bank_currency}")
        return {"status": "NO_MATCH", "confidence": None,
                "reason": ("customer named in the narration, but no set of up to "
                           f"{max_invoices} open invoices adds up to {amount} {bank_currency} (partial payments "
                           "are not cleared): " + "; ".join(lines))}
    other_ccy = [i for i in invoices if i["amount"] == amount and i["currency"] != bank_currency]
    hint = (f"; {len(other_ccy)} open invoice(s) have this amount in another currency "
            f"({', '.join(sorted({i['currency'] for i in other_ccy}))})") if other_ccy else ""
    return {"status": "NO_MATCH", "confidence": None,
            "reason": "no SAP customer or invoice named in the bank narration and no open invoice with "
                      f"{amount} {bank_currency}{hint}"}


# ---------------------------------------------------------------------------
# Writing the connection (AR Inbox layout)
# ---------------------------------------------------------------------------
CNX_INSERT_COLUMNS = (
    "connectingdocumentnumber", "connectinggroupref", "customeraccount", "customername", "documentdate",
    "documentid", "documentstatus", "documenttype", "documentvalue", "forprdfrom", "forprdto", "inprdfrom",
    "inprdto", "internaldocumentid", "linkref", "linkref1", "linkref2", "linkref3", "linkreftype", "orgid",
    "orgname", "pk", "postingdate", "sk", "source", "sourceamount", "transactionstatus", "transactiontype",
    "uuid", "vendoraccount", "vendorname", "sno", "themename", "processcode", "checksum", "input", "output",
    "transactionid", "basevalue", "percentageoftds", "tdsvalue", "taxpercentage", "taxvalue", "sectionname",
    "tablecode",
)


def connection_rows(org_id: str, bank: Dict[str, Any], match: Dict[str, Any], context_id: str) -> List[Dict[str, Any]]:
    """active_cnx rows for one match: one row per invoice (sno 1..n) and the bank row (sno n+1)."""
    now = datetime.now().replace(microsecond=0)
    blank = {c: "" for c in CNX_INSERT_COLUMNS}
    common = {
        **blank,
        "connectinggroupref": bank["transaction_number"], "documentid": bank["transaction_number"],
        "documentstatus": "fully_cleared", "forprdfrom": date(1900, 1, 1), "forprdto": date(1900, 1, 1),
        "inprdfrom": now, "inprdto": datetime(2100, 12, 31), "postingdate": date(1900, 1, 1),
        "linkreftype": MARKER, "orgid": org_id, "transactiontype": "Connections", "themename": "IT_CNX",
        "processcode": "CNX", "transactionid": context_id, "percentageoftds": 0.0, "tdsvalue": 0.0,
        "taxpercentage": 0.0, "taxvalue": 0.0,
    }
    rows = []
    for n, inv in enumerate(match["invoices"], start=1):
        rows.append({**common, "connectingdocumentnumber": inv["belnr"], "customeraccount": match["customer"],
                     "customername": match["customer_name"],
                     "documentdate": parse_date(inv["posting_date"]) or bank["date"],
                     "documentvalue": inv["amount"], "sourceamount": inv["amount"], "basevalue": inv["amount"],
                     "source": "Invoice", "sno": str(n)})
    rows.append({**common, "connectingdocumentnumber": bank["utr"], "documentdate": bank["date"],
                 "documentvalue": float(bank["amount"]), "sourceamount": float(bank["amount"]), "basevalue": 0.0,
                 "source": "Bank", "sno": str(len(match["invoices"]) + 1)})
    return rows


def write_connection(org_id: str, rows: List[Dict[str, Any]], column: str, column_exists: bool,
                     env: Optional[str] = None) -> Dict[str, Any]:
    """Insert the rows in one transaction; nothing is written when the connection id already exists."""
    column = _check_column_name(column)
    cols = list(CNX_INSERT_COLUMNS) + ([column] if column_exists else [])
    doc_id = rows[0]["documentid"]
    q = sql.SQL("INSERT INTO {tbl} ({cols}) VALUES ({vals})").format(
        tbl=sql.Identifier(CNX_SCHEMA, CNX_TABLE),
        cols=sql.SQL(", ").join(sql.Identifier(c) for c in cols),
        vals=sql.SQL(", ").join(sql.Placeholder() for _ in cols),
    )
    conn = _connect(env)
    try:
        with conn.cursor() as cur:
            # same lock key for every writer of this connection id: no double insert under concurrency
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"{CNX_TABLE}:{org_id}:{doc_id}",))
            cur.execute(sql.SQL("SELECT count(*) FROM {tbl} WHERE orgid = %s AND documentid = %s")
                        .format(tbl=sql.Identifier(CNX_SCHEMA, CNX_TABLE)), (org_id, doc_id))
            if cur.fetchone()[0]:
                conn.rollback()
                return {"created": False, "reason": f"connection {doc_id} already exists in {CNX_TABLE}"}
            for r in rows:
                cur.execute(q, [r.get(c, "") for c in cols])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    logger.info(f"[MATCH] {CNX_TABLE}: connection {org_id} / {doc_id} created with {len(rows)} row(s)")
    return {"created": True, "rows": len(rows), "transactionid": rows[0]["transactionid"]}


def _preview_connection(rows: List[Dict[str, Any]], column_exists: bool) -> Dict[str, Any]:
    """The connection as read_connection() would return it after write_connection()."""
    view = [{"source": r["source"], "sno": r["sno"], "ref": r["connectingdocumentnumber"],
             "amount": _dec(r["documentvalue"]), "status": r["documentstatus"], "clearing_doc": "",
             "customer_name": r["customername"]} for r in rows]
    return {"column_exists": column_exists, "rows": view,
            "invoices": [r for r in view if r["source"] == "Invoice"],
            "bank": [r for r in view if r["source"] == "Bank"], "cleared_by": [],
            "customer_names": sorted({r["customer_name"] for r in view if r["customer_name"]}),
            "customer_accounts": sorted({r["customeraccount"] for r in rows
                                         if r["source"] == "Invoice" and r["customeraccount"]})}


CLEARING_KEYS = ("batch_id", "result", "message", "company_code", "customer", "customer_from", "bank_gl",
                 "posting_date", "invoices", "document_no", "clearing_doc", "cleared_invoices", "db_update",
                 "from_config")


def _clear(log: str, req: Dict[str, Any], dry_run: bool, index: OpenItemIndex,
           preview: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """POST /api/clearing/post logic for one connection; errors are returned, not raised."""
    try:
        out = process_clearing(req, dry_run=dry_run, update_db=True, match_by="BELNR",
                               preview_connection=preview, index=index)
        return {k: out[k] for k in CLEARING_KEYS if k in out}
    except Exception as e:
        logger.exception(f"{log}: clearing {req['connection_id']} failed: {e}")
        return {"result": "ERROR", "message": str(e)}


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------
def process_auto_match(payload: Dict[str, Any]) -> Dict[str, Any]:
    org_id = _text(payload.get("org_id"))
    if not org_id:
        raise ValueError("org_id is required")
    dry_run = payload.get("dry_run", True) is not False
    create = bool(payload.get("create_connections"))
    clear = bool(payload.get("clear"))
    max_invoices = int(payload.get("max_invoices") or 5)
    if not 1 <= max_invoices <= MAX_INVOICES_LIMIT:
        raise ValueError(f"max_invoices must be 1..{MAX_INVOICES_LIMIT}")
    date_from = parse_date(payload.get("date_from")) if payload.get("date_from") else None
    date_to = parse_date(payload.get("date_to")) if payload.get("date_to") else None
    if (payload.get("date_from") and not date_from) or (payload.get("date_to") and not date_to):
        raise ValueError("date_from / date_to must be dates (YYYY-MM-DD)")
    posting_date = parse_date(payload.get("posting_date")) if payload.get("posting_date") else None
    if payload.get("posting_date") and not posting_date:
        raise ValueError("posting_date must be a date (YYYY-MM-DD)")
    bank_refs = [r for r in (payload.get("bank_refs") or []) if _text(r)]
    if clear and not dry_run and not bank_refs:
        raise ValueError("Real clearing (clear = true, dry_run = false) needs bank_refs: list the bank receipts "
                         "to post. Run with dry_run = true first to see the matches.")
    wanted = {r.strip().upper() for r in bank_refs}

    cfg = load_bank_config()
    column = _check_column_name(cfg["cnx_clearing_column"])
    started = datetime.now()
    batch_id = f"MCH{started:%Y%m%d%H%M%S}-{uuid.uuid4().hex[:4].upper()}"
    log = f"[MATCH] {batch_id}"
    logger.info(f"{log}: start, org {org_id}, dry_run={dry_run}, create_connections={create}, clear={clear}, "
                f"dates {date_from or '-'}..{date_to or '-'}, bank_refs={bank_refs or 'all'}, max_invoices={max_invoices}")

    if payload.get("bank_items"):
        if not dry_run:
            raise ValueError("bank_items can only be checked with dry_run = true: connections and clearing need "
                             "the receipt in active_bs")
        receipts = request_receipts(payload["bank_items"])
    else:
        receipts = load_bank_receipts(org_id, date_from, date_to, bank_refs)
    cnx = load_connections(org_id, column)
    codes, codes_from = org_company_codes(org_id)
    org = org_settings(org_id)
    bank_currency_default = (org.get("currency") or cfg.get("bank_currency") or "INR").upper()

    tables = list(dict.fromkeys(configured_sap_tables()[0] + ["BSAD", "KNA1"]))
    try:
        sap_index = OpenItemIndex(tables)   # read once; also used by the clearing checks below
    except Exception as e:
        logger.exception(f"{log}: could not read {', '.join(tables)} from SAP: {e}")
        raise RuntimeError(f"Could not read {', '.join(tables)} from SAP: {e}") from e
    invoices, skipped = open_invoices(sap_index, codes, cnx["open_invoices"])
    by_customer: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for i in invoices:
        by_customer[(i["company_code"], i["customer"])].append(i)
    names = configured_names(org_id)
    logger.info(f"{log}: {len(receipts)} bank receipt(s), {len(invoices)} open SAP invoice(s) in "
                f"{len(by_customer)} customer account(s); company codes {codes or 'all'} ({codes_from}); "
                f"left out {skipped or '-'}")

    results: List[Tuple[Dict[str, Any], str, Dict[str, Any]]] = []
    for bank in receipts:
        currency = bank["currency"] or bank_currency_default
        head = {
            "bank": {"transaction_number": bank["transaction_number"], "utr": bank["utr"],
                     "date": bank["date"].isoformat() if bank["date"] else None, "amount": float(bank["amount"]),
                     "currency": currency + ("" if bank["currency"] else " (default)"),
                     "account": bank["account"], "sno": bank["sno"], "narration": bank["narration"]},
            "connection_id": bank["transaction_number"],
        }
        cid = bank["transaction_number"] if bank["transaction_number"] in cnx["by_id"] \
            else cnx["bank_refs"].get(bank["utr"].upper()) if bank["utr"] else None
        existing = cnx["by_id"].get(cid) if cid else None
        if existing is not None:
            res = {**head, "connection_id": cid, "status": "CONNECTED", "confidence": None,
                   "reason": (f"already connected ({cid}, {existing['rows']} row(s))"
                              + (f", cleared by {', '.join(existing['cleared_by'])}" if existing["cleared_by"] else
                                 "; clear it with POST /api/clearing/post"))}
        elif not bank["transaction_number"] or not bank["utr"] or not bank["date"]:
            res = {**head, "status": "NO_MATCH", "confidence": None,
                   "reason": "bank row has no transactionnumber / reference / date"}
        else:
            res = {**head, **match_receipt(bank, invoices, by_customer, names, currency, max_invoices)}
        results.append((bank, currency, res))

    # one invoice, one receipt: invoices claimed by several MATCHED receipts send all of them to REVIEW
    claims: Dict[str, List[str]] = defaultdict(list)
    for bank, _, res in results:
        if res["status"] == "MATCHED":
            for inv in res["match"]["invoices"]:
                claims[f"{res['match']['company_code']}/{res['match']['customer']}/{inv['belnr']}"].append(
                    bank["transaction_number"])
    for bank, _, res in results:
        if res["status"] != "MATCHED":
            continue
        m = res["match"]
        others = sorted({t for inv in m["invoices"]
                         for t in claims[f"{m['company_code']}/{m['customer']}/{inv['belnr']}"]
                         if t != bank["transaction_number"]})
        if others:
            res.update({"status": "REVIEW", "confidence": "MEDIUM", "suggested": m, "options": [m], "option_count": 1,
                        "reason": res["reason"] + f"; but the same invoice(s) also match receipt(s) "
                                  f"{', '.join(others)}: pick one"})
            res.pop("match")

    counts: Dict[str, int] = defaultdict(int)
    for bank, currency, res in results:
        counts[res["status"]] += 1
        logger.info(f"{log}: {bank['transaction_number']} {bank['amount']} {currency} -> {res['status']} - {res['reason']}")
        selected = not wanted or bool({bank["transaction_number"].upper(), bank["utr"].upper()} & wanted)

        if res["status"] == "MATCHED":
            m = res["match"]
            defaults = sap_defaults(org_id, m["company_code"])
            res["clearable"] = bool(defaults.get("bank_gl"))
            if not res["clearable"]:
                res["clearable_reason"] = f"add bank_gl under [company:{m['company_code']}] in config.ini"
            rows = connection_rows(org_id, bank, m, f"CTXSAP{uuid.uuid4().hex[:12].upper()}")
            if create and selected:
                if dry_run:
                    res["connection"] = {"created": False, "preview_rows": len(rows), "reason": "dry_run"}
                else:
                    try:
                        res["connection"] = write_connection(org_id, rows, column, cnx["column_exists"])
                    except Exception as e:
                        logger.exception(f"{log}: could not write connection {bank['transaction_number']}: {e}")
                        res["connection"] = {"created": False, "error": str(e)}
            if clear and selected:
                if not dry_run and not (res.get("connection") or {}).get("created"):
                    res["clearing"] = {"result": "SKIPPED",
                                       "message": "connection not written (send create_connections = true)"}
                else:
                    req = {"org_id": org_id, "connection_id": bank["transaction_number"],
                           "company_code": m["company_code"], "customer": m["customer"],
                           "posting_date": (posting_date or bank["date"]).isoformat(),
                           "amount": float(bank["amount"]), "utr": bank["utr"], "currency": currency,
                           "invoices": [{"invoice_ref": i["belnr"], "amount": i["amount"]} for i in m["invoices"]]}
                    res["clearing"] = _clear(log, req, dry_run, sap_index,
                                             _preview_connection(rows, cnx["column_exists"]) if dry_run else None)
        elif res["status"] == "CONNECTED" and clear and selected and not cnx["by_id"][res["connection_id"]]["cleared_by"]:
            # an existing connection (AR Inbox or an earlier run): the normal /api/clearing/post logic
            req = {"org_id": org_id, "connection_id": res["connection_id"],
                   "posting_date": (posting_date or bank["date"]).isoformat()}
            res["clearing"] = _clear(log, req, dry_run, sap_index, None)

    out = [res for _, _, res in results]
    order = {"MATCHED": 0, "REVIEW": 1, "AMOUNT_ONLY": 2, "CONNECTED": 3, "NO_MATCH": 4}
    out.sort(key=lambda r: (order.get(r["status"], 9), r["bank"]["date"] or "", r["bank"]["sno"]))
    logger.info(f"{log}: done in {(datetime.now() - started).total_seconds():.1f}s, {dict(counts)}")
    return {
        "status": "completed",
        "batch_id": batch_id,
        "org_id": org_id,
        "dry_run": dry_run,
        "create_connections": create,
        "clear": clear,
        "summary": {"bank_receipts": len(receipts), **{k: counts.get(k, 0) for k in order}},
        "sap": {"open_invoices": len(invoices), "customer_accounts": len(by_customer),
                "company_codes": codes or "all", "company_codes_from": codes_from, "left_out": skipped},
        "sap_tables": tables,
        "results": out,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }
