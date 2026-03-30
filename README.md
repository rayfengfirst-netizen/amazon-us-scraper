# amazon-us-scraper

Amazon **美国站（amazon.com）** 商品数据采集与展示小工具：支持 **ASIN / 链接解析**、**入库排队**、**ScraperAPI 拉取结构化数据**、**ASIN 级 JSON 缓存**、**high_res_images 本地下载**，以及 **FastAPI + Jinja2** 的电商风格详情页。

远程仓库：<https://github.com/rayfengfirst-netizen/amazon-us-scraper>

## 功能概览

| 能力 | 说明 |
|------|------|
| 目标提交 | 表单输入 ASIN 或商品 URL，自动解析 ASIN |
| 批量录入 | 支持多行输入批量提交，重复 ASIN 只刷新排序不重抓 |
| 先入库再采 | 记录 `pending`，用户点击「开始/再次采集」才请求接口 |
| ASIN 缓存 | `AsinSnapshot` 表按 ASIN 存 JSON，减少重复请求 |
| 强制拉取 | 忽略缓存，重新调用 ScraperAPI |
| 图片 | 仅下载 JSON 中 `high_res_images`，存 `data/images/{ASIN}/` |
| 列表体验 | 序号 + 缩略图 + 采集进度 + 50 条分页 + Shopify 发布状态 |
| 详情 UI | 左图右信息、要点、规格默认收起、折叠 JSON |
| UPC 码池 | 二级页面批量维护 UPC；发布成功自动消耗并标记已使用 |
| Shopify 网店链接 | 已发布商品详情页「商品 ID」可点开 **前台商品页**（`/products/{handle}`）；`handle` 随发布写入日志，旧记录打开详情时按需 API 回填 |
| 默认提示词模板 | `/settings/prompt-libraries` 可设全站默认（`prompts/shopify_openai/meta.json`）；详情页未保存过模板选择时使用该默认 |

详细需求见 [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md)。**其余文档目录与用途**见 [docs/README.md](docs/README.md)。

### 近期更新摘要（截至 2026-03）

- **商品描述编辑器**：TinyMCE 富文本（中文界面 + 思源黑体类字体栈），内容同步到 `body_html` 发布至 Shopify。
- **AI 改写**：支持 OpenAI 与 **豆包（火山方舟）** 等，详情页可选模型；环境变量见 `.env.example`。
- **Shopify 链接**：详情页商品 ID 指向 **网店前台** `/products/{handle}`（`shopify_publish_log.shopify_product_handle`，旧数据打开详情可自动回填）。
- **提示词**：全站默认模板 + 单商品可覆盖；模板列表 `libraries.json`，默认项 `meta.json`（各环境独立，线上需在设置页保存一次默认）。
- **部署**：生产约定见 [deploy/README.md](deploy/README.md)、[docs/DEPLOY_TARGET.md](docs/DEPLOY_TARGET.md)；日常 `git pull` + 重启 `amazon-us-scraper`（端口 **8989**）。

当前里程碑：

- **V1（已完成）**：采集 + 详情页 + Shopify 发布闭环（含多店配置、发布日志、二次编辑、手动 AI 改写）
- **V2（规划中）**：配置化发布策略、内容治理、批量能力、可观测性与测试体系

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
├── docs/                    # 需求/阶段规划/复盘归档（入口 docs/README.md）
├── prompts/                 # Shopify AI 提示词：shopify_openai/libraries.json、meta.json（默认模板）
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

**当前默认生产机**（与 [docs/DEPLOY_TARGET.md](docs/DEPLOY_TARGET.md) 一致）：

- 首页：<http://8.221.99.46:8989/>
- eBay 列表：<http://8.221.99.46:8989/ebay>
- 健康检查：<http://8.221.99.46:8989/health>

若公网打不开且服务器上 `curl 127.0.0.1:8989` 为 **Connection refused**：说明服务未安装，按 `deploy/README.md` 首次部署；云安全组需放行 **TCP 8989**。未做 80 反代时，访问需**显式带端口 `:8989`**。

上云过程复盘与排障见 [docs/DEPLOY_RETROSPECTIVE_2026-03.md](docs/DEPLOY_RETROSPECTIVE_2026-03.md)。

### 环境变量（要点）

| 变量 | 说明 |
|------|------|
| `SCRAPERAPI_KEY` | ScraperAPI 密钥（**勿提交**） |
| `SCRAPERAPI_WEBHOOK_URL` | 仅异步模式需要；默认同步可不配 |
| `SCRAPERAPI_USE_ASYNC` | 设为 `1` 时使用异步 POST + 轮询 |
| `SCRAPERAPI_SYNC_TIMEOUT_SEC` | 同步请求超时（秒），默认 120 |
| `SCRAPERAPI_COUNTRY_CODE` / `SCRAPERAPI_TLD` | 默认 `us` / `com` |
| `SCRAPERAPI_PROXY_DESC_ENABLE` | eBay 描述页抓取是否优先走 ScraperAPI 代理（默认 `1`） |
| `APP_BASIC_AUTH_ENABLE` / `APP_BASIC_AUTH_USERNAME` / `APP_BASIC_AUTH_PASSWORD` | 线上访问保护（HTTP Basic 登录） |

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

