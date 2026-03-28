from __future__ import annotations

import base64
import binascii
import hmac
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, Body, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func
from sqlmodel import Session, select

from webapp.asin_parse import parse_asin
from webapp.ebay_parse import parse_ebay_item_id
from webapp.db import DATA_DIR, engine, init_db
from webapp.models import AsinSnapshot, EbaySnapshot, ShopifyPublishLog, ShopifyShop, Target, UpcCode
from webapp.ai_copy import (
    list_ai_provider_choices,
    normalize_ai_provider,
    optimize_shopify_copy,
    optimize_shopify_field,
    provider_is_configured,
)
from webapp.prompt_library import (
    create_prompt_library,
    delete_prompt_library,
    get_prompt_library,
    list_prompt_libraries,
    update_prompt_library,
)
from webapp.services.collect import list_latest_per_asin, run_collect
from webapp.services.images import extract_high_res_image_urls, list_media_urls
from webapp.services.payload_view import build_product_view
from webapp.shopify_service import (
    ShopifyShopConfig,
    build_shopify_editor_defaults,
    fetch_shopify_product_editor_values,
    normalize_shop_domain,
    publish_target_to_shopify,
    verify_admin_credentials,
)

ROOT = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(ROOT / "templates"))
_CN_TZ = ZoneInfo("Asia/Shanghai")


def _format_datetime_cn(dt: Optional[datetime]) -> str:
    """UTC（或 naive 视为 UTC）→ 中国时间，用于列表/页面展示。"""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_CN_TZ).strftime("%Y-%m-%d %H:%M")


templates.env.filters["cn_time"] = _format_datetime_cn
IMAGES_DIR = DATA_DIR / "images"
IMAGES_DIR.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Amazon/eBay 采集台", lifespan=lifespan)


def _is_basic_auth_enabled() -> bool:
    enabled = (os.getenv("APP_BASIC_AUTH_ENABLE") or "").strip().lower() in {"1", "true", "yes", "on"}
    user = (os.getenv("APP_BASIC_AUTH_USERNAME") or "").strip()
    pwd = os.getenv("APP_BASIC_AUTH_PASSWORD") or ""
    return enabled and bool(user and pwd)


def _build_basic_auth_401() -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"detail": "Unauthorized"},
        headers={"WWW-Authenticate": 'Basic realm="amazon-us-scraper"'},
    )


def _verify_basic_auth(authorization: str | None) -> bool:
    if not authorization:
        return False
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "basic":
        return False
    try:
        decoded = base64.b64decode(parts[1]).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False
    if ":" not in decoded:
        return False
    username, password = decoded.split(":", 1)
    expected_user = (os.getenv("APP_BASIC_AUTH_USERNAME") or "").strip()
    expected_pass = os.getenv("APP_BASIC_AUTH_PASSWORD") or ""
    return hmac.compare_digest(username, expected_user) and hmac.compare_digest(password, expected_pass)


@app.middleware("http")
async def app_basic_auth_guard(request: Request, call_next):
    if not _is_basic_auth_enabled():
        return await call_next(request)
    # Keep health endpoint open for server probes/systemd checks.
    if request.url.path == "/health":
        return await call_next(request)
    if not _verify_basic_auth(request.headers.get("Authorization")):
        return _build_basic_auth_401()
    return await call_next(request)


@app.get("/health")
def health() -> dict:
    """无模板、无 DB 复杂逻辑，供 systemd/探活使用。"""
    return {"ok": True, "service": "amazon-us-scraper"}


app.mount(
    "/media/product",
    StaticFiles(directory=str(IMAGES_DIR)),
    name="product_images",
)


def _mask_token(t: str) -> str:
    t = (t or "").strip()
    if len(t) <= 8:
        return "****"
    return f"{t[:6]}…{t[-4:]}"


def _shopify_token_hint(shop: ShopifyShop) -> str:
    """列表展示：OAuth 模式或静态 token 掩码。"""
    if (shop.oauth_client_id or "").strip() and (shop.oauth_client_secret or "").strip():
        cid = (shop.oauth_client_id or "").strip()
        tail = cid[-4:] if len(cid) >= 4 else "****"
        return f"OAuth · …{tail}"
    if (shop.admin_token or "").strip():
        return _mask_token(shop.admin_token)
    return "—"


def _shopify_cfg(shop: ShopifyShop) -> ShopifyShopConfig:
    return ShopifyShopConfig(
        shop_domain=shop.shop_domain,
        admin_token=shop.admin_token or "",
        api_version=(shop.api_version or "2025-01").strip(),
        oauth_client_id=shop.oauth_client_id,
        oauth_client_secret=shop.oauth_client_secret,
    )


def _target_to_api_dict(t: Target) -> dict:
    out = {
        "id": t.id,
        "source": t.source or "amazon",
        "asin": t.asin,
        "original_input": t.original_input,
        "status": t.status,
        "error_message": t.error_message,
        "collect_via": t.collect_via,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }
    if t.result_json:
        try:
            out["data"] = json.loads(t.result_json)
        except json.JSONDecodeError:
            out["data_raw"] = t.result_json
    else:
        out["data"] = None
    return out


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _split_inputs(raw: str) -> list[str]:
    text = (raw or "").replace("\r", "\n")
    text = text.replace(",", "\n").replace(";", "\n")
    items = [x.strip() for x in text.split("\n")]
    return [x for x in items if x]


def _normalize_upc(raw: str) -> str:
    return (raw or "").strip()


