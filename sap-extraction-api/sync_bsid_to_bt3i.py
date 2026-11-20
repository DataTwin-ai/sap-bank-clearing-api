"""
=============================================================================
Transformation & Ingestion: active_bsid -> active_bt3h (AR Invoices)
=============================================================================
Features:
  1. Fully Dynamic: NO hardcoded row limits. Handles any volume of records.
  2. Extracts only required AR columns needed for DataTwin Agentic Finance UI.
  3. Filters only valid Invoice documents (Debit 'S', DR, RV, etc.).
  4. Robust UPSERT on PRIMARY KEY (processcode, themename, transactiontype, orgid, documentnumber, sno).
  5. Can be called standalone, via chained CLI, or via REST API.

Target: active_bt3h (header table), processcode BT3H / themename IT_Invoice / transactiontype Invoice.
Previously this wrote to active_bt3i (backup: sync_bsid_to_bt3i.py.bak_20261120).
=============================================================================
"""

import re
import uuid
from datetime import datetime, date
from typing import Optional, Dict, Any, List, Tuple

import psycopg2
from psycopg2.extras import execute_values

import settings
from db_manager import get_db_credentials
from context import ReserveMasterObjectIDs
from logger_setup import get_logger

logger = get_logger("SAP_Pipeline.BT3H_Sync")

TARGET_TABLE = "active_bt3h"
PROCESS_CODE = "BT3H"
THEME_NAME = "IT_Invoice"
TRANSACTION_TYPE = "Invoice"


def parse_sap_date(val: Any) -> Optional[date]:
    """
    Parses various SAP date representations into a Python date:
      - '/Date(1788998400000)/' (OData JSON timestamp)
      - '2026-10-23' or '2026/10/23'
      - '20261023' (SAP DATS format)
      - datetime / date objects
    """
    if val is None or str(val).strip() in ("", "nan", "None", "<NA>", "NaT"):
        return None

    if isinstance(val, (date, datetime)):
        return val if isinstance(val, date) else val.date()

    val_str = str(val).strip()

    # OData /Date(1788998400000)/ format
    if "/Date(" in val_str:
        m = re.search(r"-?\d+", val_str)
        if m:
            ts_ms = int(m.group(0))
            if ts_ms > 0:
                return datetime.utcfromtimestamp(ts_ms / 1000.0).date()

    # Standard YYYY-MM-DD or YYYY/MM/DD
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y", "%Y%m%d"):
        try:
            return datetime.strptime(val_str[:10], fmt).date()
        except ValueError:
            pass

    return None


