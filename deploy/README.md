# 线上部署（端口 **8989**）

> 当前默认发布目标服务器：`root@8.221.99.46`（除非明确说明切换）。

## 路径与端口

| 项 | 值 |
|----|-----|
| 代码目录（建议） | `/opt/amazon-us-scraper` |
| 对外端口 | **8989**（`uvicorn --host 0.0.0.0 --port 8989`） |
| 进程 | systemd：`amazon-us-scraper.service` |

云厂商安全组需放行 **TCP 8989**（若只走 Nginx 反代，可改为监听 `127.0.0.1:8989` 并只开放 80/443）。

## 首次部署（服务器）

**若本机 `curl 127.0.0.1:8989` 为 Connection refused、且 `systemctl status amazon-us-scraper` 提示 unit 不存在**：说明尚未部署，按下面做即可。

### 方式 A：一键脚本（推荐）

在已 clone 的仓库目录下（或先 clone 再 `cd` 进去）：

```bash
cd /opt
sudo git clone https://github.com/rayfengfirst-netizen/amazon-us-scraper.git
cd /opt/amazon-us-scraper
sudo chmod +x deploy/bootstrap_server.sh
sudo bash deploy/bootstrap_server.sh
```

脚本会：`git pull`、venv、`pip install`、缺省复制 `.env`、安装并启动 `amazon-us-scraper`（**8989**）。

部署后务必编辑 `/opt/amazon-us-scraper/.env`，填入 **`SCRAPERAPI_KEY`**，否则页面能开但采集会失败：

```bash
nano /opt/amazon-us-scraper/.env
systemctl restart amazon-us-scraper
```

### 方式 B：手动（与 README 主文档一致）

```bash
sudo mkdir -p /opt && cd /opt
sudo git clone https://github.com/rayfengfirst-netizen/amazon-us-scraper.git
cd /opt/amazon-us-scraper
sudo python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env，至少填 SCRAPERAPI_KEY
sudo cp deploy/amazon-us-scraper.service.example /etc/systemd/system/amazon-us-scraper.service
sudo systemctl daemon-reload
sudo systemctl enable --now amazon-us-scraper
curl -sS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8989/
```

## 日常发布

```bash
sudo cp deploy/deploy.sh.example /opt/amazon-us-scraper/deploy/deploy.sh
sudo chmod +x /opt/amazon-us-scraper/deploy/deploy.sh
sudo bash /opt/amazon-us-scraper/deploy/deploy.sh
```

或与 myapp 一样手动：

```bash
cd /opt/amazon-us-scraper && git pull && source .venv/bin/activate && pip install -r requirements.txt && sudo systemctl restart amazon-us-scraper
```

## 与其它服务同机

当前机已占用示例：80（nginx/myapp）、8010（spelab）、8765（winit viewer）。**8989** 专用于本应用，勿与上述冲突。

---

## 复盘与排障

本次上云过程（Mac 误在 `/opt` 操作、未 push 导致无 `deploy/`、首页 500、`.env`/systemd 等）的**完整梳理**见：

- [docs/DEPLOY_RETROSPECTIVE_2026-03.md](../docs/DEPLOY_RETROSPECTIVE_2026-03.md)

运维侧摘要见工作区 **`server-ops/reports/2026-03-26-amazon-us-scraper-deploy.md`**。

**快速自检**：

```bash
systemctl is-active amazon-us-scraper
curl -sS http://127.0.0.1:8989/health
curl -sS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8989/
```
