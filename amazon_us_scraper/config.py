"""美国站默认配置。"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# 美国零售站主域（商品详情、搜索等）
AMAZON_US_BASE = "https://www.amazon.com"

# 常见请求头：英语美国、桌面 Chrome
DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


def request_delay_sec() -> float:
    raw = os.getenv("AMAZON_REQUEST_DELAY_SEC", "1.0")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 1.0


def user_agent() -> str:
    return os.getenv("AMAZON_USER_AGENT", "").strip() or DEFAULT_USER_AGENT