def _home_context(session: Session, page: int, source: str, per_page: int = 50) -> dict[str, Any]:
    source = (source or "amazon").strip().lower()
    rows = [r for r in list_latest_per_asin(session) if (r.source or "amazon").strip().lower() == source]
    total = len(rows)
    total_pages = max(1, (total + per_page - 1) // per_page)
    safe_page = max(1, min(page, total_pages))
    start = (safe_page - 1) * per_page
    page_rows = rows[start : start + per_page]

    item_keys = {r.asin for r in page_rows}
    if item_keys and source == "ebay":
        snaps = session.exec(select(EbaySnapshot).where(EbaySnapshot.item_id.in_(list(item_keys)))).all()
        cached_asins = {s.item_id for s in snaps}
    elif item_keys:
        snaps = session.exec(select(AsinSnapshot).where(AsinSnapshot.asin.in_(list(item_keys)))).all()
        cached_asins = {s.asin for s in snaps}
    else:
        cached_asins = set()

    thumb_urls: dict[int, Optional[str]] = {}
    for t in page_rows:
        thumb = None
        local_urls = list_media_urls(t.asin) if t.status == "success" else []
        if local_urls:
            thumb = local_urls[0]
        elif t.status == "success" and t.result_json:
            try:
                parsed = json.loads(t.result_json)
                if isinstance(parsed, dict):
                    remote = extract_high_res_image_urls(parsed)
                    thumb = remote[0] if remote else None
            except Exception:
                thumb = None
        thumb_urls[t.id] = thumb

    target_ids = [t.id for t in page_rows if t.id is not None]
    shopify_state_by_target: dict[int, str] = {}
    if target_ids:
        logs = session.exec(
            select(ShopifyPublishLog)
            .where(ShopifyPublishLog.target_id.in_(target_ids))
            .order_by(desc(ShopifyPublishLog.id))
        ).all()
        latest_by_target: dict[int, ShopifyPublishLog] = {}
        for lg in logs:
            if lg.target_id not in latest_by_target:
                latest_by_target[lg.target_id] = lg
        for tid in target_ids:
            lg = latest_by_target.get(tid)
            if not lg:
                shopify_state_by_target[tid] = "never"
                continue
            if lg.shopify_product_id and not lg.error_message:
                shopify_state_by_target[tid] = "published"
            elif lg.error_message:
                shopify_state_by_target[tid] = "failed"
            else:
                shopify_state_by_target[tid] = "never"

    running_cnt = len([r for r in page_rows if r.status == "running"])
    done_cnt = len([r for r in page_rows if r.status in {"success", "failed"}])
    progress_total = len(page_rows)
    progress_pct = int(done_cnt * 100 / progress_total) if progress_total else 0

    return {
        "source": source,
        "targets": page_rows,
        "cached_asins": cached_asins,
        "thumb_urls": thumb_urls,
        "shopify_state_by_target": shopify_state_by_target,
        "page": safe_page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "has_prev": safe_page > 1,
        "has_next": safe_page < total_pages,
        "prev_page": safe_page - 1,
        "next_page": safe_page + 1,
        "progress_total": progress_total,
        "progress_done": done_cnt,
        "progress_running": running_cnt,
        "progress_pct": progress_pct,
    }


def _merge_editor_state(defaults: dict[str, Any], saved_json: Optional[str]) -> tuple[dict[str, Any], bool]:
    if not saved_json:
        return defaults, False
    try:
        saved = json.loads(saved_json)
    except Exception:
        return defaults, False
    if not isinstance(saved, dict):
        return defaults, False
    out = dict(defaults)
    for k in (
        "title",
        "body_html",
        "seo_title",
        "seo_description",
        "price",
        "vendor",
        "tags",
        "sku",
        "inventory_quantity",
        "metafield_warehouse",
        "metafield_specifications",
        "metafield_delivery_time",
        "metafield_qa",
        "metafield_vehicle_fitment",
        "metafield_package_list",
        "prompt_library_id",
        "ai_provider",
    ):
        if k in saved and saved.get(k) is not None:
            val = saved.get(k)
            # 这两个字段若历史草稿为空，回退默认值，避免发布时被空值覆盖
            if k in {"metafield_warehouse", "metafield_delivery_time"} and not str(val).strip():
                continue
            out[k] = val
    return out, True


def _persist_editor_state(session: Session, target: Target, editor_values: dict[str, Any], *, rewritten: bool) -> None:
    payload = {
        "title": str(editor_values.get("title") or ""),
        "body_html": str(editor_values.get("body_html") or ""),
        "seo_title": str(editor_values.get("seo_title") or ""),
        "seo_description": str(editor_values.get("seo_description") or ""),
        "price": str(editor_values.get("price") or ""),
        "vendor": str(editor_values.get("vendor") or ""),
        "tags": str(editor_values.get("tags") or ""),
        "sku": str(editor_values.get("sku") or ""),
        "inventory_quantity": str(editor_values.get("inventory_quantity") or ""),
        "metafield_warehouse": str(editor_values.get("metafield_warehouse") or ""),
        "metafield_specifications": str(editor_values.get("metafield_specifications") or ""),
        "metafield_delivery_time": str(editor_values.get("metafield_delivery_time") or ""),
        "metafield_qa": str(editor_values.get("metafield_qa") or ""),
        "metafield_vehicle_fitment": str(editor_values.get("metafield_vehicle_fitment") or ""),
        "metafield_package_list": str(editor_values.get("metafield_package_list") or ""),
        "prompt_library_id": str(editor_values.get("prompt_library_id") or "default_v1"),
        "ai_provider": str(editor_values.get("ai_provider") or "openai").strip().lower()[:32],
    }
    target.shopify_editor_json = json.dumps(payload, ensure_ascii=False)
    if rewritten:
        target.shopify_ai_rewritten_at = _utcnow()
    session.add(target)
    session.commit()


def _merge_non_empty_editor_values(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """
    合并规则：incoming 仅在“有非空值”时覆盖 base，避免同步时空值抹掉本地已存内容。
    """
    out = dict(base)
    for k, v in (incoming or {}).items():
        if v is None:
            continue
        if isinstance(v, str):
            if not v.strip():
                continue
            out[k] = v
            continue
        out[k] = v
    return out


def _normalize_sku_for_source(source: str, item_key: str, sku: str) -> str:
    src = (source or "amazon").strip().lower()
    raw_item = (item_key or "").strip()
    raw_sku = (sku or "").strip()
    if src != "ebay":
        return raw_sku
    if raw_item.isdigit() and 9 <= len(raw_item) <= 15:
        expected = f"EB-{raw_item}"
        # If old default AM-* or empty, auto-correct to eBay SKU rule.
        if (not raw_sku) or raw_sku.upper().startswith("AM-"):
            return expected
    return raw_sku


def _parse_selected_image_urls(raw: str) -> list[str]:
    text = (raw or "").strip()
    if not text:
        return []
    out: list[str] = []
    try:
        v = json.loads(text)
        if isinstance(v, list):
            for x in v:
                s = str(x or "").strip()
                if s and s not in out:
                    out.append(s)
            return out
    except Exception:
        pass
    for part in text.replace("\r", "\n").replace(",", "\n").split("\n"):
        s = part.strip()
        if s and s not in out:
            out.append(s)
    return out


@app.get("/", response_class=HTMLResponse)
def page_home(request: Request, page: int = Query(1, ge=1)):
    with Session(engine) as session:
        ctx = _home_context(session, page=page, source="amazon", per_page=50)
    batch_msg = request.query_params.get("batch_msg")
    if batch_msg:
        ctx["batch_msg"] = batch_msg
    ctx["source_label"] = "Amazon"
    ctx["source_name_cn"] = "亚马逊"
    ctx["item_key_label"] = "ASIN"
    ctx["home_path"] = "/"
    ctx["page_title_hint"] = "按更新时间倒序，50 条/页"
    return templates.TemplateResponse(
        request,
        "index.html",
        ctx,
    )


@app.get("/ebay", response_class=HTMLResponse)
def page_home_ebay(request: Request, page: int = Query(1, ge=1)):
    with Session(engine) as session:
        ctx = _home_context(session, page=page, source="ebay", per_page=50)
    batch_msg = request.query_params.get("batch_msg")
    if batch_msg:
        ctx["batch_msg"] = batch_msg
    ctx["source_label"] = "eBay"
    ctx["source_name_cn"] = "eBay"
    ctx["item_key_label"] = "Item ID"
    ctx["home_path"] = "/ebay"
    ctx["page_title_hint"] = "按更新时间倒序，50 条/页"
    return templates.TemplateResponse(
        request,
        "index.html",
        ctx,
    )


@app.post("/targets")
def post_target(
    request: Request,
    background_tasks: BackgroundTasks,
    raw: str = Form(..., alias="input"),
    auto_collect: int = Form(0),
    source: str = Form("amazon"),
):
    source = (source or "amazon").strip().lower()
    if source not in {"amazon", "ebay"}:
        source = "amazon"
    home_path = "/ebay" if source == "ebay" else "/"
    entries = _split_inputs(raw)
    if not entries:
        with Session(engine) as session:
            ctx = _home_context(session, page=1, source=source, per_page=50)
            ctx["source_label"] = "eBay" if source == "ebay" else "Amazon"
            ctx["source_name_cn"] = "eBay" if source == "ebay" else "亚马逊"
            ctx["item_key_label"] = "Item ID" if source == "ebay" else "ASIN"
            ctx["home_path"] = home_path
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                **ctx,
                "error": "请输入至少 1 条商品标识或商品链接。",
                "form_value": raw,
            },
            status_code=400,
        )

    parsed_pairs: list[tuple[str, str]] = []
    invalid_inputs: list[str] = []
    for item in entries:
        item_key = parse_ebay_item_id(item) if source == "ebay" else parse_asin(item)
        if not item_key:
            invalid_inputs.append(item)
            continue
        parsed_pairs.append((item_key, item))

    if not parsed_pairs:
        with Session(engine) as session:
            ctx = _home_context(session, page=1, source=source, per_page=50)
            ctx["source_label"] = "eBay" if source == "ebay" else "Amazon"
            ctx["source_name_cn"] = "eBay" if source == "ebay" else "亚马逊"
            ctx["item_key_label"] = "Item ID" if source == "ebay" else "ASIN"
            ctx["home_path"] = home_path
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                **ctx,
                "error": "没有可识别的商品标识，请检查输入格式。",
                "form_value": raw,
                "invalid_inputs": invalid_inputs[:8],
            },
            status_code=400,
        )

    unique_pairs: dict[str, str] = {}
    for asin, item in parsed_pairs:
        unique_pairs[asin] = item

    created = 0
    refreshed = 0
    auto_started = 0
    with Session(engine) as session:
        asins = list(unique_pairs.keys())
        existing_rows = (
            session.exec(
                select(Target)
                .where(Target.asin.in_(asins), Target.source == source)
                .order_by(Target.id.desc())
            ).all()
            if asins
            else []
        )
        existing_by_asin: dict[str, Target] = {}
        for row in existing_rows:
            if row.asin not in existing_by_asin:
                existing_by_asin[row.asin] = row
        now = _utcnow()
        new_ids: list[int] = []
        for asin, original_input in unique_pairs.items():
            existing = existing_by_asin.get(asin)
            if existing is not None:
                existing.original_input = original_input.strip()[:2048]
                existing.updated_at = now
                session.add(existing)
                refreshed += 1
                continue
            t = Target(source=source, asin=asin, original_input=original_input.strip()[:2048], status="pending")
            session.add(t)
            session.flush()
            if t.id is not None:
                new_ids.append(t.id)
            created += 1
        session.commit()

    if auto_collect:
        for tid in new_ids:
            background_tasks.add_task(run_collect, tid, force_refresh=False)
            auto_started += 1

    msg = f"已处理 {len(unique_pairs)} 条：新增 {created}，刷新排序 {refreshed}"
    if auto_collect:
        msg += f"，已启动采集 {auto_started}"
    if invalid_inputs:
        msg += f"；无法识别 {len(invalid_inputs)} 条"
    return RedirectResponse(url=f"{home_path}?batch_msg={quote(msg, safe='')}", status_code=303)


