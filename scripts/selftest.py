#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线自检：不碰网络、不需要真实密钥，验证「建表 / 提示词 / 解析器 / HTTP 通道」是否正常。

用法：
    python scripts/selftest.py

CI 里也跑这一个脚本（见 .github/workflows/ci.yml）。
"""
import json
import os
import shutil
import sys
import tempfile
import atexit
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:  # python-dotenv 没装也能跑
    pass

# 本地没有 .env 时给一套无害的占位值，让模块能 import
for k in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "DEEPSEEK_API_KEY"):
    os.environ.setdefault(k, "selftest-dummy")
os.environ.setdefault("DEEPSEEK_BASE_URL", "https://example.invalid")

_tmp = tempfile.mkdtemp(prefix="fitness-selftest-")
atexit.register(shutil.rmtree, _tmp, True)
os.environ["FITNESS_DB_PATH"] = str(Path(_tmp) / "fitness.db")
os.environ["WHOOP_TOKEN_PATH"] = str(Path(_tmp) / "whoop_token.json")
os.environ["FITNESS_PROFILE_PATH"] = str(ROOT / "examples" / "profile.example.json")

import bot  # noqa: E402
import whoop_client  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


print("== 1. 数据库建表 ==")
conn = bot.get_db()
tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
for t in ("message_state", "training_logs", "pending_actions", "whoop_workouts"):
    check(f"表 {t}", t in tables)
cols = {r[1] for r in conn.execute("PRAGMA table_info(training_logs)")}
for c in ("activity_type", "duration_minutes", "intensity", "whoop_matched", "whoop_workout_id"):
    check(f"training_logs.{c}", c in cols)
conn.close()

print("== 2. 提示词组装 ==")
profile = bot.load_profile()
prompt = bot.build_system_prompt(profile)
check("包含身份定义", "Fitness Agent" in prompt)
check("包含 WHOOP 真实性规则", "已获取 WHOOP 数据" in prompt)
check("包含 JSON 计划规范", "plan_title" in prompt)
check("带入档案年龄", "当前周岁" in prompt)
check("年龄按当前日期算", str(bot.calc_age("1995-06-01", bot.datetime.now(bot.SHANGHAI_TZ).date())) in prompt)

print("== 3. 自然语言解析 ==")
parsed = bot._parse_natural_language_log("晚上练了胸，卧推 60kg 4组8次，上斜哑铃 22.5kg 4x10")
check("识别为训练记录", parsed is not None)
if parsed:
    check("活动类型=strength", parsed["activity_type"] == "strength", parsed["activity_type"])
    check("解析出日期", len(parsed["training_date"]) == 10, parsed["training_date"])
    names = [e.get("name", "") for e in parsed["exercises"]]
    check("解析出动作", len(parsed["exercises"]) >= 1, names)
    bench = next((e for e in parsed["exercises"] if "卧推" in e.get("name", "")), None)
    check("卧推重量 60kg", bench and "60" in str(bench.get("weight")), bench)
    check("卧推 4 组", bench and str(bench.get("sets")) == "4", bench)
check("不像训练的话不触发", bot._parse_natural_language_log("今天适合练什么？") is None)
check("昨天日期正确", bot._extract_date("昨天练了腿")[0] ==
      (bot.datetime.now(bot.SHANGHAI_TZ) - bot.timedelta(days=1)).strftime("%Y-%m-%d"))

print("== 4. Strain → 强度 ==")
check("4.0 → 低", bot.strain_to_intensity(4.0)[0] == "low")
check("11.2 → 中高", bot.strain_to_intensity(11.2)[0] == "medium_high")
check("21 → 极高", bot.strain_to_intensity(21)[0] == "very_high")
check("None 容错", bot.strain_to_intensity(None) == ("", ""))
check("超范围容错", bot.strain_to_intensity(99) == ("", ""))

print("== 5. WHOOP 客户端纯函数 ==")
check("毫秒转 HhMm", whoop_client._ms_to_hm(7200000) == "2h00m", whoop_client._ms_to_hm(7200000))
check("毫秒 None 容错", whoop_client._ms_to_hm(None) == "N/A")
check("展开 recovery score", whoop_client._expand_score({"recovery_score": 62, "hrv": 68})["recovery_score"] == 62)
# 浮点噪音：卡片上不该出现 33.8266668°C / 91.28212%
_rounded = whoop_client._expand_score({
    "skin_temp_celsius": 33.8266668,
    "sleep_efficiency_percentage": 91.28212,
    "strain": 4.4499999,
})
check("皮肤温度保留 1 位", _rounded["skin_temp_celsius"] == 33.8, _rounded["skin_temp_celsius"])
check("睡眠效率保留 1 位", _rounded["sleep_efficiency_percentage"] == 91.3,
      _rounded["sleep_efficiency_percentage"])
check("strain 保留 1 位", _rounded["strain"] == 4.4, _rounded["strain"])
check("None 不会被四舍五入成 0", whoop_client._r1(None) is None)

print("== 6. HTTP 通道 ==")
client = bot.flask_app.test_client()
r = client.get("/health")
check("/health 200", r.status_code == 200)
check("/health db connected", r.get_json().get("database") == "connected", r.get_json())
r = client.post("/internal/chat", json={"message": "/status", "session_id": "selftest"})
check("/internal/chat 可用", r.status_code == 200 and "正常" in r.get_json().get("content", ""), r.get_json())
r = client.post("/internal/chat", json={})
check("缺参返回 400", r.status_code == 400)
r = client.post("/internal/confirm", json={"confirmation_id": "nope", "session_id": "x", "action": "confirm"})
check("未知确认返回 404", r.status_code == 404, r.status_code)

print("== 7. 飞书卡片构建（不发送）==")
card = bot._build_whoop_card({
    "status": "ok",
    "recovery": {"latest_date": "2026-10-28 07:10", "recovery_score": 62, "hrv": 68,
                 "resting_heart_rate": 52, "respiratory_rate": 14.2},
    "sleep": {"latest_date": "2026-10-28 06:40", "actual_sleep_duration": "7h12m",
              "light_sleep": "4h02m", "deep_sleep": "1h05m", "rem_sleep": "2h05m",
              "efficiency": 93, "performance": 88, "nap_count": 0},
    "cycles": {"count": 6, "latest_start": "2026-10-28 06:50", "latest_end": "2026-10-28 22:30",
               "step_count": 8231},
    "workout": {"latest_start": "2026-10-27 20:05", "sport_name": "Weightlifting",
                "duration": "1h02m", "strain": 11.2, "kilojoule": 1520, "avg_hr": 118},
    "body": {"height_cm": 178.0, "weight_kg": 74.5, "max_heart_rate": 190},
    "baseline": {"status": "ok", "avg_recovery": 61.4, "avg_hrv": 66.2, "sample_count": 21},
})
card_json = json.dumps(card, ensure_ascii=False)
check("卡片有 header", "header" in card and "elements" in card)
check("卡片含恢复分", "62" in card_json)
check("卡片含运动项目", "Weightlifting" in card_json)

print()
print(f"结果：{PASS} 通过 / {FAIL} 失败")
raise SystemExit(0 if FAIL == 0 else 1)
