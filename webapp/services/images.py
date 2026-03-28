from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
import copy

import httpx

from webapp.db import DATA_DIR

IMAGES_ROOT = DATA_DIR / "images"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


def _norm_key(name: str) -> str:
    return str(name).lower().replace("-", "_")


def _item_to_url(item: Any) -> str | None:
    if isinstance(item, str):
        s = item.strip()
        if s.startswith("http://") or s.startswith("https://"):
            return s
        return None
    if isinstance(item, dict):
        for key in ("url", "href", "link", "src", "image", "image_url", "imageUrl", "large", "hi_res"):
            u = item.get(key)
            if isinstance(u, str) and u.startswith("http"):
                return u.strip()
    return None


def normalize_product_image_url(url: str) -> str:
    """
    Normalize source image URL to favor large variants.
    - Amazon: strip `._..._.ext` suffix to original
    - eBay: replace `/s-l140.*`/`/s-l500.*` with `/s-l1600.*`
    """
    u = (url or "").strip()
    if not u:
        return u
    # Amazon original image
    if "m.media-amazon.com" in u:
        return re.sub(r"\._[^.]+_\.(jpg|jpeg|png|webp)$", r".\1", u, flags=re.IGNORECASE)
    # eBay larger image variant
    if "i.ebayimg.com" in u:
        u = re.sub(r"/s-l\d+\.(jpg|jpeg|png|webp)(?:$|\?)", r"/s-l1600.\1", u, flags=re.IGNORECASE)
    return u


def normalize_image_urls_in_data(data: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize image URLs inside structured payload before DB persistence.
    Returns a deep-copied payload.
    """
    out = copy.deepcopy(data)
    image_keys = {"high_res_images", "images", "image_urls", "image_list"}
    url_keys = {"url", "href", "link", "src", "image", "image_url", "imageUrl", "large", "hi_res"}

    def walk(obj: Any, parent_key_norm: str = "") -> Any:
        if isinstance(obj, dict):
            for k, v in list(obj.items()):
                nk = _norm_key(k)
                if isinstance(v, str):
                    if nk in url_keys or parent_key_norm in image_keys:
                        obj[k] = normalize_product_image_url(v)
                    continue
                obj[k] = walk(v, nk)
            return obj
        if isinstance(obj, list):
            for i, v in enumerate(obj):
                if isinstance(v, str):
                    if parent_key_norm in image_keys:
                        obj[i] = normalize_product_image_url(v)
                else:
                    obj[i] = walk(v, parent_key_norm)
            return obj
        return obj

    return walk(out)


def extract_high_res_images_only(data: dict[str, Any]) -> list[str]:
    """仅收集 high_res_images 字段中的 URL（任意嵌套），不与 images 等合并。Amazon / Shopify 亚马逊源用。"""
    found: list[str] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if _norm_key(k) == "high_res_images" and isinstance(v, list):
                    for item in v:
                        u = _item_to_url(item)
                        if u:
                            found.append(u)
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(obj, list):
            for el in obj:
                walk(el)

    walk(data)
    seen: set[str] = set()
    out: list[str] = []
    for u in found:
        nu = normalize_product_image_url(u)
        if nu not in seen:
            seen.add(nu)
            out.append(nu)
    return out


# 不进入这些键的子树，避免 eBay 的 full_description 等 HTML 结构里误收集图片
_EBAY_IMAGE_WALK_SKIP_KEYS = frozenset(
    {
        "full_description",
        "description",
        "body",
        "body_html",
        "html_description",
        "item_description",
        "short_description",
    }
)


def extract_ebay_listing_images_only(data: dict[str, Any]) -> list[str]:
    """
    eBay：只使用结构化字段 `images`（采集时 normalize_image_urls_in_data 已改为 s-l1600）。
    不使用 high_res_images；不遍历 full_description 等，避免描述里嵌入图混入。
    """
    found: list[str] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                nk = _norm_key(k)
                if nk in _EBAY_IMAGE_WALK_SKIP_KEYS:
                    continue
                if nk == "images" and isinstance(v, list):
                    for item in v:
                        u = _item_to_url(item)
                        if u:
                            found.append(u)
                elif isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(obj, list):
            for el in obj:
                walk(el)

    walk(data)
    seen: set[str] = set()
    out: list[str] = []
    for u in found:
        nu = normalize_product_image_url(u)
        if nu not in seen:
            seen.add(nu)
            out.append(nu)
    return out


def extract_shopify_listing_images(data: dict[str, Any], source: str) -> list[str]:
    """
    Shopify 预览/发布白名单图集：Amazon → high_res_images；eBay → images（大图已入库）。
    """
    src = (source or "amazon").strip().lower()
    if src == "ebay":
        return extract_ebay_listing_images_only(data)
    return extract_high_res_images_only(data)


def extract_high_res_image_urls(data: dict[str, Any]) -> list[str]:
    """收集 high_res_images、images、image_urls 等字段内的图片 URL（任意嵌套）；非 Shopify 场景或兼容用。"""
    found: list[str] = []
    image_keys = {"high_res_images", "images", "image_urls", "image_list"}

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if _norm_key(k) in image_keys and isinstance(v, list):
                    for item in v:
                        u = _item_to_url(item)
                        if u:
                            found.append(u)
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(obj, list):
            for el in obj:
                walk(el)

    walk(data)
    seen: set[str] = set()
    out: list[str] = []
    for u in found:
        nu = normalize_product_image_url(u)
        if nu not in seen:
            seen.add(nu)
            out.append(nu)
    return out


def _suffix_from_url(url: str) -> str:
    path = unquote(urlparse(url).path)
    if "." in path:
        ext = path.rsplit(".", 1)[-1].lower()
        ext = re.sub(r"[^a-z0-9]", "", ext)[:5]
        if ext in ("jpg", "jpeg", "png", "gif", "webp"):
            return "." + ("jpg" if ext == "jpeg" else ext)
    return ".jpg"


def download_high_res_images(item_key: str, data: dict[str, Any], *, listing_source: str = "amazon") -> list[str]:
    """
    下载该标识（ASIN/item_id）下 JSON 中第一张图片。
    返回已保存的相对路径列表（相对于 images 根目录）：{item_key}/001.jpg
    """
    urls = extract_shopify_listing_images(data, listing_source)[:1]
    if not urls:
        urls = extract_high_res_image_urls(data)[:1]
    if not urls:
        return []

    item_key = item_key.strip().upper()
    dest_dir = IMAGES_ROOT / item_key
    dest_dir.mkdir(parents=True, exist_ok=True)

    saved: list[str] = []
    timeout = httpx.Timeout(60.0, connect=15.0)
    headers = {"User-Agent": USER_AGENT, "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}

    with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
        for i, url in enumerate(urls):
            url = normalize_product_image_url(url)
            name = f"{i + 1:03d}{_suffix_from_url(url)}"
            path = dest_dir / name
            try:
                r = client.get(url)
                r.raise_for_status()
                path.write_bytes(r.content)
                saved.append(f"{item_key}/{name}")
            except Exception:
                continue

    return saved


def list_media_urls(item_key: str) -> list[str]:
    """已下载图片的浏览器路径（对应 StaticFiles 挂载前缀 /media/product）。"""
    item_key = item_key.strip().upper()
    d = IMAGES_ROOT / item_key
    if not d.is_dir():
        return []
    out: list[str] = []
    for f in sorted(d.iterdir()):
        if f.is_file():
            out.append(f"/media/product/{item_key}/{f.name}")
    return out
