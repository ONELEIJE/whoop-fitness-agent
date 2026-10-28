#!/usr/bin/env bash
# 阿里云 ECS（Ubuntu 22.04 / 24.04）一键准备并启动 Fitness Agent。
#
# 做的事：装 Docker → 小内存机器加 swap → 校验 .env → 构建启动 → 跟日志。
# 用法：
#   git clone <your-repo> whoop-fitness-agent && cd whoop-fitness-agent
#   cp .env.example .env && vim .env        # 先填好密钥
#   sudo bash scripts/install_ecs.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== 0/4 检查配置 =="
if [ ! -f .env ]; then
  echo "缺少 .env：先 cp .env.example .env 并填好飞书 / 模型 / WHOOP 三组密钥"
  exit 1
fi
if grep -q "xxxxxxxx" .env; then
  echo "⚠️  .env 里还有 xxxx 占位符，确认已经全部替换"
fi

echo "== 1/4 安装 Docker（阿里云镜像源）=="
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh -s -- --mirror Aliyun
  systemctl enable --now docker
else
  echo "   已安装: $(docker --version)"
fi
if ! docker compose version >/dev/null 2>&1; then
  apt-get update -qq && apt-get install -y docker-compose-plugin
fi

echo "== 2/4 内存检查（2G 以下加 2G swap）=="
mem_mb=$(awk '/MemTotal/{printf "%d", $2/1024}' /proc/meminfo)
echo "   当前内存: ${mem_mb} MB"
if [ "$mem_mb" -lt 2048 ] && [ ! -f /swapfile ]; then
  echo "   加 2G swap…"
  fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "== 3/4 构建并启动 =="
docker compose up -d --build

echo "== 4/4 状态与日志 =="
docker compose ps
echo
echo "  走飞书通道：在飞书里给机器人发一条消息，容器日志应出现 WebSocket 连接与消息处理记录"
echo "  走 HTTP 通道：curl -s localhost:5100/health"
echo
docker compose logs -f --tail 50
