"""商品页相关：拉取 HTML、简单解析占位（页面结构常变，需自行维护选择器）。"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup

from amazon_us_scraper.client import get


def product_url(asin: str) -> str:
    asin = asin.strip().upper()
    if len(asin) != 10 or not asin.isalnum():
        raise ValueError("ASIN 应为 10 位字母数字")
    return f"/dp/{asin}"


def fetch_product_html(asin: str, *, client: httpx.Client | None = None) -> str:
    """获取商品详情页 HTML。可能遇到验证码或 503，需自行处理。"""
    r = get(product_url(asin), client=client)
    r.raise_for_status()
    return r.text


@dataclass
class ProductSnippet:
    asin: str
    title: str | None
    # 可扩展：price, rating, availability 等


def parse_product_title(html: str) -> str | None:
    """尝试从详情页解析标题（#productTitle 等，失效时需改选择器）。"""
    soup = BeautifulSoup(html, "lxml")
    node = soup.select_one("#productTitle")
    if node:
        return node.get_text(strip=True)
    og = soup.select_one('meta[property="og:title"]')
    if og and og.get("content"):
        return og["content"].strip()
    return None


def fetch_product_snippet(asin: str, *, client: httpx.Client | None = None) -> ProductSnippet:
    html = fetch_product_html(asin, client=client)
    return ProductSnippet(asin=asin.strip().upper(), title=parse_product_title(html))
