"""
Delta sync: keeps active_<table> in step with SAP and queues new / changed rows.

SAP's OData service (ZGENERIC_DATA_SRV) can only return a whole table, so every
poll fetches the full table and compares it with the database:

  key  = DDIC key fields  -> sap_key_hash   (identifies the SAP record)
  data = all column values -> row_hash      (detects changes)

  new key            -> INSERT, new passportid, sap_status NEW,     ingestion_status PENDING
  row_hash changed   -> UPDATE,                 sap_status CHANGED, ingestion_status PENDING
  missing N polls    -> sap_status REMOVED (row kept, not sent to ingestion)
  back after REMOVED -> sap_status ACTIVE

PENDING rows are the ingestion queue (see ingestion.py).

passportid: <prefix><org last 2><FY><7-digit seq>, e.g. PPSAP0126270000001, reserved in
MASTEROBJECTSEQUENCINGV2. One SAP record keeps the same passportid for ever.

Column types come from the SAP DDIC: CURR/QUAN/FLTP/DEC(with decimals) -> double precision,
NUMC/INT/DEC(0 decimals) -> bigint, everything else -> text.
"""

import hashlib
import json
import re
import time
from datetime import datetime
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

import psycopg2
from psycopg2.extras import execute_values

import settings
from context import ReserveMasterObjectIDs
from db_manager import clean_column_name, get_active_table_name, get_db_credentials
from logger_setup import get_logger
from pipeline import pipeline_lock
from sap_odata_extractor import SAPODataExtractor, clean_table_name

logger = get_logger("SAP_Pipeline.Delta")

STATE_TABLE = "sap_extraction_state"
RUN_TABLE = "sap_ingestion_run"

CONTROL_COLUMNS: List[Tuple[str, str]] = [
    ("passportid", "text NOT NULL"),
    ("sap_key_hash", "text NOT NULL"),
    ("row_hash", "text NOT NULL"),
    ("sap_status", "text NOT NULL"),
    ("ingestion_status", "text NOT NULL"),
    ("ingestion_run_id", "text"),
    ("retry_count", "integer NOT NULL DEFAULT 0"),
    ("error_message", "text"),
    ("missing_count", "integer NOT NULL DEFAULT 0"),
    ("first_seen_at", "timestamptz NOT NULL DEFAULT now()"),
    ("last_seen_at", "timestamptz NOT NULL DEFAULT now()"),
    ("changed_at", "timestamptz"),
    ("removed_at", "timestamptz"),
    ("ingested_at", "timestamptz"),
]
CONTROL_NAMES = {name for name, _ in CONTROL_COLUMNS}

_INT_TYPES = {"NUMC", "INT1", "INT2", "INT4", "INT8"}
_FLOAT_TYPES = {"CURR", "QUAN", "FLTP"}
_INT_RE = re.compile(r"-?\d+")


class DeltaNotInitialized(RuntimeError):
    """The target table is missing or was not converted with `delta-init` yet."""


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def delta_settings() -> Dict[str, Any]:
    return {
        "tables": settings.delta_tables(),
        "poll_seconds": max(10, settings.get_int("delta", "poll_seconds", "DELTA_POLL_SECONDS", 60)),
        "missing_polls": max(1, settings.get_int("delta", "missing_polls_before_removed", "DELTA_MISSING_POLLS", 2)),
        "passport_prefix": settings.get("delta", "passport_prefix", "DELTA_PASSPORT_PREFIX", "PPSAP"),
        "tenant_id": settings.get("defaults", "tenant_id", "TENANT_ID", "DM00005"),
        "org_id": settings.get("defaults", "org_id", "ORG_ID", "OM0000000001"),
    }


def _connect(env: Optional[str] = None):
    c = get_db_credentials(env)
    return psycopg2.connect(host=c["host"], port=c["port"], user=c["user"],
                            password=c["password"], dbname=c["database"])


def q(name: str) -> str:
    """Quoted identifier."""
    return '"' + name.replace('"', '""') + '"'


# ---------------------------------------------------------------------------
# Column spec + value normalisation
# ---------------------------------------------------------------------------
class Column(NamedTuple):
    sap: str      # SAP name (upper case), key in the JSON row is sap.lower()
    db: str       # PostgreSQL column name
    pg: str       # PostgreSQL type
    is_key: bool


