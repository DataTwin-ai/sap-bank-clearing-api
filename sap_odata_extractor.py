"""
=============================================================================
SAP OData Generic Table Extractor (ZGENERIC_DATA_SRV)
=============================================================================
Connects to SAP S/4HANA / Gateway via OData V2 (ZGENERIC_DATA_SRV) to extract
data and DDIC metadata from ANY transparent table or compatibility view
(e.g., BSID, BKPF, BSEG, KNA1, T001, ACDOCA, MARA, VBAK, etc.).

Features:
  - No hardcoded row limits: full pagination using $top & $skip until complete
  - Supports arbitrary row limits when max_rows > 0 is explicitly requested
  - Reads SAP connection config from config.ini ([sap]), sap_extractor/.env,
    or environment variables (SAP_HOST, SAP_PORT, SAP_CLIENT, SAP_USER, ...)
  - Automatic retries with exponential backoff for transient network/5xx errors
  - Lowercases column names to match PostgreSQL active_<table_name> schema
=============================================================================
"""

import os
import json
import time
import configparser
from pathlib import Path
from typing import Optional, Dict, Any, List

import pandas as pd
import requests
import urllib3
from requests.auth import HTTPBasicAuth

from logger_setup import get_logger

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = get_logger("SAP_Pipeline.ODataExtractor")

PROJECT_ROOT = Path(__file__).resolve().parent


