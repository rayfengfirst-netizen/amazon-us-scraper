# 需求说明（Amazon/eBay 采集 + Shopify 发布台）

## 1. 目标录入

- 用户在页面提交 **ASIN** 或 **Amazon 商品链接**（美国站）。
- 若为链接，需 **解析出 ASIN** 供后续流程使用。

## 2. 入库与采集节奏

- 目标数据 **先入库**，用户再 **手动发起采集**，避免批量任务一次性失败。
- 用户可查看每条记录的 **状态**（pending / running / success / failed）与错误信息。

## 3. 列表与详情

- **列表**：按 **ASIN 维度** 展示（同一 ASIN 多条提交时，列表展示最新一条），并提示是否已有 **ASIN 缓存快照**。
- **详情**：进入单条记录后查看采集结果；展示形式参考 **电商商品详情页**（左图右信息、要点、规格表等），并提供 **原始 JSON** 供调试。
- **详情页新增**：顶部增加「Shopify 传输预览」模块（默认折叠），用于核对将发送的字段映射，不能影响原详情内容展示。

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

- 在采集成功的详情页支持：
  - 选择店铺
  - 选择商品状态（`draft` / `active`）
  - 选择渠道范围（`all` / `online_store`）
- 发布结果写入 `ShopifyPublishLog`，并在详情页回显最近一次发布信息。

### 7.3 Shopify payload 规则（本版本）

- `product.title`：优先 JSON 顶层 `name`，否则回退启发式标题。
- `product.vendor`：固定 `EGR Performance`。
- `product.tags`：空字符串。
- `variants[0].sku`：
  - Amazon：`AM-{ASIN}`
  - eBay：`EB-{item_id}`（优先字段提取，回退链接正则）
- `variants[0].price`：采集价 * `1.7`（两位小数）。
- `variants[0].inventory_quantity`：默认 `30`。
- `product.images[]`：仅使用原图 `src URL`，不做本地 attachment 上传。

### 7.4 OpenAI 文案优化（可选）

- 发布 Shopify 时可开启 OpenAI 优化 4 个字段：
  - 标题（`product.title`）
  - 描述（`product.body_html`）
  - SEO 标题（`metafields_global_title_tag`）
  - SEO 描述（`metafields_global_description_tag`）
- 触发模式：**手动触发**。仅当发布表单勾选 `use_ai` 时调用 OpenAI。
- 通过 `.env` 控制可用性：`OPENAI_ENABLE=1` + `OPENAI_API_KEY`。
- 提示词需拆分为 4 份独立模板，支持后续单独调优，不与业务代码硬耦合。
- OpenAI 请求失败时应回退到本地规则文案，不中断发布流程。

### 7.5 已知坑与约束

- 测试连接成功但发布失败，多为详情页选错店铺（多店配置场景）。
- 仅填 `Client ID` 不可用，OAuth 模式必须 `Client ID + Client Secret` 成对。
- 若 `publish_scope=online_store` 未匹配到 Online Store publication，应给出可理解错误提示。
- 需保留详情页原有商品视图；新增模块必须是“附加模块”，不能破坏主视图。

## 8. 后续迭代方向（建议）

- 店铺级发布策略：价格系数、vendor、SKU 前缀可配置。
- 增加 eBay 来源识别稳定性（更多 URL/字段样本测试）。
- 增加“发布前校验”与“发布后结构化报表”能力。

配套规划文档：`docs/PHASE2_PLAN.md`

## 9. 合规声明

- Amazon 与 ScraperAPI 均有各自服务条款与使用限制；使用者需自行确保用途合法合规。本项目仅为技术脚手架示例。
