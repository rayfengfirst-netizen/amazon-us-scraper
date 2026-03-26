# 线上部署复盘（2026-03）

本文记录 **amazon-us-scraper** 首次上云（`8.218.58.28`，端口 **8989**）过程中出现的问题、根因与最终修复，便于后续部署与排障。

---

## 1. 背景与目标

- **应用**：FastAPI + Uvicorn，SQLite 数据与图片在 `data/`，依赖 **ScraperAPI**（`SCRAPERAPI_KEY`）。
- **约定**：监听 **`0.0.0.0:8989`**，systemd 单元名 `amazon-us-scraper.service`，代码目录 **`/opt/amazon-us-scraper`**。

---

## 2. 问题与根因（按时间线）

### 2.1 在 Mac 上执行 `cd /opt && git clone …`

- **现象**：`fatal: could not create work tree dir 'amazon-us-scraper': Permission denied`
- **根因**：命令在 **本机 macOS** 执行；`/opt` 通常需 root 或不可写。**部署命令应在 SSH 登录后的 Linux 服务器上执行**，不要在 Mac 的 `/opt` 操作。

### 2.2 服务器上 `deploy/bootstrap_server.sh` 不存在

- **现象**：`chmod: cannot access 'deploy/bootstrap_server.sh'`
- **根因**：`deploy/` 等文件**尚未提交并 push 到 GitHub**，服务器 `git clone` 只有初始 commit。  
- **处理**：本地 `git add deploy/` + `git commit` + `git push origin main`，服务器再 `git pull`。

### 2.3 首次 bootstrap 后 `curl` 首页 500

- **现象**：服务 `active`，`ss` 显示 **8989** 在监听，但 `curl http://127.0.0.1:8989/` 返回 **HTTP 500**。
- **根因（综合）**：
  1. **Starlette** 新版推荐 `TemplateResponse(request, name, context)`；旧写法 `TemplateResponse("index.html", {"request": request, ...})` 在部分版本组合下易引发异常或不稳定。
  2. **systemd `EnvironmentFile=`** 加载项目根 `.env` 时，若注释/编码与 systemd 解析规则不一致，可能带来环境变量异常；**改为仅由应用内 `python-dotenv` 加载 `.env`** 更一致。
- **处理**：
  - 修改 `webapp/main.py` 全部模板为 **request 优先** 的 `TemplateResponse`。
  - 在 `webapp/db.py` 中 **`load_dotenv(PROJECT_ROOT / ".env")`**。
  - 从 `amazon-us-scraper.service.example` 中**移除 **`EnvironmentFile=`**。
  - 新增 **`GET /health`** 探活（无模板、无复杂 DB），`bootstrap_server.sh` 先测 `/health` 再测 `/`。

### 2.4 公网 `8989` 无法访问 / 行为异常

- **现象**：浏览器打不开或早期探测异常。
- **根因**：
  - **未部署**：本机 `Connection refused` → 无进程监听。
  - **已部署但安全组未放行**：服务器内 `curl` 正常，公网不通 → 需在云厂商放行 **TCP 8989**。
- **处理**：确认 `systemctl` + 本机 `curl` 后，再检查 **安全组/防火墙**。

### 2.5 Cursor Agent 无法代 SSH

- **现象**：`Permission denied (publickey,password)`。
- **根因**：私钥带 passphrase 时未加入 `ssh-agent`，或 Agent 执行环境读不到私钥。  
- **见**：`server-ops/SSH_FOR_AGENT.md`（非本仓库，运维备忘）。

---

## 3. 配置要点（`.env`）

- 路径：**`/opt/amazon-us-scraper/.env`**（由 `.env.example` 复制）。
- **必填**：`SCRAPERAPI_KEY=...`（采集依赖；页面可打开但采集会失败）。
- 修改后执行：`systemctl restart amazon-us-scraper`。

---

## 4. 日常发布（服务器）

```bash
cd /opt/amazon-us-scraper
git pull --ff-only origin main
cp -f deploy/amazon-us-scraper.service.example /etc/systemd/system/amazon-us-scraper.service   # unit 有变更时
systemctl daemon-reload
source .venv/bin/activate && pip install -r requirements.txt
systemctl restart amazon-us-scraper
curl -sS http://127.0.0.1:8989/health
```

---

## 5. 相关文档与提交

| 文件 | 说明 |
|------|------|
| `deploy/README.md` | 端口、首次/日常部署、与其它服务同机 |
| `deploy/bootstrap_server.sh` | 一键首次部署 |
| `deploy/amazon-us-scraper.service.example` | systemd 模板（无 `EnvironmentFile`，依赖应用内 dotenv） |
| `webapp/main.py` | `TemplateResponse`/`/health` |
| `webapp/db.py` | `load_dotenv` |

Git 中修复合入：`main` 分支（含 `fix: Starlette TemplateResponse API, load_dotenv, /health probe` 等）。

---

## 6. 修订记录

| 日期 | 说明 |
|------|------|
| 2026-03-26 | 初稿：复盘 Mac/服务器混淆、未 push、HTTP 500、env 与安全组、Agent SSH |
