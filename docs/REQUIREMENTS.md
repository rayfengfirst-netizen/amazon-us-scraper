# 需求说明（Amazon 美国站采集台）

## 1. 目标录入

- 用户在页面提交 **ASIN** 或 **Amazon 商品链接**（美国站）。
- 若为链接，需 **解析出 ASIN** 供后续流程使用。

## 2. 入库与采集节奏

- 目标数据 **先入库**，用户再 **手动发起采集**，避免批量任务一次性失败。
- 用户可查看每条记录的 **状态**（pending / running / success / failed）与错误信息。

## 3. 列表与详情

- **列表**：按 **ASIN 维度** 展示（同一 ASIN 多条提交时，列表展示最新一条），并提示是否已有 **ASIN 缓存快照**。
- **详情**：进入单条记录后查看采集结果；展示形式参考 **电商商品详情页**（左图右信息、要点、规格表等），并提供 **原始 JSON** 供调试。

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

## 7. 合规声明

- Amazon 与 ScraperAPI 均有各自服务条款与使用限制；使用者需自行确保用途合法合规。本项目仅为技术脚手架示例。