def build_spec(meta: Sequence[Dict[str, Any]]) -> List[Column]:
    cols: List[Column] = []
    used = set(CONTROL_NAMES)
    for m in sorted(meta, key=lambda c: int(c.get("position") or 0)):
        sap = str(m.get("column_name") or "").strip().upper()
        if not sap or sap.startswith(".INCLU"):
            continue
        ddic = str(m.get("data_type") or "").upper()
        length = int(m.get("length") or 0)
        decimals = int(m.get("decimals") or 0)
        if ddic in _INT_TYPES and length <= 18:
            pg = "bigint"
        elif ddic == "DEC":
            pg = "double precision" if decimals else "bigint"
        elif ddic in _FLOAT_TYPES:
            pg = "double precision"
        else:
            pg = "text"
        db = clean_column_name(sap)
        base, n = db, 1
        while db in used:
            n += 1
            db = f"{base}_{n}"
        used.add(db)
        cols.append(Column(sap, db, pg, bool(m.get("is_key"))))
    if not any(c.is_key for c in cols):
        raise ValueError("SAP metadata has no key fields; delta sync needs a DDIC primary key")
    return cols


def to_db(value: Any, pg: str) -> Any:
    """Converts a raw SAP JSON value (or a stored DB value) to the column type."""
    if value is None:
        return None
    if pg == "text":
        if isinstance(value, str):
            return value
        if isinstance(value, (dict, list)):
            return json.dumps(value, sort_keys=True)
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
    try:
        if pg == "bigint":
            if isinstance(value, int):
                return value
            if isinstance(value, str) and _INT_RE.fullmatch(value):
                return int(value)
            f = float(value)
            return int(f) if f.is_integer() else None
        return float(value)
    except (TypeError, ValueError):
        return None


def _canon(v: Any) -> str:
    return repr(v) if isinstance(v, float) else str(v)


def key_hash(sap_table: str, key_values: Iterable[Any]) -> str:
    raw = sap_table + "|" + "\x1f".join("\x00" if v is None else _canon(v) for v in key_values)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def row_hash(spec: Sequence[Column], values: Sequence[Any]) -> str:
    # None values are skipped so a new, empty SAP column does not mark every row as changed
    raw = "\x1f".join(f"{c.db}={_canon(v)}" for c, v in sorted(zip(spec, values), key=lambda x: x[0].db)
                      if v is not None)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def normalise_rows(sap_table: str, spec: Sequence[Column],
                   rows: Iterable[Dict[str, Any]]) -> Dict[str, Tuple[tuple, str]]:
    """{sap_key_hash: (values in spec order, row_hash)}"""
    out: Dict[str, Tuple[tuple, str]] = {}
    key_idx = [i for i, c in enumerate(spec) if c.is_key]
    dupes = 0
    for raw in rows:
        values = tuple(to_db(raw.get(c.sap.lower()), c.pg) for c in spec)
        k = key_hash(sap_table, (values[i] for i in key_idx))
        if k in out:
            dupes += 1
        out[k] = (values, row_hash(spec, values))
    if dupes:
        logger.warning(f"[DELTA] {sap_table}: {dupes} rows with a duplicate DDIC key in the SAP snapshot (last one kept)")
    return out


# ---------------------------------------------------------------------------
# DDL helpers
# ---------------------------------------------------------------------------
def ensure_control_tables(cur) -> None:
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS public.{STATE_TABLE} (
            target_table     text PRIMARY KEY,
            sap_table        text NOT NULL,
            last_poll_at     timestamptz,
            last_success_at  timestamptz,
            sap_count        integer,
            fetched_rows     integer,
            db_active_count  integer,
            last_new         integer,
            last_changed     integer,
            last_removed     integer,
            last_error       text,
            updated_at       timestamptz NOT NULL DEFAULT now()
        )""")
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS public.{RUN_TABLE} (
            run_id         text PRIMARY KEY,
            sap_table      text NOT NULL,
            target_table   text NOT NULL,
            staging_table  text NOT NULL,
            status         text NOT NULL,
            job_id         text,
            job_status     text,
            record_count   integer NOT NULL DEFAULT 0,
            started_at     timestamptz NOT NULL DEFAULT now(),
            finished_at    timestamptz,
            error          text,
            result         jsonb
        )""")
    # Only one ingestion run may be RUNNING at any time (across processes and servers)
    cur.execute(f"""CREATE UNIQUE INDEX IF NOT EXISTS {RUN_TABLE}_one_running
                    ON public.{RUN_TABLE} ((true)) WHERE status = 'RUNNING'""")


