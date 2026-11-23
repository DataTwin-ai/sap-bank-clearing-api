"""
=============================================================================
Bank Receipt Posting with Clearing (F-30 replacement) - API backend
=============================================================================
Input  : Excel file (same layout as ZFI_BANK_PROGRAM / BANK EXCEL SHEET.xlsx)
         or a JSON list of items.
Steps  : 1. Read + normalise rows (dates, amounts, customer ALPHA format)
         2. Validate (mandatory fields, Dr = Cr, duplicates in the batch)
         3. Check the invoice is an OPEN item in live SAP BSID
            (via ZGENERIC_DATA_SRV, read-only)
         4. If not dry_run: call RFC function Z_BANK_POST_CLEAR for each valid row
            via SOAP-RFC (/sap/bc/soap/rfc). The ABAP side calls
            POSTING_INTERFACE_CLEARING and returns posting + clearing document.
Output : JSON with a result per row.
=============================================================================
"""

import configparser
import os
import re
import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path, PureWindowsPath
from typing import Any, Dict, List, Optional, Tuple

import sys

import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xml_escape

import requests
from requests.auth import HTTPBasicAuth

# Shared project modules (logger, SAP OData client, config.ini) live in the parent folder
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from logger_setup import get_logger
from sap_odata_extractor import SAPODataExtractor, load_sap_odata_config

logger = get_logger("SAP_Pipeline.BankPosting")

CONFIG_PATH = PROJECT_ROOT / "config.ini"
MATCH_FIELDS = ("XBLNR", "ZUONR", "BELNR")

# Excel header -> internal field. Same 17 columns as ZFI_BANK_PROGRAM.
EXCEL_COLUMNS = {
    "BLDAT": "doc_date",
    "BLART": "doc_type",
    "BUKRS": "company_code",
    "BUDAT": "posting_date",
    "MONAT": "period",
    "WAERS": "currency",
    "NEWBS_1": "cust_posting_key",
    "NEWKO_1": "customer",
    "WRBTR_1": "cust_amount",
    "ZFBDT": "baseline_date",
    "ZUONR_1": "invoice_ref",
    "SGTXT_1": "cust_text",
    "NEWBS_2": "bank_posting_key",
    "NEWKO_2": "bank_gl",
    "WRBTR_2": "amount",
    "ZUONR_2": "bank_assignment",
    "SGTXT_2": "item_text",
}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_bank_config() -> Dict[str, str]:
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH, encoding="utf-8")
    sec = cfg["bank_posting"] if cfg.has_section("bank_posting") else {}
    return {
        "rfc_function": sec.get("rfc_function", "Z_BANK_POST_CLEAR"),
        # connection clearing (POST /api/clearing/post): one bank receipt, several invoices
        "clearing_rfc_function": sec.get("clearing_rfc_function", "Z_BANK_POST_CLEAR_MULTI"),
        "cnx_clearing_column": sec.get("cnx_clearing_column", "clearingdocid").strip().lower(),
        # BELNR = SAP document number from active_bt3i.documentid (set by /api/invoice/post)
        "clearing_match_by": sec.get("clearing_match_by", "BELNR").strip().upper(),
        # new customer invoices (POST /api/invoice/post)
        "invoice_rfc_function": sec.get("invoice_rfc_function", "Z_INVOICE_POST"),
        "invoice_doc_type": sec.get("invoice_doc_type", "DR").strip().upper(),
        "default_doc_type": sec.get("default_doc_type", "DZ"),
        "default_match_by": sec.get("default_match_by", "XBLNR").upper(),
        "check_open_items": sec.get("check_open_items", "true").lower()
        in ("true", "1", "yes"),
        # Tenant files in S3: {tenant_id} lower-cased, {suffix_s3} from the active env section
        "s3_bucket_template": sec.get(
            "s3_bucket_template", "dt-{tenant_id}{suffix_s3}"
        ),
        "s3_key_template": sec.get("s3_key_template", "{dir}/{org_id}/{name}"),
    }


# SAP tables the validation can use. List them in [sap] tables (config.ini).
# BSID is always read; each other table adds its check when listed.
SAP_TABLE_RULES = {
    "BSID": "open customer items: the invoice must be open",
    "BSAD": "cleared customer items: say when the invoice is already cleared",
    "KNA1": "customer master: customer exists, not blocked / flagged for deletion",
    "KNB1": "customer per company code: customer is created for the company code",
}


def configured_sap_tables() -> Tuple[List[str], List[str]]:
    """Read [sap] tables. Returns (tables with a validation rule, tables without one)."""
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH, encoding="utf-8")
    raw = cfg.get("sap", "tables", fallback="BSID")
    names: List[str] = []
    for t in re.split(r"[,\s;]+", raw):
        t = t.strip().upper()
        if t and t not in names:
            names.append(t)
    used = ["BSID"] + [t for t in names if t in SAP_TABLE_RULES and t != "BSID"]
    ignored = [t for t in names if t not in SAP_TABLE_RULES]
    if ignored:
        logger.warning(
            f"[sap] tables: no validation rule for {', '.join(ignored)} - not read "
            f"(supported: {', '.join(SAP_TABLE_RULES)})"
        )
    return used, ignored


