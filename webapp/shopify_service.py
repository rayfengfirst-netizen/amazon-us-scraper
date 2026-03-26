"""从采集结果发布到 Shopify Admin API（REST + GraphQL publications）。"""

from __future__ import annotations

import base64
import io
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import requests
from PIL import Image

from webapp.db import DATA_DIR
from webapp.services.images import extract_high_res_image_urls
from webapp.services.payload_view import build_product_view


def normalize_shop_domain(raw: str) -> str:
    """去掉 https://、路径与多余字符，避免误填导致 401。"""
    s = (raw or "").strip()
    s = re.sub(r"^https?://", "", s, flags=re.IGNORECASE)
    s = s.split("/")[0].strip().rstrip("/")
    return s.lower()


@dataclass
class ShopifyShopConfig:
    shop_domain: str
    admin_token: str = ""
    api_version: str = "2025-01"
    oauth_client_id: Optional[str] = None
    oauth_client_secret: Optional[str] = None
    # 同一请求内复用 OAuth 换得的 token，避免多次 POST access_token
    _resolved_access_token: Optional[str] = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.shop_domain = normalize_shop_domain(self.shop_domain)
        self.admin_token = (self.admin_token or "").strip()
        self.api_version = (self.api_version or "2025-01").strip()
        self.oauth_client_id = (self.oauth_client_id or "").strip() or None
        self.oauth_client_secret = (self.oauth_client_secret or "").strip() or None

    @property
    def base_admin_url(self) -> str:
        return f"https://{self.shop_domain}/admin/api/{self.api_version}"


def exchange_client_credentials_token(
    shop_domain: str,
    client_id: str,
    client_secret: str,
) -> str:
    """
    Dev Dashboard 应用：POST /admin/oauth/access_token，grant_type=client_credentials。
    返回的 access_token 用于 X-Shopify-Access-Token（约 24h 有效）。
    https://shopify.dev/docs/apps/build/authentication-authorization/client-secrets
    """
    domain = normalize_shop_domain(shop_domain)
    url = f"https://{domain}/admin/oauth/access_token"
    resp = requests.post(
        url,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id.strip(),
            "client_secret": client_secret.strip(),
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"OAuth client_credentials 失败 ({resp.status_code}): {resp.text[:1500]}。"
            "请确认 Client ID/Secret 来自 Dev Dashboard，且应用已安装到该店铺。"
        )
    body = resp.json()
    token = body.get("access_token")
    if not token:
        raise RuntimeError(f"OAuth 响应缺少 access_token: {body}")
    return str(token).strip()


def access_token_for_config(cfg: ShopifyShopConfig) -> str:
    """优先使用 OAuth client_credentials 换 token；否则用静态 admin_token。"""
    if cfg._resolved_access_token:
        return cfg._resolved_access_token
    if cfg.oauth_client_id and cfg.oauth_client_secret:
        tok = exchange_client_credentials_token(
            cfg.shop_domain,
            cfg.oauth_client_id,
            cfg.oauth_client_secret,
        )
        cfg._resolved_access_token = tok
        return tok
    if cfg.admin_token:
        return cfg.admin_token
    raise RuntimeError(
        "请填写「Admin API access token」，或填写 Dev Dashboard 的 Client ID + Client Secret "
        "（将按 client_credentials 自动换取 token，见 shopify.dev client-secrets）。"
    )


def verify_admin_credentials(cfg: ShopifyShopConfig) -> Dict[str, Any]:
    """
    调用 GET .../shop.json 校验域名 + 凭据。
    成功返回 shop 摘要；失败抛 RuntimeError（多为 401）。
    """
    token = access_token_for_config(cfg)
    url = f"{cfg.base_admin_url}/shop.json"
    resp = requests.get(url, headers=_auth_headers(token), timeout=30)
    if resp.status_code == 401:
        raise RuntimeError(
            "401：凭据无效或与该 .myshopify.com 店铺不匹配。"
            "若使用 Dev Dashboard：请填写 Client ID + Client Secret（不是只填 Client ID）；"
            "若使用店铺后台「开发应用」：请填写安装后的 Admin API access token（shpat_）。"
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:800]}")
    data = resp.json()
    shop = data.get("shop") or {}
    return {"id": shop.get("id"), "name": shop.get("name"), "domain": shop.get("domain")}


def _auth_headers(token: str) -> Dict[str, str]:
    return {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json",
    }


def _auth_headers_from_cfg(cfg: ShopifyShopConfig) -> Dict[str, str]:
    return _auth_headers(access_token_for_config(cfg))


def _to_original_amazon_image(url: str) -> str:
    u = (url or "").strip()
    if "m.media-amazon.com" not in u:
        return u
    return re.sub(r"\._[^.]+_\.(jpg|jpeg|png|webp)$", r".\1", u, flags=re.IGNORECASE)