def table_columns(cur, table: str) -> Dict[str, str]:
    cur.execute("""SELECT column_name, data_type FROM information_schema.columns
                   WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position""", (table,))
    return {r[0]: r[1] for r in cur.fetchall()}


def data_columns(cur, table: str) -> List[str]:
    """SAP data columns of a delta table (everything except the control columns), in table order."""
    return [c for c in table_columns(cur, table) if c not in CONTROL_NAMES]


def _create_delta_table(cur, name: str, index_prefix: str, spec: Sequence[Column]) -> None:
    cols = [f"{q(c.db)} {c.pg}" for c in spec] + [f"{q(n)} {d}" for n, d in CONTROL_COLUMNS]
    cur.execute(f"CREATE TABLE public.{q(name)} (\n  " + ",\n  ".join(cols) + "\n)")
    cur.execute(f"CREATE UNIQUE INDEX {q(index_prefix + '_passportid_uq')} ON public.{q(name)} (passportid)")
    cur.execute(f"CREATE UNIQUE INDEX {q(index_prefix + '_keyhash_uq')} ON public.{q(name)} (sap_key_hash)")
    cur.execute(f"CREATE INDEX {q(index_prefix + '_ingestion_status_ix')} ON public.{q(name)} (ingestion_status, first_seen_at)")


def _add_missing_columns(cur, table: str, spec: Sequence[Column], existing: Dict[str, str]) -> List[str]:
    added = []
    for c in spec:
        if c.db not in existing:
            cur.execute(f"ALTER TABLE public.{q(table)} ADD COLUMN {q(c.db)} {c.pg}")
            added.append(c.db)
    if added:
        logger.info(f"[DELTA] {table}: added new SAP columns {added}")
    return added


def _insert_rows(cur, table: str, spec: Sequence[Column], items: List[Tuple[str, tuple, str]],
                 passports: List[str], sap_status: str, ingestion_status: str) -> None:
    if not items:
        return
    names = [c.db for c in spec] + ["passportid", "sap_key_hash", "row_hash", "sap_status", "ingestion_status"]
    template = "(" + ", ".join([f"%s::{c.pg}" for c in spec] + ["%s"] * 5) + ")"
    data = [values + (pid, k, rh, sap_status, ingestion_status)
            for (k, values, rh), pid in zip(items, passports)]
    execute_values(cur, f"INSERT INTO public.{q(table)} ({', '.join(q(n) for n in names)}) VALUES %s",
                   data, template=template, page_size=500)


def _update_changed(cur, table: str, spec: Sequence[Column], items: List[Tuple[str, tuple, str]]) -> None:
    if not items:
        return
    names = [c.db for c in spec] + ["sap_key_hash", "row_hash"]
    template = "(" + ", ".join([f"%s::{c.pg}" for c in spec] + ["%s", "%s"]) + ")"
    sets = ", ".join(f"{q(c.db)} = v.{q(c.db)}" for c in spec)
    execute_values(cur, f"""
        UPDATE public.{q(table)} AS t SET {sets},
               row_hash = v.row_hash, sap_status = 'CHANGED', ingestion_status = 'PENDING',
               ingestion_run_id = NULL, retry_count = 0, error_message = NULL, missing_count = 0,
               removed_at = NULL, changed_at = now(), last_seen_at = now()
        FROM (VALUES %s) AS v ({', '.join(q(n) for n in names)})
        WHERE t.sap_key_hash = v.sap_key_hash""",
                   [values + (k, rh) for k, values, rh in items], template=template, page_size=500)


def _reserve_passports(count: int, env: Optional[str]) -> List[str]:
    if count <= 0:
        return []
    s = delta_settings()
    return ReserveMasterObjectIDs(s["tenant_id"], s["passport_prefix"], "MASTEROBJECTSEQUENCINGV2",
                                  s["org_id"], count, "", env)


NOW = object()   # _save_state value meaning "database now()" (avoids local-time / UTC mix-ups)


def _save_state(cur, target: str, sap_table: str, **fields: Any) -> None:
    fields["updated_at"] = NOW
    cols = ["target_table", "sap_table"] + list(fields)
    vals = [target, sap_table] + [v for v in fields.values() if v is not NOW]
    holders = ["%s", "%s"] + ["now()" if v is NOW else "%s" for v in fields.values()]
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols[1:])
    cur.execute(f"""INSERT INTO public.{STATE_TABLE} ({', '.join(cols)})
                    VALUES ({', '.join(holders)})
                    ON CONFLICT (target_table) DO UPDATE SET {updates}""", vals)