def _s3_suffix() -> str:
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH, encoding="utf-8")
    env = (
        os.getenv("APP_ENV")
        or os.getenv("DB_ENV")
        or cfg.get("environment", "current_env", fallback="development")
    ).strip()
    return (
        cfg.get(env, "suffix_s3", fallback="-dev") if cfg.has_section(env) else "-dev"
    )


def tenant_s3_location(
    file_path: str, tenant_id: str, org_id: Optional[str] = None
) -> Tuple[str, str]:
    r"""
    Map a client path to the tenant's S3 object, e.g.
    C:\SAP\BANK.xlsx + DM00005 / OM0000000001 -> (dt-dm00005-dev, SAP/OM0000000001/BANK.xlsx)

    Key template placeholders: {path} = full path without drive (SAP/BANK.xlsx),
    {dir} = folder part (SAP), {name} = file name (BANK.xlsx), {org_id}, {tenant_id}.
    """
    cfg = load_bank_config()
    win = PureWindowsPath(file_path.strip())
    parts = [p for p in win.parts if p not in (win.anchor, "\\", "/")]
    rel = "/".join(parts).lstrip("/")
    if not rel:
        raise ValueError("file_path is empty")
    bucket = cfg["s3_bucket_template"].format(
        tenant_id=tenant_id.strip().lower(),
        org_id=(org_id or "").strip(),
        suffix_s3=_s3_suffix(),
    )
    key = file_path
    key = re.sub(r"/{2,}", "/", key)
    return bucket, key


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------
def _blank(v: Any) -> bool:
    return v is None or str(v).strip() in ("", "nan", "None")


def parse_date(v: Any) -> Optional[date]:
    if _blank(v):
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y%m%d"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            pass
    return None


def parse_amount(v: Any) -> Optional[Decimal]:
    """Accepts 15000 / 15000.00 / 8250,50 / 8,250.50 / 8.250,50."""
    if _blank(v):
        return None
    if isinstance(v, (int, float, Decimal)):
        return Decimal(str(v)).quantize(Decimal("0.01"))
    s = str(v).strip().replace(" ", "")
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("." if dec == "," else ",", "").replace(dec, ".")
    elif "," in s:
        # single comma not followed by exactly 3 digits -> decimal comma (8250,50)
        # otherwise thousands separators (1,234,567)
        tail = s.rpartition(",")[2]
        s = (
            s.replace(",", ".")
            if s.count(",") == 1 and len(tail) != 3
            else s.replace(",", "")
        )
    try:
        return Decimal(s).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def alpha(v: Any, length: int = 10) -> str:
    """SAP ALPHA conversion: numeric keys are left-padded with zeros."""
    s = "" if _blank(v) else str(v).strip().upper()
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s.zfill(length) if s.isdigit() else s


def _text(v: Any) -> str:
    if _blank(v):
        return ""
    s = str(v).strip()
    return s[:-2] if s.endswith(".0") and s[:-2].isdigit() else s


# ---------------------------------------------------------------------------
# Input readers
# ---------------------------------------------------------------------------
def data_dir() -> Path:
    """Folder for uploaded / dropped Excel files: DATA_DIR env var or ./data (cwd)."""
    raw = os.environ.get("DATA_DIR") or "data"
    path = Path(raw).expanduser()
    return path if path.is_absolute() else Path.cwd() / path


_S3_HTTP_PATTERNS = (
    # https://<bucket>.s3.<region>.amazonaws.com/<key>  or  https://<bucket>.s3.amazonaws.com/<key>
    re.compile(
        r"^https?://(?P<bucket>[^./]+(?:\.[^./]+)*?)\.s3[.-](?:[a-z0-9-]+\.)?amazonaws\.com/(?P<key>.+)$",
        re.I,
    ),
    # https://s3.<region>.amazonaws.com/<bucket>/<key>
    re.compile(
        r"^https?://s3[.-](?:[a-z0-9-]+\.)?amazonaws\.com/(?P<bucket>[^/]+)/(?P<key>.+)$",
        re.I,
    ),
)


def parse_s3_uri(file_path: str) -> Optional[Tuple[str, str]]:
    """Return (bucket, key) for s3://bucket/key or an S3 https URL, else None."""
    from urllib.parse import unquote

    fp = (file_path or "").strip()
    if fp.lower().startswith("s3://"):
        bucket, _, key = fp[5:].partition("/")
        return (bucket, key) if bucket and key else None
    for pat in _S3_HTTP_PATTERNS:
        m = pat.match(fp.split("?", 1)[0])
        if m:
            return m.group("bucket"), unquote(m.group("key"))
    return None