def _parse_dotenv(env_path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not env_path.exists():
        return values
    for raw_line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k:
            values[k] = v
    return values


def load_sap_odata_config() -> Dict[str, Any]:
    """Load SAP OData connection settings from env, sap_extractor/.env, and config.ini."""
    dotenv_vals = _parse_dotenv(PROJECT_ROOT / "sap_extractor" / ".env")

    cfg = configparser.ConfigParser()
    cfg_path = PROJECT_ROOT / "config.ini"
    if cfg_path.exists():
        cfg.read(cfg_path, encoding="utf-8")

    def get_val(env_key: str, ini_key: str, default: str = "") -> str:
        if os.getenv(env_key):
            return os.getenv(env_key, "").strip()
        if cfg.has_option("sap", ini_key):
            return cfg.get("sap", ini_key).strip()
        if dotenv_vals.get(env_key):
            return dotenv_vals[env_key].strip()
        return default

    verify_ssl_raw = get_val("SAP_VERIFY_SSL", "verify_ssl", "false").lower()
    use_https_raw = get_val("SAP_USE_HTTPS", "use_https", "true").lower()

    return {
        "host": get_val("SAP_HOST", "host", ""),
        "port": int(get_val("SAP_PORT", "port", "44343") or 44343),
        "client": get_val("SAP_CLIENT", "client", "200"),
        "user": get_val("SAP_USER", "user", ""),
        "password": get_val("SAP_PASSWORD", "password", ""),
        "service_name": get_val("SAP_SERVICE_NAME", "service_name", "ZGENERIC_DATA_SRV"),
        "use_https": use_https_raw in ("true", "1", "yes"),
        "verify_ssl": verify_ssl_raw in ("true", "1", "yes"),
        "timeout": int(get_val("SAP_TIMEOUT", "timeout", "90") or 90),
        "max_retries": int(get_val("SAP_MAX_RETRIES", "max_retries", "3") or 3),
        "batch_size": int(get_val("SAP_BATCH_SIZE", "batch_size", "1000") or 1000),
    }


class SAPODataError(Exception):
    """Raised when SAP OData extraction encounters an unrecoverable error."""


class SAPODataExtractor:
    """
    Live SAP OData V2 extractor communicating with ZGENERIC_DATA_SRV.
    Supports dynamic extraction of ANY SAP table or view.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or load_sap_odata_config()
        scheme = "https" if self.config.get("use_https", True) else "http"
        self.base_url = (
            f"{scheme}://{self.config['host']}:{self.config['port']}"
            f"/sap/opu/odata/sap/{self.config['service_name']}"
        )
        self.client = str(self.config.get("client", "200"))
        self.timeout = int(self.config.get("timeout", 90))
        self.max_retries = int(self.config.get("max_retries", 3))
        self.default_batch_size = int(self.config.get("batch_size", 1000))

        self.session = requests.Session()
        if self.config.get("user"):
            self.session.auth = HTTPBasicAuth(self.config["user"], self.config["password"])
        self.session.verify = bool(self.config.get("verify_ssl", False))
        self.session.headers.update({"Accept": "application/json"})

    def _request(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        expect_json: bool = True,
    ) -> Any:
        url = f"{self.base_url}/{endpoint.lstrip('/')}" if endpoint else f"{self.base_url}/"
        query_params = {"sap-client": self.client}
        if expect_json:
            query_params["$format"] = "json"
        if params:
            query_params.update(params)

        last_err: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.get(url, params=query_params, timeout=self.timeout)
                if resp.status_code in (200, 201):
                    return resp.json() if expect_json else resp.text

                if resp.status_code in (401, 403):
                    raise SAPODataError(
                        f"SAP authentication/authorization failed ({resp.status_code}): {resp.text[:300]}"
                    )
                if resp.status_code == 404:
                    raise SAPODataError(
                        f"SAP OData endpoint not found (404): {url} -> {resp.text[:300]}"
                    )
                if resp.status_code >= 500 and attempt < self.max_retries:
                    wait_s = 2 ** attempt
                    logger.warning(
                        f"SAP HTTP {resp.status_code} on {endpoint}; retrying in {wait_s}s "
                        f"({attempt}/{self.max_retries})..."
                    )
                    time.sleep(wait_s)
                    continue

                raise SAPODataError(f"SAP HTTP {resp.status_code} on {endpoint}: {resp.text[:400]}")

            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                last_err = exc
                if attempt < self.max_retries:
                    wait_s = 2 ** attempt
                    logger.warning(
                        f"SAP connection/timeout error on {endpoint} ({exc}); "
                        f"retrying in {wait_s}s ({attempt}/{self.max_retries})..."
                    )
                    time.sleep(wait_s)
                    continue
                raise SAPODataError(
                    f"SAP OData connection failed after {self.max_retries} attempts: {exc}"
                ) from exc

        raise SAPODataError(f"SAP OData request failed: {last_err}")

    def test_connection(self) -> bool:
        """Verify live SAP OData service connectivity."""
        try:
            data = self._request("", expect_json=True)
            entity_sets = data.get("d", {}).get("EntitySets", [])
            logger.info(
                f"SAP OData connected ({self.base_url}, client={self.client}). "
                f"EntitySets: {entity_sets}"
            )
            return "TableDataSet" in entity_sets or bool(entity_sets)
        except Exception as exc:
            logger.error(f"SAP OData connectivity check failed: {exc}")
            return False

    def get_table_metadata(self, table_name: str) -> List[Dict[str, Any]]:
        """Fetch DDIC column definitions for any SAP table via TableMetadataSet."""
        table_upper = table_name.strip().upper()
        data = self._request(
            "TableMetadataSet",
            params={"$filter": f"TableName eq '{table_upper}'", "$top": 5000},
        )
        results = data.get("d", {}).get("results", [])
        cols: List[Dict[str, Any]] = []
        for r in results:
            cols.append({
                "column_name": str(r.get("ColumnName", "")).strip(),
                "position": int(r.get("ColPosition", 0) or 0),
                "data_type": str(r.get("DataType", "")).strip(),
                "length": int(r.get("Length", 0) or 0),
                "decimals": int(r.get("Decimals", 0) or 0),
                "is_key": bool(r.get("IsKey", False)),
                "description": str(r.get("Description", "")).strip(),
            })
        return cols

    def list_available_tables(
        self, name_filter: Optional[str] = None, top: int = 100
    ) -> List[Dict[str, Any]]:
        """List SAP transparent tables/views exposed by AvailableTableSet."""
        params: Dict[str, Any] = {"$top": top}
        if name_filter:
            upper_pat = name_filter.strip().upper().replace("*", "")
            params["$filter"] = f"substringof('{upper_pat}', TableName)"
        data = self._request("AvailableTableSet", params=params)
        return data.get("d", {}).get("results", [])

    def get_record_count(self, table_name: str) -> int:
        """Query exact live SAP record count for the requested table."""
        table_upper = table_name.strip().upper()
        txt = self._request(
            "TableDataSet/$count",
            params={"$filter": f"TableName eq '{table_upper}'"},
            expect_json=False,
        )
        return int(str(txt).strip() or 0)

    def fetch_table_data(
        self,
        table_name: str,
        max_rows: Optional[int] = None,
        batch_size: Optional[int] = None,
        allow_fallback: bool = False,
    ) -> pd.DataFrame:
        """
        Extract rows dynamically from ANY SAP table via OData ZGENERIC_DATA_SRV.

        Args:
            table_name: SAP DDIC table or compatibility view name (e.g. 'BSID', 'BKPF', 'T001').
            max_rows: Max rows to fetch; None or <= 0 extracts ALL rows without limit.
            batch_size: Rows per OData page ($top). Defaults to configured batch_size (1000).
            allow_fallback: Ignored here (kept for signature compatibility with pipeline callers).
        """
        table_upper = table_name.strip().upper()
        step = int(batch_size or self.default_batch_size or 1000)
        unlimited = max_rows is None or max_rows <= 0

        # Validate table metadata first so unknown table names fail/report clearly
        meta_cols = self.get_table_metadata(table_upper)
        if not meta_cols:
            raise SAPODataError(
                f"SAP table/view '{table_upper}' was not found in SAP Data Dictionary (DD03L/DD02L)."
            )

        try:
            total_sap_rows = self.get_record_count(table_upper)
        except Exception as exc:
            logger.warning(f"Could not read exact $count for {table_upper} ({exc}); paging until empty.")
            total_sap_rows = -1

        target_desc = "ALL (dynamic)" if unlimited else str(max_rows)
        logger.info(
            f"[ODATA LIVE] Extracting SAP table '{table_upper}' via {self.config['service_name']} "
            f"(SAP total: {total_sap_rows if total_sap_rows >= 0 else 'unknown'}, Limit: {target_desc}, Batch: {step})"
        )

        if total_sap_rows == 0:
            col_names = [c["column_name"].lower() for c in meta_cols if c.get("column_name")]
            logger.warning(f"[ODATA LIVE] SAP table '{table_upper}' has 0 rows in client {self.client}.")
            return pd.DataFrame(columns=col_names)

        all_rows: List[Dict[str, Any]] = []
        skip = 0

        while True:
            if not unlimited:
                remaining = int(max_rows) - len(all_rows)
                if remaining <= 0:
                    break
                current_top = min(step, remaining)
            else:
                current_top = step

            data = self._request(
                "TableDataSet",
                params={
                    "$filter": f"TableName eq '{table_upper}'",
                    "$top": current_top,
                    "$skip": skip,
                },
            )
            results = data.get("d", {}).get("results", [])
            if not results:
                break

            chunk_rows: List[Dict[str, Any]] = []
            for item in results:
                raw_json = item.get("RowJson") or item.get("JsonData") or ""
                if raw_json:
                    parsed = json.loads(raw_json)
                    chunk_rows.append({str(k).lower(): v for k, v in parsed.items()})
                else:
                    clean_item = {
                        str(k).lower(): v
                        for k, v in item.items()
                        if k not in ("__metadata", "TableName", "RowIndex")
                    }
                    if clean_item:
                        chunk_rows.append(clean_item)

            if not chunk_rows:
                break

            all_rows.extend(chunk_rows)
            skip += len(chunk_rows)
            logger.info(
                f"  -> {table_upper}: fetched {len(all_rows)}"
                f"{f'/{total_sap_rows}' if total_sap_rows >= 0 else ''} rows..."
            )

            if len(results) < current_top:
                break
            if total_sap_rows >= 0 and skip >= total_sap_rows:
                break

        df = pd.DataFrame(all_rows)
        logger.info(
            f"[ODATA LIVE SUCCESS] Extracted {len(df)} rows x {len(df.columns)} columns for '{table_upper}'."
        )
        return df

