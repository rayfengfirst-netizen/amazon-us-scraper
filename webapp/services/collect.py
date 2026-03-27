from __future__ import annotations

import json
from datetime import datetime, timezone
import re
import os
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup
from sqlmodel import Session, select

from amazon_us_scraper.scraperapi_amazon import ScraperAPIError, fetch_amazon_product_us
from amazon_us_scraper.scraperapi_ebay import ScraperAPIEbayError, fetch_ebay_product
from webapp.db import engine
from webapp.models import AsinSnapshot, EbaySnapshot, Target
from webapp.services.images import IMAGES_ROOT, download_high_res_images, normalize_image_urls_in_data


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _image_dir_has_files(item_key: str) -> bool:
    d = IMAGES_ROOT / item_key.strip().upper()
    return d.is_dir() and any(d.iterdir())


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for bad in soup.select("script,style,noscript"):
        bad.decompose()
    text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _enrich_ebay_description(data: dict) -> dict:
    """
    eBay Structured payload often has description URL only.
    Fetch and fill `full_description` for downstream rendering/publishing.
    """
    if not isinstance(data, dict):
        return data
    if str(data.get("full_description") or "").strip() or str(data.get("description") or "").strip():
        return data
    desc_url = str(data.get("item_description_from_seller_url") or "").strip()
    if not desc_url.startswith("http"):
        return data
    scraper_key = (os.getenv("SCRAPERAPI_KEY") or "").strip()
    use_proxy = (os.getenv("SCRAPERAPI_PROXY_DESC_ENABLE") or "1").strip().lower() in {"1", "true", "yes", "on"}
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            )
        }
        resp = None
        if use_proxy and scraper_key:
            proxy_url = f"https://api.scraperapi.com/?api_key={quote_plus(scraper_key)}&url={quote_plus(desc_url)}"
            resp = requests.get(proxy_url, timeout=25, headers=headers)
        # Fallback: direct fetch
        if resp is None or resp.status_code >= 400 or not resp.text:
            resp = requests.get(desc_url, timeout=20, headers=headers)
        if resp.status_code >= 400 or not resp.text:
            return data
        text = _html_to_text(resp.text)
        if text:
            data["full_description"] = text[:15000]
    except Exception:
        pass
    return data


def run_collect(target_id: int, *, force_refresh: bool = False) -> None:
    """同步执行采集（由后台线程 / BackgroundTasks 调用）。"""
    with Session(engine) as session:
        target = session.get(Target, target_id)
        if target is None:
            return
        target.status = "running"
        target.result_json = None
        target.error_message = None
        target.collect_via = None
        target.updated_at = _utcnow()
        session.add(target)
        session.commit()
        session.refresh(target)

        item_key = target.asin.strip().upper()
        source = (target.source or "amazon").strip().lower()
        if source == "ebay":
            snapshot = session.get(EbaySnapshot, item_key)
        else:
            snapshot = session.get(AsinSnapshot, item_key)
        data: dict | None = None

        try:
            use_cache = (
                not force_refresh
                and snapshot is not None
                and snapshot.result_json
            )
            if use_cache:
                data = json.loads(snapshot.result_json)
                if not isinstance(data, dict):
                    raise ValueError("缓存 JSON 格式异常")
                data = normalize_image_urls_in_data(data)
                if source == "ebay":
                    data = _enrich_ebay_description(data)
                normalized_body = json.dumps(data, ensure_ascii=False)
                target.result_json = normalized_body
                # Backfill old cache with normalized image URLs.
                if snapshot.result_json != normalized_body:
                    snapshot.result_json = normalized_body
                    snapshot.updated_at = _utcnow()
                    session.add(snapshot)
                target.status = "success"
                target.collect_via = "cache"
                target.error_message = None
            else:
                data = fetch_ebay_product(item_key) if source == "ebay" else fetch_amazon_product_us(item_key)
                data = normalize_image_urls_in_data(data)
                if source == "ebay":
                    data = _enrich_ebay_description(data)
                body = json.dumps(data, ensure_ascii=False)
                target.result_json = body
                target.status = "success"
                target.collect_via = "api"
                target.error_message = None
                if snapshot is None and source == "ebay":
                    snapshot = EbaySnapshot(item_id=item_key, result_json=body, updated_at=_utcnow())
                elif snapshot is None:
                    snapshot = AsinSnapshot(asin=item_key, result_json=body, updated_at=_utcnow())
                else:
                    snapshot.result_json = body
                    snapshot.updated_at = _utcnow()
                session.add(snapshot)

        except (ScraperAPIError, ScraperAPIEbayError) as e:
            target.status = "failed"
            target.error_message = str(e)
            target.collect_via = None
        except Exception as e:
            target.status = "failed"
            target.error_message = f"{type(e).__name__}: {e}"
            target.collect_via = None

        target.updated_at = _utcnow()
        session.add(target)
        session.commit()

        if target.status != "success" or not data:
            return

        if source == "ebay":
            snap2 = session.get(EbaySnapshot, item_key)
        else:
            snap2 = session.get(AsinSnapshot, item_key)
        skip_images = (
            not force_refresh
            and target.collect_via == "cache"
            and snap2
            and snap2.images_synced_at is not None
            and _image_dir_has_files(item_key)
        )
        if skip_images:
            return

        try:
            download_high_res_images(item_key, data)
            if source == "ebay":
                snap3 = session.get(EbaySnapshot, item_key)
            else:
                snap3 = session.get(AsinSnapshot, item_key)
            if snap3:
                snap3.images_synced_at = _utcnow()
                session.add(snap3)
                session.commit()
        except Exception:
            pass


def list_latest_per_asin(session: Session) -> list[Target]:
    """按 ASIN 去重，每个 ASIN 保留 id 最大的一条（最新提交）。"""
    rows = session.exec(select(Target).order_by(Target.id.desc())).all()
    seen: set[tuple[str, str]] = set()
    out: list[Target] = []
    for t in rows:
        key = ((t.source or "amazon").lower(), t.asin)
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    out.sort(key=lambda x: x.updated_at or _utcnow(), reverse=True)
    return out