def download_from_s3(bucket: str, key: str) -> Path:
    """Download s3://bucket/key into <data>/s3/ and return the local path."""
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    name = re.sub(r"[^A-Za-z0-9._ -]", "_", PureWindowsPath(key).name) or "file.xlsx"
    folder = data_dir() / "s3"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{datetime.now():%Y%m%d_%H%M%S}_{name}"
    region = (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-east-1"
    )
    try:
        boto3.client("s3", region_name=region).download_file(bucket, key, str(target))
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NoSuchBucket"):
            raise FileNotFoundError(f"S3 file not found: s3://{bucket}/{key}") from e
        raise RuntimeError(
            f"Could not download s3://{bucket}/{key}: {code or e}"
        ) from e
    except BotoCoreError as e:
        raise RuntimeError(f"Could not download s3://{bucket}/{key}: {e}") from e
    logger.info(f"[BANK POST] downloaded s3://{bucket}/{key} -> {target}")
    return target


def resolve_excel_path(file_path: str) -> Path:
    """
    s3://bucket/key (or an S3 https URL) is downloaded from S3 first.
    Otherwise use file_path as given if it exists on this server. Otherwise look for a file
    with the same name in the data folder, so a client can send a Windows path
    such as 'C:/SAP/log/BANK.xlsx' (or with backslashes) and the server
    uses data/BANK.xlsx.
    """
    s3 = parse_s3_uri(file_path)
    if s3:
        return download_from_s3(*s3)
    path = Path(file_path)
    if path.is_file():
        return path
    name = PureWindowsPath(file_path).name  # splits on both back- and forward slashes
    candidate = data_dir() / name
    if name and candidate.is_file():
        logger.info(f"[BANK POST] '{file_path}' not on server; using {candidate}")
        return candidate
    raise FileNotFoundError(
        f"Excel file not found on API server: '{file_path}'. "
        f"Upload it with POST /api/bank/upload or copy it to the server data folder ({data_dir()})."
    )


def load_sheet_rows(
    file_path: str,
    sheet: Optional[str] = None,
    tenant_id: Optional[str] = None,
    org_id: Optional[str] = None,
) -> List[tuple]:
    import openpyxl

    if tenant_id and not parse_s3_uri(file_path):
        bucket, key = tenant_s3_location(file_path, tenant_id, org_id)
        logger.info(
            f"[BANK POST] tenant {tenant_id}: '{file_path}' -> s3://{bucket}/{key}"
        )
        path = download_from_s3(bucket, key)
    else:
        path = resolve_excel_path(file_path)

    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    if not sheet:
        ws = wb.worksheets[0]
    elif sheet in wb.sheetnames:
        ws = wb[sheet]
    elif len(wb.sheetnames) == 1:
        logger.warning(
            f"[BANK POST] sheet '{sheet}' not found; using the only sheet '{wb.sheetnames[0]}'"
        )
        ws = wb.worksheets[0]
    else:
        names = ", ".join(wb.sheetnames)
        wb.close()
        raise ValueError(
            f"Sheet '{sheet}' not found in {path.name}. Available sheets: {names}"
        )
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    return rows


def header_of(rows: List[tuple]) -> List[str]:
    return [
        re.sub(r"[^A-Z0-9_]", "", str(h or "").upper())
        for h in (rows[0] if rows else ())
    ]


FILE_TYPES = ("bank", "invoice")


def detect_file_type(rows: List[tuple]) -> Optional[str]:
    """bank = F-30 layout (NEWBS_2 / NEWKO_2), invoice = FB70 layout (BUSCS / ACCNT / HKONT)."""
    header = set(header_of(rows))
    if {"NEWKO_2", "WRBTR_2"} <= header:
        return "bank"
    if {"ACCNT", "HKONT"} <= header:
        return "invoice"
    return None


