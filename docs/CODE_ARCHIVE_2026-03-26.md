# Code Archive (2026-03-26)

本归档用于记录本阶段（Shopify 二次编辑 + AI 改写 + 发布链路排障）的代码状态、关键变更和复盘结论，便于后续迭代与回滚核对。

## 1) 阶段目标

- 在 ASIN 详情页完成 Shopify 发布前二次编辑能力
- 将 AI 文案优化改为手动触发，避免页面加载阻塞
- 引入提示词库模板机制，支持模板切换和可视化查看
- 固化 Shopify 映射规则（价格、SKU、vendor、images 等）
- 记录并闭环 Shopify / OpenAI 接入高频问题

## 2) 关键代码变更（按模块）

- `webapp/main.py`
  - 详情页改为注入 `shopify_editor` 默认值与提示词库列表
  - 新增单字段改写接口：`POST /api/targets/{id}/shopify-rewrite`
  - 发布接口支持编辑字段 override，不再提交时自动改写
  - 店铺设置支持 OAuth 凭据并支持删除店铺

- `webapp/shopify_service.py`
  - 新增 `ShopifyShopConfig` OAuth 字段与 token 解析逻辑
  - `normalize_shop_domain` 统一域名格式
  - `access_token_for_config` 支持 client_credentials 换 token
  - 创建 payload 固定规则：
    - `vendor = EGR Performance`
    - `tags = ""`
    - `variants[0].price = scrape_price * 1.7`
    - `variants[0].sku = AM-{ASIN} / EB-{item_id}`
    - `images[]` 仅传 `src URL`
  - 价格解析优先 `pricing` 等结构化字段（修复原始价误取）

- `webapp/ai_copy.py`
  - OpenAI 调用抽象到统一模块
  - 新增 `optimize_shopify_field`，支持四字段分开请求
  - 描述改写输入统一为 `full_description + feature_bullets`
  - 加入失败重试与 warning 日志，保证部分失败可降级

- `webapp/prompt_library.py`
  - 新增提示词库加载器（`libraries.json`）
  - 提供模板列表和按 ID 获取能力

- `templates/detail.html`
  - “传输预览”升级为“Shopify 二次编辑与发布”
  - 支持可编辑字段 + 提示词模板选择 + 手动改写按钮
  - 前端按字段分请求，允许部分成功回填

- `templates/settings_shops.html`
  - 店铺新增 OAuth 输入项
  - 列表支持删除店铺
  - 增加连接验证反馈

- `templates/settings_prompt_libraries.html`
  - 新增提示词库查看页面（英文模板 + 中文注释）

- `webapp/models.py` / `webapp/db.py`
  - `ShopifyShop` 新增 OAuth 字段
  - SQLite 迁移逻辑新增对应列兼容

- `webapp/services/collect.py` / `templates/index.html`
  - 列表新增序号列
  - ASIN 列表按最新更新时间降序展示

## 3) 文档同步范围

- `README.md`：功能概览、映射规则、OpenAI 使用说明、环境变量排障
- `docs/REQUIREMENTS.md`：与当前实现对齐
- `docs/PHASE2_PLAN.md`：阶段任务拆分
- `docs/PHASE2_CHANGELOG_BRIEF.md`：业务侧简版说明
- `docs/PROMPT_LIBRARY_ARCHIVE.md`：四类提示词完整归档
- `docs/DEPLOY_RETROSPECTIVE_2026-03.md`：线上部署与 OpenAI 可用性复盘

## 4) 本阶段高频问题与结论

- Shopify 401：
  - 常见根因是凭据类型混用（把 Client ID 当 token）或选错店铺
  - 解决策略：店铺页先“测试连接”，发布前核对店铺标识

- OpenAI 改写无效/超时：
  - 若接口 200 但文案变化小，优先检查 key/base/model 实际值
  - 服务器出口受限会触发 403（地区限制），需切换可达网关或出口
  - 四字段拆分调用可显著提升可用性（局部失败不阻断全量）

- 本地环境变量误用：
  - shell 中旧 `export` 会覆盖 `.env` 期望值
  - `.env` 若非标准 `KEY=VALUE` 可能无法 `source`

## 5) 后续建议（进入下一迭代前）

- 增加“当前 AI 提供方/模型”可视化标识（详情页可见）
- 为改写接口补充 `max_tokens` 与字段级上下文裁剪，继续降延迟
- 对发布 payload 做快照记录，便于审计“编辑值 vs 实发值”

## 6) 增量更新（首页与详情页体验优化）

本节对应后续新增需求：批量提交、分页与导航优化、改写内容持久化。

- 首页（`templates/index.html` + `webapp/main.py`）
  - 支持批量输入（多行/逗号/分号）
  - 重复 ASIN 提交仅刷新 `updated_at`，不自动重抓
  - 新增“加入并开始采集”（仅触发新增 ASIN）
  - 列表改为 50 条分页，支持序号、缩略图占位、当前页进度条

- 全局导航（`templates/base.html`）
  - 定义一级/二级结构：一级“首页”；二级“Shopify 店铺设置 / 提示词库 / 商品详情”
  - 各页面统一导航体验与高亮

- 详情页（`templates/detail.html`）
  - 面包屑精简为：采集列表 / 商品详情 / ASIN
  - 二次编辑模块重排：文案编辑 -> 价格参数 -> 发布配置+发布按钮
  - “商品规格与字段”默认收起，按需展开

- 改写结果持久化（`webapp/models.py` + `webapp/db.py` + `webapp/main.py`）
  - `Target` 新增 `shopify_editor_json`、`shopify_ai_rewritten_at`
  - AI 改写后自动落库；下次进入详情页优先回填
  - 页面显示“已 ChatGPT 改写”提示，明确内容来源

## 7) 增量更新（UPC + 元字段 + 更新发布）

- UPC 码池（`webapp/models.py` + `templates/settings_upc.html` + `webapp/main.py`）
  - 新增 `UpcCode` 表与 `/settings/upc` 管理页面
  - 批量录入（每行一个）+ 长度 12 位校验
  - 首次发布自动占用 UPC，成功后标记已使用并绑定目标/商品

- 发布模式升级（`webapp/main.py` + `webapp/shopify_service.py`）
  - 首次：创建 Shopify 商品
  - 后续：按钮改为“更新内容”，调用 `PUT /products/{id}.json` 更新同一商品
  - 更新时复用已绑定 UPC，不重复消耗新码

- 元字段链路（`templates/detail.html` + `webapp/shopify_service.py`）
  - `warehouse` / `delivery_time`：有默认值且可编辑
  - `specifications` / `qa`：默认空，可输入富文本
  - 写入方式统一改为 GraphQL `metafieldsSet`（创建/更新后执行）
  - 富文本输入支持 HTML 粘贴，后端转换为 `rich_text_field` JSON