@app.post("/targets/{target_id}/collect")
def post_collect(
    target_id: int,
    background_tasks: BackgroundTasks,
    force: bool = Query(False),
):
    with Session(engine) as session:
        t = session.get(Target, target_id)
        if t is None:
            raise HTTPException(404, "记录不存在")
        if t.status == "running":
            return RedirectResponse(url=f"/targets/{target_id}", status_code=303)
        background_tasks.add_task(run_collect, target_id, force_refresh=force)
    return RedirectResponse(url=f"/targets/{target_id}", status_code=303)


@app.post("/targets/{target_id}/shopify-publish")
def post_shopify_publish(
    target_id: int,
    shop_id: int = Form(...),
    product_status: str = Form("draft"),
    publish_scope: str = Form("all"),
    title: str = Form(""),
    body_html: str = Form(""),
    seo_title: str = Form(""),
    seo_description: str = Form(""),
    price: str = Form(""),
    vendor: str = Form(""),
    tags: str = Form(""),
    sku: str = Form(""),
    inventory_quantity: str = Form("30"),
    metafield_warehouse: str = Form(""),
    metafield_specifications: str = Form(""),
    metafield_delivery_time: str = Form(""),
    metafield_qa: str = Form(""),
    metafield_vehicle_fitment: str = Form(""),
    metafield_package_list: str = Form(""),
    selected_image_urls: str = Form(""),
    prompt_library_id: str = Form("default_v1"),
    ai_provider: str = Form("openai"),
):
    if product_status not in {"draft", "active", "archived"}:
        raise HTTPException(400, "无效的商品状态")
    if publish_scope not in {"all", "online_store"}:
        raise HTTPException(400, "无效的发布范围")

    with Session(engine) as session:
        t = session.get(Target, target_id)
        shop = session.get(ShopifyShop, shop_id)
        if t is None or shop is None:
            raise HTTPException(404, "记录或店铺不存在")
        if not t.result_json or t.status != "success":
            return RedirectResponse(
                url=f"/targets/{target_id}?shopify_err=1",
                status_code=303,
            )
        existing_pub = session.exec(
            select(ShopifyPublishLog)
            .where(
                ShopifyPublishLog.target_id == target_id,
                ShopifyPublishLog.shop_id == shop_id,
                ShopifyPublishLog.shopify_product_id.is_not(None),
                ShopifyPublishLog.error_message.is_(None),
            )
            .order_by(desc(ShopifyPublishLog.id))
        ).first()
        existing_product_id = int(existing_pub.shopify_product_id) if existing_pub and existing_pub.shopify_product_id else None

        upc: Optional[UpcCode] = None
        if existing_product_id:
            upc = session.exec(
                select(UpcCode)
                .where(UpcCode.used_shopify_product_id == existing_product_id, UpcCode.used == True)  # noqa: E712
                .order_by(desc(UpcCode.id))
            ).first()
        else:
            upc = session.exec(
                select(UpcCode).where(UpcCode.used == False).order_by(UpcCode.id.asc())  # noqa: E712
            ).first()
            if upc is None:
                log = ShopifyPublishLog(
                    target_id=target_id,
                    shop_id=shop_id,
                    shopify_product_id=None,
                    product_status=product_status,
                    publish_scope=publish_scope,
                    error_message="无可用 UPC，请先到「UPC维护」页面添加 12 位 UPC。",
                    report_json=None,
                )
                session.add(log)
                session.commit()
                return RedirectResponse(
                    url=f"/targets/{target_id}?shopify_err=1",
                    status_code=303,
                )
        try:
            parsed = json.loads(t.result_json)
            if not isinstance(parsed, dict):
                raise ValueError("采集 JSON 不是对象")
            norm_sku = _normalize_sku_for_source(t.source or "amazon", t.asin, sku)
            if norm_sku:
                sku = norm_sku
            cfg = _shopify_cfg(shop)
            image_urls_override = _parse_selected_image_urls(selected_image_urls)
            pid, report = publish_target_to_shopify(
                parsed,
                t.asin,
                cfg,
                product_status=product_status,
                publish_scope=publish_scope,
                use_ai=False,
                title_override=title or None,
                body_html_override=body_html or None,
                seo_title_override=seo_title or None,
                seo_desc_override=seo_description or None,
                price_override=float(price) if price.strip() else None,
                vendor_override=vendor or None,
                tags_override=tags,
                sku_override=sku or None,
                inventory_qty_override=int(inventory_quantity) if inventory_quantity.strip() else None,
                upc_override=(upc.code if upc else None),
                existing_product_id=existing_product_id,
                metafield_warehouse_override=metafield_warehouse or None,
                metafield_specifications_override=metafield_specifications or None,
                metafield_delivery_time_override=metafield_delivery_time or None,
                metafield_qa_override=metafield_qa or None,
                metafield_vehicle_fitment_override=metafield_vehicle_fitment or None,
                metafield_package_list_override=metafield_package_list or None,
                image_urls_override=image_urls_override or None,
                prompt_library_id=prompt_library_id or None,
            )
            if upc is not None and not upc.used:
                upc.used = True
                upc.used_at = _utcnow()
                upc.used_target_id = target_id
                upc.used_shopify_product_id = int(pid)
                session.add(upc)
            log = ShopifyPublishLog(
                target_id=target_id,
                shop_id=shop_id,
                shopify_product_id=pid,
                product_status=product_status,
                publish_scope=publish_scope,
                error_message=None,
                report_json=json.dumps(report, ensure_ascii=False)[:8000],
            )
            session.add(log)
            _persist_editor_state(
                session,
                t,
                {
                    "title": title,
                    "body_html": body_html,
                    "seo_title": seo_title,
                    "seo_description": seo_description,
                    "price": price,
                    "vendor": vendor,
                    "tags": tags,
                    "sku": sku,
                    "inventory_quantity": inventory_quantity,
                    "metafield_warehouse": metafield_warehouse,
                    "metafield_specifications": metafield_specifications,
                    "metafield_delivery_time": metafield_delivery_time,
                    "metafield_qa": metafield_qa,
                    "metafield_vehicle_fitment": metafield_vehicle_fitment,
                    "metafield_package_list": metafield_package_list,
                    "prompt_library_id": prompt_library_id,
                    "ai_provider": normalize_ai_provider(ai_provider),
                },
                rewritten=False,
            )
            session.commit()
        except Exception as exc:  # noqa: BLE001
            log = ShopifyPublishLog(
                target_id=target_id,
                shop_id=shop_id,
                shopify_product_id=None,
                product_status=product_status,
                publish_scope=publish_scope,
                error_message=str(exc)[:4090],
                report_json=None,
            )
            session.add(log)
            session.commit()
            return RedirectResponse(
                url=f"/targets/{target_id}?shopify_err=1",
                status_code=303,
            )

    action = "update" if existing_product_id else "create"
    return RedirectResponse(url=f"/targets/{target_id}?shopify_ok=1&spid={pid}&act={action}", status_code=303)