# ---------------------------------------------------------------------------
# One-time conversion of an existing / new table
# ---------------------------------------------------------------------------
def init_table(sap_table: str, env: Optional[str] = None, target: Optional[str] = None,
               all_done: bool = False) -> Dict[str, Any]:
    """
    Creates the delta version of active_<table> from a fresh SAP snapshot.

    * Rows that already exist in the current active_<table> -> ACTIVE / DONE (not re-ingested)
    * Rows that are not there yet                            -> NEW / PENDING (all_done=True: DONE)
    * The old table is kept as active_<table>_bak_<timestamp>.
    """
    sap_table = clean_table_name(sap_table)
    target = target or get_active_table_name(sap_table)
    with pipeline_lock(f"delta_{target}"):
        conn = _connect(env)
        try:
            cur = conn.cursor()
            ensure_control_tables(cur)
            conn.commit()
            legacy_cols = table_columns(cur, target)
            if "passportid" in legacy_cols:
                raise ValueError(f"{target} is already a delta table (has passportid)")

            snap = SAPODataExtractor().fetch_records(sap_table)
            spec = build_spec(snap["meta"])
            fetched = normalise_rows(sap_table, spec, snap["rows"])

            legacy: Dict[str, Tuple[tuple, str]] = {}
            legacy_rows = 0
            if legacy_cols:
                key_cols = [c for c in spec if c.is_key]
                missing = [c.db for c in key_cols if c.db not in legacy_cols]
                if missing:
                    raise ValueError(f"Existing {target} lacks key columns {missing}; cannot match rows")
                select = ", ".join(q(c.db) if c.db in legacy_cols else "NULL" for c in spec)
                cur.execute(f"SELECT {select} FROM public.{q(target)}")
                key_idx = [i for i, c in enumerate(spec) if c.is_key]
                for r in cur.fetchall():
                    legacy_rows += 1
                    values = tuple(to_db(v, c.pg) for v, c in zip(r, spec))
                    legacy[key_hash(sap_table, (values[i] for i in key_idx))] = (values, row_hash(spec, values))

            done_keys = set(fetched) if all_done else set(fetched) & set(legacy)
            done = [(k, v, rh) for k, (v, rh) in fetched.items() if k in done_keys]
            pending = [(k, v, rh) for k, (v, rh) in fetched.items() if k not in done_keys]
            # rows of the old table that SAP no longer returns (e.g. cleared) are kept as REMOVED
            gone = [(k, v, rh) for k, (v, rh) in legacy.items() if k not in fetched]
            passports = _reserve_passports(len(done) + len(pending) + len(gone), env)

            tmp = f"{target}__delta_new"
            backup = None
            cur.execute(f"DROP TABLE IF EXISTS public.{q(tmp)}")
            _create_delta_table(cur, tmp, target, spec)
            p1, p2 = len(done), len(done) + len(pending)
            _insert_rows(cur, tmp, spec, done, passports[:p1], "ACTIVE", "DONE")
            _insert_rows(cur, tmp, spec, pending, passports[p1:p2], "NEW", "PENDING")
            _insert_rows(cur, tmp, spec, gone, passports[p2:], "REMOVED", "DONE")
            if gone:
                cur.execute(f"UPDATE public.{q(tmp)} SET removed_at = now(), missing_count = 1 "
                            "WHERE sap_status = 'REMOVED'")
            if legacy_cols:
                backup = f"{target}_bak_{datetime.now():%Y%m%d_%H%M%S}"
                cur.execute(f"ALTER TABLE public.{q(target)} RENAME TO {q(backup)}")
            cur.execute(f"ALTER TABLE public.{q(tmp)} RENAME TO {q(target)}")
            _save_state(cur, target, sap_table, last_poll_at=NOW, last_success_at=NOW,
                        sap_count=snap["sap_count"], fetched_rows=len(fetched), db_active_count=len(fetched),
                        last_new=len(pending), last_changed=0, last_removed=0, last_error=None)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    res = {
        "status": "success",
        "sap_table": sap_table,
        "target_table": target,
        "backup_table": backup,
        "sap_rows": len(fetched),
        "existing_rows_matched_done": len(done),
        "new_rows_pending": len(pending),
        "legacy_rows": legacy_rows,
        "legacy_rows_not_in_sap_kept_as_removed": len(gone),
        "first_passportid": passports[0] if passports else None,
        "last_passportid": passports[-1] if passports else None,
    }
    logger.info(f"[DELTA INIT] {res}")
    return res


