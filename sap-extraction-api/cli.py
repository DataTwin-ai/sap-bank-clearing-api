"""
Command line / cron entry point for the SAP extraction service.

  Delta (every minute, only new / changed rows, ingestion queue):
    python cli.py delta-init BSID            # one time: convert active_bsid, existing rows = DONE
    python cli.py worker                     # long-running loop: poll every 60s + ingestion (systemd)
    python cli.py delta --cron               # one poll of all [delta] tables + one ingestion cycle (1-minute cron)
    python cli.py delta-poll [BSID]          # poll only
    python cli.py ingest [--no-wait]         # ingestion cycle only
    python cli.py delta-status               # queue counts, last poll, recent runs
    python cli.py delta-retry BSID           # FAILED rows -> PENDING

  Full loads (tables NOT in [delta] tables):
    python cli.py sync                       # [sap] tables -> DB -> bt3i -> ingestion command
    python cli.py extract KNA1 --rows 100    # one table
    python cli.py sync-bt3i                  # active_bsid -> active_bt3i only
    python cli.py check                      # SAP + DB connectivity
    python cli.py list-tables | sap-tables --filter BS | sap-metadata BSID

Prints one JSON result on stdout; details go to log/cron.log (worker: log/worker.log).

Exit codes (for cron / monitoring):
    0  success
    1  failed
    2  partial success (some tables or a downstream step failed)
   75  skipped - another run still holds the lock
"""

import argparse
import json
import sys
from datetime import datetime

from logger_setup import get_logger, setup_logging

