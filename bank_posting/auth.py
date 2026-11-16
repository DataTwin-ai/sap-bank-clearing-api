"""
API key authorization for the Bank Posting API.

Clients send the key in the `X-API-Key` header:

    curl -H "X-API-Key: <key>" ...

Valid keys come from (first match wins):
    1. Environment variable API_KEYS  (comma-separated)
    2. config.ini  [auth] api_keys    (comma-separated)

Several keys can be active at once, so a key can be rotated without downtime:
add the new key, move the clients over, then remove the old key.

If no key is configured the protected endpoints answer 503 (fail closed),
so a missing setting never leaves SAP posting open to anyone.
"""

import configparser
import hmac
import os
from functools import lru_cache
from pathlib import Path
from typing import FrozenSet, Optional

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.ini"
API_KEY_HEADER_NAME = "X-API-Key"

api_key_header = APIKeyHeader(
    name=API_KEY_HEADER_NAME,
    auto_error=False,
    description="API key issued for this service",
)


def _split_keys(raw: str) -> FrozenSet[str]:
    return frozenset(k.strip() for k in raw.split(",") if k.strip())


@lru_cache(maxsize=1)
def load_api_keys() -> FrozenSet[str]:
    env_keys = os.getenv("API_KEYS")
    if env_keys:
        return _split_keys(env_keys)
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH, encoding="utf-8")
    return _split_keys(cfg.get("auth", "api_keys", fallback=""))


def require_api_key(api_key: Optional[str] = Security(api_key_header)) -> str:
    """FastAPI dependency: rejects the request unless it carries a valid API key."""
    valid_keys = load_api_keys()
    if not valid_keys:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API key authorization is not configured on the server",
        )
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Missing {API_KEY_HEADER_NAME} header",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    # Constant-time comparison against every key (avoids timing leaks)
    supplied = api_key.encode("utf-8")
    if not any(hmac.compare_digest(supplied, k.encode("utf-8")) for k in valid_keys):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    return api_key
