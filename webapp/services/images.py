from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

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
        for key in ("url", "href", "link", "src", "image", "image_url", "large", "hi_res"):
            u = item.get(key)
            if isinstance(u, str) and u.startswith("http"):
                return u.strip()
    return None


def extract_high_res_image_urls(data: dict[str, Any]) -> list[str]:
    """只收集字段名等价于 high_res_images 的列表中的图片 URL（任意嵌套）。"""
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
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _suffix_from_url(url: str) -> str:
    path = unquote(urlparse(url).path)
    if "." in path:
        ext = path.rsplit(".", 1)[-1].lower()
        ext = re.sub(r"[^a-z0-9]", "", ext)[:5]
        if ext in ("jpg", "jpeg", "png", "gif", "webp"):
            return "." + ("jpg" if ext == "jpeg" else ext)
    return ".jpg"


def download_high_res_images(asin: str, data: dict[str, Any]) -> list[str]:
    """
    下载该 ASIN 下 JSON 中全部 high_res_images。
    返回已保存的相对路径列表（相对于 images 根目录）：{asin}/001.jpg
    """
    urls = extract_high_res_image_urls(data)
    if not urls:
        return []

    asin = asin.strip().upper()
    dest_dir = IMAGES_ROOT / asin
    dest_dir.mkdir(parents=True, exist_ok=True)

    saved: list[str] = []
    timeout = httpx.Timeout(60.0, connect=15.0)
    headers = {"User-Agent": USER_AGENT, "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}

    with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
        for i, url in enumerate(urls):
            name = f"{i + 1:03d}{_suffix_from_url(url)}"
            path = dest_dir / name
            try:
                r = client.get(url)
                r.raise_for_status()
                path.write_bytes(r.content)
                saved.append(f"{asin}/{name}")
            except Exception:
                continue

    return saved


def list_media_urls(asin: str) -> list[str]:
    """已下载图片的浏览器路径（对应 StaticFiles 挂载前缀 /media/product）。"""
    asin = asin.strip().upper()
    d = IMAGES_ROOT / asin
    if not d.is_dir():
        return []
    out: list[str] = []
    for f in sorted(d.iterdir()):
        if f.is_file():
            out.append(f"/media/product/{asin}/{f.name}")
    return out
