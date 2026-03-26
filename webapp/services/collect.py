from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlmodel import Session, select

from amazon_us_scraper.scraperapi_amazon import ScraperAPIError, fetch_amazon_product_us
from webapp.db import engine
from webapp.models import AsinSnapshot, Target
from webapp.services.images import IMAGES_ROOT, download_high_res_images


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _image_dir_has_files(asin: str) -> bool:
    d = IMAGES_ROOT / asin.strip().upper()
    return d.is_dir() and any(d.iterdir())


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

        asin = target.asin.strip().upper()
        snapshot = session.get(AsinSnapshot, asin)
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
                target.result_json = snapshot.result_json
                target.status = "success"
                target.collect_via = "cache"
                target.error_message = None
            else:
                data = fetch_amazon_product_us(asin)
                body = json.dumps(data, ensure_ascii=False)
                target.result_json = body
                target.status = "success"
                target.collect_via = "api"
                target.error_message = None
                if snapshot is None:
                    snapshot = AsinSnapshot(asin=asin, result_json=body, updated_at=_utcnow())
                else:
                    snapshot.result_json = body
                    snapshot.updated_at = _utcnow()
                session.add(snapshot)

        except ScraperAPIError as e:
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

        snap2 = session.get(AsinSnapshot, asin)
        skip_images = (
            not force_refresh
            and target.collect_via == "cache"
            and snap2
            and snap2.images_synced_at is not None
            and _image_dir_has_files(asin)
        )
        if skip_images:
            return

        try:
            download_high_res_images(asin, data)
            snap3 = session.get(AsinSnapshot, asin)
            if snap3:
                snap3.images_synced_at = _utcnow()
                session.add(snap3)
                session.commit()
        except Exception:
            pass


def list_latest_per_asin(session: Session) -> list[Target]:
    """按 ASIN 去重，每个 ASIN 保留 id 最大的一条（最新提交）。"""
    rows = session.exec(select(Target).order_by(Target.id.desc())).all()
    seen: set[str] = set()
    out: list[Target] = []
    for t in rows:
        if t.asin in seen:
            continue
        seen.add(t.asin)
        out.append(t)
    out.sort(key=lambda x: x.updated_at or _utcnow(), reverse=True)
    return out
