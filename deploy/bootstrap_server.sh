#!/usr/bin/env bash
# 在服务器上以 root 执行（首次部署 amazon-us-scraper → 监听 8989）
# 用法：bash deploy/bootstrap_server.sh
# 若代码已在 /opt/amazon-us-scraper，cd 到该目录后执行即可。
set -euo pipefail

REPO_URL="${AMAZON_SCRAPER_REPO_URL:-https://github.com/rayfengfirst-netizen/amazon-us-scraper.git}"
INSTALL_DIR="${AMAZON_SCRAPER_DIR:-/opt/amazon-us-scraper}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "请使用 root 执行：sudo bash deploy/bootstrap_server.sh"
  exit 1
fi

mkdir -p /opt
if [[ ! -d "${INSTALL_DIR}/.git" ]]; then
  echo ">>> clone -> ${INSTALL_DIR}"
  git clone "${REPO_URL}" "${INSTALL_DIR}"
fi

cd "${INSTALL_DIR}"
echo ">>> git pull"
git pull --ff-only origin main || git pull --ff-only origin master

echo ">>> venv + pip"
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
# shellcheck source=/dev/null
source .venv/bin/activate
pip install -q -U pip
pip install -q -r requirements.txt

if [[ ! -f .env ]]; then
  echo ">>> 创建 .env（从 .env.example），请稍后编辑填入 SCRAPERAPI_KEY"
  cp .env.example .env
fi

echo ">>> systemd"
cp -f deploy/amazon-us-scraper.service.example /etc/systemd/system/amazon-us-scraper.service
systemctl daemon-reload
systemctl enable amazon-us-scraper.service
systemctl restart amazon-us-scraper.service

sleep 1
systemctl is-active --quiet amazon-us-scraper.service
echo ">>> service: $(systemctl is-active amazon-us-scraper.service)"

echo ">>> listen:"
ss -tlnp | grep 8989 || true

echo ">>> curl /health + /"
if ! curl -sfS --max-time 5 http://127.0.0.1:8989/health | grep -q ok; then
  echo "health 失败，请看日志: journalctl -u amazon-us-scraper -n 80 --no-pager"
  exit 1
fi
code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 10 http://127.0.0.1:8989/ || echo "000")
echo "GET / -> HTTP ${code}"
if [[ "$code" != "200" ]]; then
  echo "首页非 200，请看日志: journalctl -u amazon-us-scraper -n 80 --no-pager"
  exit 1
fi
echo "OK: 本机可访问。公网请确认安全组已放行 TCP 8989。"
