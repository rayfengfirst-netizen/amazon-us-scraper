# amazon-us-scraper

Amazon **美国站（amazon.com）** 商品数据采集与展示小工具：支持 **ASIN / 链接解析**、**入库排队**、**ScraperAPI 拉取结构化数据**、**ASIN 级 JSON 缓存**、**high_res_images 本地下载**，以及 **FastAPI + Jinja2** 的电商风格详情页。

远程仓库：<https://github.com/rayfengfirst-netizen/amazon-us-scraper>

## 功能概览

| 能力 | 说明 |
|------|------|
| 目标提交 | 表单输入 ASIN 或商品 URL，自动解析 ASIN |
| 先入库再采 | 记录 `pending`，用户点击「开始/再次采集」才请求接口 |
| ASIN 缓存 | `AsinSnapshot` 表按 ASIN 存 JSON，减少重复请求 |
| 强制拉取 | 忽略缓存，重新调用 ScraperAPI |
| 图片 | 仅下载 JSON 中 `high_res_images`，存 `data/images/{ASIN}/` |
| 详情 UI | 左图右信息、要点、规格表、折叠 JSON |

详细需求见 [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md)。

## 技术栈

- Python 3.9+
- FastAPI、Uvicorn、SQLModel（SQLite）、Jinja2
- httpx、BeautifulSoup（直连 HTML 示例）、python-dotenv

## 目录结构

```
amazon-us-scraper/
├── amazon_us_scraper/       # 核心库：直连客户端、ScraperAPI 封装
├── webapp/                  # Web：路由、模型、采集与图片服务
├── templates/               # Jinja2 模板（列表 / 商品详情）
├── docs/
│   └── REQUIREMENTS.md      # 需求文档
├── requirements.txt
├── run_example.py           # 命令行示例（可选）
├── .env.example             # 环境变量示例（复制为 .env）
└── data/                    # 运行时生成：app.db、images/（勿提交）
```

## 快速开始

```bash
cd amazon-us-scraper
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env：填入 SCRAPERAPI_KEY（必填）
```

### 启动 Web

```bash
uvicorn webapp.main:app --reload --host 127.0.0.1 --port 8000
```

浏览器打开 <http://127.0.0.1:8000>。

### 环境变量（要点）

| 变量 | 说明 |
|------|------|
| `SCRAPERAPI_KEY` | ScraperAPI 密钥（**勿提交**） |
| `SCRAPERAPI_WEBHOOK_URL` | 仅异步模式需要；默认同步可不配 |
| `SCRAPERAPI_USE_ASYNC` | 设为 `1` 时使用异步 POST + 轮询 |
| `SCRAPERAPI_SYNC_TIMEOUT_SEC` | 同步请求超时（秒），默认 120 |
| `SCRAPERAPI_COUNTRY_CODE` / `SCRAPERAPI_TLD` | 默认 `us` / `com` |

完整说明见 `.env.example`。

### 命令行示例（可选）

```bash
python run_example.py B08N5WRWNW
```

## API 说明

- `GET /api/targets` — 列表（按 ASIN 去重后的最新记录）
- `GET /api/targets/{id}` — 单条含结构化 `data`

## ScraperAPI 文档参考

- [Structured Data / Amazon（异步说明）](https://docs.scraperapi.com/structured-data-endpoints/e-commerce/amazon/amazon-product-api-async)
- 同步 Structured Product：`GET https://api.scraperapi.com/structured/amazon/product`（参数见官方文档与 `.env.example`）

## 免责声明

使用本工具访问 Amazon 与 ScraperAPI 时，请自行遵守相关服务条款与适用法律。

## 贡献

Issue / PR 欢迎指向：<https://github.com/rayfengfirst-netizen/amazon-us-scraper>
