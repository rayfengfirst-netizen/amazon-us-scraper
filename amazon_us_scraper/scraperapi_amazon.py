"""ScraperAPI Structured Amazon Product（美国站 amazon.com）。

- 默认：**同步** GET `https://api.scraperapi.com/structured/amazon/product`（`api_key` + `asin` 等），
  避免异步任务 `statusUrl` 轮询在你方账号/线路上返回 404 的问题。
- 可选：设置环境变量 `SCRAPERAPI_USE_ASYNC=1` 使用异步 POST + 轮询（需有效 webhook / 轮询地址）。

文档:
- 异步: https://docs.scraperapi.com/structured-data-endpoints/e-commerce/amazon/amazon-product-api-async
- 同步 Structured 合集见 ScraperAPI「Structured Data」文档（api.scraperapi.com/structured/...）
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")

SYNC_PRODUCT_URL = "https://api.scraperapi.com/structured/amazon/product"
ASYNC_PRODUCT_URL = "https://async.scraperapi.com/structured/amazon/product"
DEFAULT_WEBHOOK_PLACEHOLDER = "https://httpbin.org/post"
DEFAULT_HTTP_TIMEOUT = 70.0
DEFAULT_SYNC_TIMEOUT = 120.0
DEFAULT_POLL_TIMEOUT = 180.0
DEFAULT_POLL_INTERVAL = 3.0


class ScraperAPIError(RuntimeError):
    pass


def _api_key() -> str:
    key = os.getenv("SCRAPERAPI_KEY", "").strip()
    if not key:
        raise ScraperAPIError("请设置环境变量 SCRAPERAPI_KEY（勿提交到 Git）")
    return key


def _webhook_url() -> str:
    url = os.getenv("SCRAPERAPI_WEBHOOK_URL", "").strip()
    if url:
        return url
    return DEFAULT_WEBHOOK_PLACEHOLDER


def _use_async() -> bool:
    return os.getenv("SCRAPERAPI_USE_ASYNC", "").strip().lower() in ("1", "true", "yes", "on")


def _sync_timeout() -> float:
    raw = os.getenv("SCRAPERAPI_SYNC_TIMEOUT_SEC", "").strip()
    if raw:
        try:
            return max(10.0, float(raw))
        except ValueError:
            pass
    return DEFAULT_SYNC_TIMEOUT


def _normalize_status_url(url: str) -> str:
    return url.replace("http://async.scraperapi.com", "https://async.scraperapi.com", 1)


def _attach_api_key_query(url: str, api_key: str, param_name: str = "apiKey") -> str:
    parts = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in ("apikey", "api_key")]
    q.append((param_name, api_key))
    new_query = urlencode(q)
    return urlunparse((parts.scheme, parts.netloc, parts.path, parts.params, new_query, parts.fragment))


def _status_poll_url_candidates(status_url: str) -> list[str]:
    key = _api_key()
    norm = _normalize_status_url(status_url.strip())
    out: list[str] = []
    seen: set[str] = set()

    def add(raw: str, pname: str) -> None:
        u = _attach_api_key_query(raw, key, pname)
        if u not in seen:
            seen.add(u)
            out.append(u)

    path = urlparse(norm).path
    m = re.search(r"/jobs/([0-9a-fA-F-]{36})/?$", path)
    if m:
        jid = m.group(1)
        add(f"https://async.scraperapi.com/structured/amazon/product/{jid}", "apiKey")
        add(f"https://async.scraperapi.com/structured/amazon/product/{jid}", "api_key")
    add(norm, "apiKey")
    add(norm, "api_key")
    return out


def submit_amazon_product_job_us(
    asin: str,
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    asin = asin.strip().upper()
    if len(asin) != 10 or not asin.isalnum():
        raise ValueError("ASIN 应为 10 位字母数字")

    payload: dict[str, Any] = {
        "apiKey": _api_key(),
        "asin": asin,
        "country_code": os.getenv("SCRAPERAPI_COUNTRY_CODE", "us").strip() or "us",
        "tld": os.getenv("SCRAPERAPI_TLD", "com").strip() or "com",
        "callback": {"type": "webhook", "url": _webhook_url()},
    }
    timeout = httpx.Timeout(DEFAULT_HTTP_TIMEOUT)
    if client is None:
        with httpx.Client(timeout=timeout) as c:
            r = c.post(ASYNC_PRODUCT_URL, json=payload, headers={"Content-Type": "application/json"})
    else:
        r = client.post(ASYNC_PRODUCT_URL, json=payload, headers={"Content-Type": "application/json"})

    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict):
        raise ScraperAPIError(f"提交任务返回非对象: {data!r}")
    status_url = data.get("statusUrl")
    if not status_url:
        raise ScraperAPIError(f"响应缺少 statusUrl: {data}")
    return data


def poll_job(
    status_url: str,
    *,
    client: httpx.Client | None = None,
    poll_timeout: float = DEFAULT_POLL_TIMEOUT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
) -> dict[str, Any]:
    poll_urls = _status_poll_url_candidates(status_url)
    if not poll_urls:
        raise ScraperAPIError(f"无法构造轮询 URL: {status_url!r}")

    deadline = time.monotonic() + poll_timeout
    timeout = httpx.Timeout(DEFAULT_HTTP_TIMEOUT)
    close_client = client is None
    c = client or httpx.Client(timeout=timeout)
    preferred_idx = 0

    try:
        while time.monotonic() < deadline:
            resp: httpx.Response | None = None
            for i in range(preferred_idx, len(poll_urls)):
                r = c.get(poll_urls[i])
                if r.status_code == 404:
                    continue
                preferred_idx = i
                resp = r
                break

            if resp is None:
                raise ScraperAPIError(
                    "轮询状态接口全部返回 404。已尝试多种 URL 与 apiKey/api_key 参数。"
                    "建议改用默认的同步接口（去掉环境变量 SCRAPERAPI_USE_ASYNC），"
                    "或到 ScraperAPI 控制台确认异步任务与文档是否一致。"
                )

            resp.raise_for_status()
            body = resp.json()
            if not isinstance(body, dict):
                raise ScraperAPIError(f"状态接口返回非对象: {body!r}")

            if "response" in body and body["response"] is not None:
                return body

            status = (body.get("status") or "").lower()
            if status in ("failed", "error", "cancelled", "canceled"):
                raise ScraperAPIError(f"任务失败: {body}")

            time.sleep(poll_interval)

        raise ScraperAPIError(f"轮询超时（{poll_timeout}s），最后使用: {poll_urls[preferred_idx]}")
    finally:
        if close_client:
            c.close()


def _parse_response_payload(resp: Any) -> dict[str, Any]:
    if resp is None:
        raise ScraperAPIError("response 为空")
    if isinstance(resp, str):
        try:
            parsed = json.loads(resp)
        except json.JSONDecodeError:
            raise ScraperAPIError("response 为字符串但非 JSON") from None
        if not isinstance(parsed, dict):
            raise ScraperAPIError(f"response JSON 非对象: {type(parsed)}")
        return parsed
    if isinstance(resp, dict):
        return resp
    raise ScraperAPIError(f"response 类型不支持: {type(resp)}")


def _normalize_sync_body(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ScraperAPIError(f"同步接口返回非对象: {type(data)}")
    if data.get("success") is False:
        msg = data.get("message") or data.get("error") or json.dumps(data, ensure_ascii=False)[:500]
        raise ScraperAPIError(f"ScraperAPI: {msg}")
    if "error" in data and data.get("error") not in (None, False, ""):
        raise ScraperAPIError(f"ScraperAPI: {data.get('error')}")
    inner = data.get("response")
    if isinstance(inner, dict):
        return inner
    if isinstance(inner, str):
        return _parse_response_payload(inner)
    return data


def fetch_amazon_product_us_sync(
    asin: str,
    *,
    client: httpx.Client,
) -> dict[str, Any]:
    """同步 GET Structured Amazon Product（推荐）。"""
    asin = asin.strip().upper()
    if len(asin) != 10 or not asin.isalnum():
        raise ValueError("ASIN 应为 10 位字母数字")

    params: dict[str, str] = {
        "api_key": _api_key(),
        "asin": asin,
        "country_code": os.getenv("SCRAPERAPI_COUNTRY_CODE", "us").strip() or "us",
        "tld": os.getenv("SCRAPERAPI_TLD", "com").strip() or "com",
    }
    t = httpx.Timeout(_sync_timeout(), connect=20.0)
    r = client.get(SYNC_PRODUCT_URL, params=params, timeout=t)
    try:
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        body = (e.response.text or "")[:800]
        raise ScraperAPIError(f"同步接口 HTTP {e.response.status_code}: {body}") from e
    try:
        data = r.json()
    except json.JSONDecodeError as e:
        raise ScraperAPIError(f"同步接口返回非 JSON: {(r.text or '')[:500]}") from e
    return _normalize_sync_body(data)


def fetch_amazon_product_us_async(
    asin: str,
    *,
    client: httpx.Client,
    poll_timeout: float = DEFAULT_POLL_TIMEOUT,
) -> dict[str, Any]:
    submitted = submit_amazon_product_job_us(asin, client=client)
    status_url = submitted["statusUrl"]
    final = poll_job(status_url, client=client, poll_timeout=poll_timeout)
    return _parse_response_payload(final.get("response"))


def fetch_amazon_product_us(
    asin: str,
    *,
    client: httpx.Client | None = None,
    poll_timeout: float = DEFAULT_POLL_TIMEOUT,
) -> dict[str, Any]:
    """
    拉取美国站结构化商品数据。
    默认走同步 GET；仅当 `SCRAPERAPI_USE_ASYNC=1` 时使用异步 POST + 轮询。
    """

    def _with_http(c: httpx.Client) -> dict[str, Any]:
        if _use_async():
            return fetch_amazon_product_us_async(asin, client=c, poll_timeout=poll_timeout)
        return fetch_amazon_product_us_sync(asin, client=c)

    if client is not None:
        return _with_http(client)
    timeout = httpx.Timeout(max(_sync_timeout(), DEFAULT_HTTP_TIMEOUT), connect=20.0)
    with httpx.Client(timeout=timeout) as c:
        return _with_http(c)


def guess_product_title(data: dict[str, Any]) -> str | None:
    for key in ("name", "title", "product_title", "productName"):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    prod = data.get("product")
    if isinstance(prod, dict):
        for key in ("name", "title"):
            v = prod.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None
