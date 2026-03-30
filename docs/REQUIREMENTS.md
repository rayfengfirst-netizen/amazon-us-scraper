# 需求说明（Amazon/eBay 采集 + Shopify 发布台）

## 1. 目标录入

- 用户在页面支持**批量提交**（每行一条）ASIN 或 Amazon 商品链接（美国站）。
- 若为链接，需 **解析出 ASIN** 供后续流程使用。
- 支持两种动作：
  - 仅加入列表（不触发采集）
  - 加入并开始采集（仅对新增 ASIN 启动采集）

## 2. 入库与采集节奏

- 目标数据 **先入库**，用户再 **手动发起采集**（或提交时选择自动采集新增项），避免批量任务一次性失败。
- 用户可查看每条记录的 **状态**（pending / running / success / failed）与错误信息。
- 对于重复 ASIN：提交后只刷新该记录更新时间并提升排序，不自动重抓；仅点击“再次采集”时重抓。

## 3. 列表与详情

- **列表**：按 **ASIN 维度** 展示（同一 ASIN 多条提交时，列表展示最新一条），并提示是否已有 **ASIN 缓存快照**。
- 列表支持 **50 条/页分页**、序号列、缩略图（无图时默认占位）、当前页采集进度，以及 Shopify 发布状态（已发布/未发布/发布失败）。
- **详情**：进入单条记录后查看采集结果；展示形式参考 **电商商品详情页**（左图右信息、要点、规格表等），并提供 **原始 JSON** 供调试。
- 全局导航采用一级/二级结构：
  - 一级：首页
  - 二级：Shopify 店铺设置 / UPC维护 / 提示词库 / 商品详情

## 4. 数据与图片

- 每个 ASIN 的结构化 JSON **持久化缓存**（`AsinSnapshot`），**采集前优先读库**，减少重复调用 ScraperAPI。
- 支持 **强制重新拉取**（忽略缓存）。
- 从 JSON 中仅下载 **`high_res_images`** 指向的图片到本地；其它图片不下载。
- 已下载图片在详情页展示，并通过静态路径访问。

## 5. 外部服务（ScraperAPI）

