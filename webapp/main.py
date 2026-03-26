from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from datetime import datetime, timezone

from fastapi import BackgroundTasks, Body, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func
from sqlmodel import Session, select

from webapp.asin_parse import parse_asin
from webapp.db import DATA_DIR, engine, init_db
from webapp.models import AsinSnapshot, ShopifyPublishLog, ShopifyShop, Target, UpcCode
from webapp.prompt_library import get_prompt_library, list_prompt_libraries
from webapp.services.collect import list_latest_per_asin, run_collect
from webapp.services.images import extract_high_res_image_urls, list_media_urls
from webapp.services.payload_view import build_product_view
from webapp.shopify_service import (
    ShopifyShopConfig,
    build_shopify_editor_defaults,
    normalize_shop_domain,
    publish_target_to_shopify,
    verify_admin_credentials,
)

ROOT = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(ROOT / "templates"))
IMAGES_DIR = DATA_DIR / "images"
IMAGES_DIR.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Amazon US 采集台", lifespan=lifespan)


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


def _home_context(session: Session, page: int, per_page: int = 50) -> dict[str, Any]:
    rows = list_latest_per_asin(session)
    total = len(rows)
    total_pages = max(1, (total + per_page - 1) // per_page)
    safe_page = max(1, min(page, total_pages))
    start = (safe_page - 1) * per_page
    page_rows = rows[start : start + per_page]

    asins = {r.asin for r in page_rows}
    if asins:
        snaps = session.exec(select(AsinSnapshot).where(AsinSnapshot.asin.in_(list(asins)))).all()
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

    running_cnt = len([r for r in page_rows if r.status == "running"])
    done_cnt = len([r for r in page_rows if r.status in {"success", "failed"}])
    progress_total = len(page_rows)
    progress_pct = int(done_cnt * 100 / progress_total) if progress_total else 0

    return {
        "targets": page_rows,
        "cached_asins": cached_asins,
        "thumb_urls": thumb_urls,
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
        "prompt_library_id",
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
        "prompt_library_id": str(editor_values.get("prompt_library_id") or "default_v1"),
    }
    target.shopify_editor_json = json.dumps(payload, ensure_ascii=False)
    if rewritten:
        target.shopify_ai_rewritten_at = _utcnow()
    session.add(target)
    session.commit()


@app.get("/", response_class=HTMLResponse)
def page_home(request: Request, page: int = Query(1, ge=1)):
    with Session(engine) as session:
        ctx = _home_context(session, page=page, per_page=50)
    batch_msg = request.query_params.get("batch_msg")
    if batch_msg:
        ctx["batch_msg"] = batch_msg
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
):
    entries = _split_inputs(raw)
    if not entries:
        with Session(engine) as session:
            ctx = _home_context(session, page=1, per_page=50)
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                **ctx,
                "error": "请输入至少 1 条 ASIN 或 Amazon 商品链接。",
                "form_value": raw,
            },
            status_code=400,
        )

    parsed_pairs: list[tuple[str, str]] = []
    invalid_inputs: list[str] = []
    for item in entries:
        asin = parse_asin(item)
        if not asin:
            invalid_inputs.append(item)
            continue
        parsed_pairs.append((asin, item))

    if not parsed_pairs:
        with Session(engine) as session:
            ctx = _home_context(session, page=1, per_page=50)
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                **ctx,
                "error": "没有可识别的 ASIN。请填写 10 位 ASIN 或有效 Amazon 商品链接。",
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
            session.exec(select(Target).where(Target.asin.in_(asins)).order_by(Target.id.desc())).all() if asins else []
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
            t = Target(asin=asin, original_input=original_input.strip()[:2048], status="pending")
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
    return RedirectResponse(url=f"/?batch_msg={quote(msg, safe='')}", status_code=303)


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
    prompt_library_id: str = Form("default_v1"),
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
            cfg = _shopify_cfg(shop)
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
                    "prompt_library_id": prompt_library_id,
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
    from webapp.ai_copy import optimize_shopify_copy, optimize_shopify_field

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
                        "prompt_library_id": lib_id,
                    },
                    rewritten=True,
                )
        return JSONResponse({"field": field, "value": val})

    optimized = optimize_shopify_copy(parsed, pv, t.asin, defaults, library_id=lib_id)
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
                    "prompt_library_id": lib_id,
                },
                rewritten=True,
            )
    return JSONResponse(optimized)


