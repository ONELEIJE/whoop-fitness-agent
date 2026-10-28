# 运维手册

## 1. 日常巡检

```bash
cd /opt/whoop-fitness-agent

# 1) 进程活着、健康检查通过
docker compose ps
curl -s localhost:5100/health

# 2) 最近日志有没有反复报错
docker compose logs --since 24h | grep -E "ERROR|Traceback|FAIL|code=" | tail -20

# 3) 资源
docker stats --no-stream fitness-agent-bot
df -h / | tail -1
```

正常情况下日志里应该只有这三类：

```text
Starting Fitness Agent via WebSocket...
WHOOP_REQUEST | time=... | endpoint=all_summaries | status=200 | ...
Training log v2 saved: date=... type=... whoop_matched=True msg=...
```

## 2. 关键日志与它们的含义

| 日志 | 含义 | 该不该管 |
| --- | --- | --- |
| `WHOOP_REQUEST ... status=200` | 每次拉 WHOOP 都有一行，含记录日期 | 正常 |
| `WHOOP token refreshed successfully` | 令牌续期成功（约每小时一次） | 正常 |
| `WHOOP API 429, waiting Ns` | 触发限流，已自动退避 | 偶发正常，频繁则查调用频率 |
| `Duplicate event skipped` | 飞书重投被幂等挡住 | 正常 |
| `Pending action stored` | HTTP 通道产生待确认 | 正常 |
| `DeepSeek error for ...` | 模型调用失败 | 连续出现要查 key/网络 |
| `Card reply API error: code=99991672` | 飞书权限不足 | 补权限并重新发布版本 |
| `Failed to save training log v2` | 数据库写失败 | 查磁盘/inode/权限 |

## 3. 备份与恢复

### 备份（推荐每天）

```bash
cd /opt/whoop-fitness-agent
mkdir -p /root/backups

# SQLite 在线备份（WAL 模式下不能直接 cp）
docker compose exec -T fitness-bot python -c "
import sqlite3
s = sqlite3.connect('/app/data/fitness.db'); d = sqlite3.connect('/app/data/fitness.db.bak')
s.backup(d); d.close(); s.close()"

cp app/data/fitness.db.bak "/root/backups/fitness_$(date +%Y%m%d).db"
cp app/data/profile.json  "/root/backups/profile_$(date +%Y%m%d).json"
ls -lh /root/backups | tail -5
```

`.env` 和 `whoop_token.json` 也建议单独离线保存（它们是凭证，别放公共网盘）。

### 恢复

```bash
docker compose down
cp /root/backups/fitness_20261028.db app/data/fitness.db
rm -f app/data/fitness.db-wal app/data/fitness.db-shm   # 清掉旧的 WAL
docker compose up -d
```

`whoop_token.json` 丢了不用慌，重跑一次 `scripts/whoop_login.sh` 即可。

## 4. 升级

```bash
cd /opt/whoop-fitness-agent
git status                 # 本地不应有未提交改动
git pull
docker compose up -d --build
docker compose logs -f --tail 50
```

数据兼容：`_ensure_schema()` 是幂等的，只加表/加列，不删不改。
升级后第一次启动会自动补列，看到 `ALTER TABLE` 相关日志属正常（只发生一次）。

## 5. 故障速查

| 现象 | 先看什么 | 常见原因 |
| --- | --- | --- |
| 机器人不回消息 | `docker compose ps` + `logs` | 容器挂了 / 应用未发布 / 事件订阅不是长连接 |
| 回「AI 调用暂时不可用」 | 日志里的 `DeepSeek error` | key 失效、余额不足、base_url 写错 |
| `/whoop` 说获取失败 | `python scripts/whoop_dump.py` | token 过期、scope 不足、网络不通 |
| 训练记录重复 | `SELECT * FROM training_logs ORDER BY id DESC LIMIT 5` | 用户重复发送 / 幂等失效（检查是否多实例） |
| 训练记录没落库 | 日志 `Failed to save` | 磁盘满 / 文件权限 / 表结构不匹配 |
| 卡片发不出 | 日志 `code=...` | 权限、机器人不在会话、限流 |
| 内存持续上涨 | `docker stats` | 长历史/大卡片导致，重启治标，需查泄漏 |
| 时间不对 | `date`、`docker compose exec fitness-bot date` | 宿主时区 / 容器 TZ |

## 6. 安全清单

上线前对着打勾：

- [ ] `.env` 权限 `600`，不在仓库里（`git status` 确认）
- [ ] `app/data/` 全部在 `.gitignore` 里
- [ ] `whoop_token.json` 权限 `600`
- [ ] 安全组只放行 22，且限来源 IP
- [ ] 没有把 5100 端口映射到 `0.0.0.0`
- [ ] 容器不带 `privileged`、不挂 `docker.sock`
- [ ] 日志里搜不到 `access_token` / `refresh_token` / `app_secret`
- [ ] 飞书应用权限只给了消息相关的最小集合
- [ ] 定期 `docker compose pull`/`build` 更新基础镜像（`python:3.11-slim` 安全补丁）

快速自查：

```bash
git -C /opt/whoop-fitness-agent status --porcelain          # 应为空
grep -rIl "sk-\|cli_\|LTAI" /opt/whoop-fitness-agent --exclude-dir=.git --exclude-dir=data || echo "no secrets in tree"
docker compose logs | grep -iE "token=|secret=|code=" || echo "no secrets in logs"
```

## 7. 容量与成本

| 项 | 实测 |
| --- | --- |
| 镜像大小 | 约 250 MB（python:3.11-slim + 4 个依赖） |
| 常驻内存 | 90–130 MiB |
| 磁盘增长 | 每千条训练记录 < 1 MB；日志按 10m×3 轮转封顶 |
| WHOOP 调用 | 每次 `/whoop` 约 5 个请求；日常被动拉取很少 |
| 模型调用 | 每条消息 1 次，约 1–3k tokens |

小机器上次要的优化只有一条：**别在容器里装编译工具**，依赖都是纯 Python，无需 gcc。

## 8. 可选：加个外部探活

容器自带 healthcheck，但宿主机整体挂了没人知道。最省事的做法是云监控拨测
`https://<你的面板或反代>/health`，或者用任意 cron + 飞书自定义机器人发告警：

```bash
# /etc/cron.d/fitness-watch
*/5 * * * * root curl -fsS --max-time 5 http://127.0.0.1:5100/health >/dev/null || \
  curl -s -X POST "https://open.feishu.cn/open-apis/bot/v2/hook/<你的webhook>" \
    -H 'Content-Type: application/json' \
    -d '{"msg_type":"text","content":{"text":"⚠️ fitness-agent health check failed"}}'
```

## 9. 数据流向（隐私自查用）

```text
WHOOP API ──→ whoop_client ──→ bot.py ──→ 你的 SQLite（本地）
                                  │
                                  └──→ 模型服务商：仅「本次问题 + 最近 10 轮对话 + 本次 WHOOP 摘要 + 个人档案」
```

也就是说：完整历史、原始 API 响应、训练明细都只在本地库；发给模型的是摘要级数据。
如果这也不能接受，可以把 `DEEPSEEK_BASE_URL` 指向自建模型服务（任何 OpenAI 兼容网关均可）。
