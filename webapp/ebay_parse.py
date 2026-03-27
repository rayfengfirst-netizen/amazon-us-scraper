"""从用户输入（纯 item_id 或 eBay 商品链接）解析 eBay item_id。"""

from __future__ import annotations

import re

_ITEM_ID_STRICT = re.compile(r"^\d{9,15}$")
_URL_PATTERNS = [
    re.compile(r"/itm/(?:[^/]+/)?(\d{9,15})(?:[/?#]|$)", re.I),
    re.compile(r"[?&](?:item|itemid|item_id)=(\d{9,15})", re.I),
]


def parse_ebay_item_id(user_input: str) -> str | None:
    """返回 item_id，无法识别时返回 None。"""
    raw = (user_input or "").strip()
    if not raw:
        return None
    compact = re.sub(r"\s+", "", raw)
    if _ITEM_ID_STRICT.match(compact):
        return compact
    for pat in _URL_PATTERNS:
        m = pat.search(raw)
        if m:
            return m.group(1)
    return None
