# 接 WHOOP：开发者应用与 OAuth 授权

WHOOP 的数据要经过 OAuth2 授权才拿得到。这一段只做**一次**，之后交给
`whoop_client.py` 自动续期。

## 1. 创建 WHOOP 开发者应用

1. 打开 WHOOP Developer Dashboard（`developer.whoop.com`），用你的 WHOOP 账号登录。
2. 创建一个 App，拿到 **Client ID** 和 **Client Secret**。
3. 填 **Redirect URI**：必须是 `https://`，并且要和代码里发出的值**一字不差**。
   例如 `https://abc-def-ghi.trycloudflare.com/callback`。
4. 勾选 scopes（见下）。

把 Client ID / Secret 写进 `.env`：

```dotenv
WHOOP_CLIENT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
WHOOP_CLIENT_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

## 2. 申请的权限（scopes）

代码固定申请这一组（`app/whoop_auth.py` 里的 `SCOPES`）：

| scope | 拿到什么 | 用在哪 |
| --- | --- | --- |
| `offline` | refresh_token | 长期自动续期，**必须有** |
| `read:recovery` | 恢复分、HRV、静息心率、血氧、皮温、呼吸频率 | `/whoop` 卡片、教练建议 |
| `read:sleep` | 睡眠时长、分期（浅睡/深睡/REM）、效率、小睡 | 睡眠卡与恢复判断 |
| `read:cycles` | 生理周期、步数 | 周期信息 |
| `read:workout` | 运动类型、时长、Strain、心率、消耗 | 训练记录自动匹配 |
| `read:body_measurement` | 身高、体重、最大心率 | 个性化建议 |

少一个 scope 不会崩，但那部分字段会返回 `null`——代码里对空值是容错的。

## 3. 为什么要 `redirect_uri` 是 https

WHOOP 只接受 HTTPS 回调，而个人服务器往往没有域名/证书。这里用 **Cloudflare 快速隧道**
临时给 `127.0.0.1:9876` 一个公网 HTTPS 地址：

```bash
bash scripts/whoop_login.sh
```

脚本做的事：

```text
1. cloudflared tunnel --url http://127.0.0.1:9876
   → 打印 https://xxxx.trycloudflare.com
2. 把 https://xxxx.trycloudflare.com/callback 写进 app/data/whoop_auth_config.json
3. 起 app/whoop_auth.py（监听 127.0.0.1:9876）
4. 你在 WHOOP 后台把 Redirect URI 改成同一个地址
5. 浏览器打开 https://xxxx.trycloudflare.com → WHOOP 授权页 → 回调 → 换 token
```

`whoop_auth.py` 的几个细节：

- **state 防 CSRF**：随机 32 字节，10 分钟有效，一次性消费
- **回调日志脱敏**：`code=` / `token=` 一律打码，日志里不出现凭证
- **原子写**：先写临时文件 + `fsync`，再 `rename`，最后 `chmod 600`
- 令牌写 `app/data/whoop_token.json`

### 有域名时的替代方案

把隧道换成 nginx 反代 + certbot：

```nginx
server {
    listen 443 ssl http2;
    server_name whoop.example.com;

    ssl_certificate     /etc/letsencrypt/live/whoop.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/whoop.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:9876;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

然后 `redirect_uri` 填 `https://whoop.example.com/callback`。
长期用建议这样，比每次换隧道地址省心。

## 4. token 长什么样

```json
{
  "access_token": "eyJ...",
  "refresh_token": "...",
  "expires_in": 3599,
  "expires_at": 1793159202,
  "saved_at": 1793155603,
  "scope": "offline read:recovery ...",
  "token_type": "bearer"
}
```

`expires_at` 由 `saved_at + expires_in` 算出，代码在**过期前 60 秒**就主动刷新。

## 5. 授权的次数：一次，然后永远自动

这是最容易被误解的一点，先说结论：

| 令牌 | 有效期 | 谁负责续期 |
| --- | --- | --- |
| `access_token` | 约 1 小时（WHOOP 返回 `expires_in≈3600`） | `whoop_client.get_valid_token()` 在过期前 60 秒用 refresh_token 换新 |
| `refresh_token` | 每次刷新都会**轮换**出新值，旧值随即失效 | 换新后原子写回 `whoop_token.json`，无需人工介入 |

也就是说：**只要完成一次浏览器授权，之后跑多久都不用再登录。**
需要重新授权的只有三种情况：

1. 令牌文件被删 / 丢失 / 迁移服务器时没带走
2. 你在 WHOOP 账号里撤销了这个 App 的授权
3. `WHOOP_CLIENT_SECRET` 被更换（旧令牌对应旧应用凭据）

> 具体有效期以 [WHOOP 开发者文档](https://developer.whoop.com/docs/developing/oauth/) 为准；
> 代码不依赖固定时长，而是每次按返回的 `expires_in` 计算。

## 6. 自动续期是怎么做的

```text
get_valid_token()
├── 本地 expires_at > now + 60  → 直接用
└── 否则
    ├── 拿进程内锁 _refresh_lock
    ├── 抢到锁后**重新读一次**文件（别的线程可能刚刷完）
    ├── 仍然过期 → POST /oauth/oauth2/token (grant_type=refresh_token)
    └── 新的 access_token + refresh_token 原子写回
```

三个关键点，缺一个就会在线上出事：

1. **锁**：飞书是并发处理消息的，两个线程同时刷新会互相把对方的 refresh_token 作废
2. **双重检查**：拿到锁后必须重读 token 文件，否则会用旧 refresh_token 再刷一次
3. **原子写**：`rename` 保证任何时刻文件都是完整的（进程被杀也不会写坏）

另外 `_get()` 里对 **401 强制刷新一次**再重试，因为 WHOOP 偶尔会在本地看似未过期时拒绝令牌。

## 7. 排障

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `invalid_client` | Client ID/Secret 错，或环境变量没进容器 | `docker compose exec fitness-bot env | grep WHOOP` |
| `invalid_grant` | refresh_token 失效：被别的进程轮换过、被撤销、或长期未使用 | 删 `whoop_token.json` 重新授权 |
| 授权页报 `redirect_uri` 不匹配 | 后台登记值和实际发出的不一致（含末尾斜杠、http/https） | 逐字符对齐 |
| 401 反复出现 | 系统时间偏差过大 | `timedatectl` 开 NTP |
| 429 | 触发限流 | 代码按 `Retry-After` 退避，最多 3 次；不要高频轮询 |
| 页面 502 | 隧道通了但 `whoop_auth.py` 没起 | 看终端是否有 `Listening on 127.0.0.1:9876` |
| `whoop_token.json` 权限不是 600 | 手工拷贝过 | `chmod 600` |

## 8. 安全提醒

- `whoop_token.json` **等同密码**：能读你全部健康数据。不要提交、不要贴图、不要放公共网盘。
- `client_secret` 同样只放 `.env`（已 gitignore）。
- 令牌文件权限保持 `600`，并且只挂载给这一个容器。
- 不再想授权时，去 WHOOP 账号设置里撤销该 App 的访问。