def read_excel(
    file_path: str,
    sheet: Optional[str] = None,
    tenant_id: Optional[str] = None,
    org_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    return bank_items_from_rows(load_sheet_rows(file_path, sheet, tenant_id, org_id))


def bank_items_from_rows(rows: List[tuple]) -> List[Dict[str, Any]]:
    if not rows:
        return []

    header = [re.sub(r"[^A-Z0-9_]", "", str(h or "").upper()) for h in rows[0]]
    header_set = set(header)

    # Detect layout:
    # 1. AR Inbox / F-30 ERP format (contains HKONT / ACCNT / KKBER)
    # 2. Legacy ZFI_BANK_PROGRAM (17 columns with NEWKO_1 / NEWKO_2 / WRBTR_1)
    is_ar_inbox = (
        "HKONT" in header_set or "ACCNT" in header_set
    ) and "NEWKO_1" not in header_set

    if is_ar_inbox:
        missing = [
            h for h in ("BLDAT", "BUDAT", "WAERS", "HKONT") if h not in header_set
        ]
        if "KKBER" not in header_set and "BUKRS" not in header_set:
            missing.append("KKBER/BUKRS")
        if "ACCNT" not in header_set and "NEWKO_1" not in header_set:
            missing.append("ACCNT")
        if "WRBTR" not in header_set and "WRBTR1" not in header_set:
            missing.append("WRBTR/WRBTR1")
        if "ZUONR" not in header_set and "XBLNR" not in header_set:
            missing.append("ZUONR/XBLNR")
        if missing:
            raise ValueError(f"Excel header is missing columns: {', '.join(missing)}")

        inferred_match_by = (
            "ZUONR" if "ZUONR" in header_set and "XBLNR" not in header_set else None
        )

        items = []
        for excel_row, values in enumerate(rows[1:], start=2):
            if all(_blank(v) for v in values):
                continue
            row_dict = {
                header[i]: values[i] for i in range(min(len(header), len(values)))
            }
            amt = (
                row_dict.get("WRBTR1")
                if not _blank(row_dict.get("WRBTR1"))
                else row_dict.get("WRBTR")
            )
            cust_amt = (
                row_dict.get("WRBTR") if not _blank(row_dict.get("WRBTR")) else amt
            )
            buscs = str(row_dict.get("BUSCS", "")).strip()
            doc_type = "DZ" if buscs.lower() in ("invoice", "dz", "") else buscs

            raw = {
                "row": excel_row,
                "doc_date": row_dict.get("BLDAT"),
                "posting_date": row_dict.get("BUDAT"),
                "currency": row_dict.get("WAERS"),
                "company_code": row_dict.get("KKBER") or row_dict.get("BUKRS"),
                "customer": row_dict.get("ACCNT"),
                "bank_gl": row_dict.get("HKONT"),
                "amount": amt,
                "cust_amount": cust_amt,
                "invoice_ref": row_dict.get("ZUONR") or row_dict.get("XBLNR"),
                "bank_assignment": row_dict.get("ZUONR"),
                "item_text": row_dict.get("SGTXT"),
                "cust_text": row_dict.get("SGTXT"),
                "doc_type": doc_type,
                "_inferred_match_by": inferred_match_by,
            }
            items.append(raw)
        return items

    col_idx = {EXCEL_COLUMNS[h]: i for i, h in enumerate(header) if h in EXCEL_COLUMNS}
    missing = [
        h
        for h, f in EXCEL_COLUMNS.items()
        if f not in col_idx and h not in ("ZFBDT", "ZUONR_2", "SGTXT_1", "MONAT")
    ]
    if missing:
        raise ValueError(f"Excel header is missing columns: {', '.join(missing)}")

    items = []
    for excel_row, values in enumerate(rows[1:], start=2):
        if all(_blank(v) for v in values):
            continue
        raw = {f: (values[i] if i < len(values) else None) for f, i in col_idx.items()}
        raw["row"] = excel_row
        # ZUONR_2 (assignment on the bank line) is the bank reference / UTR:
        # SAP duplicate check (BKPF-XBLNR) and active_bs.transactionref match
        raw["utr"] = raw.get("bank_assignment")
        items.append(raw)
    return items


def normalise(raw: Dict[str, Any], defaults: Dict[str, str]) -> Dict[str, Any]:
    return {
        "row": raw.get("row"),
        "doc_date_raw": raw.get("doc_date"),
        "posting_date_raw": raw.get("posting_date"),
        "doc_date": parse_date(raw.get("doc_date")),
        "posting_date": parse_date(raw.get("posting_date")),
        "doc_type": _text(raw.get("doc_type")).upper() or defaults["default_doc_type"],
        "company_code": _text(raw.get("company_code")).upper(),
        "period": _text(raw.get("period")),
        "currency": _text(raw.get("currency")).upper(),
        "customer": alpha(raw.get("customer")),
        "bank_gl": alpha(raw.get("bank_gl")),
        "amount_raw": raw.get("amount"),
        "amount": parse_amount(raw.get("amount")),
        "cust_amount_raw": raw.get("cust_amount"),
        "cust_amount": parse_amount(raw.get("cust_amount"))
        if "cust_amount" in raw
        else None,
        "cust_posting_key": _text(raw.get("cust_posting_key")),
        "bank_posting_key": _text(raw.get("bank_posting_key")),
        "invoice_ref": _text(raw.get("invoice_ref")),
        "utr": _text(raw.get("utr")),
        # only sent when explicitly given: BSEG-VALUT may be hidden for the bank G/L
        "value_date": parse_date(raw.get("value_date")),
        "item_text": _text(raw.get("item_text"))[:50],
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate(item: Dict[str, Any]) -> List[str]:
    errs = []
    if item["doc_date"] is None:
        errs.append(f"Invalid/missing document date: '{_text(item['doc_date_raw'])}'")
    if item["posting_date"] is None:
        errs.append(
            f"Invalid/missing posting date: '{_text(item['posting_date_raw'])}'"
        )
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
    if (
        item["cust_amount"] is not None
        and item["amount"] is not None
        and item["cust_amount"] != item["amount"]
    ):
        errs.append(
            f"Customer amount {item['cust_amount']} != bank amount {item['amount']} (Dr must equal Cr)"
        )
    if item["cust_amount_raw"] is not None and item["cust_amount"] is None:
        errs.append(f"Invalid customer amount: '{_text(item['cust_amount_raw'])}'")
    if not item["invoice_ref"]:
        errs.append("Invoice reference is missing - nothing to clear")
    if len(item["utr"]) > 16:
        errs.append(
            f"UTR '{item['utr']}' is {len(item['utr'])} characters; SAP reference (XBLNR) allows 16"
        )
    if item["period"] and not (
        item["period"].isdigit() and 1 <= int(item["period"]) <= 16
    ):
        errs.append(f"Invalid posting period: '{item['period']}'")
    if item["bank_posting_key"] and item["bank_posting_key"] != "40":
        errs.append(
            f"Bank line posting key must be 40 (debit G/L), got '{item['bank_posting_key']}'"
        )
    return errs


def _records(extractor: SAPODataExtractor, table: str) -> List[Dict[str, Any]]:
    df = extractor.fetch_table_data(table)
    return df.to_dict("records") if not df.empty else []


class CustomerIndex:
    """Customer master (KNA1) and company-code data (KNB1) from SAP."""

    def __init__(self, extractor: SAPODataExtractor, kna1: bool = True, knb1: bool = True):
        self.kna1 = (
            {alpha(r.get("kunnr")): r for r in _records(extractor, "KNA1")} if kna1 else None
        )
        self.knb1 = (
            {
                (str(r.get("bukrs", "")).upper(), alpha(r.get("kunnr"))): r
                for r in _records(extractor, "KNB1")
            }
            if knb1
            else None
        )

    def check(self, kunnr: str, bukrs: str) -> List[str]:
        errs: List[str] = []
        if self.kna1 is not None:
            cust = self.kna1.get(kunnr)
            if not cust:
                return [f"Customer {kunnr} does not exist in SAP (KNA1)"]
            if _text(cust.get("loevm")):
                errs.append(f"Customer {kunnr} is flagged for deletion in SAP")
            if _text(cust.get("sperr")):
                errs.append(f"Customer {kunnr} is blocked for posting in SAP")
        if self.knb1 is not None and bukrs:
            cc = self.knb1.get((bukrs, kunnr))
            if not cc:
                errs.append(f"Customer {kunnr} is not created for company code {bukrs} (KNB1)")
            elif _text(cc.get("sperr")):
                errs.append(f"Customer {kunnr} is blocked for posting in company code {bukrs}")
        return errs


class OpenItemIndex:
    """
    Customer items from SAP, fetched once per request.
    tables: BSID (open, always), BSAD (cleared), KNA1 / KNB1 (customer checks).
    """

    def __init__(self, tables: Optional[List[str]] = None):
        tables = tables or ["BSID"]
        x = SAPODataExtractor()
        self.rows = _records(x, "BSID")
        self.cleared = _records(x, "BSAD") if "BSAD" in tables else None
        self.customers = (
            CustomerIndex(x, kna1="KNA1" in tables, knb1="KNB1" in tables)
            if "KNA1" in tables or "KNB1" in tables
            else None
        )

    @staticmethod
    def _match(
        rows: List[Dict[str, Any]], bukrs: str, kunnr: str, field: str, value: str
    ) -> List[Dict[str, Any]]:
        key = field.lower()
        val = alpha(value) if field == "BELNR" else value.strip().upper()
        out = []
        for r in rows:
            if (
                str(r.get("bukrs", "")).upper() != bukrs
                or alpha(r.get("kunnr")) != kunnr
            ):
                continue
            cand = (
                alpha(r.get(key))
                if field == "BELNR"
                else str(r.get(key) or "").strip().upper()
            )
            if cand and cand == val:
                out.append(r)
        return out

    def find(
        self, bukrs: str, kunnr: str, field: str, value: str
    ) -> List[Dict[str, Any]]:
        return self._match(self.rows, bukrs, kunnr, field, value)

    def find_cleared(
        self, bukrs: str, kunnr: str, field: str, value: str
    ) -> List[Dict[str, Any]]:
        """Cleared invoice lines (BSAD, debit) - empty when BSAD is not configured."""
        if not self.cleared:
            return []
        rows = [r for r in self.cleared if str(r.get("shkzg")) == "S"]
        return self._match(rows, bukrs, kunnr, field, value)


def check_open_items(
    item: Dict[str, Any], index: OpenItemIndex, match_by: str
) -> Tuple[List[str], Dict[str, Any]]:
    errs: List[str] = []
    if index.customers is not None:
        errs.extend(index.customers.check(item["customer"], item["company_code"]))

    matches = index.find(
        item["company_code"], item["customer"], match_by, item["invoice_ref"]
    )
    if not matches:
        cleared = index.find_cleared(
            item["company_code"], item["customer"], match_by, item["invoice_ref"]
        )
        if cleared:
            docs = sorted(
                {
                    (str(m.get("belnr")), str(m.get("gjahr")),
                     _text(m.get("augbl")), _text(m.get("augdt")))
                    for m in cleared
                }
            )
            errs.append(
                f"Invoice '{item['invoice_ref']}' is already cleared in SAP: "
                + ", ".join(f"document {b}/{y} cleared by {c} on {d}" for b, y, c, d in docs)
            )
            return errs, {
                "cleared_items": [
                    {"belnr": b, "gjahr": y, "augbl": c, "augdt": d} for b, y, c, d in docs
                ]
            }
        errs.append(
            f"No open item in SAP for company {item['company_code']}, customer {item['customer']} "
            f"with {match_by} = '{item['invoice_ref']}'"
        )
        return errs, {}

    currencies = {str(m.get("waers", "")).upper() for m in matches}
    if currencies != {item["currency"]}:
        errs.append(
            f"Currency mismatch: bank {item['currency']} vs open item {', '.join(sorted(currencies))}"
        )

    open_amt = sum(
        (
            Decimal(str(m.get("wrbtr") or 0))
            * (1 if str(m.get("shkzg")) == "S" else -1)
            for m in matches
        ),
        Decimal("0"),
    ).quantize(Decimal("0.01"))
    if item["amount"] is not None and open_amt != item["amount"]:
        errs.append(
            f"Amount mismatch: bank {item['amount']} vs open item(s) {open_amt} "
            f"(difference {item['amount'] - open_amt})"
        )

    info = {
        "open_items": [
            {
                "belnr": m.get("belnr"),
                "gjahr": str(m.get("gjahr")),
                "buzei": str(m.get("buzei")),
                "xblnr": m.get("xblnr"),
                "amount": float(m.get("wrbtr") or 0),
                "dc": m.get("shkzg"),
            }
            for m in matches
        ],
        "open_amount": float(open_amt),
    }
    return errs, info


# ---------------------------------------------------------------------------
# SAP posting client (RFC Z_BANK_POST_CLEAR via SOAP)
# ---------------------------------------------------------------------------
class SAPBankPostingClient:
    """Calls RFC function module Z_BANK_POST_CLEAR via SAP SOAP-RFC (/sap/bc/soap/rfc)."""

    NS = "urn:sap-com:document:sap:rfc:functions"

    def __init__(self, bank_cfg: Dict[str, str]):
        sap = load_sap_odata_config()
        scheme = "https" if sap.get("use_https", True) else "http"
        self.url = f"{scheme}://{sap['host']}:{sap['port']}/sap/bc/soap/rfc"
        self.fm = bank_cfg["rfc_function"]
        self.params = {"sap-client": str(sap.get("client", "200"))}
        self.timeout = int(sap.get("timeout", 90))
        self.s = requests.Session()
        self.s.auth = HTTPBasicAuth(sap["user"], sap["password"])
        self.s.verify = bool(sap.get("verify_ssl", False))

    def _call(self, fm: str, params: Dict[str, Any]) -> ET.Element:
        body = "".join(
            f"<{k}>{xml_escape(str(v))}</{k}>"
            for k, v in params.items()
            if v not in (None, "")
        )
        env = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
            f'xmlns:urn="{self.NS}"><soapenv:Body><urn:{fm}>{body}</urn:{fm}></soapenv:Body></soapenv:Envelope>'
        )
        started = datetime.now()
        try:
            r = self.s.post(
                self.url,
                params=self.params,
                data=env.encode("utf-8"),
                timeout=self.timeout,
                headers={
                    "Content-Type": "text/xml; charset=utf-8",
                    "SOAPAction": f"{self.NS}:{fm}",
                },
            )
        except requests.RequestException as e:
            logger.error(f"[SAP RFC] {fm}: no response after {(datetime.now() - started).total_seconds():.1f}s: {e}")
            raise
        secs = (datetime.now() - started).total_seconds()
        root = ET.fromstring(r.content) if r.content.strip().startswith(b"<") else None
        fault = root.find(".//faultstring") if root is not None else None
        if r.status_code != 200 or fault is not None:
            detail = fault.text if fault is not None else r.text[:300]
            logger.warning(f"[SAP RFC] {fm}: HTTP {r.status_code} in {secs:.1f}s: {detail}")
            raise RuntimeError(f"SOAP-RFC {fm} failed (HTTP {r.status_code}): {detail}")
        logger.info(f"[SAP RFC] {fm}: HTTP 200 in {secs:.1f}s")
        resp = root.find(f".//{{{self.NS}}}{fm}.Response")
        if resp is None:
            raise RuntimeError(f"SOAP-RFC {fm}: unexpected response {r.text[:300]}")
        return resp

    def available(self) -> Tuple[bool, str]:
        try:
            self._call("RFC_GET_FUNCTION_INTERFACE", {"FUNCNAME": self.fm})
            return True, "ok"
        except Exception as e:
            return False, f"SAP function {self.fm} not available: {e}"

    def post(self, item_id: str, item: Dict[str, Any], match_by: str) -> Dict[str, Any]:
        def d(v: Optional[date]) -> Optional[str]:
            return v.isoformat() if v else None

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
            "IV_INVOICE_REF": item["invoice_ref"],
            "IV_UTR": item["utr"],
            "IV_SGTXT": item["item_text"],
        }
        try:
            resp = self._call(self.fm, params)
        except Exception as e:
            return {"Status": "E", "Message": str(e)}
        out = {el.tag: (el.text or "").strip() for el in resp}
        augdt = out.get("EV_AUGDT", "")
        return {
            "Status": out.get("EV_STATUS"),
            "Belnr": out.get("EV_BELNR"),
            "Gjahr": out.get("EV_GJAHR") if out.get("EV_GJAHR") != "0000" else None,
            "Augbl": out.get("EV_AUGBL"),
            "Augdt": augdt if augdt and not augdt.startswith("0000") else None,
            "Message": out.get("EV_MESSAGE"),
        }


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------
def process_bank_posting(
    file_path: Optional[str] = None,
    items: Optional[List[Dict[str, Any]]] = None,
    sheet: Optional[str] = None,
    dry_run: bool = False,
    match_by: Optional[str] = None,
    check_sap_open_items: Optional[bool] = None,
    update_db: bool = True,
    tenant_id: Optional[str] = None,
    org_id: Optional[str] = None,
    sheet_rows: Optional[List[tuple]] = None,
) -> Dict[str, Any]:
    cfg = load_bank_config()
    do_check = (
        cfg["check_open_items"]
        if check_sap_open_items is None
        else check_sap_open_items
    )

    if sheet_rows is not None:
        raw_items = bank_items_from_rows(sheet_rows)
    elif file_path:
        raw_items = read_excel(file_path, sheet, tenant_id=tenant_id, org_id=org_id)
    elif items:
        raw_items = [
            {**it, "row": it.get("row", i)} for i, it in enumerate(items, start=1)
        ]
    else:
        raise ValueError("Provide either 'file_path' or 'items'")

    inferred_match_by = raw_items[0].get("_inferred_match_by") if raw_items else None
    match_by = (match_by or inferred_match_by or cfg["default_match_by"]).upper()
    if match_by not in MATCH_FIELDS:
        raise ValueError(f"match_by must be one of {MATCH_FIELDS}")

    batch_id = f"BNK{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:4].upper()}"
    logger.info(
        f"[BANK POST] {batch_id}: {len(raw_items)} row(s), dry_run={dry_run}, match_by={match_by}"
    )

    sap_tables, ignored_tables = configured_sap_tables()
    index = None
    if do_check:
        try:
            index = OpenItemIndex(sap_tables)
        except Exception as e:
            raise RuntimeError(
                f"Could not read {', '.join(sap_tables)} from SAP: {e}"
            ) from e
    else:
        sap_tables = []

    results: List[Dict[str, Any]] = []
    prepared: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    seen = set()

    for raw in raw_items:
        item = normalise(raw, cfg)
        errs = validate(item)
        key = (item["company_code"], item["customer"], item["invoice_ref"].upper())
        if not errs and key in seen:
            errs.append(
                "Duplicate row in this batch for the same company/customer/invoice"
            )
        seen.add(key)

        extra: Dict[str, Any] = {}
        if not errs and index is not None:
            oi_errs, extra = check_open_items(item, index, match_by)
            errs.extend(oi_errs)

        res = {
            "row": item["row"],
            "status": "INVALID" if errs else "VALID",
            "company_code": item["company_code"],
            "customer": item["customer"],
            "invoice_ref": item["invoice_ref"],
            "utr": item["utr"] or None,
            "amount": float(item["amount"]) if item["amount"] is not None else None,
            "currency": item["currency"],
            "document_no": None,
            "fiscal_year": None,
            "clearing_doc": None,
            "clearing_date": None,
            "message": "; ".join(errs) if errs else "Validation passed",
            **extra,
        }
        results.append(res)
        if not errs:
            prepared.append((item, res))

    sap_status = "skipped (dry_run)" if dry_run else None
    if not dry_run and prepared:
        client = SAPBankPostingClient(cfg)
        ok, msg = client.available()
        if not ok:
            sap_status = msg
            for _, res in prepared:
                res["status"] = "NOT_POSTED"
                res["message"] = msg
        else:
            sap_status = "ok"
            for n, (item, res) in enumerate(prepared, start=1):
                d = client.post(f"{batch_id}-{n:04d}", item, match_by)
                sap_st = d.get("Status")
                success = sap_st == "S"
                res.update(
                    {
                        "status": {"S": "SUCCESS", "W": "POSTED_NOT_CLEARED"}.get(
                            sap_st, "ERROR"
                        ),
                        "document_no": d.get("Belnr") or None,
                        "fiscal_year": d.get("Gjahr") or None,
                        "clearing_doc": d.get("Augbl") or None,
                        "clearing_date": d.get("Augdt"),
                        "message": d.get("Message")
                        or ("Posted" if success else "Unknown SAP error"),
                    }
                )
                logger.info(
                    f"[BANK POST] row {res['row']}: {res['status']} {res['document_no']} - {res['message']}"
                )

    # Write SAP document number back to active_bt3i / active_bs (documentid)
    db_update: Dict[str, Any] = {"skipped": "dry_run" if dry_run else "disabled"}
    if not dry_run and update_db:
        posted = []
        for r in results:
            if r["status"] in ("SUCCESS", "POSTED_NOT_CLEARED") and r["document_no"]:
                if r.get("invoice_ref"):
                    posted.append(
                        {
                            "assignment": r["invoice_ref"],
                            "document_no": r["document_no"],
                        }
                    )
                if r.get("utr"):
                    posted.append(
                        {"assignment": r["utr"], "document_no": r["document_no"]}
                    )
        if posted:
            try:
                from bank_posting.document_link import link_document_ids

                db_update = link_document_ids(posted, org_id=org_id)
            except Exception as e:
                logger.error(f"[BANK POST] documentid update failed: {e}")
                db_update = {"error": str(e)}
        else:
            db_update = {"skipped": "no posted documents"}

    counts: Dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    return {
        "status": "completed",
        "batch_id": batch_id,
        "dry_run": dry_run,
        "match_by": match_by,
        "source": file_path or "json",
        "sap_posting": sap_status,
        "sap_tables": {"used": sap_tables, "ignored": ignored_tables},
        "total": len(results),
        "summary": counts,
        "db_update": db_update,
        "results": results,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# Entry point for API requests: bank or invoice file
# ---------------------------------------------------------------------------
def process_request(
    file_type: Optional[str] = None,
    file_path: Optional[str] = None,
    items: Optional[List[Dict[str, Any]]] = None,
    sheet: Optional[str] = None,
    dry_run: bool = False,
    match_by: Optional[str] = None,
    check_sap_open_items: Optional[bool] = None,
    update_db: bool = True,
    tenant_id: Optional[str] = None,
    org_id: Optional[str] = None,
    company_code: Optional[str] = None,
) -> Dict[str, Any]:
    """
    file_type: "bank" (F-30 receipt + clearing) or "invoice" (FB70 invoice validation).
    If omitted, it is detected from the Excel header (or assumed "bank" for items).
    """
    ftype = (file_type or "").strip().lower() or None
    if ftype and ftype not in FILE_TYPES:
        raise ValueError(f"file_type must be one of {FILE_TYPES}")

    rows: Optional[List[tuple]] = None
    if file_path:
        rows = load_sheet_rows(file_path, sheet, tenant_id=tenant_id, org_id=org_id)
        detected = detect_file_type(rows)
        if ftype and detected and ftype != detected:
            raise ValueError(
                f"file_type is '{ftype}' but the Excel columns look like a {detected} file"
            )
        ftype = ftype or detected
        if not ftype:
            raise ValueError(
                "Could not tell if this is a bank or invoice file from its header; "
                "set file_type to 'bank' or 'invoice'"
            )
    ftype = ftype or "bank"

    if ftype == "invoice":
        from bank_posting.invoice import process_invoice_file

        return process_invoice_file(
            sheet_rows=rows,
            items=None if rows is not None else items,
            source=file_path or "json",
            dry_run=dry_run,
            match_by=match_by,
            update_db=update_db,
            company_code=company_code,
            org_id=org_id,
        )

    res = process_bank_posting(
        file_path=file_path,
        items=items,
        sheet=sheet,
        dry_run=dry_run,
        match_by=match_by,
        check_sap_open_items=check_sap_open_items,
        update_db=update_db,
        tenant_id=tenant_id,
        org_id=org_id,
        sheet_rows=rows,
    )
    return {"file_type": "bank", **res}


if __name__ == "__main__":
    import argparse, json

    p = argparse.ArgumentParser(description="Bank receipt posting with clearing")
    p.add_argument("file_path")
    p.add_argument("--sheet")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--match-by", choices=MATCH_FIELDS)
    p.add_argument("--no-sap-check", action="store_true")
    a = p.parse_args()
    out = process_bank_posting(
        a.file_path,
        sheet=a.sheet,
        dry_run=a.dry_run,
        match_by=a.match_by,
        check_sap_open_items=not a.no_sap_check,
    )
    print(json.dumps(out, indent=2, default=str))
