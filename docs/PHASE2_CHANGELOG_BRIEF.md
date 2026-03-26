# Phase 2 变更清单（简版）

适用对象：开发 / 运营 / 协作同学  
版本节点：`44e74fb`

## 1) 页面与流程

- ASIN 详情页升级为 **Shopify 二次编辑后台**，可直接编辑后发布
- 原“传输预览”弱化，改为更贴近刊登后台的高效编辑体验
- 发布前可手动点击 **ChatGPT 改写四项**（标题、描述、SEO 标题、SEO 描述）
- 提交发布时不再自动 AI 改写，避免不可控耗时

## 2) Shopify 发布规则（当前）

- `vendor` 固定：`EGR Performance`
- `tags`：空字符串
- `SKU`：
  - Amazon：`AM-{ASIN}`
  - eBay：`EB-{item_id}`
- `price`：采集价 * `1.7`（可在页面继续编辑）
- 图片：使用高分图 URL 直传（不走本地 attachment）

## 3) 数据与采集策略

- 详情页读取数据库缓存结果，不依赖每次重新采集
- 图片下载策略收敛为：仅下载第一张高分图（降低资源开销）

## 4) 提示词库体系

- 新增提示词库配置文件：`prompts/shopify_openai/libraries.json`
- 每个模板包含四类提示词：标题、描述、SEO 标题、SEO 描述
- 每类同时支持：英文模板 + 中文注释
- 新增查看页面：`/settings/prompt-libraries`
- 详情页改写时可按“模板名”选择提示词库

## 5) 本轮文档产出

- `docs/PHASE2_PLAN.md`：第二阶段规划（范围、优先级、DoD）
- `docs/PROMPT_LIBRARY_ARCHIVE.md`：提示词完整归档
- `README.md`、`docs/REQUIREMENTS.md`：与当前实现对齐

## 6) 建议下一步（短期）

- 把 `vendor / 价格系数 / SKU 规则` 改成“店铺级可配置”
- 给提示词库增加“新增/编辑 UI”（目前以 JSON 管理为主）
- 补一组回归测试：编辑发布、AI 改写、多模板切换、eBay SKU 提取