EXIT_OK, EXIT_FAILED, EXIT_PARTIAL, EXIT_BUSY = 0, 1, 2, 75


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _cron_activity(res: dict) -> bool:
    """`delta --cron` result worth a line in cron.out: rows found / sent, a job running, or an error."""
    if res.get("status") != "success":
        return True
    for t in (res.get("poll") or {}).get("tables", {}).values():
        if t.get("status") == "failed":
            return True
        if any(t.get(k) for k in ("new", "changed", "removed", "back_from_removed")):
            return True
    ing = res.get("ingestion") or {}
    return bool(ing.get("runs")) or ing.get("status") not in ("success", "disabled", "busy")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="SAP extraction (cron / CLI)")
    parser.add_argument("--env", help="config.ini section: development | production")
    parser.add_argument("--verbose", "-v", action="store_true", help="Also log to the console")
    sub = parser.add_subparsers(dest="action", required=True)

    p = sub.add_parser("sync", help="Extract all configured tables, then bt3i sync + ingestion")
    p.add_argument("--tables", help="Override [sap] tables, e.g. BSID,KNA1")
    p.add_argument("--rows", type=int, help="Row limit per table (default: all)")
    p.add_argument("--mode", choices=["replace", "append"])
    p.add_argument("--no-bt3i", action="store_true", help="Skip active_bsid -> active_bt3i")

    p = sub.add_parser("extract", help="Extract one SAP table")
    p.add_argument("table")
    p.add_argument("--rows", type=int)
    p.add_argument("--mode", choices=["replace", "append"])
    p.add_argument("--csv", action="store_true", help="Also write data/exports/<table>_latest.csv")
    p.add_argument("--sync-bt3i", action="store_true")

    p = sub.add_parser("sync-bt3i", help="active_bsid -> active_bt3i")
    p.add_argument("--company-code")
    p.add_argument("--org-id")
    p.add_argument("--tenant-id")

    sub.add_parser("check", help="Test SAP OData + database connectivity")
    sub.add_parser("list-tables", help="active_* tables in PostgreSQL")
    p = sub.add_parser("sap-tables", help="SAP tables exposed by the OData service")
    p.add_argument("--filter")
    p.add_argument("--top", type=int, default=100)
    p = sub.add_parser("sap-metadata", help="SAP DDIC columns of a table")
    p.add_argument("table")

    p = sub.add_parser("delta-init", help="One time: turn active_<table> into a delta table")
    p.add_argument("table")
    p.add_argument("--all-done", action="store_true", help="Mark every current SAP row DONE (ingest nothing)")
    p = sub.add_parser("delta-poll", help="Poll SAP once for new / changed / removed rows")
    p.add_argument("tables", nargs="*", help="Default: [delta] tables")
    p = sub.add_parser("ingest", help="Send PENDING rows to the ingestion API (one run per table)")
    p.add_argument("--no-wait", action="store_true", help="Submit and return; the next cycle finishes the run")
    p = sub.add_parser("delta", help="delta-poll + ingest (for a 1-minute cron)")
    p.add_argument("--cron", action="store_true",
                   help="Don't wait for ingestion jobs (the next run checks them); one-line JSON output")
    sub.add_parser("worker", help="Loop: poll every [delta] poll_seconds, ingestion in the background")
    sub.add_parser("delta-status", help="Queue counts, last poll, recent ingestion runs")
    p = sub.add_parser("delta-retry", help="Move FAILED rows back to PENDING")
    p.add_argument("table")

    args = parser.parse_args(argv)

    if args.action == "worker":
        setup_logging("worker", console=args.verbose or sys.stdout.isatty())
        import worker
        return worker.run(env=args.env)

    # Under cron stdout is not a terminal: keep stdout for the JSON result only.
    setup_logging("cron", console=args.verbose or sys.stdout.isatty())
    logger = get_logger("SAP_Pipeline.CLI")

    import pipeline
    from db_manager import get_engine, list_active_tables
    from sap_odata_extractor import SAPODataExtractor

    try:
        if args.action == "delta-init":
            import delta
            res = delta.init_table(args.table, env=args.env, all_done=args.all_done)
        elif args.action == "delta-poll":
            import delta
            if args.tables:
                res = {"status": "success", "tables": {}}
                for t in args.tables:
                    res["tables"][t.upper()] = delta.poll_table(t, env=args.env)
            else:
                res = delta.poll_all(env=args.env)
        elif args.action == "ingest":
            import ingestion
            res = ingestion.run_ingestion(env=args.env, wait=not args.no_wait)
        elif args.action == "delta":
            import delta
            import ingestion
            res = {"poll": delta.poll_all(env=args.env)}
            try:
                # --cron: submit / check jobs without waiting, so each run ends well within the minute
                res["ingestion"] = ingestion.run_ingestion(env=args.env, wait=not args.cron)
            except pipeline.PipelineBusy as exc:
                res["ingestion"] = {"status": "busy", "message": str(exc)}
            res["status"] = res["poll"]["status"]
            if res["status"] == "success" and res["ingestion"].get("status") in ("failed", "partial_success"):
                res["status"] = "partial_success"
        elif args.action == "delta-status":
            import delta
            res = delta.status(env=args.env)
        elif args.action == "delta-retry":
            import delta
            res = delta.retry_failed(args.table, env=args.env)
        elif args.action == "sync":
            res = pipeline.run_configured_tables(
                tables=args.tables.split(",") if args.tables else None,
                max_rows=args.rows,
                db_mode=args.mode,
                run_bt3i=False if args.no_bt3i else None,
                env=args.env,
            )
        elif args.action == "extract":
            res = pipeline.extract(
                table=args.table, max_rows=args.rows, db_mode=args.mode,
                save_csv=True if args.csv else None, sync_bt3i=args.sync_bt3i, env=args.env,
            )
        elif args.action == "sync-bt3i":
            res = pipeline.transform_bt3i(
                env=args.env, company_code=args.company_code,
                org_id=args.org_id, tenant_id=args.tenant_id,
            )
        elif args.action == "check":
            from sqlalchemy import text
            res = {"sap": "ok" if SAPODataExtractor().test_connection() else "failed"}
            try:
                with get_engine(args.env).connect() as conn:
                    conn.execute(text("SELECT 1"))
                res["database"] = "ok"
            except Exception as exc:
                res["database"] = f"failed: {exc}"
            res["status"] = "success" if res["sap"] == "ok" and res["database"] == "ok" else "failed"
        elif args.action == "list-tables":
            res = {"status": "success", "tables": list_active_tables(env=args.env)}
        elif args.action == "sap-tables":
            res = {"status": "success",
                   "tables": SAPODataExtractor().list_available_tables(name_filter=args.filter, top=args.top)}
        else:  # sap-metadata
            res = {"status": "success", "columns": SAPODataExtractor().get_table_metadata(args.table)}
    except pipeline.PipelineBusy as exc:
        logger.warning(str(exc))
        _print({"status": "busy", "message": str(exc)})
        return EXIT_BUSY
    except Exception as exc:
        logger.exception(f"'{args.action}' failed")
        _print({"status": "failed", "error": str(exc)})
        return EXIT_FAILED

    if getattr(args, "cron", False):
        # cron.out gets a line only when data moved or something went wrong, not every minute
        if _cron_activity(res):
            print(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), **res},
                             default=str, separators=(",", ":")))
    else:
        _print(res)
    status = res.get("status") if isinstance(res, dict) else "success"
    return {"success": EXIT_OK, "partial_success": EXIT_PARTIAL}.get(status, EXIT_FAILED)


if __name__ == "__main__":
    sys.exit(main())