def _parse_price_number(parsed: Dict[str, Any], product_view: Dict[str, Any]) -> float:
    raw = product_view.get("price") or ""
    if isinstance(raw, str):
        m = re.search(r"[\d]+(?:[.,]\d+)?", raw.replace(",", ""))
        if m:
            try:
                return max(0.01, float(m.group(0).replace(",", ".")))
            except ValueError:
                pass
    # fallback: first numeric in common keys
    for key in ("price", "list_price", "current_price"):
        v = parsed.get(key)
        if isinstance(v, (int, float)):
            return max(0.01, float(v))
        if isinstance(v, str):
            m = re.search(r"[\d.]+", v)
            if m:
                try:
                    return max(0.01, float(m.group(0)))
                except ValueError:
                    pass
    return 19.99


def _build_description_html(parsed: Dict[str, Any], product_view: Dict[str, Any]) -> str:
    bullets = product_view.get("bullets") or []
    parts: List[str] = []
    desc = ""
    if isinstance(parsed.get("description"), str):
        desc = parsed["description"].strip()
    if desc:
        parts.append(f"<p>{_html_escape(desc[:8000])}</p>")
    if bullets:
        parts.append("<ul>")
        for b in bullets[:30]:
            parts.append(f"<li>{_html_escape(b)}</li>")
        parts.append("</ul>")
    if not parts:
        parts.append(f"<p>Imported ASIN {_html_escape(str(parsed.get('asin', '')))}</p>")
    return "\n".join(parts)


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _build_image_attachments(
    parsed: Dict[str, Any],
    asin: str,
    local_media_prefix: str,
) -> List[Dict[str, Any]]:
    """返回 Shopify images[]：优先远程 URL 下载；支持本地已下载文件。"""
    urls = extract_high_res_image_urls(parsed)
    min_w = int(os.getenv("SHOPIFY_MIN_IMAGE_WIDTH", "500"))
    min_h = int(os.getenv("SHOPIFY_MIN_IMAGE_HEIGHT", "500"))
    out: List[Dict[str, Any]] = []
    img_dir = DATA_DIR / "images" / asin.strip().upper()

    for idx, src in enumerate(urls[:15]):
        src = (src or "").strip()
        if not src:
            continue
        try:
            best = _to_original_amazon_image(src)
            r = requests.get(best, timeout=30)
            if r.status_code < 400 and r.content:
                try:
                    im = Image.open(io.BytesIO(r.content))
                    w, h = im.size
                except Exception:  # noqa: BLE001
                    w, h = 0, 0
                if w and h and (w < min_w or h < min_h):
                    continue
                mime = r.headers.get("Content-Type", "image/jpeg")
                ext = "jpg"
                if "png" in mime:
                    ext = "png"
                elif "webp" in mime:
                    ext = "webp"
                out.append(
                    {
                        "attachment": base64.b64encode(r.content).decode("ascii"),
                        "filename": f"amz_{idx + 1}.{ext}",
                    }
                )
        except requests.RequestException:
            continue

    if out:
        return out

    # 本地图
    if img_dir.is_dir():
        for idx, f in enumerate(sorted(img_dir.iterdir())[:15]):
            if not f.is_file():
                continue
            try:
                data = f.read_bytes()
                ext = f.suffix.lower().lstrip(".") or "jpg"
                if ext not in ("jpg", "jpeg", "png", "webp", "gif"):
                    ext = "jpg"
                out.append(
                    {
                        "attachment": base64.b64encode(data).decode("ascii"),
                        "filename": f"local_{idx + 1}.{ext}",
                    }
                )
            except OSError:
                continue
    return out


