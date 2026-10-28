# 接入飞书：自建应用 + 长连接

目标：飞书里有一个机器人，单聊或群里 @ 它就能对话。
用**长连接**接收事件，所以不需要公网地址、不需要内网穿透、不需要在飞书后台填请求 URL。

## 1. 创建应用

1. 打开 [飞书开放平台](https://open.feishu.cn/app) → 创建企业自建应用。
2. 填名称、描述、图标（随便，之后能改）。
3. 进入应用 → **添加应用能力** → 打开「机器人」。
4. 「凭证与基础信息」里拿 **App ID** / **App Secret**，写进 `.env`：

```dotenv
FEISHU_APP_ID=cli_xxxxxxxxxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

## 2. 开权限

「权限管理」里搜索并勾选：

| 权限 | 作用 | 必需 |
| --- | --- | --- |
| `im:message` | 获取与发送单聊、群组消息 | ✅ |
| `im:message:send_as_bot` | 以应用身份发消息 | ✅ |
| `im:message.p2p_msg:readonly` | 读取用户发给机器人的单聊消息 | ✅ |
| `im:message.group_at_msg:readonly` | 获取群里 @ 机器人的消息 | 群里用就勾 |
| `im:chat:readonly` | 读群信息 | 可选 |

本项目只用文本消息和消息 API，不需要通讯录、云文档、审批等任何其它权限。
权限越小，审核越快，出问题也越好排查。

## 3. 订阅事件（关键一步）

「事件与回调」→ **订阅方式** → 选 **「使用长连接接收事件」**
（不要选「将事件发送至开发者服务器」，那是 Webhook 模式）

然后「添加事件」→ 搜索并添加：

```text
接收消息  im.message.receive_v1   (v2.0)
```

只订阅这一个事件就够了。卡片按钮回调（`card.action.trigger`）本项目没用。

## 4. 发布

1. 「版本管理与发布」→ 创建版本（填版本号、更新说明）。
2. 申请发布。企业自建应用一般由企业管理员直接通过。
3. 「可用范围」选自己（或需要的部门/成员）。

**没发布、或可用范围里没有你，机器人就是不会回消息**——这是最常见的「配好了但不理我」。

## 5. 用起来

- **单聊**：在飞书搜索框搜应用名 → 直接发消息
- **群聊**：把机器人拉进群 → **@机器人** 再发（群里默认只推送 @ 的消息）

发 `/help` 或 `/status` 试一下。

## 6. 代码侧是怎么连的

`app/bot.py` 的 `main()`：

```python
handler = lark.EventDispatcherHandler.builder("", "") \
    .register_p2_im_message_receive_v1(handle_message) \
    .build()

ws = lark.ws.Client(app_id=..., app_secret=..., event_handler=handler)
ws.start()          # 阻塞，内部自动重连
```

几个要点：

- 用的是官方 `lark-oapi` SDK（`requirements.txt` 锁版本），长连接由 SDK 维护，断线自动重连
- **同一个应用只能有一个长连接进程**。多副本会互抢事件，导致「有时回有时不回」
- 事件处理函数里做了幂等（`message_state` 表），因为飞书会重投事件
- 发消息用 `cli.im.v1.message.reply(req)`（回复原消息）或 `create`（另发一条新消息）

## 7. 卡片

`/whoop`、`/月报`、训练计划都用交互卡片（`msg_type: interactive`），内容是：

```json
{
  "config": { "wide_screen_mode": true },
  "header": { "template": "green", "title": { "tag": "plain_text", "content": "..." } },
  "elements": [
    { "tag": "div", "text": { "tag": "lark_md", "content": "**恢复** 62% ..." } },
    { "tag": "hr" }
  ]
}
```

卡片的好处：手机上排版稳定、能分区块、颜色能表达状态（绿=状态好、橙=注意、红=差）。
构建函数在 `bot.py` 里：`_build_whoop_card()` / `_build_monthly_card()` / `_build_single_card()`。

## 8. 报错速查

| 报错 / 现象 | 含义 | 处理 |
| --- | --- | --- |
| 日志 `code=99991663` | 应用未发布 / 不在可用范围 | 发布版本并勾选自己 |
| `code=99991672` | 权限不足 | 回到「权限管理」补权限，然后**重新发布版本** |
| `code=230002` | 机器人不在该会话 | 把机器人拉进群，或改用单聊 |
| `code=99991400` | 发消息频率超限 | 降低发送频率，别刷屏 |
| 机器人完全没反应 | 事件订阅不是长连接 / 没加 `im.message.receive_v1` | 检查订阅方式与事件列表 |
| 改完代码行为没变 | 没重建镜像 | `docker compose up -d --build` |
| 偶尔重复回复 | 两个进程同时连 | `docker ps` 只留一个实例 |

SDK 的错误码会打印在容器日志里：

```bash
docker compose logs -f --tail 100 | grep -E "code=|error"
```

## 9. 上线前自检

- [ ] 应用已发布，可用范围包含你自己的账号
- [ ] 事件订阅方式 = 长连接，事件列表含 `im.message.receive_v1`
- [ ] `.env` 里 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` 与后台一致
- [ ] 容器日志出现 WebSocket 连接成功，没有反复重连
- [ ] `/status` 有回复
- [ ] `/whoop` 能出卡（说明 WHOOP token 也正常）
- [ ] 发一句「今天练了胸，卧推 60kg 4x8」能走完确认流程
- [ ] 群里 @ 机器人也能回（如果要用群）