# ---------------------------------------------------------------------------
# Poll
# ---------------------------------------------------------------------------
def poll_table(sap_table: str, env: Optional[str] = None, target: Optional[str] = None,
               odata: Optional[SAPODataExtractor] = None) -> Dict[str, Any]:
    """Fetches the SAP table once and applies new / changed / removed rows. Raises PipelineBusy when a poll is running."""
    sap_table = clean_table_name(sap_table)
    target = target or get_active_table_name(sap_table)
    cfg = delta_settings()
    start = time.time()
    with pipeline_lock(f"delta_{target}"):
        conn = _connect(env)
        try:
            cur = conn.cursor()
            ensure_control_tables(cur)
            cols = table_columns(cur, target)
            if "passportid" not in cols:
                raise DeltaNotInitialized(
                    f"{target} is not a delta table yet - run: python cli.py delta-init {sap_table}")

            snap = (odata or SAPODataExtractor()).fetch_records(sap_table, quiet=True)
            spec = build_spec(snap["meta"])
            _add_missing_columns(cur, target, spec, cols)
            fetched = normalise_rows(sap_table, spec, snap["rows"])

            cur.execute(f"SELECT sap_key_hash, row_hash, sap_status FROM public.{q(target)}")
            existing = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

            new_items, changed_items, back_keys = [], [], []
            for k, (values, rh) in fetched.items():
                old = existing.get(k)
                if old is None:
                    new_items.append((k, values, rh))
                elif old[0] != rh:
                    changed_items.append((k, values, rh))
                elif old[1] == "REMOVED":
                    back_keys.append(k)

            check_removed = snap["complete"] and (fetched or not existing)
            if not check_removed and existing:
                logger.warning(f"[DELTA] {sap_table}: incomplete snapshot ({len(fetched)} of {snap['sap_count']} rows) "
                               "- removal check skipped this poll")
            missing_keys = [k for k, (_, st) in existing.items() if k not in fetched and st != "REMOVED"] \
                if check_removed else []

            passports = _reserve_passports(len(new_items), env)
            _insert_rows(cur, target, spec, new_items, passports, "NEW", "PENDING")
            _update_changed(cur, target, spec, changed_items)
            if back_keys:
                cur.execute(f"""UPDATE public.{q(target)} SET sap_status = 'ACTIVE', removed_at = NULL,
                                missing_count = 0, last_seen_at = now() WHERE sap_key_hash = ANY(%s)""", (back_keys,))
            seen = [k for k in fetched if k in existing]
            if seen:
                cur.execute(f"""UPDATE public.{q(target)} SET last_seen_at = now(), missing_count = 0
                                WHERE sap_key_hash = ANY(%s)
                                  AND (missing_count <> 0 OR last_seen_at < now() - interval '10 minutes')""", (seen,))
            removed: List[str] = []
            if missing_keys:
                cur.execute(f"""UPDATE public.{q(target)} SET missing_count = missing_count + 1
                                WHERE sap_key_hash = ANY(%s) AND sap_status <> 'REMOVED'""", (missing_keys,))
                cur.execute(f"""UPDATE public.{q(target)} SET sap_status = 'REMOVED', removed_at = now()
                                WHERE sap_key_hash = ANY(%s) AND sap_status <> 'REMOVED' AND missing_count >= %s
                                RETURNING passportid""", (missing_keys, cfg["missing_polls"]))
                removed = [r[0] for r in cur.fetchall()]

            cur.execute(f"SELECT count(*) FROM public.{q(target)} WHERE sap_status <> 'REMOVED'")
            active = cur.fetchone()[0]
            cur.execute(f"SELECT count(*) FROM public.{q(target)} WHERE ingestion_status = 'PENDING'")
            pending = cur.fetchone()[0]
            _save_state(cur, target, sap_table, last_poll_at=NOW, last_success_at=NOW,
                        sap_count=snap["sap_count"], fetched_rows=len(fetched), db_active_count=active,
                        last_new=len(new_items), last_changed=len(changed_items), last_removed=len(removed),
                        last_error=None)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            try:
                ensure_control_tables(cur)
                _save_state(cur, target, sap_table, last_poll_at=NOW, last_error=str(exc)[:1000])
                conn.commit()
            except Exception:
                conn.rollback()
            raise
        finally:
            conn.close()

    res = {
        "status": "success",
        "sap_table": sap_table,
        "target_table": target,
        "sap_count": snap["sap_count"],
        "fetched": len(fetched),
        "new": len(new_items),
        "changed": len(changed_items),
        "back_from_removed": len(back_keys),
        "missing_this_poll": len(missing_keys),
        "removed": len(removed),
        "pending_queue": pending,
        "new_passportids": passports[:20],
        "elapsed_seconds": round(time.time() - start, 2),
    }
    if snap["sap_count"] >= 0 and active != snap["sap_count"] and snap["complete"]:
        res["reconciliation_warning"] = f"SAP has {snap['sap_count']} rows, DB has {active} active rows"
    if new_items or changed_items or removed:
        logger.info(f"[DELTA] {sap_table}: new={len(new_items)} changed={len(changed_items)} "
                    f"removed={len(removed)} pending={pending} passportids={passports[:5]}")
    else:
        logger.debug(f"[DELTA] {sap_table}: no changes ({len(fetched)} rows)")
    return res


