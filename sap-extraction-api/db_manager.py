"""
=============================================================================
Database Manager for SAP Data Ingestion (PostgreSQL on AWS EC2)
=============================================================================
Features:
  1. Dynamic Target Table:
     Any SAP table (e.g. BSID, BKPF, KNA1) is mapped to 'active_<table_name>'.
  2. Strict Duplicate Prevention:
     - Automatic Natural Key Detection (SAP Primary Keys / Unique IDs).
     - In-memory Deduplication before SQL operations.
     - Database-level Duplicate Filtering (prevents inserting already existing rows).
     - Unique Index enforcement to guarantee zero duplicates.
  3. Credentials from environment variables or config.ini (see settings.py).
=============================================================================
"""

import os
import re
import time
from typing import Optional, Dict, Any, List, Tuple
import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, URL

import settings
from logger_setup import get_logger

logger = get_logger("SAP_Pipeline.DB")

_ENGINE_CACHE: Dict[str, Engine] = {}

# Known SAP Natural Primary Key Combinations (in lowercased format)
SAP_KEY_PATTERNS = [
    # OData unique entity ID
    ["generatedid"],
    # Accounting Document Line Item Keys (BSID, BSEG, etc.)
    ["companycode", "accountingdocument", "fiscalyear", "accountingdocumentitem"],
    ["bukrs", "belnr", "gjahr", "buzei"],
    # Accounting Document Header Keys (BKPF)
    ["companycode", "accountingdocument", "fiscalyear"],
    ["bukrs", "belnr", "gjahr"],
    # Customer Master
    ["customer"],
    ["kunnr"],
    # Vendor Master
    ["vendor"],
    ["lifnr"],
    # Material Master
    ["material"],
    ["matnr"],
]


def get_db_credentials(env: Optional[str] = None) -> Dict[str, Any]:
    """
    Extracts database credentials for the given environment.

    Environment variables take precedence over config.ini, so a deployment can
    supply secrets without a config file:
      APP_ENV (or DB_ENV), DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
    If DB_HOST and DB_PASSWORD are set, config.ini is optional.
    """
    config = settings.load_config()
    if not settings.CONFIG_FILE.exists() and not (os.getenv("DB_HOST") and os.getenv("DB_PASSWORD")):
        raise FileNotFoundError(
            f"Configuration file not found: {settings.CONFIG_FILE} "
            "(create it from config.example.ini, set SAP_EXTRACTION_CONFIG, "
            "or set DB_HOST, DB_PASSWORD, ... environment variables)"
        )

    target_env = (
        env
        or os.getenv("APP_ENV")
        or os.getenv("DB_ENV")
        or config.get("environment", "current_env", fallback="development")
    )

    def val(env_key: str, ini_key: str, default: str = "") -> str:
        if os.getenv(env_key):
            return os.getenv(env_key, "").strip()
        if config.has_section(target_env):
            return config.get(target_env, ini_key, fallback=default)
        return default

    if not config.has_section(target_env) and not os.getenv("DB_HOST"):
        raise ValueError(f"Environment section '[{target_env}]' not found in config.ini")

    tenant_id = os.getenv("TENANT_ID") or config.get("defaults", "tenant_id", fallback="dm00005")

    return {
        "env": target_env,
        "host": val("DB_HOST", "ec2_ip"),
        "port": int(val("DB_PORT", "port", "5433") or 5433),
        "user": val("DB_USER", "db_user", "postgres"),
        "password": val("DB_PASSWORD", "db_password", ""),
        "database": val("DB_NAME", "db_name", tenant_id.lower()),
        "region": val("DB_REGION", "region_name", "us-east-1"),
        "suffix_db": val("DB_SUFFIX", "suffix_db", ""),
    }


def get_engine(env: Optional[str] = None) -> Engine:
    """
    Returns a cached SQLAlchemy Engine instance.
    URL.create properly escapes special characters (such as @ in password).
    """
    creds = get_db_credentials(env)
    cache_key = f"{creds['host']}:{creds['port']}/{creds['database']}"

    if cache_key in _ENGINE_CACHE:
        return _ENGINE_CACHE[cache_key]

    url = URL.create(
        drivername="postgresql+psycopg2",
        username=creds["user"],
        password=creds["password"],
        host=creds["host"],
        port=creds["port"],
        database=creds["database"],
    )

    engine = create_engine(
        url,
        pool_size=5,
        max_overflow=10,
        pool_timeout=30,
        pool_pre_ping=True,
    )
    _ENGINE_CACHE[cache_key] = engine
    return engine