@app.post("/api/targets/{target_id}/shopify-rewrite")
def post_shopify_rewrite(
    target_id: int,
    payload: dict[str, Any] = Body(...),
):
    with Session(engine) as session:
        t = session.get(Target, target_id)
        if t is None or not t.result_json:
            raise HTTPException(404, "记录不存在或无采集数据")
    parsed = json.loads(t.result_json)
    if not isinstance(parsed, dict):
        raise HTTPException(400, "采集数据格式不正确")
    pv = build_product_view(parsed)
    defaults = {
        "title": str(payload.get("title") or ""),
        "body_html": str(payload.get("body_html") or ""),
        "seo_title": str(payload.get("seo_title") or ""),
        "seo_description": str(payload.get("seo_description") or ""),
    }
    lib_id = str(payload.get("prompt_library_id") or "default_v1")
    prov = normalize_ai_provider(str(payload.get("ai_provider") or ""))
    if not provider_is_configured(prov):
        raise HTTPException(
            400,
            "所选 AI 模型未配置或未开启（豆包需 DOUBAO_ENABLE、DOUBAO_API_KEY、DOUBAO_MODEL 接入点 ID）",
        )
    field = str(payload.get("field") or "").strip()
    if field:
        if field not in defaults:
            raise HTTPException(400, "field 参数无效")
        val = optimize_shopify_field(
            parsed,
            pv,
            t.asin,
            field,
            defaults[field],
            library_id=lib_id,
            provider=prov,
        )
        defaults[field] = val
        with Session(engine) as session:
            t2 = session.get(Target, target_id)
            if t2 is not None:
                _persist_editor_state(
                    session,
                    t2,
                    {
                        **defaults,
                        "price": str(payload.get("price") or ""),
                        "vendor": str(payload.get("vendor") or ""),
                        "tags": str(payload.get("tags") or ""),
                        "sku": str(payload.get("sku") or ""),
                        "inventory_quantity": str(payload.get("inventory_quantity") or ""),
                        "metafield_warehouse": str(payload.get("metafield_warehouse") or ""),
                        "metafield_specifications": str(payload.get("metafield_specifications") or ""),
                        "metafield_delivery_time": str(payload.get("metafield_delivery_time") or ""),
                        "metafield_qa": str(payload.get("metafield_qa") or ""),
                        "metafield_vehicle_fitment": str(payload.get("metafield_vehicle_fitment") or ""),
                        "metafield_package_list": str(payload.get("metafield_package_list") or ""),
                        "prompt_library_id": lib_id,
                        "ai_provider": prov,
                    },
                    rewritten=True,
                )
        return JSONResponse({"field": field, "value": val})

    optimized = optimize_shopify_copy(parsed, pv, t.asin, defaults, library_id=lib_id, provider=prov)
    with Session(engine) as session:
        t2 = session.get(Target, target_id)
        if t2 is not None:
            _persist_editor_state(
                session,
                t2,
                {
                    "title": optimized.get("title", defaults["title"]),
                    "body_html": optimized.get("body_html", defaults["body_html"]),
                    "seo_title": optimized.get("seo_title", defaults["seo_title"]),
                    "seo_description": optimized.get("seo_description", defaults["seo_description"]),
                    "price": str(payload.get("price") or ""),
                    "vendor": str(payload.get("vendor") or ""),
                    "tags": str(payload.get("tags") or ""),
                    "sku": str(payload.get("sku") or ""),
                    "inventory_quantity": str(payload.get("inventory_quantity") or ""),
                    "metafield_warehouse": str(payload.get("metafield_warehouse") or ""),
                    "metafield_specifications": str(payload.get("metafield_specifications") or ""),
                    "metafield_delivery_time": str(payload.get("metafield_delivery_time") or ""),
                    "metafield_qa": str(payload.get("metafield_qa") or ""),
                    "metafield_vehicle_fitment": str(payload.get("metafield_vehicle_fitment") or ""),
                    "metafield_package_list": str(payload.get("metafield_package_list") or ""),
                    "prompt_library_id": lib_id,
                    "ai_provider": prov,
                },
                rewritten=True,
            )
    return JSONResponse(optimized)