def sync_bsid_to_bt3h(
    env: Optional[str] = None,
    doc_types: Optional[List[str]] = None,
    company_code: Optional[str] = None,
    org_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    batch_size: int = 1000
) -> Dict[str, Any]:
    """
    Dynamically transforms all open invoice records from active_bsid
    and upserts them into active_bt3h.

    NO hardcoded row limit: Processes ALL matching records in batches.
    """
    creds = get_db_credentials(env)

    target_org_id = org_id or settings.get("defaults", "org_id", "ORG_ID", "OM0000000001")
    target_tenant_id = tenant_id or settings.get("defaults", "tenant_id", "TENANT_ID", "DM00005")

    logger.info(
        f"[BT3H SYNC] Starting dynamic transformation: active_bsid -> {TARGET_TABLE} "
        f"on {creds['host']}:{creds['port']}/{creds['database']} (Org: {target_org_id}, Tenant: {target_tenant_id})..."
    )

    # 1. Connect to PostgreSQL
    conn = psycopg2.connect(
        host=creds["host"],
        port=creds["port"],
        user=creds["user"],
        password=creds["password"],
        dbname=creds["database"]
    )
    conn.autocommit = False
    cur = conn.cursor()

    try:
        # 2. Detect active_bsid schema dynamically (SAP DDIC BSID via OData vs Fiori CDS columns)
        cur.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'active_bsid';
        """)
        bsid_cols = {row[0].lower() for row in cur.fetchall()}

        if not bsid_cols:
            logger.warning("[BT3H SYNC] Table public.active_bsid does not exist or has no columns.")
            return {
                "status": "success",
                "records_processed": 0,
                "message": "active_bsid table not found"
            }

        params: List[Any] = []
        if "belnr" in bsid_cols and "bukrs" in bsid_cols:
            def dd_col(col: str, cast: str = "text") -> str:
                return f"b.{col}::{cast}" if col in bsid_cols else f"NULL::{cast}"

            cur.execute("""
                SELECT COUNT(DISTINCT column_name) = 2
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'active_kna1'
                  AND column_name IN ('kunnr', 'name1');
            """)
            has_kna1 = bool(cur.fetchone()[0])

            kna1_join = (
                "LEFT JOIN (SELECT DISTINCT ON (kunnr) kunnr, name1 FROM public.active_kna1 "
                "WHERE kunnr IS NOT NULL) k ON k.kunnr = b.kunnr"
                if has_kna1 else ""
            )
            cust_name_expr = (
                "NULLIF(TRIM(k.name1::text), '')"
                if has_kna1 else "NULL::text"
            )

            filter_clauses = [
                "(COALESCE(b.shkzg::text, '') = 'S' OR COALESCE(b.blart::text, '') IN ('DR', 'RV', 'DG', 'AB'))",
                "COALESCE(NULLIF(TRIM(b.dmbtr::text), ''), '0')::numeric > 0"
            ]
            if doc_types and "blart" in bsid_cols:
                filter_clauses.append("b.blart = ANY(%s)")
                params.append(doc_types)
            if company_code and "bukrs" in bsid_cols:
                filter_clauses.append("b.bukrs = %s")
                params.append(company_code)
            if "sap_status" in bsid_cols:
                # delta table: rows no longer in SAP (cleared) are kept as REMOVED - not open invoices
                filter_clauses.append("b.sap_status <> 'REMOVED'")

            where_sql = " AND ".join(filter_clauses)
            gen_id_expr = (
                f"COALESCE({dd_col('bukrs')}, '') || COALESCE({dd_col('belnr')}, '') || "
                f"COALESCE({dd_col('gjahr')}, '') || COALESCE({dd_col('buzei')}, '')"
            )
            select_query = f"""
                SELECT
                    {dd_col('belnr')} AS accountingdocument,
                    {dd_col('blart')} AS accountingdocumenttype,
                    {dd_col('bukrs')} AS companycode,
                    {dd_col('kunnr')} AS customer,
                    {cust_name_expr} AS customername,
                    {dd_col('bldat')} AS documentdate,
                    {dd_col('budat')} AS postingdate,
                    {dd_col('xblnr')} AS documentreferenceid,
                    COALESCE(NULLIF(TRIM({dd_col('dmbtr')}), ''), '0')::numeric AS amountincompanycodecurrency,
                    {dd_col('waers')} AS companycodecurrency,
                    {dd_col('xref1')} AS reference1idbybusinesspartner,
                    {dd_col('zuonr')} AS assignmentreference,
                    ({gen_id_expr}) AS generatedid
                FROM public.active_bsid AS b
                {kna1_join}
                WHERE {where_sql}
                ORDER BY b.belnr;
            """
        else:
            # Legacy Fiori CDS schema (accountingdocument, debitcreditcode, etc.)
            filter_clauses = [
                "(debitcreditcode = 'S' OR accountingdocumenttype IN ('DR', 'RV', 'DG', 'AB'))",
                "amountincompanycodecurrency > 0"
            ]
            if doc_types:
                filter_clauses.append("accountingdocumenttype = ANY(%s)")
                params.append(doc_types)
            if company_code:
                filter_clauses.append("companycode = %s")
                params.append(company_code)

            where_sql = " AND ".join(filter_clauses)
            select_query = f"""
                SELECT 
                    accountingdocument,
                    accountingdocumenttype,
                    companycode,
                    customer,
                    customername,
                    documentdate,
                    postingdate,
                    documentreferenceid,
                    amountincompanycodecurrency,
                    companycodecurrency,
                    reference1idbybusinesspartner,
                    assignmentreference,
                    generatedid
                FROM public.active_bsid
                WHERE {where_sql}
                ORDER BY accountingdocument;
            """

        cur.execute(select_query, params)
        rows = cur.fetchall()
        total_invoices = len(rows)

        if total_invoices == 0:
            logger.warning("[BT3H SYNC] No matching invoice records found in active_bsid.")
            return {
                "status": "success",
                "records_processed": 0,
                "message": "No invoice records found in active_bsid"
            }

        # 3. Deduplication by checking ALL columns:
        # A row is ONLY a duplicate if EVERY single column value is identical.
        # If any column (amount, customer, company code, date, reference) is different,
        # it is treated as a DIFFERENT distinct line item / invoice.
        seen_signatures = set()
        distinct_rows = []
        for r in rows:
            # Tuple of all column values in the row
            row_sig = tuple(str(x).strip() if x is not None else "" for x in r)
            if row_sig in seen_signatures:
                continue
            seen_signatures.add(row_sig)
            distinct_rows.append(r)

        exact_duplicates_count = total_invoices - len(distinct_rows)
        if exact_duplicates_count > 0:
            logger.info(
                f"[DEDUP ALL COLUMNS] Found and dropped {exact_duplicates_count} exact duplicates "
                f"(where ALL columns were identical). {len(distinct_rows)} unique rows remaining."
            )

        logger.info(
            f"[BT3H SYNC] Processing {len(distinct_rows)} distinct invoice records from active_bsid "
            f"(checked all columns across {total_invoices} raw rows)."
        )

        # 4a. Resolve Context IDs (GroupTransactionRef) - format CTXSAP<org2><FY><seq7>
        #     One context ID per SAP document (companycode + accountingdocument).
        #     Existing ids (active_bt3h, then the old active_bt3i rows) are reused,
        #     so a re-sync never changes them.
        def _doc_keys(r) -> Tuple[str, str, str]:
            acct_doc, comp_code, doc_ref_id = r[0], r[2], r[7]
            d_id = str(acct_doc).strip()
            d_num = str(doc_ref_id).strip() if doc_ref_id and str(doc_ref_id).strip() not in ("", "nan", "None") else d_id
            return str(comp_code).strip(), d_id, d_num

        existing_ctx: Dict[Tuple[str, str], str] = {}
        for tbl in ("active_bt3i", TARGET_TABLE):  # later table wins
            cur.execute(
                f"""
                SELECT DISTINCT ON (documentnumber, documentid) documentnumber, documentid, grouptransactionref
                FROM public.{tbl}
                WHERE orgid = %s AND source_table = 'active_bsid'
                  AND grouptransactionref LIKE 'CTXSAP%%';
                """,
                (target_org_id,),
            )
            existing_ctx.update({(row[0], row[1]): row[2] for row in cur.fetchall()})

        ctx_map: Dict[Tuple[str, str], str] = {}
        new_docs: List[Tuple[str, str]] = []
        for r in distinct_rows:
            c_code, d_id, d_num = _doc_keys(r)
            key = (c_code, d_id)
            if key in ctx_map or key in new_docs:
                continue
            found = existing_ctx.get((d_num, d_id))
            if found:
                ctx_map[key] = found
            else:
                new_docs.append(key)

        if new_docs:
            new_ids = ReserveMasterObjectIDs(
                target_tenant_id, "CTXSAP", "MASTEROBJECTSEQUENCINGV2",
                target_org_id, len(new_docs), "", env,
            )
            ctx_map.update(zip(new_docs, new_ids))
        logger.info(
            f"[BT3H SYNC] Context IDs: {len(ctx_map) - len(new_docs)} reused, {len(new_docs)} newly generated."
        )

        # 4b. Transform records into active_bt3h schema
        transformed_records = []
        now_ts = datetime.now()
        sno_tracker: Dict[Tuple[str, str, str, str, str], int] = {}

        for r in distinct_rows:
            (
                acct_doc,
                doc_type,
                comp_code,
                cust_no,
                cust_name,
                doc_date_raw,
                post_date_raw,
                doc_ref_id,
                amount,
                currency_code,
                ref1_partner,
                assign_ref,
                gen_id
            ) = r

            # Clean & resolve fields
            doc_id_str = str(acct_doc).strip()
            
            # Document Number: use documentreferenceid if available, otherwise accountingdocument
            doc_num_str = str(doc_ref_id).strip() if doc_ref_id and str(doc_ref_id).strip() not in ("", "nan", "None") else doc_id_str

            # Dynamic unique sno sequence per (processcode, themename, transactiontype, orgid, documentnumber)
            pk_key = (PROCESS_CODE, THEME_NAME, TRANSACTION_TYPE, target_org_id, doc_num_str)
            sno_tracker[pk_key] = sno_tracker.get(pk_key, 0) + 1
            sno_val = str(sno_tracker[pk_key])

            # Parse document date, fallback to posting date
            parsed_post_date = parse_sap_date(post_date_raw)
            parsed_doc_date = parse_sap_date(doc_date_raw) or parsed_post_date or now_ts.date()

            # Customer Name & Code
            customer_name_clean = str(cust_name).strip() if cust_name else f"Customer {cust_no}"
            customer_code_clean = str(cust_no).strip() if cust_no else ""

            # Values
            gross_amount = float(amount) if amount is not None else 0.0
            net_amount = gross_amount
            currency_clean = str(currency_code).strip() if currency_code else "INR"

            # Context ID (GroupTransactionRef) - pre-resolved in step 4a
            context_id = ctx_map[(str(comp_code).strip(), doc_id_str)]

            # LinkRef (UTR Ref / Bank Reference)
            link_ref_clean = str(ref1_partner).strip() if ref1_partner and str(ref1_partner).strip() not in ("", "nan", "None") else None

            # Primary Key Tuple: (processcode, themename, transactiontype, orgid, documentnumber, sno)
            record_tuple = (
                PROCESS_CODE,           # processcode (PK)
                THEME_NAME,             # themename (PK)
                TRANSACTION_TYPE,       # transactiontype (PK)
                target_org_id,          # orgid (PK)
                doc_num_str,            # documentnumber (PK)
                sno_val,                # sno (PK)
                context_id,             # grouptransactionref
                gross_amount,           # basevalue
                net_amount,             # documentvalue
                0.0,                    # tdsvalue
                0.0,                    # adjustedamount
                doc_id_str,             # documentid (SAP Doc Ref)
                parsed_doc_date,        # documentdate
                parsed_post_date,       # postingdate
                customer_name_clean,    # sendername
                customer_code_clean,    # senderaccount
                gross_amount,           # totalbasevalue
                net_amount,             # totaldocumentvalue
                currency_clean,         # currency
                currency_clean,         # transactioncurrency
                currency_clean,         # localcurrency
                0.0,                    # taxpercentage
                link_ref_clean,         # linkref
                "Invoice",              # documenttype
                "active_bsid",          # source_table
                target_tenant_id,       # tenant_id
                now_ts,                 # createddatetime
                now_ts                  # modifieddatetime
            )
            transformed_records.append(record_tuple)

        # 5. Perform High-Performance UPSERT in batches
        upsert_sql = f"""
            INSERT INTO public.{TARGET_TABLE} (
                processcode, themename, transactiontype, orgid, documentnumber, sno,
                grouptransactionref, basevalue, documentvalue, tdsvalue, adjustedamount,
                documentid, documentdate, postingdate, sendername, senderaccount,
                totalbasevalue, totaldocumentvalue, currency, transactioncurrency, localcurrency,
                taxpercentage, linkref, documenttype,
                source_table, tenant_id, createddatetime, modifieddatetime
            ) VALUES %s
            ON CONFLICT (processcode, themename, transactiontype, orgid, documentnumber, sno)
            DO UPDATE SET
                grouptransactionref = COALESCE({TARGET_TABLE}.grouptransactionref, EXCLUDED.grouptransactionref),
                basevalue           = EXCLUDED.basevalue,
                documentvalue       = EXCLUDED.documentvalue,
                tdsvalue            = COALESCE({TARGET_TABLE}.tdsvalue, EXCLUDED.tdsvalue),
                adjustedamount      = COALESCE({TARGET_TABLE}.adjustedamount, EXCLUDED.adjustedamount),
                documentid          = EXCLUDED.documentid,
                documentdate        = EXCLUDED.documentdate,
                postingdate         = EXCLUDED.postingdate,
                sendername          = EXCLUDED.sendername,
                senderaccount       = EXCLUDED.senderaccount,
                totalbasevalue      = EXCLUDED.totalbasevalue,
                totaldocumentvalue  = EXCLUDED.totaldocumentvalue,
                currency            = EXCLUDED.currency,
                transactioncurrency = EXCLUDED.transactioncurrency,
                localcurrency       = EXCLUDED.localcurrency,
                linkref             = COALESCE(EXCLUDED.linkref, {TARGET_TABLE}.linkref),
                modifieddatetime    = NOW();
        """

        # Execute in chunks dynamically
        for i in range(0, len(transformed_records), batch_size):
            chunk = transformed_records[i : i + batch_size]
            execute_values(cur, upsert_sql, chunk, page_size=batch_size)

        conn.commit()
        logger.info(f"[BT3H SYNC SUCCESS] Successfully upserted {len(transformed_records)} invoices into {TARGET_TABLE}.")

        # 6. Verification count
        cur.execute(
            f"""
            SELECT COUNT(*)
            FROM {TARGET_TABLE}
            WHERE processcode = %s AND transactiontype = %s AND source_table = 'active_bsid';
            """,
            (PROCESS_CODE, TRANSACTION_TYPE),
        )
        synced_count = cur.fetchone()[0]

        return {
            "status": "success",
            "source_table": "active_bsid",
            "target_table": TARGET_TABLE,
            "invoices_found_in_bsid": total_invoices,
            "invoices_upserted_to_bt3h": len(transformed_records),
            "total_bsid_invoices_in_bt3h": synced_count,
            "org_id": target_org_id,
            "tenant_id": target_tenant_id,
            "timestamp": now_ts.isoformat(),
        }

    except Exception as e:
        conn.rollback()
        logger.error(f"[BT3H SYNC ERROR] Failed to sync to {TARGET_TABLE}: {e}")
        raise
    finally:
        cur.close()
        conn.close()


# Old name kept so pipeline.run_bt3i_sync and existing callers keep working
sync_bsid_to_bt3i = sync_bsid_to_bt3h


if __name__ == "__main__":
    import argparse
    from logger_setup import setup_logging

    setup_logging("cron")
    parser = argparse.ArgumentParser(description="Sync Invoice records from active_bsid to active_bt3h")
    parser.add_argument("--env", help="Environment section from config.ini (default: current_env)")
    parser.add_argument("--org-id", help="Organization ID override")
    parser.add_argument("--tenant-id", help="Tenant ID override")
    parser.add_argument("--company-code", help="Filter specific company code")
    parser.add_argument("--batch-size", type=int, default=1000, help="Batch size for bulk upsert (default: 1000)")
    args = parser.parse_args()

    result = sync_bsid_to_bt3h(
        env=args.env,
        company_code=args.company_code,
        org_id=args.org_id,
        tenant_id=args.tenant_id,
        batch_size=args.batch_size
    )
    print("\n" + "=" * 60)
    print("SYNC RESULT:")
    print("=" * 60)
    for k, v in result.items():
        print(f"  {k:<30} : {v}")
    print("=" * 60 + "\n")
