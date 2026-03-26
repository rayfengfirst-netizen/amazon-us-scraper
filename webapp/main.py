from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from webapp.asin_parse import parse_asin
from webapp.db import DATA_DIR, engine, init_db
from webapp.models import AsinSnapshot, Target
from webapp.services.collect import list_latest_per_asin, run_collect
from webapp.services.images import list_media_urls
from webapp.services.payload_view import build_product_view

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


@app.get("/", response_class=HTMLResponse)
def page_home(request: Request):
    with Session(engine) as session:
        rows = list_latest_per_asin(session)
        asins = {r.asin for r in rows}
        if asins:
            snaps = session.exec(
                select(AsinSnapshot).where(AsinSnapshot.asin.in_(list(asins)))
            ).all()
            cached_asins = {s.asin for s in snaps}
        else:
            cached_asins = set()
    return templates.TemplateResponse(
        request,
        "index.html",
        {"targets": rows, "cached_asins": cached_asins},
    )


@app.post("/targets")
def post_target(
    request: Request,
    raw: str = Form(..., alias="input"),
):
    asin = parse_asin(raw)
    if not asin:
        with Session(engine) as session:
            rows = list_latest_per_asin(session)
            asins = {r.asin for r in rows}
            if asins:
                snaps = session.exec(
                    select(AsinSnapshot).where(AsinSnapshot.asin.in_(list(asins)))
                ).all()
                cached_asins = {s.asin for s in snaps}
            else:
                cached_asins = set()
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "targets": rows,
                "cached_asins": cached_asins,
                "error": "无法识别 ASIN，请填写 10 位 ASIN 或含 /dp/、/gp/product/ 等路径的亚马逊商品链接。",
                "form_value": raw,
            },
            status_code=400,
        )
    with Session(engine) as session:
        t = Target(asin=asin, original_input=raw.strip(), status="pending")
        session.add(t)
        session.commit()
        session.refresh(t)
    return RedirectResponse(url="/", status_code=303)


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


@app.get("/targets/{target_id}", response_class=HTMLResponse)
def page_target_detail(request: Request, target_id: int):
    with Session(engine) as session:
        t = session.get(Target, target_id)
        if t is None:
            raise HTTPException(404, "记录不存在")
        same = session.exec(
            select(Target).where(Target.asin == t.asin).order_by(Target.id.desc())
        ).all()
        has_snapshot = session.get(AsinSnapshot, t.asin.strip().upper()) is not None

    data_pretty: str | None = None
    product_view: dict | None = None
    parsed: dict | None = None
    if t.result_json:
        try:
            parsed = json.loads(t.result_json)
            data_pretty = json.dumps(parsed, ensure_ascii=False, indent=2)
            if isinstance(parsed, dict):
                product_view = build_product_view(parsed)
        except json.JSONDecodeError:
            data_pretty = t.result_json

    image_urls = list_media_urls(t.asin) if t.status == "success" else []

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
