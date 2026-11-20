"""
=============================================================================
Master Object ID Sequencing (Context IDs etc.)
=============================================================================
Format:  <OBJNAME><ORGID last 2><FY YYyy><7-digit sequence>
Example: CTXSAP + 01 + 2627 + 0000001  ->  CTXSAP0126270000001

Sequence counters live in table MASTEROBJECTSEQUENCINGV2 (pk, sk) and are
incremented atomically via INSERT ... ON CONFLICT DO UPDATE ... RETURNING.
=============================================================================
"""

import time
from datetime import datetime
from typing import List, Optional, Tuple

import psycopg2

from db_manager import get_db_credentials
from logger_setup import get_logger

logger = get_logger("SAP_Pipeline.Context")

LEGACY_OBJECTS = ("WF", "VM", "IM", "BE", "OM")


def _financial_year(now: Optional[datetime] = None) -> str:
    """Indian FY (Apr-Mar) as 'YYyy', e.g. Nov 2026 -> '2627'."""
    now = now or datetime.now()
    fy_start = now.year if now.month >= 4 else now.year - 1
    return f"{str(fy_start)[-2:]}{str(fy_start + 1)[-2:]}"


def _org_suffix(orgid: Optional[str]) -> str:
    return orgid[-2:] if orgid and orgid != "NoLDE" else "01"


def _connect(env: Optional[str] = None):
    creds = get_db_credentials(env)
    return psycopg2.connect(
        host=creds["host"],
        port=creds["port"],
        user=creds["user"],
        password=creds["password"],
        dbname=creds["database"],
    )


def _resolve_key(masterobjName: str, masterobjNamekey: str, orgid: str, context: str) -> Tuple[str, str, str, str]:
    """Returns (table, pk, sk, financialyear) exactly as per the original sequencing rules."""
    pk = masterobjNamekey
    financialyear = ""

    if masterobjName in LEGACY_OBJECTS:
        table = "MASTEROBJECTSEQUENCING"
        sk = f"#OBJNAME#{masterobjName}"
    elif context == "temp":
        table = "MASTEROBJECTSEQUENCINGTEMP"
        financialyear = _financial_year()
        sk = f"#OBJNAME#{masterobjName}{orgid}{financialyear}$"
    else:
        table = "MASTEROBJECTSEQUENCINGV2"
        financialyear = _financial_year()
        sk = f"#OBJNAME#{masterobjName}{orgid}{financialyear}"

    return table, pk, sk, financialyear


def _increment(conn, table: str, pk: str, sk: str, step: int) -> int:
    """Atomically adds `step` to the counter and returns the new last_updated_id."""
    query = f"""
        INSERT INTO {table} (pk, sk, last_updated_id, last_updated_on)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (pk, sk)
        DO UPDATE SET
            last_updated_id = CAST({table}.last_updated_id AS INTEGER) + %s,
            last_updated_on = EXCLUDED.last_updated_on
        RETURNING last_updated_id;
    """
    params = (pk, sk, step, int(time.time_ns()), step)
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            last_id = cur.fetchone()[0]
        conn.commit()
        return int(last_id)
    except psycopg2.errors.UndefinedTable:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {table} (
                    pk VARCHAR(255),
                    sk VARCHAR(255),
                    last_updated_id INT,
                    last_updated_on BIGINT,
                    PRIMARY KEY (pk, sk)
                );
            """)
            cur.execute(query, params)
            last_id = cur.fetchone()[0]
        conn.commit()
        return int(last_id)


def ReserveMasterObjectIDs(
    tenantID: str,
    masterobjName: str,
    masterobjNamekey: str,
    orgID: str,
    count: int,
    context: str = "",
    env: Optional[str] = None,
) -> List[str]:
    """
    Reserves `count` consecutive IDs with ONE database round-trip.
    Raises on DB failure (never returns fallback/duplicate IDs).
    """
    if count <= 0:
        return []

    orgid = _org_suffix(orgID)
    table, pk, sk, financialyear = _resolve_key(masterobjName, masterobjNamekey, orgid, context)

    conn = _connect(env)
    try:
        last_id = _increment(conn, table, pk, sk, count)
    except Exception:
        conn.rollback()
        logger.error(f"[SEQ] Failed to reserve {count} ids for {sk} (tenant {tenantID})", exc_info=True)
        raise
    finally:
        conn.close()

    first_id = last_id - count + 1
    prefix = f"{masterobjName}{orgid}{financialyear}"
    ids = [f"{prefix}{str(n).rjust(7, '0')}" for n in range(first_id, last_id + 1)]
    logger.info(f"[SEQ] Reserved {count} ids for {sk}: {ids[0]} .. {ids[-1]}")
    return ids


def GetMasterObjectID(tenantID, masterobjName, masterobjNamekey, orgid="NoLDE", context="", env=None):
    """Increments the counter by 1. Returns (objName, orgid, financialyear, lastUpdatedID)."""
    org = _org_suffix(orgid)
    table, pk, sk, financialyear = _resolve_key(masterobjName, masterobjNamekey, org, context)
    conn = _connect(env)
    try:
        last_id = _increment(conn, table, pk, sk, 1)
    finally:
        conn.close()
    return masterobjName, org, financialyear, last_id


def CreateMasterObject(tenantID, masterobjName, masterobjNamekey, orgID, context="", env=None):
    """Single ID, e.g. CreateMasterObject(tenant, "CTXSAP", "MASTEROBJECTSEQUENCINGV2", org_id)."""
    return ReserveMasterObjectIDs(tenantID, masterobjName, masterobjNamekey, orgID, 1, context, env)[0]