@app.get("/settings/prompt-libraries", response_class=HTMLResponse)
def page_prompt_libraries(request: Request):
    libs = list_prompt_libraries()
    return templates.TemplateResponse(
        request,
        "settings_prompt_libraries.html",
        {"libraries": libs},
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
):
    with Session(engine) as session:
        t = session.get(Target, target_id)
        if t is None:
            raise HTTPException(404, "记录不存在")
        same = session.exec(
            select(Target).where(Target.asin == t.asin).order_by(Target.id.desc())
        ).all()
        has_snapshot = session.get(AsinSnapshot, t.asin.strip().upper()) is not None
        shops = session.exec(select(ShopifyShop).order_by(ShopifyShop.id)).all()
        shop_options = [{"id": s.id, "label": s.label, "domain": s.shop_domain} for s in shops]

        last_pub = session.exec(
            select(ShopifyPublishLog)
            .where(ShopifyPublishLog.target_id == target_id)
            .order_by(desc(ShopifyPublishLog.id))
        ).first()
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

    data_pretty: Optional[str] = None
    product_view: Optional[dict[str, Any]] = None
    parsed: Optional[dict[str, Any]] = None
    if t.result_json:
        try:
            parsed = json.loads(t.result_json)
            data_pretty = json.dumps(parsed, ensure_ascii=False, indent=2)
            if isinstance(parsed, dict):
                product_view = build_product_view(parsed)
        except json.JSONDecodeError:
            data_pretty = t.result_json

    image_urls = list_media_urls(t.asin) if t.status == "success" else []

    shopify_flash: Optional[dict[str, Any]] = None
    if shopify_ok and spid:
        shopify_flash = {"ok": True, "spid": spid, "act": act or "create"}
    elif shopify_err:
        err_msg = ""
        if last_pub and last_pub.error_message:
            err_msg = last_pub.error_message
        shopify_flash = {"ok": False, "message": err_msg or "发布失败，请查看下方记录或重试。"}

    shopify_editor: Optional[dict[str, Any]] = None
    shopify_editor_saved = False
    if parsed and isinstance(parsed, dict) and t.status == "success":
        defaults = build_shopify_editor_defaults(parsed, t.asin)
        shopify_editor, shopify_editor_saved = _merge_editor_state(defaults, t.shopify_editor_json)
    prompt_libraries = list_prompt_libraries()
    default_prompt_library_id = "default_v1"
    if prompt_libraries and not get_prompt_library(default_prompt_library_id):
        default_prompt_library_id = prompt_libraries[0]["id"]

    return templates.TemplateResponse(
        request,
        "detail.html",
        {
            "t": t,
            "data_pretty": data_pretty,
            "parsed": parsed,
            "product_view": product_view,
            "image_urls": image_urls,
            "same_asin_targets": same,
            "has_snapshot": has_snapshot,
            "shop_options": shop_options,
            "last_publish": last_pub,
            "has_published_shopify": bool(last_ok_pub and last_ok_pub.shopify_product_id),
            "shopify_flash": shopify_flash,
            "shopify_editor": shopify_editor,
            "shopify_editor_saved": shopify_editor_saved,
            "shopify_ai_rewritten_at": t.shopify_ai_rewritten_at,
            "used_upc_code": used_upc_row.code if used_upc_row else "",
            "upc_available_count": int(upc_available_count or 0),
            "prompt_libraries": prompt_libraries,
            "default_prompt_library_id": default_prompt_library_id,
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
