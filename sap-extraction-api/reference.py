"""
SAP reference data used to complete SAP rows before ingestion:

  sap_ref_company  (bukrs -> waers, land1, butxt)   from SAP T001  - company code currency
  sap_ref_customer (kunnr -> name1, land1)          from SAP KNA1  - customer name

Both tables are small (hundreds of rows). They are refreshed from SAP when older than
[sync] reference_refresh_minutes (60), or sooner when a row needs a company code / customer
that is not in them yet (at most every [sync] reference_retry_minutes, 10, so a code that does
not exist in SAP does not cause an SAP call every minute).
"""

from typing import Any, Dict, Iterable, Optional

import settings
from logger_setup import get_logger

logger = get_logger("SAP_Pipeline.Reference")

COMPANY_TABLE = "sap_ref_company"
CUSTOMER_TABLE = "sap_ref_customer"
STATE_TABLE = "sap_extraction_state"


def ensure_tables(cur) -> None:
    cur.execute(f"""CREATE TABLE IF NOT EXISTS public.{COMPANY_TABLE} (
                        bukrs text PRIMARY KEY, waers text, land1 text, butxt text,
                        updated_at timestamptz NOT NULL DEFAULT now())""")
    cur.execute(f"""CREATE TABLE IF NOT EXISTS public.{CUSTOMER_TABLE} (
                        kunnr text PRIMARY KEY, name1 text, land1 text,
                        updated_at timestamptz NOT NULL DEFAULT now())""")


def _minutes(key: str, default: int) -> int:
    return max(1, settings.get_int("sync", key, None, default))


def _last_refresh_age_minutes(cur) -> Optional[float]:
    cur.execute(f"""SELECT EXTRACT(EPOCH FROM now() - last_poll_at) / 60 FROM public.{STATE_TABLE}
                    WHERE target_table = %s""", (CUSTOMER_TABLE,))
    row = cur.fetchone()
    return float(row[0]) if row and row[0] is not None else None


def _get(r: Dict[str, Any], key: str) -> str:
    v = r.get(key.lower(), r.get(key.upper()))
    return "" if v is None else str(v).strip()


def refresh(cur, odata=None) -> Dict[str, int]:
    """Reloads both tables from SAP (T001, KNA1) inside the caller's transaction."""
    from sap_odata_extractor import SAPODataExtractor
    odata = odata or SAPODataExtractor()
    ensure_tables(cur)
    out = {}
    for sap_table, table, key, cols in (
        ("T001", COMPANY_TABLE, "bukrs", ("waers", "land1", "butxt")),
        ("KNA1", CUSTOMER_TABLE, "kunnr", ("name1", "land1")),
    ):
        snap = odata.fetch_records(sap_table, quiet=True)
        rows = {}
        for r in snap["rows"]:
            k = _get(r, key)
            if k:
                rows[k] = tuple(_get(r, c) or None for c in cols)
        if rows:
            placeholders = ", ".join(["%s"] * (len(cols) + 1))
            sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols)
            from psycopg2.extras import execute_values
            execute_values(cur, f"""INSERT INTO public.{table} ({key}, {', '.join(cols)}) VALUES %s
                                    ON CONFLICT ({key}) DO UPDATE SET {sets}, updated_at = now()""",
                           [(k,) + v for k, v in rows.items()], template=f"({placeholders})")
        out[sap_table] = len(rows)
    cur.execute(f"""INSERT INTO public.{STATE_TABLE} (target_table, sap_table, last_poll_at, last_success_at, fetched_rows)
                    VALUES (%s, 'KNA1+T001', now(), now(), %s)
                    ON CONFLICT (target_table) DO UPDATE SET last_poll_at = now(), last_success_at = now(),
                        fetched_rows = EXCLUDED.fetched_rows, last_error = NULL, updated_at = now()""",
                (CUSTOMER_TABLE, out.get("KNA1", 0)))
    logger.info(f"[REF] refreshed from SAP: T001 {out.get('T001', 0)} company codes, KNA1 {out.get('KNA1', 0)} customers")
    return out


def refresh_if_needed(cur, kunnrs: Iterable[str] = (), bukrs: Iterable[str] = ()) -> Optional[Dict[str, int]]:
    """
    Refreshes when stale, or when a needed code is missing (rate limited). Never raises and never
    leaves the caller's transaction aborted (everything runs under a savepoint).
    """
    cur.execute("SAVEPOINT ref_refresh")
    try:
        ensure_tables(cur)
        age = _last_refresh_age_minutes(cur)
        stale = age is None or age >= _minutes("reference_refresh_minutes", 60)
        if not stale:
            kunnrs = sorted({k for k in kunnrs if k})
            bukrs = sorted({b for b in bukrs if b})
            missing = False
            if kunnrs:
                cur.execute(f"SELECT count(*) FROM public.{CUSTOMER_TABLE} WHERE kunnr = ANY(%s)", (kunnrs,))
                missing = cur.fetchone()[0] < len(kunnrs)
            if not missing and bukrs:
                cur.execute(f"SELECT count(*) FROM public.{COMPANY_TABLE} WHERE bukrs = ANY(%s)", (bukrs,))
                missing = cur.fetchone()[0] < len(bukrs)
            if not (missing and age >= _minutes("reference_retry_minutes", 10)):
                cur.execute("RELEASE SAVEPOINT ref_refresh")
                return None
        res = refresh(cur)
        cur.execute("RELEASE SAVEPOINT ref_refresh")
        return res
    except Exception as exc:
        cur.execute("ROLLBACK TO SAVEPOINT ref_refresh")
        logger.warning(f"[REF] refresh from SAP failed, using the stored values: {exc}")
        return None
