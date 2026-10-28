#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WHOOP 接入自检 + 数据导出。

直接复用 app/whoop_client.py，不需要启动飞书 Bot 就能验证令牌和接口是否正常。

用法：
    python scripts/whoop_dump.py                  # 打印最近 7 天概览
    python scripts/whoop_dump.py --json out.json  # 同时导出结构化 JSON
    python scripts/whoop_dump.py --db             # 把本月运动同步进 SQLite
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")
# 在宿主机上跑时，默认路径要落到仓库里，而不是容器内的 /app/data
os.environ.setdefault("WHOOP_TOKEN_PATH", str(ROOT / "app" / "data" / "whoop_token.json"))
os.environ.setdefault("FITNESS_DB_PATH", str(ROOT / "app" / "data" / "fitness.db"))

import whoop_client  # noqa: E402

WHOOP_WORKOUTS_DDL = """
CREATE TABLE IF NOT EXISTS whoop_workouts (
    workout_id TEXT PRIMARY KEY,
    sport_name TEXT NOT NULL DEFAULT '',
    start_utc TEXT NOT NULL DEFAULT '',
    end_utc TEXT NOT NULL DEFAULT '',
    start_cst TEXT NOT NULL DEFAULT '',
    end_cst TEXT NOT NULL DEFAULT '',
    date_cst TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER,
    strain REAL,
    average_heart_rate REAL,
    max_heart_rate REAL,
    kilojoule REAL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""


def sync_workouts(db_path: str) -> int:
    """把本月 WHOOP 运动 upsert 进 whoop_workouts，返回写入条数。"""
    now = datetime.now()
    data = whoop_client.get_monthly_workouts(now.year, now.month)
    if data.get("status") != "ok":
        print(f"取运动数据失败: {data.get('error')}")
        return 0
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.execute(WHOOP_WORKOUTS_DDL)
        for r in data["records"]:
            conn.execute(
                """INSERT INTO whoop_workouts
                   (workout_id, sport_name, start_utc, end_utc, start_cst, end_cst,
                    date_cst, duration_ms, strain, average_heart_rate, max_heart_rate,
                    kilojoule, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(workout_id) DO UPDATE SET
                     sport_name=excluded.sport_name,
                     start_utc=excluded.start_utc, end_utc=excluded.end_utc,
                     start_cst=excluded.start_cst, end_cst=excluded.end_cst,
                     date_cst=excluded.date_cst, duration_ms=excluded.duration_ms,
                     strain=excluded.strain,
                     average_heart_rate=excluded.average_heart_rate,
                     max_heart_rate=excluded.max_heart_rate,
                     kilojoule=excluded.kilojoule,
                     updated_at=CURRENT_TIMESTAMP""",
                (
                    r["workout_id"], r.get("sport_name", ""),
                    r.get("start_utc", ""), r.get("end_utc", ""),
                    r.get("start_cst", ""), r.get("end_cst", ""),
                    r.get("date", ""), r.get("duration_ms"),
                    r.get("strain"), r.get("average_heart_rate"),
                    r.get("max_heart_rate"), r.get("kilojoule"),
                ),
            )
        conn.commit()
        return len(data["records"])
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="WHOOP 接入自检 / 数据导出")
    ap.add_argument("--json", metavar="PATH", help="把结构化数据写到指定 JSON 文件")
    ap.add_argument("--db", action="store_true", help="把本月运动同步进 SQLite")
    args = ap.parse_args()

    data = whoop_client.get_whoop_dashboard_data()
    if data.get("status") != "ok":
        print("❌ WHOOP 数据获取失败:", data.get("error"))
        token_path = os.environ.get("WHOOP_TOKEN_PATH", "app/data/whoop_token.json")
        if "No such file" in str(data.get("error")):
            print(f"\n还没有授权过的令牌（{token_path}）。先跑一次：")
            print("    bash scripts/whoop_login.sh")
        return 1

    print(whoop_client.get_whoop_dashboard())

    if args.json:
        data["fetched_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        Path(args.json).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写入 {args.json}")

    if args.db:
        db_path = os.environ["FITNESS_DB_PATH"]
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        n = sync_workouts(db_path)
        print(f"\n已同步 {n} 条运动会话到 {db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
