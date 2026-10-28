# Agent 设计：提示词、解析器、匹配器、状态机

这一篇讲「它为什么这么决策」，也是改造它时最该先读的部分。
原则只有一句：**确定性的归代码，模糊的归模型。**

## 1. 系统提示词的四组规则

`build_system_prompt(profile)` 每次请求现场拼，13 条规则分四组：

### ① 身份与边界（规则 1–3）

- 只聊训练、营养、恢复、体态；无关话题礼貌挡回
- 基于档案给个性化建议，不给通用模板
- 伤病/疼痛优先建议就医，不做医疗诊断

### ② WHOOP 数据真实性（规则 7，最高优先级）

这一段是踩坑换来的：模型一度会回答「我无法读取你的 WHOOP 数据」——明明数据就在上下文里。

现在的做法：由系统在提示词里**显式写入状态段**，并规定模型必须照做：

```text
## 本次 WHOOP 数据（后台集成自动获取，查询成功）
<恢复/睡眠/周期/运动/身体指标>
以上数据由后台 WHOOP 集成本次实时获取，查询成功。
```

失败时写另一段：

```text
## WHOOP 数据获取失败
API 请求失败时间: ...
失败原因: 网络超时、认证失败或服务端错误
...
```

规则明确要求：

- 成功就**必须**以「已获取 WHOOP 数据」开头，并列出各条记录的实际日期，不准否认能力
- 失败就准确说「获取失败」，不准冒充成功，也不准编造指标
- 引用历史旧数据必须标注日期，说明「非本次实时获取」
- 历史对话里若出现「我无法读取」这类旧错误表述，**本次一律覆盖**
  （代码里还有 `_sanitize_assistant_text()` 把历史里这类句子洗掉，防止模型模仿）

### ③ 训练计划必须结构化（规则 9）

要求模型在需要给计划时输出**纯 JSON 代码块**：

```json
{
  "plan_title": "胸 + 三头（推日）",
  "target_area": "胸+三头",
  "duration_minutes": 60,
  "intensity": "中等",
  "recovery_status": "恢复 62%，中等偏上",
  "exercises": [
    {"name": "杠铃卧推", "sets": 4, "reps": "8-10", "weight": null,
     "rpe_target": 7, "rest_seconds": 90, "notes": "暂无历史重量，先问用户"}
  ]
}
```

硬约束：**没有明确工作重量时 `weight` 必须为 `null`**，并反问用户，禁止编造。
`_try_parse_plan_card()` 解析这段 JSON 并转成飞书卡片；解析失败就退化成纯文本回复。

### ④ 数据解释的诚实性（规则 8、10–13）

- 年龄只用系统算出的「当前周岁」（`calc_age`，按 Asia/Shanghai 当天实时算），不信历史对话里的数字
- WHOOP 周期条数 ≠ 训练次数；不能用条数推断「连续高负荷」
- 恢复分只是参考信号，不自创「低于 X% 就不能练」的门槛
- 观测与推测分开说；建议减量要说明具体做法并允许按感受调整
- 不同日期的体重差异不直接当「正常波动」

## 2. 自然语言训练日志解析

入口：`_parse_natural_language_log(text)`，只在「像在说已经练完了」时才触发。

```text
_is_completed_tone()  ← 有没有「练了 / 打了 / 跑了 / 打卡 / 帮我记 / done」
        │
        ├─ _extract_date()      今天 / 昨天 / 前天 / YYYY-MM-DD / M月D日
        ├─ 时间段              晚上8点 / 20:00 / 8点半 / 中午12点05分
        ├─ _classify_activity() 关键词表 → strength / basketball / running / ...
        ├─ 动作解析             "卧推 60kg 4组8次" → {name, sets, reps, weight}
        ├─ 时长                 "1 小时" / "45 分钟" / "一个半小时"
        └─ 强度                 用户明说才用，否则留空
```

不支持的一概留空（`None` / `""`），**绝不猜**。
所以「昨天打了会儿球」只会得到日期 + `basketball`，时长是空的，确认信息里也会显示「时长未记录」。

支持的活动类型（`_ACTIVITY_MAP` 关键词 → 类型）：

```text
strength（胸背腿肩手臂三头二头核心臀/卧推/深蹲/硬拉/引体/划船…）
basketball 篮球    running 跑步    swimming 游泳    cycling 骑行
yoga 瑜伽          stretching 拉伸  boxing 拳击      hiit/tabata
badminton 羽毛球    table_tennis 乒乓  tennis 网球
soccer 足球        volleyball 排球   cardio 有氧/椭圆机/划船机/登山机
other 其它
```

## 3. WHOOP 运动匹配

入口：`_try_match_whoop_workout(training_date, activity_type, user_start_time, user_duration_minutes)`。

```text
1. 拉当月全部 WHOOP workout（get_monthly_workouts，带分页）
2. 过滤：同一天 + 运动类型命中 + 没被别的训练记录占用过
   （运动类型是 other 时不过滤项目）
3. 打分：score = (时间差分钟, 时长差分钟)
   ├─ 用户说了开始时间   → 与 WHOOP start_cst 的时:分距离
   ├─ 用户说了时长       → 与 WHOOP 实际时长之差
   └─ 两者都没有         → (9999, 9999)
4. 只有 1 条候选 → 直接命中
   多条候选 → 取 score 最小；若前两名**完全并列** → ambiguous，列前 3 条让用户选
5. 命中后：时长用 WHOOP 实测值覆盖用户口述值；运动类型、Strain、最高心率、消耗一并带上
```