def _graphql(
    cfg: ShopifyShopConfig,
    query: str,
    variables: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    url = f"{cfg.base_admin_url}/graphql.json"
    try:
        resp = requests.post(
            url,
            headers=_auth_headers_from_cfg(cfg),
            json={"query": query, "variables": variables or {}},
            timeout=90,
        )
        body = resp.json()
        if resp.status_code >= 400:
            return None, f"HTTP {resp.status_code}: {body}"
        if body.get("errors"):
            return None, str(body["errors"])
        return body.get("data") or {}, None
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def _list_publications(cfg: ShopifyShopConfig) -> Tuple[List[Dict[str, str]], Optional[str]]:
    pubs: List[Dict[str, str]] = []
    after: Optional[str] = None
    while True:
        data, err = _graphql(
            cfg,
            """
            query PubList($first: Int!, $after: String) {
              publications(first: $first, after: $after) {
                nodes { id name }
                pageInfo { hasNextPage endCursor }
              }
            }
            """,
            {"first": 50, "after": after},
        )
        if err:
            return [], err
        pub = (data or {}).get("publications") or {}
        for node in pub.get("nodes") or []:
            pid = (node or {}).get("id")
            name = (node or {}).get("name") or ""
            if pid:
                pubs.append({"id": pid, "name": name})
        pi = pub.get("pageInfo") or {}
        if not pi.get("hasNextPage"):
            break
        after = pi.get("endCursor")
    return pubs, None


def _publish_to_publications(
    cfg: ShopifyShopConfig,
    product_id: int,
    scope: str,
) -> Dict[str, Any]:
    """
    scope:
      - all: 所有可见 publication（含 Google 等，若店铺已启用）
      - online_store: 仅名称匹配 Online Store 的渠道
    """
    pubs, err = _list_publications(cfg)
    if err:
        return {"ok": False, "error": err, "step": "list_publications"}

    if scope == "online_store":
        pubs = [p for p in pubs if "online store" in (p.get("name") or "").lower()]
        if not pubs:
            return {
                "ok": False,
                "error": "未找到名为 Online Store 的 publication，请检查后台销售渠道。",
                "publication_count": 0,
            }

    if not pubs:
        return {"ok": True, "publication_count": 0, "note": "无 publication（可能无额外渠道）。"}

    product_gid = f"gid://shopify/Product/{product_id}"
    mutation = """
    mutation PublishOne($id: ID!, $input: [PublicationInput!]!) {
      publishablePublish(id: $id, input: $input) {
        userErrors { field message }
      }
    }
    """
    errors: List[Dict[str, str]] = []
    ok_count = 0
    for pub in pubs:
        data, gerr = _graphql(
            cfg,
            mutation,
            {"id": product_gid, "input": [{"publicationId": pub["id"]}]},
        )
        if gerr:
            errors.append({"name": pub.get("name", ""), "message": gerr})
            continue
        payload = (data or {}).get("publishablePublish") or {}
        ues = payload.get("userErrors") or []
        if ues:
            for ue in ues:
                errors.append({"name": pub.get("name", ""), "message": str(ue.get("message") or ue)})
        else:
            ok_count += 1

    return {
        "ok": ok_count == len(pubs),
        "step": "publishablePublish",
        "publication_count": len(pubs),
        "succeeded": ok_count,
        "scope": scope,
        "userErrors": errors,
    }


def publish_target_to_shopify(
    parsed: Dict[str, Any],
    asin: str,
    cfg: ShopifyShopConfig,
    *,
    product_status: str = "draft",
    publish_scope: str = "all",
    local_media_prefix: str = "",
) -> Tuple[int, Dict[str, Any]]:
    """
    创建 Shopify 商品并按 scope 发布到 publication。
    返回 (shopify_product_id, publication_report)
    """
    if product_status not in {"draft", "active", "archived"}:
        raise ValueError("product_status 须为 draft | active | archived")
    if publish_scope not in {"all", "online_store"}:
        raise ValueError("publish_scope 须为 all | online_store")

    pv = build_product_view(parsed)
    title = (pv.get("title") or "").strip() or f"ASIN {asin}"
    title = title[:255]
    price = _parse_price_number(parsed, pv)
    body_html = _build_description_html(parsed, pv)
    sku = asin.strip().upper()
    vendor = (pv.get("brand") or "Imported").strip()[:255] or "Imported"
    tags = ["amazon-import", asin]
    images = _build_image_attachments(parsed, asin, local_media_prefix)

    seo_title = title[:70]
    seo_desc = (re.sub(r"<[^>]+>", " ", body_html) or title).strip()[:320]

    payload: Dict[str, Any] = {
        "product": {
            "title": title,
            "body_html": body_html,
            "vendor": vendor,
            "tags": ", ".join(tags),
            "status": product_status,
            "published_scope": "global",
            "metafields_global_title_tag": seo_title,
            "metafields_global_description_tag": seo_desc[:320],
            "variants": [
                {
                    "sku": sku,
                    "price": f"{price:.2f}",
                    "inventory_management": "shopify",
                    "inventory_policy": "deny",
                    "inventory_quantity": int(os.getenv("SHOPIFY_DEFAULT_INVENTORY", "30")),
                }
            ],
            "images": images,
        }
    }

    url = f"{cfg.base_admin_url}/products.json"
    resp = requests.post(
        url,
        headers=_auth_headers_from_cfg(cfg),
        json=payload,
        timeout=60,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Shopify 创建失败 ({resp.status_code}): {resp.text[:2000]}")

    body = resp.json()
    product_id = body.get("product", {}).get("id")
    if not product_id:
        raise RuntimeError(f"Shopify 响应异常: {body}")
    product_id = int(product_id)

    report = _publish_to_publications(cfg, product_id, publish_scope)
    return product_id, report