@app.get("/settings/prompt-libraries", response_class=HTMLResponse)
def page_prompt_libraries(request: Request):
    libs = list_prompt_libraries()
    flash = {
        "ok": request.query_params.get("ok"),
        "err": request.query_params.get("err"),
        "msg": request.query_params.get("msg"),
    }
    return templates.TemplateResponse(
        request,
        "settings_prompt_libraries.html",
        {"libraries": libs, "flash": flash},
    )


@app.post("/settings/prompt-libraries")
def post_prompt_library_create(
    name: str = Form(""),
    zh_comment: str = Form(""),
    title_zh_comment: str = Form(""),
    title_template: str = Form(""),
    description_zh_comment: str = Form(""),
    description_template: str = Form(""),
    seo_title_zh_comment: str = Form(""),
    seo_title_template: str = Form(""),
    seo_description_zh_comment: str = Form(""),
    seo_description_template: str = Form(""),
):
    try:
        create_prompt_library(
            {
                "name": name,
                "zh_comment": zh_comment,
                "prompts": {
                    "title": {"zh_comment": title_zh_comment, "template": title_template},
                    "description": {"zh_comment": description_zh_comment, "template": description_template},
                    "seo_title": {"zh_comment": seo_title_zh_comment, "template": seo_title_template},
                    "seo_description": {
                        "zh_comment": seo_description_zh_comment,
                        "template": seo_description_template,
                    },
                },
            }
        )
        return RedirectResponse(url="/settings/prompt-libraries?ok=1&msg=模板已新增", status_code=303)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(
            url=f"/settings/prompt-libraries?err=1&msg={quote(str(exc)[:180], safe='')}",
            status_code=303,
        )