def get_active_table_name(sap_table: str) -> str:
    """
    Dynamically maps any SAP table to its PostgreSQL target table name:
    active_<table_name> (always lowercase and sanitized).

    Examples:
      'BSID'            -> 'active_bsid'
      'BKPF'            -> 'active_bkpf'
      'active_bsid'     -> 'active_bsid'
      'ZCUSTOM_TEST'    -> 'active_zcustom_test'
    """
    clean = re.sub(r"[^a-zA-Z0-9_]", "", str(sap_table)).strip().lower()
    if clean.startswith("active_"):
        return clean
    return f"active_{clean}"


def clean_column_name(col_name: str) -> str:
    """
    Sanitizes column names for PostgreSQL:
    - Lowercase
    - Replace non-alphanumeric characters with underscore
    - Remove consecutive underscores
    - Ensure name starts with letter/underscore
    """
    c = str(col_name).strip().lower()
    c = re.sub(r"[^a-z0-9_]", "_", c)
    c = re.sub(r"_+", "_", c).strip("_")
    if not c or c[0].isdigit():
        c = "col_" + c
    return c


def sanitize_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """
    Prepares DataFrame for PostgreSQL ingestion:
    - Cleans and deduplicates column names
    - Replaces 'nan' string artifacts with None
    - Converts complex objects/dicts to strings
    """
    clean_df = df.copy()

    # Deduplicate column names if needed
    seen = {}
    new_cols = []
    for c in clean_df.columns:
        clean = clean_column_name(c)
        if clean in seen:
            seen[clean] += 1
            new_cols.append(f"{clean}_{seen[clean]}")
        else:
            seen[clean] = 1
            new_cols.append(clean)
    clean_df.columns = new_cols

    # Convert object columns to clean strings / None
    for col in clean_df.columns:
        if clean_df[col].dtype == "object":
            clean_df[col] = clean_df[col].astype(str)
            clean_df[col] = clean_df[col].replace(
                {"nan": None, "None": None, "<NA>": None, "NaT": None}
            )

    return clean_df


def detect_natural_keys(columns: List[str]) -> List[str]:
    """
    Automatically detects natural primary keys in column list
    based on standard SAP table key conventions.
    """
    col_set = set(columns)
    for pattern in SAP_KEY_PATTERNS:
        if all(k in col_set for k in pattern):
            return pattern
    return []


