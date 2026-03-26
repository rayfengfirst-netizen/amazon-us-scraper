"""带美国站默认头、可选节流 的 HTTP 客户端。"""

from __future__ import annotations

import time
from typing import Any

import httpx

from amazon_us_scraper.config import (
    AMAZON_US_BASE,
    DEFAULT_HEADERS,
    request_delay_sec,
    user_agent,
)

_last_request_mono: float = 0.0


def _throttle() -> None:
    global _last_request_mono
    delay = request_delay_sec()
    if delay <= 0:
        return
    now = time.monotonic()
    elapsed = now - _last_request_mono
    if elapsed < delay:
        time.sleep(delay - elapsed)
    _last_request_mono = time.monotonic()


def build_client(**kwargs: Any) -> httpx.Client:
    headers = {**DEFAULT_HEADERS, "User-Agent": user_agent()}
    return httpx.Client(headers=headers, follow_redirects=True, **kwargs)


def get(url: str, *, client: httpx.Client | None = None, **kwargs: Any) -> httpx.Response:
    """
    GET 请求；相对路径会拼到 www.amazon.com。
    每次调用前按 AMAZON_REQUEST_DELAY_SEC 节流。
    """
    if url.startswith("/"):
        url = f"{AMAZON_US_BASE.rstrip('/')}{url}"
    _throttle()
    if client is None:
        with build_client() as c:
            return c.get(url, **kwargs)
    return client.get(url, **kwargs)