@app.post("/settings/prompt-libraries/{library_id}/update")
def post_prompt_library_update(
    library_id: str,
    name: str = Form(""),
    zh_comment: str = Form(""),
    title_zh_comment: str = Form(""),
    title_template: str = Form(""),
    description_zh_comment: str = Form(""),
    description_template: str = Form(""),
    seo_title_zh_comment: str = Form(""),
    seo_title_template: str = Form(""),
    seo_description_zh_comment: str = Form(""),
    seo_description_template: str = Form(""),
):
    try:
        update_prompt_library(
            library_id,
            {
                "name": name,
                "zh_comment": zh_comment,
                "prompts": {
                    "title": {"zh_comment": title_zh_comment, "template": title_template},
                    "description": {"zh_comment": description_zh_comment, "template": description_template},
                    "seo_title": {"zh_comment": seo_title_zh_comment, "template": seo_title_template},
                    "seo_description": {
                        "zh_comment": seo_description_zh_comment,
                        "template": seo_description_template,
                    },
                },
            },
        )
        return RedirectResponse(url="/settings/prompt-libraries?ok=1&msg=模板已更新", status_code=303)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(
            url=f"/settings/prompt-libraries?err=1&msg={quote(str(exc)[:180], safe='')}",
            status_code=303,
        )


@app.post("/settings/prompt-libraries/{library_id}/delete")
def post_prompt_library_delete(library_id: str):
    try:
        delete_prompt_library(library_id)
        return RedirectResponse(url="/settings/prompt-libraries?ok=1&msg=模板已删除", status_code=303)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(
            url=f"/settings/prompt-libraries?err=1&msg={quote(str(exc)[:180], safe='')}",
            status_code=303,
        )


@app.get("/settings/upc", response_class=HTMLResponse)
def page_settings_upc(request: Request):
    with Session(engine) as session:
        rows = session.exec(select(UpcCode).order_by(UpcCode.id.desc())).all()
    return templates.TemplateResponse(
        request,
        "settings_upc.html",
        {"upcs": rows},
    )


@app.post("/settings/upc")
def post_settings_upc(raw: str = Form(..., alias="upc_input")):
    lines = [x.strip() for x in (raw or "").replace("\r", "\n").split("\n")]
    lines = [x for x in lines if x]
    if not lines:
        return RedirectResponse(url="/settings/upc?err=1&msg=请输入至少一行UPC", status_code=303)

    valid: list[str] = []
    invalid: list[str] = []
    for line in lines:
        code = _normalize_upc(line)
        if len(code) != 12:
            invalid.append(line)
            continue
        valid.append(code)
    if not valid:
        return RedirectResponse(url="/settings/upc?err=1&msg=UPC长度必须为12位", status_code=303)

    with Session(engine) as session:
        existing = session.exec(select(UpcCode).where(UpcCode.code.in_(valid))).all()
        existing_set = {x.code for x in existing}
        added = 0
        for code in valid:
            if code in existing_set:
                continue
            session.add(UpcCode(code=code, used=False))
            added += 1
        session.commit()

    msg = f"新增 {added} 条"
    if invalid:
        msg += f"，忽略无效 {len(invalid)} 条（长度非12）"
    return RedirectResponse(url=f"/settings/upc?ok=1&msg={quote(msg, safe='')}", status_code=303)


@app.post("/targets/{target_id}/shopify-sync")
def post_shopify_sync(target_id: int):
    with Session(engine) as session:
        t = session.get(Target, target_id)
        if t is None:
            raise HTTPException(404, "记录不存在")
        last_ok = session.exec(
            select(ShopifyPublishLog)
            .where(
                ShopifyPublishLog.target_id == target_id,
                ShopifyPublishLog.shopify_product_id.is_not(None),
                ShopifyPublishLog.error_message.is_(None),
            )
            .order_by(desc(ShopifyPublishLog.id))
        ).first()
        if not last_ok or not last_ok.shopify_product_id:
            return RedirectResponse(url=f"/targets/{target_id}?sync_err=1&msg=暂无已发布商品可同步", status_code=303)
        shop = session.get(ShopifyShop, int(last_ok.shop_id))
        if shop is None:
            return RedirectResponse(url=f"/targets/{target_id}?sync_err=1&msg=店铺配置不存在", status_code=303)
        try:
            remote = fetch_shopify_product_editor_values(_shopify_cfg(shop), int(last_ok.shopify_product_id))
            parsed: Optional[dict[str, Any]] = None
            if t.result_json:
                try:
                    obj = json.loads(t.result_json)
                    if isinstance(obj, dict):
                        parsed = obj
                except Exception:
                    parsed = None
            base_defaults = build_shopify_editor_defaults(parsed, t.asin) if parsed and isinstance(parsed, dict) else {}
            saved_defaults, _ = _merge_editor_state(base_defaults, t.shopify_editor_json)
            merged = _merge_non_empty_editor_values(saved_defaults, remote)
            merged["prompt_library_id"] = str(saved_defaults.get("prompt_library_id") or "default_v1")
            merged["ai_provider"] = normalize_ai_provider(str(saved_defaults.get("ai_provider") or ""))
            _persist_editor_state(session, t, merged, rewritten=False)
            return RedirectResponse(url=f"/targets/{target_id}?sync_ok=1", status_code=303)
        except Exception as exc:  # noqa: BLE001
            return RedirectResponse(
                url=f"/targets/{target_id}?sync_err=1&msg={quote(str(exc)[:300], safe='')}",
                status_code=303,
            )


@app.get("/settings/shops", response_class=HTMLResponse)
def page_settings_shops(request: Request):
    with Session(engine) as session:
        shops = session.exec(select(ShopifyShop).order_by(ShopifyShop.id)).all()
    rows = [
        {
            "id": s.id,
            "label": s.label,
            "shop_domain": s.shop_domain,
            "api_version": s.api_version,
            "token_hint": _shopify_token_hint(s),
            "created_at": s.created_at,
        }
        for s in shops
    ]
    return templates.TemplateResponse(
        request,
        "settings_shops.html",
        {"shops": rows},
    )


