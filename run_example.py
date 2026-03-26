#!/usr/bin/env python3
"""
示例：按 ASIN 拉取美国站商品信息。

- 若已配置 SCRAPERAPI_KEY：走 ScraperAPI 异步 Structured Product（推荐）。
- 否则：直连 amazon.com 抓 HTML（易被拦截）。

用法:
  python run_example.py B08N5WRWNW
  python run_example.py --direct B08N5WRWNW
"""
import json
import os
import sys

from amazon_us_scraper.client import build_client
from amazon_us_scraper.product import fetch_product_snippet
from amazon_us_scraper.scraperapi_amazon import (
    ScraperAPIError,
    fetch_amazon_product_us,
    guess_product_title,
)


def main() -> None:
    args = [a for a in sys.argv[1:] if a]
    direct = False
    if args and args[0] == "--direct":
        direct = True
        args = args[1:]
    if not args:
        print("用法: python run_example.py [--direct] <ASIN>")
        sys.exit(1)
    asin = args[0]

    use_scraperapi = not direct and bool(os.getenv("SCRAPERAPI_KEY", "").strip())

    if use_scraperapi:
        try:
            data = fetch_amazon_product_us(asin)
        except ScraperAPIError as e:
            print("ScraperAPI:", e)
            sys.exit(2)
        except Exception as e:
            print("请求失败:", e)
            sys.exit(2)
        title = guess_product_title(data)
        print("ASIN:", asin.strip().upper())
        print("Title:", title or "(未从结构化 JSON 识别到标题，见下方原始数据)")
        print("Structured (JSON):")
        print(json.dumps(data, ensure_ascii=False, indent=2)[:8000])
        if len(json.dumps(data, ensure_ascii=False)) > 8000:
            print("... (已截断，完整数据请自行 json.dumps 保存)")
        return

    with build_client() as client:
        try:
            snip = fetch_product_snippet(asin, client=client)
        except Exception as e:
            print("直连请求或解析失败（常见原因：验证码、封禁、选择器过期）:", e)
            sys.exit(2)
    print("ASIN:", snip.asin)
    print("Title:", snip.title or "(未解析到标题)")


if __name__ == "__main__":
    main()
