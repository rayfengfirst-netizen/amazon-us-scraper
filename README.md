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
├── deploy/                  # 线上 systemd / 发布脚本示例（端口 **8989**）
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

### 线上部署（端口 8989）

生产环境约定使用 **8989**（`0.0.0.0:8989`），详见 [deploy/README.md](deploy/README.md)、`deploy/bootstrap_server.sh`（一键首次部署）与 `deploy/amazon-us-scraper.service.example`。

若公网打不开且服务器上 `curl 127.0.0.1:8989` 为 **Connection refused**：说明服务未安装，按 `deploy/README.md` 首次部署；云安全组需放行 **TCP 8989**。

上云过程复盘与排障见 [docs/DEPLOY_RETROSPECTIVE_2026-03.md](docs/DEPLOY_RETROSPECTIVE_2026-03.md)。

### 环境变量（要点）

| 变量 | 说明 |
|------|------|
| `SCRAPERAPI_KEY` | ScraperAPI 密钥（**勿提交**） |
| `SCRAPERAPI_WEBHOOK_URL` | 仅异步模式需要；默认同步可不配 |
| `SCRAPERAPI_USE_ASYNC` | 设为 `1` 时使用异步 POST + 轮询 |
| `SCRAPERAPI_SYNC_TIMEOUT_SEC` | 同步请求超时（秒），默认 120 |
| `SCRAPERAPI_COUNTRY_CODE` / `SCRAPERAPI_TLD` | 默认 `us` / `com` |

完整说明见 `.env.example`。

### Shopify 发布（可选）

店铺凭据 **保存在本地 SQLite**（`data/app.db`），不在 `.env` 中。在 Web 界面「Shopify 店铺设置」（`/settings/shops`）添加店铺后，在采集成功的商品详情页可发布。

**认证二选一：**（1）店铺后台「开发应用」安装后的 **Admin API access token**（`shpat_…`）；（2）[Dev Dashboard](https://shopify.dev/docs/apps/build/dev-dashboard) 应用的 **Client ID + Client Secret** — 本工具会按官方 [client_credentials](https://shopify.dev/docs/apps/build/authentication-authorization/client-secrets) 流程向 `POST /admin/oauth/access_token` 换取 token（约 24 小时有效），**勿**把 Client ID 单独当成 API token 使用。

建议 scope：**`read_products`**、**`write_products`**；全渠道发布还需 **`read_publications`**、**`write_publications`**。凭据勿提交版本库。

### Shopify 接入踩坑记录（本次）

- **401 Invalid API key or access token**：最常见是把 Dev Dashboard 的 `Client ID` 当成了 API token。`X-Shopify-Access-Token` 必须是可用 access token（静态 `shpat_...`，或由 `Client ID + Client Secret` 通过 `client_credentials` 换取）。
- **测试连接成功但发布失败**：通常是商品详情页下拉框选错店铺（多店铺时易发生）。发布前确认店铺名称与域名和“测试连接成功”的那条一致。
- **店铺域名格式错误**：应为 `xxx.myshopify.com`，不要带 `https://`、路径或结尾 `/`。项目已做域名标准化，但建议录入时仍保持纯域名。
- **只填了 OAuth 的 Client ID**：OAuth 模式必须 `Client ID + Client Secret` 成对填写；只填一个会导致鉴权失败。
- **发布范围误解**：`all` 会对可见 publications 全量发布；`online_store` 只匹配名称含 `Online Store` 的 publication。若找不到渠道会报错。
- **权限不足**：创建商品至少需 `read_products`、`write_products`；全渠道发布还需 `read_publications`、`write_publications`。

排查顺序建议：先在 `/settings/shops` 点“测试连接” -> 再确认详情页下拉店铺 -> 最后查看详情页“最近发布记录”的报错信息。

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