@app.post("/settings/shops")
def post_settings_shop(
    request: Request,
    label: str = Form(...),
    shop_domain: str = Form(...),
    admin_token: str = Form(""),
    oauth_client_id: str = Form(""),
    oauth_client_secret: str = Form(""),
    api_version: str = Form("2025-01"),
):
    label = label.strip()
    shop_domain = normalize_shop_domain(shop_domain)
    admin_token = admin_token.strip()
    oauth_client_id = oauth_client_id.strip()
    oauth_client_secret = oauth_client_secret.strip()
    api_version = (api_version or "2025-01").strip()
    has_oauth = bool(oauth_client_id and oauth_client_secret)
    has_static = bool(admin_token)
    if not label or not shop_domain or (not has_oauth and not has_static):
        return RedirectResponse(url="/settings/shops?err=1", status_code=303)
    with Session(engine) as session:
        s = ShopifyShop(
            label=label[:128],
            shop_domain=shop_domain[:128],
            admin_token=admin_token[:512] if has_static else "",
            oauth_client_id=oauth_client_id[:128] or None,
            oauth_client_secret=oauth_client_secret[:256] or None,
            api_version=api_version[:32],
        )
        session.add(s)
        session.commit()
    return RedirectResponse(url="/settings/shops?ok=1", status_code=303)


@app.post("/settings/shops/{shop_id}/verify")
def post_verify_shop_credentials(shop_id: int):
    """用 GET /shop.json 校验已保存店铺的域名与 Admin token（不创建商品）。"""
    with Session(engine) as session:
        shop = session.get(ShopifyShop, shop_id)
        if shop is None:
            raise HTTPException(404, "店铺不存在")
        cfg = _shopify_cfg(shop)
        try:
            info = verify_admin_credentials(cfg)
        except Exception as exc:  # noqa: BLE001 — 含 RuntimeError 与网络错误
            return RedirectResponse(
                url=f"/settings/shops?verify_err=1&msg={quote(str(exc), safe='')}",
                status_code=303,
            )
        name = (info.get("name") or "")[:200]
        return RedirectResponse(
            url=f"/settings/shops?verify_ok=1&vname={quote(name, safe='')}",
            status_code=303,
        )


@app.post("/settings/shops/{shop_id}/delete")
def post_delete_shop(shop_id: int):
    """删除店铺配置；若有关联发布日志导致失败，给出提示。"""
    with Session(engine) as session:
        shop = session.get(ShopifyShop, shop_id)
        if shop is None:
            return RedirectResponse(url="/settings/shops?del_err=1&msg=店铺不存在", status_code=303)
        try:
            session.delete(shop)
            session.commit()
            return RedirectResponse(url="/settings/shops?del_ok=1", status_code=303)
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            return RedirectResponse(
                url=f"/settings/shops?del_err=1&msg={quote(str(exc), safe='')}",
                status_code=303,
            )


