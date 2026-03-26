"""从用户输入（纯 ASIN 或 Amazon 商品链接）解析 ASIN。"""

from __future__ import annotations

import re

_ASIN_STRICT = re.compile(r"^[A-Z0-9]{10}$", re.I)
# 常见路径: /dp/ASIN, /gp/product/ASIN, /d/ASIN, ?asin=ASIN
_URL_PATTERNS = [
    re.compile(r"/(?:dp|gp/product|d|product)/([A-Z0-9]{10})", re.I),
    re.compile(r"[?&]asin=([A-Z0-9]{10})", re.I),
]


def parse_asin(user_input: str) -> str | None:
    """
    返回大写 ASIN，无法识别时返回 None。
    """
    raw = (user_input or "").strip()
    if not raw:
        return None
    compact = re.sub(r"\s+", "", raw)
    if _ASIN_STRICT.match(compact):
        return compact.upper()
    for pat in _URL_PATTERNS:
        m = pat.search(raw)
        if m:
            return m.group(1).upper()
    return None