def poll_all(env: Optional[str] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"status": "success", "tables": {}}
    for t in settings.delta_tables():
        try:
            out["tables"][t] = poll_table(t, env=env)
        except Exception as exc:
            logger.error(f"[DELTA] poll {t} failed: {exc}")
            out["tables"][t] = {"status": "failed", "error": str(exc)}
            out["status"] = "partial_success"
    if out["tables"] and all(v.get("status") == "failed" for v in out["tables"].values()):
        out["status"] = "failed"
    return out


# ---------------------------------------------------------------------------
# Status / maintenance
# ---------------------------------------------------------------------------
def status(env: Optional[str] = None, runs: int = 10) -> Dict[str, Any]:
    conn = _connect(env)
    try:
        cur = conn.cursor()
        ensure_control_tables(cur)
        conn.commit()
        tables: Dict[str, Any] = {}
        for t in settings.delta_tables():
            target = get_active_table_name(t)
            info: Dict[str, Any] = {"target_table": target}
            if "passportid" not in table_columns(cur, target):
                info["initialized"] = False
            else:
                info["initialized"] = True
                cur.execute(f"SELECT sap_status, count(*) FROM public.{q(target)} GROUP BY 1")
                info["sap_status"] = dict(cur.fetchall())
                cur.execute(f"SELECT ingestion_status, count(*) FROM public.{q(target)} GROUP BY 1")
                info["ingestion_status"] = dict(cur.fetchall())
            cur.execute(f"""SELECT last_poll_at, last_success_at, sap_count, fetched_rows, db_active_count,
                                   last_new, last_changed, last_removed, last_error
                            FROM public.{STATE_TABLE} WHERE target_table = %s""", (target,))
            row = cur.fetchone()
            if row:
                info["state"] = dict(zip(["last_poll_at", "last_success_at", "sap_count", "fetched_rows",
                                          "db_active_count", "last_new", "last_changed", "last_removed",
                                          "last_error"], row))
            tables[t] = info
        return {"status": "success", "tables": tables, "runs": recent_runs(cur, runs)}
    finally:
        conn.close()


def recent_runs(cur, limit: int = 10) -> List[Dict[str, Any]]:
    cur.execute(f"""SELECT run_id, sap_table, status, job_id, job_status, record_count,
                           started_at, finished_at, error
                    FROM public.{RUN_TABLE} ORDER BY started_at DESC LIMIT %s""", (limit,))
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def retry_failed(sap_table: str, env: Optional[str] = None) -> Dict[str, Any]:
    """Moves FAILED rows back to PENDING with retry_count reset."""
    target = get_active_table_name(clean_table_name(sap_table))
    conn = _connect(env)
    try:
        cur = conn.cursor()
        cur.execute(f"""UPDATE public.{q(target)} SET ingestion_status = 'PENDING', retry_count = 0,
                        ingestion_run_id = NULL WHERE ingestion_status = 'FAILED'""")
        n = cur.rowcount
        conn.commit()
        return {"status": "success", "target_table": target, "requeued": n}
    finally:
        conn.close()