- 使用 [ScraperAPI](https://www.scraperapi.com/) **Structured Amazon Product** 接口获取美国站结构化数据。
- **默认同步** `GET https://api.scraperapi.com/structured/amazon/product`（`api_key` + `asin` 等）。
- 可选环境变量 **`SCRAPERAPI_USE_ASYNC=1`** 走异步 POST + 轮询（需有效 webhook 占位等）。
- API Key、`.env` **不得提交到 Git**。

## 6. 非功能需求

- Python 3.9+，依赖见 `requirements.txt`。
- 本地 SQLite + 文件系统存储（`data/`，默认已 `.gitignore`）。

## 7. Shopify 发布模块（当前实现基线）

### 7.1 店铺管理

- 提供店铺配置页：新增、列表、测试连接、删除。
- 认证支持二选一：
  - 静态 `Admin API access token (shpat_...)`
  - Dev Dashboard `Client ID + Client Secret`（运行时按 `client_credentials` 换 token）
- 店铺域名需标准化为 `xxx.myshopify.com`。

### 7.2 详情页发布能力

- 在采集成功的详情页支持 Shopify 二次编辑与发布：
  - 文案编辑（标题、描述、SEO 标题、SEO 描述）
  - 价格与参数（price、vendor、tags、sku、inventory、元字段）
  - 发布配置（店铺、状态、渠道）与发布按钮同模块
- 发布结果写入 `ShopifyPublishLog`，并在详情页回显最近一次发布信息。
- 成功发布时持久化 `shopify_product_handle`（用于构造网店前台 URL）；详情页上「商品 ID」在具备 `handle` 时展示为指向 `https://{shop}.myshopify.com/products/{handle}` 的链接。历史记录无 `handle` 时，打开详情页可触发一次 Admin API 读取并回填（凭据需有效）。
- 商品规格与字段默认收起，按需展开。
- 首次发布后按钮切换为“更新内容”，后续更新同一 Shopify 商品（不再新建）。

### 7.3 Shopify payload 规则（本版本）

- `product.title`：优先 JSON 顶层 `name`，否则回退启发式标题。
- `product.vendor`：固定 `EGR Performance`。
- `product.tags`：空字符串。
- `variants[0].sku`：
  - Amazon：`AM-{ASIN}`
  - eBay：`EB-{item_id}`（优先字段提取，回退链接正则）
- `variants[0].price`：采集价 * `1.7`（两位小数）。
- `variants[0].inventory_quantity`：默认 `30`。
- `variants[0].barcode`：来自 UPC 码池（首次发布占用，后续更新复用）。
- `product.images[]`：仅使用原图 `src URL`，不做本地 attachment 上传。
- 元字段（GraphQL `metafieldsSet`）：
  - `custom.warehouse`（默认 `Ontario CA / Springdale OH / Newark NJ`，可编辑）
  - `custom.delivery_time`（默认 `2-5 working days inland in the United States`，可编辑）
  - `custom.specifications`（默认空，可输入富文本）
  - `custom.qa`（默认空，可输入富文本）
  - `custom.vehicle_fitment`（默认空，可输入富文本）
  - `custom.package_list`（默认空，可输入富文本）

### 7.4 OpenAI 文案优化（可选）

- 详情页支持 OpenAI 优化 4 个字段：
  - 标题（`product.title`）
  - 描述（`product.body_html`）
  - SEO 标题（`metafields_global_title_tag`）
  - SEO 描述（`metafields_global_description_tag`）
- 触发模式：**手动触发**。点击“ChatGPT 改写四项”后按字段调用改写接口。
- 全站默认提示词模板可在 `/settings/prompt-libraries` 配置（`meta.json` 持久化）；详情页下拉优先使用商品已保存的 `prompt_library_id`，否则使用全站默认。
- 通过 `.env` 控制可用性：`OPENAI_ENABLE=1` + `OPENAI_API_KEY`。
- 提示词采用模板库 `prompts/shopify_openai/libraries.json`（四类提示词 + 中文注释）。
- OpenAI 请求失败时应回退到本地规则文案，不中断发布流程。
- AI 改写后的编辑内容需持久化；下次进入详情页默认回填，并提示“内容已 ChatGPT 改写”。
- 提示词库管理页支持新增/删除/修改；模板 ID 由系统自动生成并固定，不允许手工编辑。
- 模板格式需兼容历史占位符写法，未知占位符不应导致接口 500。

### 7.5 同步与草稿持久化（新增）

- 对已发布商品，进入详情页时默认仅加载本地草稿，不自动触发 Shopify 同步。
- 页面提供“同步 Shopify”按钮，允许人工触发同步并刷新编辑态。
- 同步流程不应破坏本地已保存草稿结构；字段映射应与发布 payload 保持一致。
- 同步策略应遵循“远端空值不覆盖本地已有值”。

### 7.6 富文本编辑要求（补充）

- 富文本字段至少支持：段落、加粗、斜体、无序列表、有序列表、H1~H5（H 标签语义）。
- 发布到 Shopify 后应尽量保留格式，不允许退化为仅换行纯文本。
- 从 Shopify 拉取回填时需支持富文本反序列化，保证前后端可视结果一致。
- 元字段 type 与定义不一致时，发布链路需具备自适应重试能力。

### 7.7 详情页编辑体验（新增）

- 商品描述支持“HTML源码 / 可视预览”双视图切换，默认打开可视预览。
- 元字段富文本输入区应限制可视高度，超长内容在输入框内部滚动，避免页面被无限拉长。
- SEO 标题/SEO 描述需实时显示字符计数（`已使用 x/70`、`已使用 x/160`），超限时高亮提醒。

### 7.8 UPC 维护（新增）

- 新增页面：`/settings/upc`，支持一行一个批量录入 UPC。
- 格式校验：当前仅校验长度为 12 位。
- UPC 维护状态：`未使用` / `已使用`，并记录使用目标与 Shopify 商品 ID。
- 发布约束：
  - 首次发布前必须存在可用 UPC；
  - 发布成功后立即标记该 UPC 为已使用；
  - 后续更新不可重复占用新 UPC。

### 7.9 已知坑与约束

- 详情页「商品 ID」前台链接指向 `*.myshopify.com/products/{handle}`；**draft** 或未对网店渠道发布时，前台可能 404，属 Shopify 行为。
- 测试连接成功但发布失败，多为详情页选错店铺（多店配置场景）。
- 仅填 `Client ID` 不可用，OAuth 模式必须 `Client ID + Client Secret` 成对。
- 若 `publish_scope=online_store` 未匹配到 Online Store publication，应给出可理解错误提示。
- 需保留详情页原有商品视图；新增模块必须是“附加模块”，不能破坏主视图。
- 模板渲染层不得直接依赖游离 ORM 实例，避免 `DetachedInstanceError`。

## 8. 后续迭代方向（建议）

- 店铺级发布策略：价格系数、vendor、SKU 前缀可配置。
- 增加 eBay 来源识别稳定性（更多 URL/字段样本测试）。
- 增加“发布前校验”与“发布后结构化报表”能力。

配套规划文档：`docs/PHASE2_PLAN.md`

## 9. 合规声明

- Amazon 与 ScraperAPI 均有各自服务条款与使用限制；使用者需自行确保用途合法合规。本项目仅为技术脚手架示例。