项目名映射（`whoop_sport_map`）关键几条：

| 本地类型 | 匹配的 WHOOP 项目名 |
| --- | --- |
| strength | weightlifting / strength / functional fitness / crossfit / resistance training |
| running | running / jogging |
| cycling | cycling / biking |
| hiit | hiit / tabata / functional fitness |
| cardio | cardio / elliptical / rowing / stair climber |
| basketball 等 | 同名项目 |

Strain → 强度（`strain_to_intensity`，确定性映射）：

| Strain | 强度 |
| --- | --- |
| < 5 | 低 |
| 5 ~ 10 | 中等 |
| 10 ~ 15 | 中高 |
| 15 ~ 20 | 高 |
| ≥ 20（且 ≤ 50） | 极高 |

强度取值优先级：**用户明说的 > Strain 映射 > 空**。

## 4. 三个状态：无待确认 / 待确认 / 待选择

```text
用户消息
   │
   ├─ 无 pending ──→ 解析 ──→ 匹配成功 ──→ 直接落库 + 回执
   │                     └──→ 无匹配   ──→ 存 pending_confirm（10 分钟）→ 问「确认保存？」
   │                     └──→ 多条并列 ──→ 存 pending_select（10 分钟）→ 列 1/2/3
   │
   ├─ 有待确认 + 「确认」 ──→ 落库 + 回执
   ├─ 有待确认 + 「取消」 ──→ 丢弃 + 「已取消」
   ├─ 待选择 + 数字 1~3  ──→ 用选中那条 WHOOP 记录落库
   └─ 其它 ──→ 当作新消息继续走解析/对话
```

细节：

- 关键词集合：确认 `{确认, 确认保存, 继续保存, 是}`，取消 `{取消, 不保存, 算了, 否}`
- 选择编号是 1–3 的纯数字或「选择 2」这类短句（`_CHOICE_PATTERN`）
- 待确认状态用**进程内字典** + 10 分钟 TTL；进程重启就丢，这是可接受的取舍
- 已被别的训练记录关联过的 WHOOP 运动不会再被关联（防重复记账）

## 5. 记忆与上下文预算

- 每个「群 + 发送者」一个 `scope`（`json.dumps([chat_id, user_id])`）
- 取最近 **10 轮**（`ORDER BY id DESC LIMIT 10`）
- 再加一道字节预算：从最近往前累加，超过 **24000 字符** 就停
- 历史里的助手发言会过一遍 `_sanitize_assistant_text()`，剥掉历史上的自我否认措辞

为什么不无限记忆：模型上下文有限，且长历史会把「当前 WHOOP 数据」淹没。
真需要长期记忆，应该做摘要表（本项目没做，属于 roadmap）。

## 6. 幂等与并发

```text
message_id ──INSERT──→ message_state（主键冲突 = 重复事件，直接丢）
scope      ──LOCK───→ 同一会话同一人的消息串行处理
```

飞书事件至少投递一次，重投很常见（尤其容器重启前后）。
`try_claim_message()` 用 SQLite 主键做原子抢锁，比「先查再插」安全。

## 7. 卡片怎么构建

| 函数 | 场景 |
| --- | --- |
| `_build_whoop_card(data)` | `/whoop`：恢复 / 睡眠 / 周期 / 运动 / 身体指标，按状态选颜色 |
| `_build_single_card(plan, exercises)` | 训练计划，一页放不下时自动分页 |
| `_build_monthly_card(data)` | 单月总结 |
| `_build_multi_month_card(months)` | 多月对比 |

卡片内容全部来自结构化数据（`whoop_client` 返回的 dict 或本地 DB 行），
不从模型自由文本里抠数据——避免「模型说 62%，卡片显示 58%」这种自相矛盾。

## 8. 想改它的话

| 你想改 | 改哪里 |
| --- | --- |
| 教练口吻、建议风格 | `build_system_prompt()` 里的规则段落 |
| 支持的训练项目 / 关键词 | `_ACTIVITY_MAP`、`whoop_sport_map` |
| Strain 分档 | `_STRAIN_INTENSITY_MAP` |
| 确认/取消关键词 | `_CONFIRM_KEYWORDS` / `_CANCEL_KEYWORDS` |
| 记忆轮数、上下文预算 | `load_history()` 里的 `LIMIT 10` 与 `24000` |
| 待确认有效期 | `_PENDING_CONFIRM_TTL` / `_PENDING_ACTION_TTL_SECONDS` |
| 卡片样式 | `_build_*_card()` 里的 JSON |
| 换模型 | `.env` 的 `DEEPSEEK_*` 三件套 |

改动提示词后，**务必用真实数据回归**这几条：

1. 有 WHOOP 数据时，回答是否以「已获取 WHOOP 数据」开头并带日期
2. WHOOP 失败时，是否如实说失败且不编数字
3. 要计划时，是否返回可解析的 JSON，且 `weight` 没瞎填
4. 问年龄时，是否与 `calc_age` 结果一致
