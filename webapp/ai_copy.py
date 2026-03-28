from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List

import requests
from dotenv import load_dotenv
from webapp.prompt_library import get_prompt_library

ROOT = Path(__file__).resolve().parent.parent
PROMPT_DIR = ROOT / "prompts" / "shopify_openai"
load_dotenv(ROOT / ".env", override=True)
logger = logging.getLogger(__name__)

_AI_PROVIDERS = ("openai", "doubao")


class _SafeFormatDict(dict):
    def __missing__(self, key: str) -> str:  # noqa: D401
        # 保留未知占位符原样，避免模板中出现自定义花括号直接抛 KeyError
        return "{" + key + "}"


def _render_prompt_template(template: str, values: Dict[str, Any]) -> str:
    return template.format_map(_SafeFormatDict(values))


def _read_prompt_template(name: str) -> str:
    p = PROMPT_DIR / name
    if not p.exists():
        raise RuntimeError(f"Prompt template not found: {p}")
    return p.read_text(encoding="utf-8")


def _truthy_env(name: str, default: str = "0") -> bool:
    return (os.getenv(name, default) or "").strip().lower() in {"1", "true", "yes", "on"}


def _openai_enabled() -> bool:
    return _truthy_env("OPENAI_ENABLE", "0")


def _doubao_enabled() -> bool:
    return _truthy_env("DOUBAO_ENABLE", "0")


def normalize_ai_provider(raw: str | None) -> str:
    p = (raw or os.getenv("AI_COPY_DEFAULT_PROVIDER") or "openai").strip().lower()
    return p if p in _AI_PROVIDERS else "openai"


def provider_is_configured(provider: str) -> bool:
    p = normalize_ai_provider(provider)
    if p == "openai":
        return _openai_enabled() and bool((os.getenv("OPENAI_API_KEY") or "").strip())
    if p == "doubao":
        return (
            _doubao_enabled()
            and bool((os.getenv("DOUBAO_API_KEY") or "").strip())
            and bool((os.getenv("DOUBAO_MODEL") or "").strip())
        )
    return False


def list_ai_provider_choices() -> List[Dict[str, str]]:
    """详情页可选模型（仅返回已配置且开启的项）。"""
    out: List[Dict[str, str]] = []
    if provider_is_configured("openai"):
        out.append({"id": "openai", "label": "OpenAI"})
    if provider_is_configured("doubao"):
        out.append({"id": "doubao", "label": "豆包（火山方舟）"})
    return out


def _chat_complete(prompt: str, provider: str) -> str:
    p = normalize_ai_provider(provider)
    if p == "doubao":
        api_key = (os.getenv("DOUBAO_API_KEY") or "").strip()
        if not api_key:
            raise RuntimeError("DOUBAO_API_KEY is empty")
        base_url = (os.getenv("DOUBAO_BASE_URL") or "https://ark.cn-beijing.volces.com/api/v3").strip().rstrip("/")
        model = (os.getenv("DOUBAO_MODEL") or "").strip()
        if not model:
            raise RuntimeError("DOUBAO_MODEL is empty (方舟控制台「推理接入点」ID，如 ep-…)")
        timeout_sec = int(os.getenv("DOUBAO_TIMEOUT_SEC") or os.getenv("OPENAI_TIMEOUT_SEC", "60"))
        temperature = float(os.getenv("DOUBAO_TEMPERATURE") or os.getenv("OPENAI_TEMPERATURE", "0.4"))
        system = (
            os.getenv("DOUBAO_SYSTEM_PROMPT") or "你是跨境电商文案助手，只输出用户要求的最终正文，不要解释。"
        ).strip()
    else:
        api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is empty")
        base_url = (os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").strip().rstrip("/")
        model = (os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()
        timeout_sec = int(os.getenv("OPENAI_TIMEOUT_SEC", "60"))
        temperature = float(os.getenv("OPENAI_TEMPERATURE", "0.4"))
        system = "You are an ecommerce copywriter. Return only the requested final text."

    url = f"{base_url}/chat/completions"
    body = {
        "model": model,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system},
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
        raise RuntimeError(f"LLM error ({p}) {resp.status_code}: {resp.text[:1200]}")
    data = resp.json()
    try:
        out = data["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Unexpected LLM response ({p}): {data}") from exc
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
    provider: str | None = None,
) -> Dict[str, str]:
    """
    返回可用于 Shopify 的四个字段：
    title, body_html, seo_title, seo_description
    所选 provider 未配置时直接返回 defaults。
    """
    out = {
        "title": defaults.get("title", ""),
        "body_html": defaults.get("body_html", ""),
        "seo_title": defaults.get("seo_title", ""),
        "seo_description": defaults.get("seo_description", ""),
    }
    prov = normalize_ai_provider(provider)
    if not provider_is_configured(prov):
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
        prompt = _render_prompt_template(
            template,
            {
                "asin": asin,
                "default_value": out[key],
                "context_json": ctx,
                "description_source": desc_source,
                # 兼容旧模板常见占位符
                "title": out.get("title", ""),
                "description": desc_source,
            },
        )
        try:
            val = _chat_complete(prompt, prov)
            if val:
                out[key] = val[:max_len]
        except Exception as exc:
            # Fail open: keep defaults for this field, but keep server-side reason for troubleshooting.
            logger.warning("shopify-rewrite field=%s provider=%s failed: %s", key, prov, exc)
            continue
    return out


def optimize_shopify_field(
    parsed: Dict[str, Any],
    product_view: Dict[str, Any],
    asin: str,
    field: str,
    default_value: str,
    *,
    library_id: str | None = None,
    provider: str | None = None,
) -> str:
    """
    单字段改写（title/body_html/seo_title/seo_description）。
    支持重试，失败时返回原值。
    """
    allowed = {"title", "body_html", "seo_title", "seo_description"}
    if field not in allowed:
        return default_value
    prov = normalize_ai_provider(provider)
    if not provider_is_configured(prov):
        return default_value

    lid = (library_id or os.getenv("OPENAI_PROMPT_LIBRARY", "default_v1")).strip()
    lib = get_prompt_library(lid) or get_prompt_library("default_v1")
    if not lib:
        return default_value
    prompts = lib.get("prompts") or {}
    pkey_map = {
        "title": "title",
        "body_html": "description",
        "seo_title": "seo_title",
        "seo_description": "seo_description",
    }
    pkey = pkey_map[field]
    item = prompts.get(pkey) or {}
    template = str(item.get("template") or "")
    if not template:
        return default_value

    ctx = _context_json(parsed, product_view, asin)
    desc_source = _description_source(parsed)
    prompt = _render_prompt_template(
        template,
        {
            "asin": asin,
            "default_value": default_value or "",
            "context_json": ctx,
            "description_source": desc_source,
            # 兼容旧模板常见占位符
            "title": default_value or "",
            "description": desc_source,
        },
    )
    max_len_map = {"title": 255, "body_html": 12000, "seo_title": 70, "seo_description": 320}
    max_len = max_len_map[field]
    retries = int(os.getenv("OPENAI_RETRY_COUNT", "1"))
    for i in range(retries + 1):
        try:
            val = _chat_complete(prompt, prov).strip()
            if val:
                return val[:max_len]
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "shopify-rewrite field=%s provider=%s attempt=%s failed: %s",
                field,
                prov,
                i + 1,
                exc,
            )
            continue
    return default_value
