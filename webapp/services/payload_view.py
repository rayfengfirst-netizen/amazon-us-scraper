from __future__ import annotations

import re
from typing import Any

# ScraperAPI / Amazon 结构化 JSON 字段名差异较大，用「深度扫描 + 路径启发式」提取展示字段

_TITLE_KEY_PRIORITY = (
    "item_name",
    "product_name",
    "product_title",
    "title",
    "name",
    "listing_title",
    "headline",
    "display_name",
    "full_name",
)


def _scalar(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        s = str(v).strip()
        return s if s else None
    if isinstance(v, str):
        s = v.strip()
        return s if s else None
    return None


def _last_segment(path: str) -> str:
    if not path:
        return ""
    tail = path.split(".")[-1]
    return tail.split("[")[0].lower().replace("-", "_")


def _title_key_score(last_key: str) -> int:
    lk = last_key.lower()
    for i, pk in enumerate(_TITLE_KEY_PRIORITY):
        if lk == pk:
            return 200 - i
    if "product" in lk and "name" in lk:
        return 120
    if lk.endswith("_title") or lk.endswith("_name"):
        return 80
    if "title" in lk:
        return 60
    if "name" in lk and "user" not in lk and "brand" not in lk and "seller" not in lk and "author" not in lk:
        return 40
    return 0


def _iter_scalar_leaves(
    obj: Any,
    prefix: str = "",
    *,
    max_depth: int = 14,
    max_list_items: int = 40,
    _counter: list[int] | None = None,
) -> list[tuple[str, str]]:
    """收集 (json路径, 标量字符串)，用于标题/表格展示。"""
    if _counter is None:
        _counter = [0]
    if _counter[0] >= 8000 or max_depth <= 0:
        return []
    out: list[tuple[str, str]] = []

    if isinstance(obj, dict):
        for k in sorted(obj.keys(), key=str):
            v = obj[k]
            p = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, (dict, list)):
                _counter[0] += 1
                out.extend(
                    _iter_scalar_leaves(
                        v, p, max_depth=max_depth - 1, max_list_items=max_list_items, _counter=_counter
                    )
                )
            else:
                s = _scalar(v)
                if s is not None and len(s) <= 4000:
                    _counter[0] += 1
                    out.append((p, s))
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:max_list_items]):
            p = f"{prefix}[{i}]" if prefix else f"[{i}]"
            if isinstance(v, (dict, list)):
                _counter[0] += 1
                out.extend(
                    _iter_scalar_leaves(
                        v, p, max_depth=max_depth - 1, max_list_items=max_list_items, _counter=_counter
                    )
                )
            else:
                s = _scalar(v)
                if s is not None and len(s) <= 4000:
                    _counter[0] += 1
                    out.append((p, s))
    return out


def _pick_title(leaves: list[tuple[str, str]]) -> str | None:
    best: tuple[int, int, str] | None = None  # (score, len, value)
    for path, val in leaves:
        if len(val) < 8:
            continue
        sk = _title_key_score(_last_segment(path))
        if sk <= 0:
            continue
        cand = (sk, len(val), val)
        if best is None or cand > best:
            best = cand
    return best[2] if best else None


def _pick_brand(leaves: list[tuple[str, str]]) -> str | None:
    for path, val in leaves:
        lk = _last_segment(path)
        if len(val) >= 200:
            continue
        if lk == "brand" or lk.endswith("_brand") or "brand_name" in lk:
            return val
        if lk == "label" and "brand" in path.lower():
            return val
    return None


def _pick_price(leaves: list[tuple[str, str]]) -> str | None:
    price_re = re.compile(r"[\d.,]+")
    best: tuple[int, str] | None = None
    for path, val in leaves:
        lk = _last_segment(path)
        pl = path.lower()
        if not (
            any(x in lk for x in ("price", "amount", "cost", "buybox", "list", "sale"))
            or "price" in pl
            or "buybox" in pl
        ):
            continue
        if "$" in val or "¥" in val or "£" in val or "€" in val or price_re.search(val):
            score = len(lk)
            if "current" in lk or "buy" in lk or "sale" in lk or "display" in pl:
                score += 20
            if "formatted" in lk or "string" in lk:
                score += 5
            cand = (score, val)
            if best is None or cand > best:
                best = cand
    return best[1] if best else None