### Shopify 创建商品当前映射字段（REST `POST .../products.json`）

详情页顶部 **「Shopify 二次编辑与发布」** 默认值与下列规则一致；标题优先取采集 JSON 顶层 **`name`**，再回退启发式字段。首次“发布到 Shopify”会创建商品，后续按钮自动变为“更新内容”并更新同一 Shopify 商品。

| Shopify 字段 | 说明 |
|--------------|------|
| `product.title` | 首选 `name`，否则原启发式标题；最长 255 |
| `product.body_html` | 描述 + 要点 HTML |
| `product.vendor` | 固定 `EGR Performance` |
| `product.tags` | 置空（空字符串） |
| `product.status` | 发布表单 `draft` / `active` |
| `product.published_scope` | 固定 `global` |
| `product.metafields_global_title_tag` | SEO 标题，≤70 |
| `product.metafields_global_description_tag` | SEO 描述（去 HTML），≤320 |
| `variants[0].sku` | Amazon: `AM-{ASIN}`；eBay: `EB-{item_id}`（从数据/链接提取） |
| `variants[0].price` | 采集价 * `1.7`（保留两位） |
| `variants[0].inventory_management` | `shopify` |
| `variants[0].inventory_policy` | `deny` |
| `variants[0].inventory_quantity` | 环境变量 `SHOPIFY_DEFAULT_INVENTORY` 或默认 `30` |
| `variants[0].barcode` | 来自 UPC 码池（12 位）；首次发布消耗并绑定，后续更新复用 |
| `product.images[]` | 仅传原始图片 `src URL`（不做本地 attachment 上传） |
| `metafields` | `custom.warehouse`、`custom.delivery_time`、`custom.specifications`、`custom.qa`、`custom.vehicle_fitment`、`custom.package_list`（GraphQL `metafieldsSet` 写入） |

创建成功后：**GraphQL** `publishablePublish`（范围：`all` 或仅 Online Store 匹配渠道）。
元字段写入：**GraphQL** `metafieldsSet`（创建/更新后统一执行，避免 REST 更新场景丢失）。

### 详情页：Shopify 商品 ID → 网店前台链接

- 顶部「已同步到 Shopify」旁的 **商品 ID**、以及模块底部「最近发布记录」中的 ID，在可解析时为 **超链接**，指向网店前台：`https://{店铺配置的 myshopify 域名}/products/{handle}`（浏览器常会再 302 到自定义主域名）。
- 发布成功时从 REST 响应写入 `shopify_publish_log.shopify_product_handle`；**旧记录**若尚无 `handle`，打开详情页时会用 Admin API `GET .../products/{id}.json` 拉取一次并写回（需凭据有效）。
- **草稿（draft）** 或未对 Online Store 可见时，前台链接可能 404 或不可访问，属 Shopify 侧规则，非本工具链接错误。

### 详情页同步与编辑态规则（本轮更新）

- 已发布商品进入详情页或刷新页面时，默认只读取本地草稿/本地默认值，不自动调用 Shopify 同步。
- 页面提供「同步 Shopify」按钮，可手动执行一次拉取并刷新编辑态。
- AI 改写后的内容会保存到本地数据库；下次进入详情页默认显示改写后内容，并提示“已 ChatGPT 改写”。
- `specifications` / `qa` / `vehicle_fitment` / `package_list` 支持富文本编辑，保留加粗、斜体、列表和 H 标签。
- 富文本工具栏支持 `H1~H5`，并可直接写入到 Shopify rich text。
- 商品描述支持 `HTML源码 / 可视预览` 切换，默认打开可视预览。
- SEO 标题与 SEO 描述输入框支持实时字符计数（已使用 x/70、x/160）。
- 发布按钮行为：
  - 首次：`发布到 Shopify`（创建商品）
  - 已发布：`更新内容`（更新同一商品）

### UPC 维护与发布占用

- 二级导航新增 `UPC维护`（`/settings/upc`），支持一行一个批量录入。
- 校验规则：当前仅校验长度为 12 位（例如 `746270023010`）。
- 状态字段：`未使用` / `已使用`，并记录关联目标与 Shopify 商品 ID。
- 发布逻辑：
  - 首次发布：必须存在可用 UPC，成功后立即标记已使用
  - 后续更新：复用首次绑定的 UPC，不重复消耗
- 若无可用 UPC，详情页会提示先补充 UPC 后再发布。

### OpenAI 文案优化（标题/描述/SEO）

