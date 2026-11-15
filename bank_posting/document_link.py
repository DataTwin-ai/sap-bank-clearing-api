"""
After SAP posting: write the SAP document number back to DataTwin tables.

Matching rules (same as the AR Inbox flow):
    Assignment == active_bt3i.documentnumber   -> active_bt3i.documentid = DocumentNo
    Assignment == active_bs.transactionref     -> active_bs.documentid   = DocumentNo
Only rows whose documentid IS NULL are touched; only the documentid column is updated.
"""

from typing import Any, Dict, List, Optional

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values

from db_manager import get_db_credentials, get_engine
from logger_setup import get_logger

logger = get_logger("SAP_Pipeline.BankPosting.DocLink")

INV_TABLE = "active_bt3i"
INV_PK = ["processcode", "themename", "transactiontype", "orgid", "documentnumber", "sno"]
BANK_TABLE = "active_bs"
BANK_PK = ["processcode", "themename", "transactiontype", "orgid", "accountnumber", "transactiondate", "sno"]


def connect_document_id(df: pd.DataFrame, inv_result: pd.DataFrame, bank: pd.DataFrame):
    """
    Matching rules:

    df['Assignment'] == inv_result['documentnumber']
    df['DocumentNo']  -> invoice_result['documentid']

    df['Assignment'] == bank['transactionref']
    df['DocumentNo']  -> bank_result['documentid']
    """
    source_df = df.copy()
    invoice_df = inv_result.copy()
    bank_df = bank.copy()

    def normalize_reference(series: pd.Series) -> pd.Series:
        normalized = (
            series.astype("string")
            .str.strip()
            .str.upper()
            .str.replace(r"\.0$", "", regex=True)
        )
        return normalized.mask(normalized.isna() | normalized.eq(""))

    # Remove existing documentid to prevent documentid_x/documentid_y
    invoice_df.drop(columns=["documentid"], errors="ignore", inplace=True)
    bank_df.drop(columns=["documentid"], errors="ignore", inplace=True)

    source_df["_match_key"] = normalize_reference(source_df["Assignment"])
    invoice_df["_match_key"] = normalize_reference(invoice_df["documentnumber"])
    bank_df["_match_key"] = normalize_reference(bank_df["transactionref"])

    # Assignment to DocumentNo mapping
    document_mapping = (
        source_df.loc[source_df["_match_key"].notna(), ["_match_key", "DocumentNo"]]
        .rename(columns={"DocumentNo": "documentid"})
        .drop_duplicates(subset=["_match_key"], keep="first")
    )

    invoice_result = invoice_df.merge(document_mapping, on="_match_key", how="inner")
    bank_result = bank_df.merge(document_mapping, on="_match_key", how="inner")

    invoice_result.drop(columns=["_match_key"], inplace=True)
    bank_result.drop(columns=["_match_key"], inplace=True)

    return invoice_result, bank_result


def _update_documentid(cur, table: str, pk: List[str], rows: pd.DataFrame) -> int:
    """UPDATE <table> SET documentid = v.documentid WHERE PK matches AND documentid IS NULL."""
    if rows.empty:
        return 0
    cols = pk + ["documentid"]
    values = [tuple(None if pd.isna(v) else v for v in r) for r in rows[cols].itertuples(index=False)]
    casts = {"transactiondate": "::date"}
    join = " AND ".join(f"t.{c} = v.{c}{casts.get(c, '')}" for c in pk)
    sql = f"""
        UPDATE public.{table} AS t
        SET documentid = v.documentid
        FROM (VALUES %s) AS v ({', '.join(cols)})
        WHERE {join} AND t.documentid IS NULL
    """
    execute_values(cur, sql, values, page_size=500)
    return cur.rowcount


def link_document_ids(posted: List[Dict[str, Any]], env: Optional[str] = None) -> Dict[str, Any]:
    """
    posted: [{"assignment": <invoice ref / UTR>, "document_no": <SAP doc>}, ...]
    Returns counts and the matched keys.
    """
    src = pd.DataFrame(
        [{"Assignment": p["assignment"], "DocumentNo": p["document_no"]}
         for p in posted if p.get("assignment") and p.get("document_no")]
    )
    if src.empty:
        return {"invoices_updated": 0, "bank_updated": 0, "message": "nothing to link"}

    engine = get_engine(env)
    inv_result = pd.read_sql_query(
        f"select * from {INV_TABLE} where transactiontype = 'Invoice' and documentid is null", engine
    )
    bank_result = pd.read_sql_query(
        f"select * from {BANK_TABLE} where transactiontype = 'Bank' and documentid is null", engine
    )

    matched_inv, matched_bank = connect_document_id(df=src, inv_result=inv_result, bank=bank_result)

    creds = get_db_credentials(env)
    conn = psycopg2.connect(host=creds["host"], port=creds["port"], user=creds["user"],
                            password=creds["password"], dbname=creds["database"])
    try:
        with conn.cursor() as cur:
            n_inv = _update_documentid(cur, INV_TABLE, INV_PK, matched_inv)
            n_bank = _update_documentid(cur, BANK_TABLE, BANK_PK, matched_bank)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    logger.info(f"[DOC LINK] {INV_TABLE}: {n_inv} row(s), {BANK_TABLE}: {n_bank} row(s) updated with SAP documentid")
    return {
        "invoices_updated": n_inv,
        "bank_updated": n_bank,
        "invoice_matches": matched_inv[["documentnumber", "documentid"]].to_dict("records") if n_inv else [],
        "bank_matches": matched_bank[["transactionref", "documentid"]].to_dict("records") if n_bank else [],
    }
