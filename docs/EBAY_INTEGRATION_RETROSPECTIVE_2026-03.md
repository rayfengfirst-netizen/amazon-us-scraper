# eBay 接入复盘与防错清单（2026-03）

> 目的：沉淀本轮 Amazon/eBay 双来源改造、问题排查与防错规则，避免后续重复踩坑。

## 1. 本次需求范围（已完成）

1. 新增 eBay 采集链路（基于 ScraperAPI Structured eBay Product）。
2. 页面支持 Amazon / eBay 列表切换。
3. 商品详情页保持一致（尤其 Shopify 二次编辑区不变）。
4. eBay 数据映射到现有详情展示与 Shopify 发布流程。
5. eBay SKU 规则固定：`EB-<eBay商品id>`。
6. 图片优先改为大图版本后再入库/发布（eBay `s-l1600`）。
7. eBay 描述缺失时，自动补抓描述页并回填。

## 2. 关键实现点

### 2.1 采集与数据模型

- `Target` 增加 `source` 字段（`amazon` / `ebay`）。
- 新增 `EbaySnapshot`，与 `AsinSnapshot` 分开缓存。
- 列表页分来源展示：
  - Amazon：`/`
  - eBay：`/ebay`

### 2.2 eBay 接口

- 使用 endpoint：`GET https://api.scraperapi.com/structured/ebay/product`
- 关键参数：
  - `api_key`
  - `product_id`（eBay item id）
  - `country_code`（默认 `us`）
  - `tld`（默认 `com`）

### 2.3 图片策略（核心）

- 入库前统一归一化图片 URL：
  - Amazon：去尺寸后缀，尽量保留原图。
  - eBay：`s-l140/s-l500/...` 统一改为 `s-l1600`。
- Shopify 发布时直接使用归一化后的 URL（`images[].src`）。

### 2.4 描述策略（核心）

- eBay 常见返回仅有 `item_description_from_seller_url`，无 `description/full_description`。
- 处理逻辑：
  1) 优先通过 ScraperAPI 代理抓描述页；
  2) 失败后直连兜底；
  3) 抽取正文后写入 `full_description` 再落库。

可选开关：
- `SCRAPERAPI_PROXY_DESC_ENABLE=1`（默认启用代理描述抓取）

### 2.5 SKU 规则（核心）

- eBay 统一使用：`EB-<item_id>`。
- 双兜底：
  - 页面加载时：若历史草稿 SKU 为 `AM-...`，自动纠正。
  - 发布提交时：后端再纠正，确保最终发布值符合规则。

## 3. 本轮问题点与根因

### 问题 A：`SCRAPERAPI_KEY` 明明填写了仍报缺失

**现象**  
报错：`请设置环境变量 SCRAPERAPI_KEY（勿提交到 Git）`

**根因**  
- `.env` 被模板覆盖，或运行时读取状态不稳定（进程环境与文件状态不一致）。

**修复**  
- 采集模块取 key 前强制重读 `.env`。
- 新增运行时自检接口：`/api/debug/runtime-env`。

**防错**  
- 禁止重复执行 `cp .env.example .env` 覆盖已配置文件。

---

### 问题 B：OpenAI 改写接口 200 但看起来“无响应”

**现象**  
- 前端 `POST /api/targets/{id}/shopify-rewrite` 返回 200，多次点击无明显变化。

**根因**  
- OpenAI 配置项被注释（`# OPENAI_*`），导致走回退逻辑返回默认值。

**修复**  
- 明确启用条件：`OPENAI_ENABLE=1` + `OPENAI_API_KEY`。
- 统一 dotenv 读取方式。

**防错**  
- 配置改完后必须重启服务并做一次自检。

---

### 问题 C：eBay 图片不清晰（大量 `s-l140`）

**根因**  
- 结构化返回里含缩略图 URL，未经标准化直接展示/发布。

**修复**  
- 入库阶段统一改大图 URL；
- 发布阶段继续使用归一化 URL。

---

### 问题 D：eBay SKU 仍显示 `AM-...`

**根因**  
- 历史 `shopify_editor_json` 草稿覆盖新规则。

**修复**  
- 页面显示与发布请求均加入 eBay SKU 自动纠正。

## 4. 后续开发强约束（建议长期执行）

1. **同源数据入库前标准化**  
   图片、描述、ID 字段统一在采集层处理，避免下游重复修补。

2. **发布层只做映射，不做“猜测修复”**  
   能前置到采集阶段的尽量前置。

3. **每个关键开关都有自检路径**  
   至少保留一个只返回“是否生效”的调试接口（不返回敏感值）。

4. **历史草稿与新规则冲突时，后端兜底优先**  
   防止前端展示正确、实际发布错误。

## 5. 配置与环境管理约定（重要）

> 你已明确要求：不要改你的 `.env`（尤其线上与本机）。

执行约定：

1. **不自动写入 `.env`**。  
2. **不在发布或重启脚本里覆盖 `.env`**。  
3. 仅允许读取 `.env`，如需调整配置，先由你确认再改。  
4. 若要初始化配置，只允许“`.env` 不存在时再从 `.env.example` 复制”。  

## 6. 快速排查顺序（推荐）

1. 采集失败先看目标详情 `error_message`。  
2. 看 `/api/debug/runtime-env` 的 `scraperapi_key_set`。  
3. eBay 无描述先看是否存在 `item_description_from_seller_url`。  
4. 图片不清晰先检查 JSON 是否已是 `s-l1600`。  
5. SKU 异常先看页面 SKU 是否被历史草稿覆盖。  

---

如后续进入 V2，建议把“字段映射规则 + 默认值 + 平台差异”整理成可配置表（YAML/JSON），减少硬编码维护成本。
