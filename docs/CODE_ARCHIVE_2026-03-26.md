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

## 8) 增量更新（同步、富文本增强与稳定性修复）

- 同步能力（`webapp/main.py` + `webapp/shopify_service.py`）
  - 已发布商品进入详情页时自动从 Shopify 拉取并刷新本地编辑态
  - 新增“同步 Shopify”按钮，支持手动回拉最新线上内容
  - 同步结果透传到页面，便于运营确认是否成功

- 富文本能力（`templates/detail.html` + `webapp/shopify_service.py`）
  - 编辑器初始化提前到页面加载阶段，避免“未点改写前富文本不生效”
  - 工具栏新增 H2/H3，支持 H 标签输入
  - 新增 `_rich_text_json_to_html`，打通 Shopify rich text JSON -> 本地 HTML 的反向回填
  - 新增元字段：`vehicle_fitment`、`package_list`（富文本）

- 元字段类型适配（`webapp/shopify_service.py`）
  - `metafieldsSet` 返回 `INVALID_TYPE` 时，解析 userError 中期望类型并自动重试
  - 对 `warehouse`、`delivery_time` 增加 REST upsert 兜底，降低后台“写入成功但不可见”的概率
  - namespace 改为按字段可配置，兼容店铺中不同定义（如 `custom` / `custom1`）

- 页面稳定性（`webapp/main.py`）
  - 修复详情页 `DetachedInstanceError`
  - 根因：session commit 后模板仍访问 ORM lazy 属性
  - 修复：在 session 内将 `Target` / `ShopifyPublishLog` / 同 ASIN 列表拷贝为普通 dict，再传给模板

## 9) 问题 -> 根因 -> 修复（本轮复盘）

- `warehouse` 不显示
  - 根因：字段定义 namespace/type 与写入不一致，且后台展示有定义依赖
  - 修复：字段级 namespace 配置 + GraphQL 写入后 REST 兜底

- 富文本丢样式（仅剩换行）
  - 根因：编辑器初始化时机不对 + 转换器未完整覆盖样式语义
  - 修复：页面加载即初始化；转换器补齐 bold/italic/list/heading

- H 标签不生效
  - 根因：前端未提供 heading 操作，后端未完整回转 heading 节点
  - 修复：前端加 H2/H3，后端 heading 双向转换

- 详情页 500（DetachedInstanceError）
  - 根因：模板渲染期访问已游离 ORM 对象
  - 修复：模板上下文改为纯 Python 结构

## 10) 当前遗留项（下一步）

- 页面可视化展示 `metafieldsSet.userErrors` 细节（字段路径 + code + message），减少“后台未生效但前端无感知”排障成本

## 11) 增量更新（提示词库可维护 + 详情页交互优化）

- 提示词库管理（`webapp/prompt_library.py` + `webapp/main.py` + `templates/settings_prompt_libraries.html`）
  - 新增提示词模板新增/编辑/删除能力（文件持久化到 `libraries.json`）
  - 模板 ID 改为系统自动生成且固定，避免人工维护错误
  - 修复改写 500：模板字符串格式化改为安全模式，未知占位符不再抛 `KeyError`
  - 兼容历史模板占位符：`{title}`、`{description}`

- 同步策略调整（`webapp/main.py` + `webapp/shopify_service.py`）
  - 详情页进入/刷新默认不自动同步 Shopify，仅读本地草稿
  - 保留“从shopify同步产品信息”按钮作为唯一同步入口
  - 同步时增加“远端空值不覆盖本地已有值”保护
  - SEO 字段增加 metafield 兜底读取（`global.title_tag` / `global.description_tag`）

- 详情页编辑体验（`templates/detail.html` + `templates/base.html`）
  - 商品描述支持 `HTML源码 / 可视预览` 双视图切换，默认预览
  - 描述编辑区域高度提升，长内容更易审阅
  - SEO 标题与 SEO 描述新增实时字符计数（70/160）
  - 富文本元字段工具栏从 H2/H3 扩展到 H1~H5
  - 详情页顶部新增醒目的 Shopify 同步状态标识（已同步/未同步）

- 列表与视觉一致性（`webapp/main.py` + `templates/index.html` + `templates/base.html` + `templates/settings_*`）
  - 列表页新增 Shopify 状态列（已发布/未发布/发布失败）
  - 全站样式收敛：统一提示条（success/error）、危险按钮、输入宽度与通用工具类
