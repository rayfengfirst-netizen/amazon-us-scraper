from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict

import requests
from dotenv import load_dotenv
from webapp.prompt_library import get_prompt_library

ROOT = Path(__file__).resolve().parent.parent
PROMPT_DIR = ROOT / "prompts" / "shopify_openai"
load_dotenv(ROOT / ".env")
logger = logging.getLogger(__name__)


def _read_prompt_template(name: str) -> str:
    p = PROMPT_DIR / name
    if not p.exists():
        raise RuntimeError(f"Prompt template not found: {p}")
    return p.read_text(encoding="utf-8")


def _openai_enabled() -> bool:
    return os.getenv("OPENAI_ENABLE", "0").strip() in {"1", "true", "TRUE", "yes", "YES"}


def _openai_complete(prompt: str) -> str:
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is empty")
    base_url = (os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").strip().rstrip("/")
    model = (os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()
    timeout_sec = int(os.getenv("OPENAI_TIMEOUT_SEC", "60"))
    temperature = float(os.getenv("OPENAI_TEMPERATURE", "0.4"))

    url = f"{base_url}/chat/completions"
    body = {
        "model": model,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": "You are an ecommerce copywriter. Return only the requested final text."},
            {"role": "user", "content": prompt},
        ],
    }
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=timeout_sec,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"OpenAI error {resp.status_code}: {resp.text[:1200]}")
    data = resp.json()
    try:
        out = data["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Unexpected OpenAI response: {data}") from exc
    return (out or "").strip()


def _context_json(parsed: Dict[str, Any], product_view: Dict[str, Any], asin: str) -> str:
    ctx = {
        "asin": asin,
        "title_guess": product_view.get("title"),
        "brand": product_view.get("brand"),
        "price_guess": product_view.get("price"),
        "bullets": (product_view.get("bullets") or [])[:20],
        "top_level_keys": (product_view.get("top_level_keys") or [])[:80],
        "raw_excerpt": parsed,
    }
    s = json.dumps(ctx, ensure_ascii=False)
    if len(s) > 20000:
        s = s[:20000]
    return s


def _description_source(parsed: Dict[str, Any]) -> str:
    root: Dict[str, Any] = parsed
    if isinstance(parsed.get("response"), dict):
        root = parsed["response"]
    full = ""
    if isinstance(root.get("full_description"), str):
        full = root["full_description"].strip()
    elif isinstance(root.get("description"), str):
        full = root["description"].strip()
    bullets: list[str] = []
    raw = root.get("feature_bullets")
    if isinstance(raw, list):
        for x in raw:
            s = str(x).strip()
            if s:
                bullets.append(s)
    if not bullets:
        for key in ("bullet_points", "about_this_item", "features"):
            rv = root.get(key)
            if isinstance(rv, list):
                for x in rv:
                    s = str(x).strip()
                    if s:
                        bullets.append(s)
    parts: list[str] = []
    if full:
        parts.append(full)
    if bullets:
        parts.append("\n".join(f"- {b}" for b in bullets))
    return "\n\n".join(parts).strip()


def optimize_shopify_copy(
    parsed: Dict[str, Any],
    product_view: Dict[str, Any],
    asin: str,
    defaults: Dict[str, str],
    *,
    library_id: str | None = None,
) -> Dict[str, str]:
    """
    返回可用于 Shopify 的四个字段：
    title, body_html, seo_title, seo_description
    未开启 OPENAI_ENABLE 时直接返回 defaults。
    """
    out = {
        "title": defaults.get("title", ""),
        "body_html": defaults.get("body_html", ""),
        "seo_title": defaults.get("seo_title", ""),
        "seo_description": defaults.get("seo_description", ""),
    }
    if not _openai_enabled():
        return out

    lid = (library_id or os.getenv("OPENAI_PROMPT_LIBRARY", "default_v1")).strip()
    lib = get_prompt_library(lid)
    if not lib:
        lib = get_prompt_library("default_v1")
    if not lib:
        return out
    prompts = lib.get("prompts") or {}

    ctx = _context_json(parsed, product_view, asin)
    desc_source = _description_source(parsed)
    mapping = [
        ("title", "title", 255),
        ("body_html", "description", 12000),
        ("seo_title", "seo_title", 70),
        ("seo_description", "seo_description", 320),
    ]
    for key, pkey, max_len in mapping:
        item = prompts.get(pkey) or {}
        template = str(item.get("template") or "")
        if not template:
            continue
        prompt = template.format(
            asin=asin,
            default_value=out[key],
            context_json=ctx,
            description_source=desc_source,
        )
        try:
            val = _openai_complete(prompt)
            if val:
                out[key] = val[:max_len]
        except Exception as exc:
            # Fail open: keep defaults for this field, but keep server-side reason for troubleshooting.
            logger.warning("shopify-rewrite field=%s failed: %s", key, exc)
            continue
    return out
