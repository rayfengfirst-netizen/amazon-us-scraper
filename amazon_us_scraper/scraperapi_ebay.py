"""ScraperAPI Structured eBay Product（默认 ebay.com）。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _ROOT / ".env"
load_dotenv(_ENV_PATH, override=True)

SYNC_PRODUCT_URL = "https://api.scraperapi.com/structured/ebay/product"
DEFAULT_SYNC_TIMEOUT = 120.0


class ScraperAPIEbayError(RuntimeError):
    pass


def _api_key() -> str:
    # Reload each call so editing .env takes effect without depending on process env state.
    load_dotenv(_ENV_PATH, override=True)
    key = os.getenv("SCRAPERAPI_KEY", "").strip()
    if not key:
        raise ScraperAPIEbayError(
            f"请设置环境变量 SCRAPERAPI_KEY（勿提交到 Git）。当前读取路径: {_ENV_PATH}"
        )
    return key


def _sync_timeout() -> float:
    raw = os.getenv("SCRAPERAPI_SYNC_TIMEOUT_SEC", "").strip()
    if raw:
        try:
            return max(10.0, float(raw))
        except ValueError:
            pass
    return DEFAULT_SYNC_TIMEOUT


def _normalize_sync_body(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ScraperAPIEbayError(f"同步接口返回非对象: {type(data)}")
    if data.get("success") is False:
        msg = data.get("message") or data.get("error") or json.dumps(data, ensure_ascii=False)[:500]
        raise ScraperAPIEbayError(f"ScraperAPI: {msg}")
    if "error" in data and data.get("error") not in (None, False, ""):
        raise ScraperAPIEbayError(f"ScraperAPI: {data.get('error')}")
    inner = data.get("response")
    if isinstance(inner, dict):
        return inner
    return data


def fetch_ebay_product(
    item_id: str,
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    item_id = (item_id or "").strip()
    if not item_id.isdigit():
        raise ValueError("eBay item_id 必须是数字")

    params: dict[str, str] = {
        "api_key": _api_key(),
        "product_id": item_id,
        "country_code": os.getenv("SCRAPERAPI_EBAY_COUNTRY_CODE", "us").strip() or "us",
        "tld": os.getenv("SCRAPERAPI_EBAY_TLD", "com").strip() or "com",
    }

    timeout = httpx.Timeout(_sync_timeout(), connect=20.0)
    if client is None:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(SYNC_PRODUCT_URL, params=params)
    else:
        r = client.get(SYNC_PRODUCT_URL, params=params, timeout=timeout)

    try:
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        body = (e.response.text or "")[:800]
        raise ScraperAPIEbayError(f"eBay 同步接口 HTTP {e.response.status_code}: {body}") from e
    try:
        data = r.json()
    except json.JSONDecodeError as e:
        raise ScraperAPIEbayError(f"eBay 同步接口返回非 JSON: {(r.text or '')[:500]}") from e
    return _normalize_sync_body(data)
