"""
Long-running delta worker (run by systemd: `python cli.py worker`).

Every [delta] poll_seconds (default 60):
  * poll each [delta] table (new / changed / removed rows -> PENDING queue)
  * if no ingestion cycle is running, start one in a background thread

Ingestion therefore never blocks polling, and a new ingestion run starts only
after the previous one finished. SIGTERM / Ctrl+C stop the loop after the current
step; a job still running at the ingestion service is picked up again on restart.
"""

import signal
import threading
import time
from typing import Optional

import delta
import ingestion
from logger_setup import get_logger
from pipeline import PipelineBusy

logger = get_logger("SAP_Pipeline.Worker")


def _ingest_safely(env: Optional[str]) -> None:
    try:
        res = ingestion.run_ingestion(env=env)
        if res.get("runs"):
            logger.info(f"[WORKER] ingestion cycle: {res['status']} - "
                        + ", ".join(f"{r.get('run_id')}={r.get('status')}" for r in res["runs"]))
    except PipelineBusy:
        logger.debug("[WORKER] ingestion still running elsewhere")
    except Exception:
        logger.exception("[WORKER] ingestion cycle failed")


def run(env: Optional[str] = None) -> int:
    cfg = delta.delta_settings()
    ing_cfg = ingestion.ingestion_settings()
    if not cfg["tables"]:
        logger.error("[WORKER] No [delta] tables configured (DELTA_TABLES); nothing to do")
        return 1

    stop = threading.Event()

    def _stop(signum, _frame):
        logger.info(f"[WORKER] signal {signum} received, stopping after the current step")
        stop.set()

    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)

    logger.info(f"[WORKER] started: tables={cfg['tables']} every {cfg['poll_seconds']}s, "
                f"ingestion={'on -> ' + ing_cfg['base_url'] if ing_cfg['enabled'] else 'off'}")
    ing_thread: Optional[threading.Thread] = None

    while not stop.is_set():
        t0 = time.time()
        for table in cfg["tables"]:
            if stop.is_set():
                break
            try:
                delta.poll_table(table, env=env)
            except PipelineBusy:
                logger.warning(f"[WORKER] previous poll of {table} still running; skipped")
            except Exception as exc:
                logger.error(f"[WORKER] poll {table} failed: {exc}")

        if ing_cfg["enabled"] and not stop.is_set() and (ing_thread is None or not ing_thread.is_alive()):
            ing_thread = threading.Thread(target=_ingest_safely, args=(env,), name="ingestion", daemon=True)
            ing_thread.start()

        stop.wait(max(1.0, cfg["poll_seconds"] - (time.time() - t0)))

    logger.info("[WORKER] stopped")
    return 0