详情页支持 OpenAI 优化四个字段（**手动触发，不自动调用**）：

- `product.title`
- `product.body_html`
- `product.metafields_global_title_tag`
- `product.metafields_global_description_tag`

触发方式：

- 点击「AI 改写四项」按钮后，一次性请求后端完成四字段改写（所选模型见详情页下拉与 `.env`）
- 点「发布到 Shopify」时不会再次自动调用 AI，只提交当前表单内容
- 改写后的编辑内容会持久化到目标记录，下次进入详情页默认回填并提示“已 ChatGPT 改写”

环境变量（`.env`）：

- `OPENAI_ENABLE=1`
- `OPENAI_API_KEY=...`
- 其余可选：`OPENAI_BASE_URL`、`OPENAI_MODEL`、`OPENAI_TEMPERATURE`、`OPENAI_TIMEOUT_SEC`

提示词模板采用“提示词库”机制：模板列表在 `prompts/shopify_openai/libraries.json`，每个模板包含 4 类提示词（标题/描述/SEO标题/SEO描述）及中文注释。可在 `/settings/prompt-libraries` **设置全站默认模板**（写入 `prompts/shopify_openai/meta.json`）；商品详情页「提示词库模板」在未保存过该商品选择时，会默认选中该模板；仍可在详情页临时改选其他模板。

提示词库管理支持页面内新增/编辑/删除；模板 ID 改为系统自动生成并固定。为兼容历史模板，改写模块已支持安全格式化：未知占位符不会导致 500，常见旧占位符（如 `{title}`、`{description}`）会做兼容映射。

说明：AI 改写“商品描述（HTML）”的关键输入源为 `full_description + feature_bullets` 拼接结果，默认描述值也由这两部分组成。`specifications` / `qa` 支持富文本输入，发布时转换为 Shopify `rich_text_field`。

环境变量切换注意（本地常见坑）：

- `python-dotenv` 默认不会覆盖已存在的 shell 环境变量；如果你在终端里 `export` 过旧值（例如 `OPENAI_MODEL=qwen3.5-plus`），应用会继续使用旧值
- `.env` 如包含 shell 不兼容语法，`source .env` 可能报 `zsh: parse error near ')'`；建议保持 `.env` 为标准 `KEY=VALUE` 格式，或改为手动 `export` 关键变量

线上排障结论（2026-03）：若点击“ChatGPT 改写四项”接口返回 `200` 但内容几乎不变，需先在服务器执行探针确认 OpenAI 连通性。已出现案例：`OPENAI_API_KEY` 可读取，但调用 `https://api.openai.com/v1/chat/completions` 返回 `403 unsupported_country_region_territory`（服务器出口地区受限）。该场景应改用可访问的 OpenAI 兼容网关（`OPENAI_BASE_URL` + 网关 key）或更换服务器出口地区。

### 最近问题复盘（元字段与页面稳定性）

- **`warehouse` 看似写入成功但后台不可见**  
  结论：多与店铺中元字段定义 namespace/type 不一致相关。已支持按字段单独配置 namespace，并在单行字段追加 REST upsert 兜底。

- **`metafieldsSet INVALID_TYPE`**（例如定义是 `rich_text_field`，请求却传 `single_line_text_field`）  
  结论：已增加自适应类型重试：从 Shopify 报错中解析期望类型后自动转换 value 并重试写入。

- **富文本丢样式 / H 标签不生效**  
  结论：前端编辑器改为页面加载即初始化；后端富文本转换已支持 `<strong>/<em>/<ul>/<ol>/<h1-h6>` 双向转换。

- **详情页 `DetachedInstanceError`（Target / ShopifyPublishLog）**  
  结论：模板渲染前统一把 ORM 对象转为普通字典，避免 session commit 后访问已游离对象。

第二阶段规划见：[docs/PHASE2_PLAN.md](docs/PHASE2_PLAN.md)
提示词归档见：[docs/PROMPT_LIBRARY_ARCHIVE.md](docs/PROMPT_LIBRARY_ARCHIVE.md)
本轮代码归档见：[docs/CODE_ARCHIVE_2026-03-26.md](docs/CODE_ARCHIVE_2026-03-26.md)

### 后续开发建议（基于当前版本）

- **字段可配置化**：把 `vendor`、价格系数（当前 `1.7`）、SKU 前缀规则放到店铺级配置，不再硬编码。
- **编辑态与实发一致性测试**：增加单测，确保详情页编辑值与真实 payload 同步演进。
- **多平台来源识别**：补充 eBay item id 提取样本（短链、重定向、嵌套字段）回归用例。
- **发布风控**：发布前增加必填校验（title/price/images），避免空图或异常价格上架。
- **日志可观测性**：把 `report_json` 结构化展示（成功渠道、失败渠道、错误类型聚合）。

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
