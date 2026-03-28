"""从采集结果发布到 Shopify Admin API（REST + GraphQL publications）。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag
from webapp.ai_copy import default_llm_selection_string, optimize_shopify_copy
from webapp.services.images import extract_high_res_image_urls, normalize_product_image_url
from webapp.services.payload_view import build_product_view, effective_product_root

DEFAULT_MF_WAREHOUSE = "Ontario CA / Springdale OH / Newark NJ"
DEFAULT_MF_DELIVERY_TIME = "2-5 working days inland in the United States"
DEFAULT_MF_SPECIFICATIONS = ""
DEFAULT_MF_QA = ""
MF_NS_WAREHOUSE = os.getenv("SHOPIFY_MF_NS_WAREHOUSE", "custom1").strip() or "custom1"
# Delivery Time 默认按需求写入 custom.delivery_time（single_line_text_field）
MF_NS_DELIVERY = os.getenv("SHOPIFY_MF_NS_DELIVERY_TIME", "custom").strip() or "custom"
MF_NS_SPECIFICATIONS = os.getenv("SHOPIFY_MF_NS_SPECIFICATIONS", "custom").strip() or "custom"
MF_NS_QA = os.getenv("SHOPIFY_MF_NS_QA", "custom").strip() or "custom"
MF_NS_VEHICLE_FITMENT = os.getenv("SHOPIFY_MF_NS_VEHICLE_FITMENT", "custom").strip() or "custom"
MF_NS_PACKAGE_LIST = os.getenv("SHOPIFY_MF_NS_PACKAGE_LIST", "custom").strip() or "custom"


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


def _parse_price_number(parsed: Dict[str, Any], product_view: Dict[str, Any]) -> float:
    root = effective_product_root(parsed)

    # eBay structured: price is usually {"value": 149.99, "currency": "USD"}
    pv = root.get("price")
    if isinstance(pv, dict):
        vv = pv.get("value")
        if isinstance(vv, (int, float)):
            return max(0.01, float(vv))
        if isinstance(vv, str):
            m = re.search(r"\d+(?:[.,]\d+)?", vv.replace(",", ""))
            if m:
                try:
                    return max(0.01, float(m.group(0)))
                except ValueError:
                    pass

    # 优先读结构化定价字段（如 pricing: "$64.59"）
    for key in ("pricing", "current_price", "price", "list_price"):
        v = root.get(key)
        if isinstance(v, (int, float)):
            return max(0.01, float(v))
        if isinstance(v, str):
            m = re.search(r"\d+(?:[.,]\d+)?", v.replace(",", ""))
            if m:
                try:
                    return max(0.01, float(m.group(0)))
                except ValueError:
                    pass

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
        v = root.get(key)
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


def _shopify_sell_price(parsed: Dict[str, Any], product_view: Dict[str, Any]) -> float:
    """Shopify 售价：采集价格 * 1.7。"""
    base = _parse_price_number(parsed, product_view)
    return round(max(0.01, base * 1.7), 2)


def _build_description_html(parsed: Dict[str, Any], product_view: Dict[str, Any]) -> str:
    root = effective_product_root(parsed)
    parts: List[str] = []
    desc = ""
    if isinstance(root.get("full_description"), str):
        desc = root["full_description"].strip()
    elif isinstance(root.get("description"), str):
        desc = root["description"].strip()
    bullets: List[str] = []
    raw_bullets = root.get("feature_bullets")
    if isinstance(raw_bullets, list):
        bullets = [str(b).strip() for b in raw_bullets if str(b).strip()]
    if not bullets:
        for key in ("bullet_points", "about_this_item", "features"):
            rv = root.get(key)
            if isinstance(rv, list):
                bullets = [str(b).strip() for b in rv if str(b).strip()]
                if bullets:
                    break
    # eBay: fallback from item_specifics list of {label, value}
    if not bullets and isinstance(root.get("item_specifics"), list):
        tmp: List[str] = []
        for row in root.get("item_specifics")[:60]:
            if not isinstance(row, dict):
                continue
            label = str(row.get("label") or "").strip()
            value = str(row.get("value") or "").strip()
            if label and value:
                tmp.append(f"{label}: {value}")
        bullets = tmp
    if not bullets:
        bullets = product_view.get("bullets") or []
    if desc:
        parts.append(f"<p>{_html_escape(desc[:12000])}</p>")
    if bullets:
        parts.append("<ul>")
        for b in bullets[:120]:
            parts.append(f"<li>{_html_escape(str(b))}</li>")
        parts.append("</ul>")
    if not parts:
        parts.append(f"<p>Imported Item {_html_escape(str(parsed.get('asin', '')))}</p>")
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
    image_urls_override: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """返回 Shopify images[]：仅使用原始 URL 的 src（不走本地/attachment 上传）。"""
    del asin, local_media_prefix
    urls = image_urls_override if image_urls_override is not None else extract_high_res_image_urls(parsed)
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for src in urls[:30]:
        u = normalize_product_image_url((src or "").strip())
        if not u or u in seen:
            continue
        seen.add(u)
        out.append({"src": u})
        if len(out) >= 15:
            break
    return out


def _pick_text_value(root: Dict[str, Any], names: List[str], default: str = "") -> str:
    norm = {n.strip().lower().replace(" ", "_").replace("-", "_") for n in names}
    for k, v in root.items():
        nk = str(k).strip().lower().replace(" ", "_").replace("-", "_")
        if nk not in norm:
            continue
        if isinstance(v, str) and v.strip():
            return v.strip()
    return default


def _rich_text_field_json(text_or_html: str) -> str:
    """
    将用户输入（纯文本或 HTML）转换为 Shopify rich_text_field JSON。
    支持段落与列表，便于从富文本编辑器粘贴后保留结构。
    """
    raw = (text_or_html or "").strip()
    if not raw:
        return json.dumps({"type": "root", "children": []}, ensure_ascii=False)

    soup = BeautifulSoup(raw, "html.parser")
    root = soup.body or soup
    children: List[Dict[str, Any]] = []

    def inline_children(node: Tag | NavigableString, marks: Optional[Dict[str, bool]] = None) -> List[Dict[str, Any]]:
        marks = marks or {}
        out: List[Dict[str, Any]] = []
        if isinstance(node, NavigableString):
            txt = str(node)
            txt = re.sub(r"\s+", " ", txt)
            if txt and txt.strip():
                item: Dict[str, Any] = {"type": "text", "value": txt.strip()}
                if marks.get("bold"):
                    item["bold"] = True
                if marks.get("italic"):
                    item["italic"] = True
                out.append(item)
            return out
        name = (node.name or "").lower()
        new_marks = dict(marks)
        if name in {"strong", "b"}:
            new_marks["bold"] = True
        if name in {"em", "i"}:
            new_marks["italic"] = True
        for ch in node.children:
            out.extend(inline_children(ch, new_marks))
        return out

    def add_paragraph_from_tag(tag: Tag) -> None:
        inlines = inline_children(tag)
        if inlines:
            children.append({"type": "paragraph", "children": inlines})

    def add_heading_from_tag(tag: Tag, level: int) -> None:
        inlines = inline_children(tag)
        if not inlines:
            return
        lv = level if 1 <= level <= 6 else 2
        children.append({"type": "heading", "level": lv, "children": inlines})

    for node in root.children:
        if isinstance(node, NavigableString):
            txt = str(node).strip()
            if txt:
                children.append({"type": "paragraph", "children": [{"type": "text", "value": txt}]})
            continue
        name = (node.name or "").lower()
        if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            try:
                lv = int(name[1:])
            except Exception:
                lv = 2
            add_heading_from_tag(node, lv)
            continue
        if name in {"p", "div", "section", "article"}:
            add_paragraph_from_tag(node)
            continue
        if name in {"ul", "ol"}:
            items: List[Dict[str, Any]] = []
            for li in node.find_all("li", recursive=False):
                li_children = inline_children(li)
                if li_children:
                    items.append({"type": "list-item", "children": li_children})
            if items:
                children.append({"type": "list", "listType": "unordered", "children": items})
            continue
        add_paragraph_from_tag(node)

    if not children:
        txt = root.get_text("\n", strip=True)
        for line in txt.splitlines():
            line = line.strip()
            if not line:
                continue
            children.append({"type": "paragraph", "children": [{"type": "text", "value": line}]})

    return json.dumps({"type": "root", "children": children}, ensure_ascii=False)


def _build_custom_metafields(
    *,
    warehouse: str,
    specifications: str,
    delivery_time: str,
    qa: str,
    vehicle_fitment: str,
    package_list: str,
) -> List[Dict[str, str]]:
    rows: List[Tuple[str, str, str, str]] = [
        (MF_NS_WAREHOUSE, "warehouse", warehouse, "single_line_text_field"),
        (MF_NS_SPECIFICATIONS, "specifications", specifications, "rich_text_field"),
        (MF_NS_DELIVERY, "delivery_time", delivery_time, "single_line_text_field"),
        (MF_NS_QA, "qa", qa, "rich_text_field"),
        (MF_NS_VEHICLE_FITMENT, "vehicle_fitment", vehicle_fitment, "rich_text_field"),
        (MF_NS_PACKAGE_LIST, "package_list", package_list, "rich_text_field"),
    ]
    out: List[Dict[str, str]] = []
    for namespace, key, raw_val, typ in rows:
        raw = (raw_val or "").strip()
        # rich_text 字段为空时不发送，避免触发 Shopify 校验并拖累其它字段写入
        if typ == "rich_text_field" and not raw:
            continue
        v = _rich_text_field_json(raw) if typ == "rich_text_field" else raw
        if not v:
            continue
        out.append(
            {
                "namespace": namespace,
                "key": key,
                "type": typ,
                "value": v[:60000],
            }
        )
    return out


def _first_scalar_str(obj: Any, key_names: set[str]) -> Optional[str]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in key_names and v is not None:
                s = str(v).strip()
                if s:
                    return s
            got = _first_scalar_str(v, key_names)
            if got:
                return got
    elif isinstance(obj, list):
        for x in obj[:40]:
            got = _first_scalar_str(x, key_names)
            if got:
                return got
    return None


def _extract_ebay_item_id(parsed: Dict[str, Any]) -> Optional[str]:
    direct = _first_scalar_str(
        parsed,
        {"item_id", "itemid", "ebay_item_id", "legacyitemid", "listing_id", "listingid"},
    )
    if direct:
        m = re.search(r"\d{9,15}", direct)
        if m:
            return m.group(0)
    blob = json.dumps(parsed, ensure_ascii=False)
    m = re.search(r"ebay\.[^/]+/itm/(?:[^/]+/)?(\d{9,15})", blob, flags=re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def _derive_sku(asin: str, parsed: Dict[str, Any]) -> str:
    ebay_id = _extract_ebay_item_id(parsed)
    if ebay_id:
        return f"EB-{ebay_id}"
    # eBay 目标通常用 item_id 作为主键（9-15 位数字），即使 JSON 未命中字段也按 EB- 规则生成。
    if re.fullmatch(r"\d{9,15}", (asin or "").strip()):
        return f"EB-{(asin or '').strip()}"
    norm = re.sub(r"[^A-Za-z0-9_-]", "", (asin or "").strip().upper()) or "UNKNOWN"
    return f"AM-{norm}"


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


def _set_product_metafields(
    cfg: ShopifyShopConfig,
    product_id: int,
    metafields: List[Dict[str, str]],
) -> Dict[str, Any]:
    if not metafields:
        return {"ok": True, "count": 0, "note": "no_metafields"}
    product_gid = f"gid://shopify/Product/{product_id}"
    payload = []
    for mf in metafields:
        payload.append(
            {
                "ownerId": product_gid,
                "namespace": mf["namespace"],
                "key": mf["key"],
                "type": mf["type"],
                "value": mf["value"],
            }
        )
    data, err = _graphql(
        cfg,
        """
        mutation SetMetafields($metafields: [MetafieldsSetInput!]!) {
          metafieldsSet(metafields: $metafields) {
            metafields { namespace key type value }
            userErrors { field message code }
          }
        }
        """,
        {"metafields": payload},
    )
    if err:
        return {"ok": False, "error": err, "step": "metafieldsSet"}
    body = (data or {}).get("metafieldsSet") or {}
    ues = body.get("userErrors") or []
    if ues:
        # 自适应修复：如果返回 INVALID_TYPE，按定义类型重写对应条目并重试一次
        patched = False
        for ue in ues:
            if str(ue.get("code") or "") != "INVALID_TYPE":
                continue
            field = ue.get("field") or []
            # 形如 ['metafields','0','type']
            if len(field) < 2 or str(field[0]) != "metafields":
                continue
            try:
                idx = int(field[1])
            except Exception:
                continue
            if idx < 0 or idx >= len(payload):
                continue
            msg = str(ue.get("message") or "")
            m = re.search(r"definition's type: '([^']+)'", msg)
            if not m:
                continue
            expected_type = m.group(1).strip()
            if not expected_type:
                continue
            current = payload[idx]
            if current.get("type") == expected_type:
                continue
            raw_value = str(current.get("value") or "")
            if expected_type == "rich_text_field":
                current["value"] = _rich_text_field_json(raw_value)
            elif expected_type == "single_line_text_field":
                # rich_text JSON -> plain text；否则原样压缩空白
                plain = raw_value
                try:
                    obj = json.loads(raw_value)
                    if isinstance(obj, dict):
                        lines: List[str] = []
                        for ch in obj.get("children") or []:
                            for t in (ch.get("children") or []):
                                v = str(t.get("value") or "").strip()
                                if v:
                                    lines.append(v)
                        if lines:
                            plain = " ".join(lines)
                except Exception:
                    pass
                current["value"] = re.sub(r"\s+", " ", plain).strip()
            else:
                # 其他类型先原值透传
                current["value"] = raw_value
            current["type"] = expected_type
            patched = True

        if patched:
            data2, err2 = _graphql(
                cfg,
                """
                mutation SetMetafields($metafields: [MetafieldsSetInput!]!) {
                  metafieldsSet(metafields: $metafields) {
                    metafields { namespace key type value }
                    userErrors { field message code }
                  }
                }
                """,
                {"metafields": payload},
            )
            if err2:
                return {"ok": False, "error": err2, "step": "metafieldsSet_retry"}
            body2 = (data2 or {}).get("metafieldsSet") or {}
            ues2 = body2.get("userErrors") or []
            if not ues2:
                return {"ok": True, "count": len(payload), "step": "metafieldsSet_retry"}
            return {"ok": False, "step": "metafieldsSet_retry", "userErrors": ues2}
        return {"ok": False, "step": "metafieldsSet", "userErrors": ues}
    return {"ok": True, "count": len(payload), "step": "metafieldsSet"}


def _upsert_single_line_metafield_rest(
    cfg: ShopifyShopConfig,
    product_id: int,
    *,
    key: str,
    value: str,
    namespace: str,
) -> Dict[str, Any]:
    """
    REST 兜底：确保单行文本元字段可见（存在则更新，不存在则创建）。
    """
    try:
        list_url = f"{cfg.base_admin_url}/products/{product_id}/metafields.json"
        resp = requests.get(list_url, headers=_auth_headers_from_cfg(cfg), timeout=45)
        if resp.status_code >= 400:
            return {"ok": False, "step": "rest_list", "error": f"HTTP {resp.status_code}: {resp.text[:400]}"}
        items = (resp.json() or {}).get("metafields") or []
        matched = None
        for m in items:
            if (m.get("namespace") or "") == namespace and (m.get("key") or "") == key:
                matched = m
                break
        if matched and matched.get("id"):
            mid = int(matched["id"])
            put_url = f"{cfg.base_admin_url}/metafields/{mid}.json"
            body = {"metafield": {"id": mid, "value": value, "type": "single_line_text_field"}}
            put_resp = requests.put(put_url, headers=_auth_headers_from_cfg(cfg), json=body, timeout=45)
            if put_resp.status_code >= 400:
                return {"ok": False, "step": "rest_put", "error": f"HTTP {put_resp.status_code}: {put_resp.text[:400]}"}
            return {"ok": True, "step": "rest_put", "id": mid}
        post_url = f"{cfg.base_admin_url}/products/{product_id}/metafields.json"
        body = {
            "metafield": {
                "namespace": namespace,
                "key": key,
                "type": "single_line_text_field",
                "value": value,
            }
        }
        post_resp = requests.post(post_url, headers=_auth_headers_from_cfg(cfg), json=body, timeout=45)
        if post_resp.status_code >= 400:
            return {"ok": False, "step": "rest_post", "error": f"HTTP {post_resp.status_code}: {post_resp.text[:400]}"}
        return {"ok": True, "step": "rest_post"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "step": "rest_exception", "error": str(exc)}


def build_shopify_create_preview(
    parsed: Dict[str, Any],
    asin: str,
    *,
    product_status: str = "draft",
    publish_scope: str = "all",
) -> Dict[str, Any]:
    """
    详情页展示：与 POST .../products.json 将发送的字段一致（不下载图片）。
    用于核对映射与后续删减字段。
    """
    from webapp.services.payload_view import _scalar, effective_product_root

    root = effective_product_root(parsed)
    pv = build_product_view(parsed)
    title = (pv.get("title") or "").strip() or f"Item {asin}"
    title = title[:255]
    price = _shopify_sell_price(parsed, pv)
    body_html = _build_description_html(parsed, pv)
    sku = _derive_sku(asin, parsed)
    vendor = "EGR Performance"
    tags = ""
    seo_title = title[:70]
    seo_desc = (re.sub(r"<[^>]+>", " ", body_html) or title).strip()[:320]
    inv = int(os.getenv("SHOPIFY_DEFAULT_INVENTORY", "30"))

    raw_name = (_scalar(root.get("name")) or "").strip()
    if raw_name and len(raw_name) >= 3:
        title_note = "JSON.name" + ("（REST title 截断至 255）" if len(raw_name) > 255 else "")
    else:
        title_note = "启发式 / title 等回退（非顶层 name）"

    urls = extract_high_res_image_urls(parsed)

    rows: List[Dict[str, str]] = [
        {"shopify": "product.title", "value": title, "note": title_note},
        {
            "shopify": "product.body_html",
            "value": body_html[:1200] + ("…" if len(body_html) > 1200 else ""),
            "note": f"HTML，全文 {len(body_html)} 字符",
        },
        {"shopify": "product.vendor", "value": vendor, "note": "固定值"},
        {"shopify": "product.tags", "value": tags, "note": "置空"},
        {"shopify": "product.status", "value": product_status, "note": "表单 draft / active"},
        {"shopify": "product.published_scope", "value": "global", "note": "REST 固定"},
        {"shopify": "product.metafields_global_title_tag", "value": seo_title, "note": "SEO，≤70"},
        {"shopify": "product.metafields_global_description_tag", "value": seo_desc[:320], "note": "SEO，去标签 ≤320"},
    ]
    variant_rows: List[Dict[str, str]] = [
        {"shopify": "variants[0].sku", "value": sku, "note": "Amazon: AM-ASIN；eBay: EB-商品编号"},
        {"shopify": "variants[0].price", "value": f"{price:.2f}", "note": "采集价 * 1.7"},
        {"shopify": "variants[0].inventory_management", "value": "shopify", "note": ""},
        {"shopify": "variants[0].inventory_policy", "value": "continue", "note": "缺货时继续销售"},
        {"shopify": "variants[0].inventory_quantity", "value": str(inv), "note": "环境变量或默认 30"},
    ]
    return {
        "rows": rows,
        "variant_rows": variant_rows,
        "images": {
            "high_res_urls_count": len(urls),
            "high_res_urls_sample": urls[:5],
            "local_dir_exists": False,
            "note": "仅使用原图 URL 的 src 上传，不做本地 attachment",
        },
        "after_rest_create": {
            "graphql": "publishablePublish",
            "publish_scope": publish_scope,
            "note": "创建成功后按范围发布到 publication",
        },
    }


def build_shopify_editor_defaults(parsed: Dict[str, Any], asin: str) -> Dict[str, Any]:
    """详情页二次编辑界面默认值（与发布口径一致，不调用 AI）。"""
    pv = build_product_view(parsed)
    title = (pv.get("title") or "").strip() or f"Item {asin}"
    title = title[:255]
    body_html = _build_description_html(parsed, pv)
    seo_title = title[:70]
    seo_desc = (re.sub(r"<[^>]+>", " ", body_html) or title).strip()[:320]
    price = _shopify_sell_price(parsed, pv)
    sku = _derive_sku(asin, parsed)
    vendor = "EGR Performance"
    tags = ""
    inv = int(os.getenv("SHOPIFY_DEFAULT_INVENTORY", "30"))
    image_urls = [normalize_product_image_url(u) for u in extract_high_res_image_urls(parsed)[:15]]
    return {
        "source_title": title,
        "source_body_html": body_html,
        "source_seo_title": seo_title,
        "source_seo_description": seo_desc,
        "title": title,
        "body_html": body_html,
        "seo_title": seo_title,
        "seo_description": seo_desc,
        "price_original": f"{_parse_price_number(parsed, pv):.2f}",
        "price": f"{price:.2f}",
        "vendor": vendor,
        "tags": tags,
        "sku": sku,
        "inventory_quantity": str(inv),
        "image_urls": image_urls,
        "metafield_warehouse": DEFAULT_MF_WAREHOUSE,
        "metafield_specifications": "",
        "metafield_delivery_time": DEFAULT_MF_DELIVERY_TIME,
        "metafield_qa": "",
        "metafield_vehicle_fitment": "",
        "metafield_package_list": "",
    }


def publish_target_to_shopify(
    parsed: Dict[str, Any],
    asin: str,
    cfg: ShopifyShopConfig,
    *,
    product_status: str = "draft",
    publish_scope: str = "all",
    use_ai: bool = False,
    title_override: Optional[str] = None,
    body_html_override: Optional[str] = None,
    seo_title_override: Optional[str] = None,
    seo_desc_override: Optional[str] = None,
    price_override: Optional[float] = None,
    vendor_override: Optional[str] = None,
    tags_override: Optional[str] = None,
    sku_override: Optional[str] = None,
    inventory_qty_override: Optional[int] = None,
    upc_override: Optional[str] = None,
    existing_product_id: Optional[int] = None,
    metafield_warehouse_override: Optional[str] = None,
    metafield_specifications_override: Optional[str] = None,
    metafield_delivery_time_override: Optional[str] = None,
    metafield_qa_override: Optional[str] = None,
    metafield_vehicle_fitment_override: Optional[str] = None,
    metafield_package_list_override: Optional[str] = None,
    image_urls_override: Optional[List[str]] = None,
    prompt_library_id: Optional[str] = None,
    local_media_prefix: str = "",
) -> Tuple[int, Dict[str, Any]]:
    """
    创建或更新 Shopify 商品并按 scope 发布到 publication。
    返回 (shopify_product_id, publication_report)
    """
    if product_status not in {"draft", "active", "archived"}:
        raise ValueError("product_status 须为 draft | active | archived")
    if publish_scope not in {"all", "online_store"}:
        raise ValueError("publish_scope 须为 all | online_store")

    pv = build_product_view(parsed)
    title = (title_override or (pv.get("title") or "")).strip() or f"Item {asin}"
    title = title[:255]
    price = float(price_override) if price_override is not None else _shopify_sell_price(parsed, pv)
    price = round(max(0.01, price), 2)
    body_html = (body_html_override or _build_description_html(parsed, pv)).strip()
    sku = (sku_override or _derive_sku(asin, parsed)).strip()
    vendor = (vendor_override or "EGR Performance").strip()[:255] or "EGR Performance"
    tags = (tags_override or "").strip()
    images = _build_image_attachments(parsed, asin, local_media_prefix, image_urls_override=image_urls_override)
    upc = (upc_override or "").strip()
    mf_warehouse = (metafield_warehouse_override or "").strip()
    if not mf_warehouse:
        mf_warehouse = DEFAULT_MF_WAREHOUSE
    mf_specs = (metafield_specifications_override or "").strip()
    mf_delivery = (metafield_delivery_time_override or "").strip()
    if not mf_delivery:
        mf_delivery = DEFAULT_MF_DELIVERY_TIME
    mf_qa = (metafield_qa_override or "").strip()
    mf_vehicle_fitment = (metafield_vehicle_fitment_override or "").strip()
    mf_package_list = (metafield_package_list_override or "").strip()

    seo_title = (seo_title_override or title[:70]).strip()[:70]
    seo_desc = (seo_desc_override or (re.sub(r"<[^>]+>", " ", body_html) or title)).strip()[:320]
    if use_ai:
        optimized = optimize_shopify_copy(
            parsed,
            pv,
            asin,
            {
                "title": title,
                "body_html": body_html,
                "seo_title": seo_title,
                "seo_description": seo_desc,
            },
            library_id=prompt_library_id,
            llm_selection=default_llm_selection_string(),
        )
        title = (optimized.get("title") or title).strip()[:255]
        body_html = (optimized.get("body_html") or body_html).strip()
        seo_title = (optimized.get("seo_title") or seo_title).strip()[:70]
        seo_desc = (optimized.get("seo_description") or seo_desc).strip()[:320]

    variant_payload: Dict[str, Any] = {
        "sku": sku,
        "price": f"{price:.2f}",
        "inventory_management": "shopify",
        # Global rule: allow selling when out of stock.
        "inventory_policy": "continue",
        "inventory_quantity": int(
            inventory_qty_override
            if inventory_qty_override is not None
            else int(os.getenv("SHOPIFY_DEFAULT_INVENTORY", "30"))
        ),
    }
    if upc:
        variant_payload["barcode"] = upc

    custom_metafields = _build_custom_metafields(
        warehouse=mf_warehouse,
        specifications=mf_specs,
        delivery_time=mf_delivery,
        qa=mf_qa,
        vehicle_fitment=mf_vehicle_fitment,
        package_list=mf_package_list,
    )

    payload: Dict[str, Any] = {
        "product": {
            "title": title,
            "body_html": body_html,
            "vendor": vendor,
            "tags": tags,
            "status": product_status,
            "published_scope": "global",
            "metafields_global_title_tag": seo_title,
            "metafields_global_description_tag": seo_desc[:320],
            "variants": [variant_payload],
            "images": images,
        }
    }

    if existing_product_id:
        payload["product"]["id"] = int(existing_product_id)
        url = f"{cfg.base_admin_url}/products/{int(existing_product_id)}.json"
        resp = requests.put(
            url,
            headers=_auth_headers_from_cfg(cfg),
            json=payload,
            timeout=60,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Shopify 更新失败 ({resp.status_code}): {resp.text[:2000]}")
    else:
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

    mf_report = _set_product_metafields(cfg, product_id, custom_metafields)
    if not mf_report.get("ok"):
        raise RuntimeError(f"元字段写入失败: {mf_report}")
    # 单行字段 REST 兜底，确保 Warehouse/Delivery Time 在后台可见
    rest_wh = _upsert_single_line_metafield_rest(
        cfg, product_id, key="warehouse", value=mf_warehouse, namespace=MF_NS_WAREHOUSE
    )
    rest_dt = _upsert_single_line_metafield_rest(
        cfg, product_id, key="delivery_time", value=mf_delivery, namespace=MF_NS_DELIVERY
    )

    report = _publish_to_publications(cfg, product_id, publish_scope)
    report["mode"] = "update" if existing_product_id else "create"
    report["metafields"] = mf_report
    report["metafields_rest_fallback"] = {"warehouse": rest_wh, "delivery_time": rest_dt}
    return product_id, report


def _render_inline_html(children: List[Dict[str, Any]]) -> str:
    out: List[str] = []
    for ch in children or []:
        if (ch or {}).get("type") != "text":
            continue
        txt = _html_escape(str((ch or {}).get("value") or ""))
        if not txt:
            continue
        if ch.get("bold"):
            txt = f"<strong>{txt}</strong>"
        if ch.get("italic"):
            txt = f"<em>{txt}</em>"
        out.append(txt)
    return "".join(out).strip()


def _rich_text_json_to_html(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    try:
        obj = json.loads(raw)
    except Exception:
        return _html_escape(raw)
    if not isinstance(obj, dict):
        return _html_escape(raw)
    html_parts: List[str] = []
    for node in obj.get("children") or []:
        ntype = (node or {}).get("type")
        if ntype == "paragraph":
            inner = _render_inline_html((node or {}).get("children") or [])
            if inner:
                html_parts.append(f"<p>{inner}</p>")
        elif ntype == "heading":
            inner = _render_inline_html((node or {}).get("children") or [])
            level = int((node or {}).get("level") or 2)
            if level < 1 or level > 6:
                level = 2
            if inner:
                html_parts.append(f"<h{level}>{inner}</h{level}>")
        elif ntype == "list":
            lis: List[str] = []
            for item in (node or {}).get("children") or []:
                inner = _render_inline_html((item or {}).get("children") or [])
                if inner:
                    lis.append(f"<li>{inner}</li>")
            if lis:
                html_parts.append("<ul>" + "".join(lis) + "</ul>")
    return "\n".join(html_parts).strip()


def fetch_shopify_product_editor_values(
    cfg: ShopifyShopConfig,
    product_id: int,
) -> Dict[str, str]:
    """从 Shopify 拉取商品并转换为详情页编辑值。"""
    purl = f"{cfg.base_admin_url}/products/{int(product_id)}.json"
    presp = requests.get(purl, headers=_auth_headers_from_cfg(cfg), timeout=45)
    if presp.status_code >= 400:
        raise RuntimeError(f"拉取 Shopify 商品失败 ({presp.status_code}): {presp.text[:800]}")
    product = (presp.json() or {}).get("product") or {}
    if not product:
        raise RuntimeError("Shopify 商品不存在或响应为空")

    variants = product.get("variants") or []
    first_v = variants[0] if variants else {}
    tags = product.get("tags")
    if isinstance(tags, list):
        tags_s = ",".join([str(x).strip() for x in tags if str(x).strip()])
    else:
        tags_s = str(tags or "")

    out: Dict[str, str] = {
        "title": str(product.get("title") or ""),
        "body_html": str(product.get("body_html") or ""),
        "seo_title": str(product.get("metafields_global_title_tag") or ""),
        "seo_description": str(product.get("metafields_global_description_tag") or ""),
        "vendor": str(product.get("vendor") or ""),
        "tags": tags_s,
        "sku": str(first_v.get("sku") or ""),
        "price": str(first_v.get("price") or ""),
        "inventory_quantity": str(first_v.get("inventory_quantity") or ""),
    }

    murl = f"{cfg.base_admin_url}/products/{int(product_id)}/metafields.json"
    mresp = requests.get(murl, headers=_auth_headers_from_cfg(cfg), timeout=45)
    if mresp.status_code >= 400:
        raise RuntimeError(f"拉取 Shopify 元字段失败 ({mresp.status_code}): {mresp.text[:800]}")
    mfs = (mresp.json() or {}).get("metafields") or []
    index: Dict[tuple[str, str], Dict[str, Any]] = {}
    for mf in mfs:
        ns = str((mf or {}).get("namespace") or "")
        key = str((mf or {}).get("key") or "")
        index[(ns, key)] = mf or {}

    def _pick(ns: str, key: str) -> str:
        it = index.get((ns, key))
        if not it:
            return ""
        typ = str(it.get("type") or "")
        val = str(it.get("value") or "")
        if typ == "rich_text_field":
            return _rich_text_json_to_html(val)
        return val

    out["metafield_warehouse"] = _pick(MF_NS_WAREHOUSE, "warehouse")
    out["metafield_delivery_time"] = _pick(MF_NS_DELIVERY, "delivery_time")
    out["metafield_specifications"] = _pick(MF_NS_SPECIFICATIONS, "specifications")
    out["metafield_qa"] = _pick(MF_NS_QA, "qa")
    out["metafield_vehicle_fitment"] = _pick(MF_NS_VEHICLE_FITMENT, "vehicle_fitment")
    out["metafield_package_list"] = _pick(MF_NS_PACKAGE_LIST, "package_list")

    # SEO 字段兜底：部分店铺/版本下 product 顶层 SEO 字段可能为空，但 metafields 中存在。
    if not str(out.get("seo_title") or "").strip():
        out["seo_title"] = _pick("global", "title_tag")
    if not str(out.get("seo_description") or "").strip():
        out["seo_description"] = _pick("global", "description_tag")
    return out