def _pick_rating_reviews(leaves: list[tuple[str, str]]) -> tuple[str | None, str | None]:
    rating = None
    reviews = None
    for path, val in leaves:
        lk = _last_segment(path)
        if rating is None and lk in ("rating", "stars", "star_rating", "average_rating"):
            if len(val) < 32:
                rating = val
        if reviews is None and any(
            x in lk for x in ("review_count", "reviews_count", "ratings_total", "num_ratings", "total_reviews")
        ):
            if len(val) < 32:
                reviews = val
    return rating, reviews


def _collect_bullets(data: dict[str, Any]) -> list[str]:
    found: list[str] = []

    def walk(obj: Any, depth: int) -> None:
        if depth <= 0:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                lk = str(k).lower().replace("-", "_")
                if isinstance(v, list) and v:
                    if any(
                        x in lk
                        for x in (
                            "bullet",
                            "feature",
                            "about_this",
                            "about_item",
                            "description_point",
                            "key_feature",
                            "highlight",
                        )
                    ):
                        for item in v[:60]:
                            s = _scalar(item)
                            if s and 15 < len(s) < 2000:
                                found.append(s)
                elif isinstance(v, (dict, list)):
                    walk(v, depth - 1)
        elif isinstance(obj, list):
            for el in obj[:25]:
                if isinstance(el, (dict, list)):
                    walk(el, depth - 1)

    walk(data, 16)
    seen: set[str] = set()
    out: list[str] = []
    for s in found:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out[:50]


def _kv_from_leaves(leaves: list[tuple[str, str]], *, skip_path_substrings: tuple[str, ...]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for path, val in leaves:
        pl = path.lower()
        if any(x in pl for x in skip_path_substrings):
            continue
        if len(val) > 1200:
            continue
        rows.append((path, val))
    rows.sort(key=lambda x: x[0].lower())
    return rows[:72]


def effective_product_root(data: dict[str, Any]) -> dict[str, Any]:
    """异步封装剥壳后，与刊登逻辑共用的根对象。"""
    root: dict[str, Any] = data
    if isinstance(data.get("response"), dict):
        inner = data["response"]
        outer_other = set(data.keys()) - {"response"}
        if not outer_other or outer_other <= {"status", "id", "statusUrl", "statusurl", "meta", "asin", "tld"}:
            root = inner
    return root


def build_product_view(data: dict[str, Any]) -> dict[str, Any]:
    """把 ScraperAPI 等返回的 JSON 整理成模板易用的结构（深度兼容嵌套字段）。"""
    root = effective_product_root(data)

    leaves = _iter_scalar_leaves(root)

    # 刊登/Shopify：优先使用结构化 JSON 顶层 name（与 Amazon 商品名一致）
    name_first = _scalar(root.get("name"))
    if name_first and len(name_first.strip()) >= 3:
        title = name_first.strip()
    else:
        title = _pick_title(leaves)
        if not title:
            title = (
                _scalar(root.get("title"))
                or _scalar(root.get("product_title"))
                or "（未识别标题）"
            )

    brand = _pick_brand(leaves) or _scalar(root.get("brand"))
    rating, reviews = _pick_rating_reviews(leaves)
    if not rating:
        rating = _scalar(root.get("rating"))
    if not reviews:
        reviews = _scalar(root.get("reviews_count")) or _scalar(root.get("ratings_total"))

    price = _pick_price(leaves) or (
        _scalar(root.get("price"))
        or _scalar(root.get("list_price"))
        or _scalar(root.get("current_price"))
    )

    bullets = _collect_bullets(root)
    if not bullets:
        for key in ("feature_bullets", "bullet_points", "about_this_item", "features"):
            raw = root.get(key)
            if isinstance(raw, list):
                for x in raw:
                    s = _scalar(x)
                    if s and len(s) > 10:
                        bullets.append(s)

    skip_sub = (
        "high_res_image",
        "image_url",
        "/image",
        "review",
        "customer_review",
        "variant",
        "seller",
    )
    kv_rows = _kv_from_leaves(leaves, skip_path_substrings=skip_sub)

    top_level_keys = sorted(str(k) for k in root.keys()) if isinstance(root, dict) else []

    return {
        "title": title,
        "brand": brand,
        "rating": rating,
        "reviews": reviews,
        "price": price,
        "bullets": bullets[:50],
        "kv_rows": kv_rows,
        "top_level_keys": top_level_keys,
        "scalar_count": len(leaves),
    }