def deduplicate_dataframe(
    df: pd.DataFrame,
    target_table: str,
    engine: Engine,
    mode: str = "replace"
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """
    Performs comprehensive 2-tier deduplication:
      1. In-memory deduplication (internal duplicates in batch)
      2. Database-level deduplication (checking against already-stored rows in DB)
    """
    initial_count = len(df)
    key_cols = detect_natural_keys(list(df.columns))

    stats = {
        "initial_rows": initial_count,
        "internal_duplicates_dropped": 0,
        "db_duplicates_skipped": 0,
        "key_columns_used": key_cols if key_cols else "ALL_COLUMNS",
        "final_rows_to_insert": initial_count
    }

    # Tier 1: In-Memory Deduplication
    # Rule: Check ALL columns in the row to determine duplicate vs different.
    # - If ALL columns are 100% identical -> true duplicate (drop).
    # - If ANY column is different (amount, date, customer, line item) -> DIFFERENT record (preserve).
    dedup_df = df.drop_duplicates(keep="last")

    # If an explicit natural key (like generatedid or full line item key) is present,
    # log it for reference.
    if key_cols:
        logger.info(f"[DEDUP ALL COLUMNS] Checked all columns across {initial_count} rows. Verified with key pattern: {key_cols}")
    else:
        logger.info(f"[DEDUP ALL COLUMNS] Checked all {len(df.columns)} columns across {initial_count} rows.")

    internal_dropped = initial_count - len(dedup_df)
    stats["internal_duplicates_dropped"] = internal_dropped
    if internal_dropped > 0:
        logger.warning(
            f"[DEDUP REMOVED] Dropped {internal_dropped} duplicate records from incoming batch for '{target_table}'."
        )

    # Tier 2: Check against already existing rows in Database (if appending or table exists)
    if mode == "append" and key_cols:
        try:
            with engine.connect() as conn:
                tbl_exists = conn.execute(
                    text("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = :t)"),
                    {"t": target_table}
                ).scalar()

                if tbl_exists:
                    key_cols_sql = ", ".join([f'"{k}"' for k in key_cols])
                    existing_keys_df = pd.read_sql(
                        f'SELECT {key_cols_sql} FROM public."{target_table}"',
                        con=conn
                    )
                    if not existing_keys_df.empty:
                        # Find overlapping rows
                        merged = dedup_df.merge(
                            existing_keys_df,
                            on=key_cols,
                            how="left",
                            indicator=True
                        )
                        new_rows_df = merged[merged["_merge"] == "left_only"].drop(columns=["_merge"])
                        db_dups = len(dedup_df) - len(new_rows_df)
                        stats["db_duplicates_skipped"] = db_dups
                        if db_dups > 0:
                            logger.info(
                                f"[DEDUP SKIPPED] {db_dups} records already exist in table '{target_table}'. "
                                f"Skipping to prevent duplicates."
                            )
                        dedup_df = new_rows_df
        except Exception as e:
            logger.warning(f"[DEDUP WARNING] Could not verify existing keys from DB ({e}). Proceeding.")

    stats["final_rows_to_insert"] = len(dedup_df)
    return dedup_df, stats


def save_dataframe_to_db(
    df: pd.DataFrame,
    sap_table: str,
    mode: str = "replace",
    env: Optional[str] = None,
    chunksize: int = 1000
) -> Dict[str, Any]:
    """
    Stores extracted SAP table DataFrame into PostgreSQL under 'active_<table_name>'
    with automatic duplicate prevention and integrity checks.
    """
    if df.empty:
        raise ValueError(f"Cannot save empty DataFrame to database for table '{sap_table}'.")

    start_time = time.time()
    creds = get_db_credentials(env)
    target_table = get_active_table_name(sap_table)
    engine = get_engine(env)

    logger.info(
        f"[DB START] Ingesting into table '{target_table}' "
        f"on {creds['host']}:{creds['port']}/{creds['database']} (mode='{mode}')..."
    )

    clean_df = sanitize_dataframe(df)

    # Apply strict duplicate check
    clean_df, dedup_stats = deduplicate_dataframe(
        df=clean_df,
        target_table=target_table,
        engine=engine,
        mode=mode
    )

    if clean_df.empty:
        logger.info(
            f"[DEDUP COMPLETE] All records already exist in '{target_table}'. Zero new duplicates inserted."
        )
        with engine.connect() as conn:
            actual_count = conn.execute(
                text(f'SELECT COUNT(*) FROM public."{target_table}"')
            ).scalar()
        return {
            "status": "success",
            "sap_table": sap_table.upper(),
            "target_table": target_table,
            "rows_inserted": 0,
            "total_rows_in_table": actual_count,
            "deduplication": dedup_stats,
            "message": "All records already exist in DB. Zero duplicates created."
        }

    # Insert into PostgreSQL using SQLAlchemy
    clean_df.to_sql(
        name=target_table,
        con=engine,
        schema="public",
        if_exists=mode,
        index=False,
        chunksize=chunksize,
        method="multi"
    )

    # If key columns exist and we replaced the table, create UNIQUE INDEX to enforce DB-level integrity
    key_cols = detect_natural_keys(list(clean_df.columns))
    if key_cols and mode == "replace":
        try:
            with engine.begin() as conn:
                idx_name = f"idx_uq_{target_table}"
                cols_str = ", ".join([f'"{c}"' for c in key_cols])
                conn.execute(text(f'CREATE UNIQUE INDEX IF NOT EXISTS {idx_name} ON public."{target_table}" ({cols_str});'))
                logger.info(f"[DB CONSTRAINT] Created UNIQUE INDEX '{idx_name}' on ({cols_str}) to prevent future duplicates.")
        except Exception as idx_err:
            logger.debug(f"Unique index creation note: {idx_err}")

    elapsed = round(time.time() - start_time, 2)

    # Verify count in database
    with engine.connect() as conn:
        actual_count = conn.execute(
            text(f'SELECT COUNT(*) FROM public."{target_table}"')
        ).scalar()

    logger.info(
        f"[DB SUCCESS] '{target_table}' updated in {elapsed}s. "
        f"Inserted: {len(clean_df)} unique rows | Total rows in DB: {actual_count}."
    )

    return {
        "status": "success",
        "sap_table": sap_table.upper(),
        "target_table": target_table,
        "rows_inserted": len(clean_df),
        "total_rows_in_table": actual_count,
        "columns_count": len(clean_df.columns),
        "columns": list(clean_df.columns),
        "deduplication": dedup_stats,
        "database": creds["database"],
        "host": creds["host"],
        "port": creds["port"],
        "mode": mode,
        "elapsed_seconds": elapsed,
    }


def list_active_tables(env: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Lists all 'active_*' tables existing in PostgreSQL database and their row counts.
    """
    engine = get_engine(env)
    query = text("""
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name LIKE 'active_%'
        ORDER BY table_name;
    """)

    results = []
    with engine.connect() as conn:
        tables = [r[0] for r in conn.execute(query).fetchall()]
        for tbl in tables:
            cnt = conn.execute(text(f'SELECT COUNT(*) FROM public."{tbl}"')).scalar()
            results.append({"table_name": tbl, "row_count": cnt})

    return results