@app.get("/targets/{target_id}", response_class=HTMLResponse)
def page_target_detail(
    request: Request,
    target_id: int,
    shopify_ok: Optional[int] = Query(None),
    shopify_err: Optional[int] = Query(None),
    spid: Optional[int] = Query(None),
    act: Optional[str] = Query(None),
    sync_ok: Optional[int] = Query(None),
    sync_err: Optional[int] = Query(None),
    msg: Optional[str] = Query(None),
):
    with Session(engine) as session:
        t = session.get(Target, target_id)
        if t is None:
            raise HTTPException(404, "记录不存在")
        t_view = {
            "id": t.id,
            "source": t.source or "amazon",
            "asin": t.asin,
            "status": t.status,
            "error_message": t.error_message,
            "collect_via": t.collect_via,
            "original_input": t.original_input,
            "created_at": t.created_at,
            "updated_at": t.updated_at,
            "result_json": t.result_json,
            "shopify_editor_json": t.shopify_editor_json,
            "shopify_ai_rewritten_at": t.shopify_ai_rewritten_at,
        }
        same_rows = session.exec(
            select(Target).where(Target.asin == t.asin, Target.source == t.source).order_by(Target.id.desc())
        ).all()
        same = [
            {
                "id": r.id,
                "asin": r.asin,
                "status": r.status,
                "created_at": r.created_at,
            }
            for r in same_rows
        ]
        if (t.source or "amazon").strip().lower() == "ebay":
            has_snapshot = session.get(EbaySnapshot, t.asin.strip().upper()) is not None
        else:
            has_snapshot = session.get(AsinSnapshot, t.asin.strip().upper()) is not None
        shops = session.exec(select(ShopifyShop).order_by(ShopifyShop.id)).all()
        shop_options = [{"id": s.id, "label": s.label, "domain": s.shop_domain} for s in shops]

        last_pub_row = session.exec(
            select(ShopifyPublishLog)
            .where(ShopifyPublishLog.target_id == target_id)
            .order_by(desc(ShopifyPublishLog.id))
        ).first()
        last_pub = (
            {
                "id": last_pub_row.id,
                "target_id": last_pub_row.target_id,
                "shop_id": last_pub_row.shop_id,
                "shopify_product_id": last_pub_row.shopify_product_id,
                "product_status": last_pub_row.product_status,
                "publish_scope": last_pub_row.publish_scope,
                "error_message": last_pub_row.error_message,
                "created_at": last_pub_row.created_at,
            }
            if last_pub_row
            else None
        )
        last_ok_pub = session.exec(
            select(ShopifyPublishLog)
            .where(
                ShopifyPublishLog.target_id == target_id,
                ShopifyPublishLog.shopify_product_id.is_not(None),
                ShopifyPublishLog.error_message.is_(None),
            )
            .order_by(desc(ShopifyPublishLog.id))
        ).first()
        used_upc_row = session.exec(
            select(UpcCode)
            .where(UpcCode.used_target_id == target_id, UpcCode.used == True)  # noqa: E712
            .order_by(desc(UpcCode.id))
        ).first()
        upc_available_count = session.exec(
            select(func.count()).select_from(UpcCode).where(UpcCode.used == False)  # noqa: E712
        ).one()
        has_published_shopify = bool(last_ok_pub and last_ok_pub.shopify_product_id)
        used_upc_code = used_upc_row.code if used_upc_row else ""

    data_pretty: Optional[str] = None
    product_view: Optional[dict[str, Any]] = None
    parsed: Optional[dict[str, Any]] = None
    if t_view.get("result_json"):
        try:
            parsed = json.loads(str(t_view.get("result_json") or ""))
            data_pretty = json.dumps(parsed, ensure_ascii=False, indent=2)
            if isinstance(parsed, dict):
                product_view = build_product_view(parsed)
        except json.JSONDecodeError:
            data_pretty = str(t_view.get("result_json") or "")

    image_urls = list_media_urls(str(t_view.get("asin") or "")) if t_view.get("status") == "success" else []
    source = (t_view.get("source") or "amazon").strip().lower()
    source_label = "eBay" if source == "ebay" else "Amazon"
    source_home_path = "/ebay" if source == "ebay" else "/"

    shopify_flash: Optional[dict[str, Any]] = None
    if shopify_ok and spid:
        shopify_flash = {"ok": True, "spid": spid, "act": act or "create"}
    elif shopify_err:
        err_msg = ""
        if last_pub and last_pub.get("error_message"):
            err_msg = str(last_pub.get("error_message") or "")
        shopify_flash = {"ok": False, "message": err_msg or "发布失败，请查看下方记录或重试。"}
    sync_flash: Optional[dict[str, Any]] = None
    if sync_ok:
        sync_flash = {"ok": True, "text": "已手动同步 Shopify 最新内容"}
    elif sync_err:
        sync_flash = {"ok": False, "text": msg or "同步失败"}

    shopify_editor: Optional[dict[str, Any]] = None
    shopify_editor_saved = False
    if parsed and isinstance(parsed, dict) and t_view.get("status") == "success":
        defaults = build_shopify_editor_defaults(parsed, str(t_view.get("asin") or ""))
        shopify_editor, shopify_editor_saved = _merge_editor_state(defaults, t_view.get("shopify_editor_json"))
        if shopify_editor:
            # eBay 价格波动频繁，页面默认价格应跟随最新采集值，避免历史草稿价格误导发布。
            if str(t_view.get("source") or "").strip().lower() == "ebay":
                shopify_editor["price"] = defaults.get("price", shopify_editor.get("price", ""))
                shopify_editor["price_original"] = defaults.get(
                    "price_original", shopify_editor.get("price_original", "")
                )
            fixed_sku = _normalize_sku_for_source(
                str(t_view.get("source") or "amazon"),
                str(t_view.get("asin") or ""),
                str(shopify_editor.get("sku") or ""),
            )
            if fixed_sku and fixed_sku != str(shopify_editor.get("sku") or ""):
                shopify_editor["sku"] = fixed_sku
    prompt_libraries = list_prompt_libraries()
    default_prompt_library_id = "default_v1"
    if prompt_libraries and not get_prompt_library(default_prompt_library_id):
        default_prompt_library_id = prompt_libraries[0]["id"]

    ai_provider_options = list_ai_provider_choices()
    allowed_prov = {o["id"] for o in ai_provider_options}
    saved_prov = normalize_ai_provider(str((shopify_editor or {}).get("ai_provider") or ""))
    if saved_prov in allowed_prov:
        default_ai_provider = saved_prov
    elif ai_provider_options:
        default_ai_provider = ai_provider_options[0]["id"]
    else:
        default_ai_provider = normalize_ai_provider(None)

    return templates.TemplateResponse(
        request,
        "detail.html",
        {
            "t": t_view,
            "data_pretty": data_pretty,
            "parsed": parsed,
            "product_view": product_view,
            "image_urls": image_urls,
            "same_asin_targets": same,
            "has_snapshot": has_snapshot,
            "shop_options": shop_options,
            "last_publish": last_pub,
            "has_published_shopify": has_published_shopify,
            "shopify_flash": shopify_flash,
            "sync_flash": sync_flash,
            "shopify_editor": shopify_editor,
            "shopify_editor_saved": shopify_editor_saved,
            "shopify_ai_rewritten_at": t_view.get("shopify_ai_rewritten_at"),
            "used_upc_code": used_upc_code,
            "upc_available_count": int(upc_available_count or 0),
            "prompt_libraries": prompt_libraries,
            "default_prompt_library_id": default_prompt_library_id,
            "ai_provider_options": ai_provider_options,
            "default_ai_provider": default_ai_provider,
            "source_label": source_label,
            "source_home_path": source_home_path,
            "item_key_label": "Item ID" if source == "ebay" else "ASIN",
        },
    )


@app.get("/api/targets")
def api_targets_list():
    with Session(engine) as session:
        rows = list_latest_per_asin(session)
    return [_target_to_api_dict(t) for t in rows]


@app.get("/api/targets/{target_id}")
def api_target_one(target_id: int):
    with Session(engine) as session:
        t = session.get(Target, target_id)
        if t is None:
            raise HTTPException(404, "记录不存在")
    return _target_to_api_dict(t)


@app.get("/api/debug/runtime-env")
def api_debug_runtime_env() -> dict[str, Any]:
    """只返回环境变量存在性，避免泄露密钥值。"""
    env_path = ROOT / ".env"
    key = (os.getenv("SCRAPERAPI_KEY") or "").strip()
    return {
        "cwd": os.getcwd(),
        "env_path": str(env_path),
        "env_exists": env_path.exists(),
        "env_mtime": env_path.stat().st_mtime if env_path.exists() else None,
        "scraperapi_key_set": bool(key),
        "scraperapi_key_len": len(key),
        "openai_llm_ready": provider_is_configured("openai"),
        "doubao_llm_ready": provider_is_configured("doubao"),
    }
