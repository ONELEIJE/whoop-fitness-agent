# 部署到阿里云 ECS

目标：一台最小规格的 ECS，从空系统到「飞书里能收到 WHOOP 卡片」。
全程只需要 SSH，**不需要域名、不需要备案、不需要开放 80/443**。

## 1. 选型

| 项 | 建议 | 说明 |
| --- | --- | --- |
| 实例规格 | 2 vCPU / 2 GiB（入门款 ecs.e/u 系列均可） | 1.7G 也能跑，但加 swap 更稳 |
| 系统盘 | 40 GiB ESSD | 镜像 + 日志 + SQLite，够用很久 |
| 镜像 | Ubuntu 22.04 / 24.04 LTS | 本文命令按 Ubuntu 写 |
| 带宽 | 1~3 Mbps 按量 | 只有出站 API 调用，流量很低 |
| 安全组 | 只放行 22（你的 IP） | 飞书是**出站**连接，不需要入站 Web 端口 |

> 成本参考：这类实例按量约几十元/月，占用几乎全在闲置状态，主要开销是磁盘。

## 2. 系统准备

```bash
sudo apt-get update && sudo apt-get install -y git curl
sudo timedatectl set-timezone Asia/Shanghai   # 代码里用 Asia/Shanghai 计算年龄/日期
timedatectl | head -3
```

内存小于 2G 时先加 swap（`install_ecs.sh` 会自动做）：

```bash
free -m
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## 3. 拉代码与配置

```bash
sudo mkdir -p /opt && cd /opt
sudo git clone https://github.com/ONELEIJE/whoop-fitness-agent.git
cd whoop-fitness-agent

cp .env.example .env
chmod 600 .env
vim .env          # 填飞书 / 模型 / WHOOP 三组密钥

cp examples/profile.example.json app/data/profile.json
vim app/data/profile.json   # 改成你自己的档案
chmod 600 app/data/profile.json
```

## 4. 先搞定 WHOOP 授权

`whoop_token.json` 不在仓库里，必须在服务器（或本地）跑一次授权。两种做法：

**做法 A：直接在 ECS 上授权（推荐）**

```bash
# 装 cloudflared（给 OAuth 回调一个临时 HTTPS 地址）
curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o /tmp/cf.deb
sudo dpkg -i /tmp/cf.deb

bash scripts/whoop_login.sh
# 屏幕会打印一个 https://xxx.trycloudflare.com 地址：
#   1) 去 WHOOP 后台把 Redirect URI 改成 <该地址>/callback
#   2) 浏览器打开该地址完成授权
```

**做法 B：本地授权后把 token 拷上去**

本地跑完 `scripts/whoop_login.sh`，再：

```bash
scp app/data/whoop_token.json root@<ecs-ip>:/opt/whoop-fitness-agent/app/data/
ssh root@<ecs-ip> 'chmod 600 /opt/whoop-fitness-agent/app/data/whoop_token.json'
```

两种都行。token 里带 refresh_token，能长期自动续期，所以只需要做一次。

> 详细原理、失败原因见 [03-whoop-oauth.md](03-whoop-oauth.md)。

## 5. 启动

```bash
sudo bash scripts/install_ecs.sh
# 或者手动：
docker compose up -d --build
docker compose ps
```

期望看到 `fitness-agent-bot` 状态是 `Up (healthy)`。

## 6. 验证

```bash
# 容器健康
curl -s localhost:5100/health
# {"status":"ok","database":"connected","service":"fitness-agent"}

# 数据链路（不经过飞书）
docker compose exec fitness-bot python -c "import whoop_client; print(whoop_client.get_whoop_dashboard()[:200])"

# 日志里应有 WS 连接记录
docker compose logs --tail 30
```

最后在飞书里发 `/whoop`，收到卡片即全链路打通。

## 7. 升级与回滚

```bash
cd /opt/whoop-fitness-agent
git pull
docker compose up -d --build       # 只重建有变化的层，通常 30 秒内
docker compose logs -f --tail 50
```

回滚：

```bash
git log --oneline -5
git checkout <上一个 commit>
docker compose up -d --build
```

`app/data/` 是挂载目录，升级回滚都不动数据。

## 8. 备份

SQLite 在 WAL 模式下不能直接 `cp`（可能拿到半写状态），用 SQLite 自带的备份：

```bash
mkdir -p /root/backups
docker compose exec -T fitness-bot python - <<'PY'
import sqlite3
src = sqlite3.connect("/app/data/fitness.db")
dst = sqlite3.connect("/app/data/fitness.db.bak")
src.backup(dst)
dst.close(); src.close()
print("ok")
PY
cp app/data/fitness.db.bak /root/backups/fitness_$(date +%Y%m%d).db
```

放进 crontab：

```bash
echo '20 3 * * * cd /opt/whoop-fitness-agent && docker compose exec -T fitness-bot python -c "import sqlite3;s=sqlite3.connect(\"/app/data/fitness.db\");d=sqlite3.connect(\"/app/data/fitness.db.bak\");s.backup(d);d.close()" && cp app/data/fitness.db.bak /root/backups/fitness_$(date +\%Y\%m\%d).db' | crontab -
```

如果想异地容灾，把备份传对象存储（OSS/S3）即可，代码不依赖，纯运维脚本就行。

## 9. 资源与稳定性

```bash
# 容器用了多少
docker stats --no-stream fitness-agent-bot

# 磁盘
df -h /  && du -sh /opt/whoop-fitness-agent/app/data

# 日志大小（compose 已限 10m × 3）
docker inspect fitness-agent-bot --format '{{.LogPath}}'
```

实测：常驻内存约 90–130 MiB，CPU 基本 0，收到消息时短时冲到 0.2~0.4 核。
`mem_limit: 512m` 的约束足够，也留了 buffer 给 Python 内存碎片。

## 10. 常见坑

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 容器反复重启 | `.env` 缺变量 / Python 语法错 | `docker compose logs --tail 100` 看 traceback |
| 飞书里机器人不回 | 没发布版本 / 可用范围没勾 / 事件没订阅 | 见 [04-feishu-bot.md](04-feishu-bot.md) |
| `/whoop` 提示获取失败 | token 缺失或过期 | 重跑 `whoop_login.sh` |
| 内存被打满（OOM） | 构建时 pip 峰值 + 无 swap | 加 swap，或 `docker compose build` 单独执行 |
| 时间是 UTC，日期差一天 | 容器没设时区 | compose 里已有 `TZ=Asia/Shanghai`；宿主也设一下 |
| 事件重复处理 | 容器内同时跑了两个长连接进程 | `docker ps` 确认只有一个实例 |
