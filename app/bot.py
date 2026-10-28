import os
import re
import json
import sqlite3
import threading
import uuid
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from openai import OpenAI
from flask import Flask, request as flask_request, jsonify
import lark_oapi as lark
from lark_oapi.api.im.v1 import *
from whoop_client import get_whoop_dashboard, get_whoop_dashboard_data, get_monthly_recovery, get_monthly_sleep, get_monthly_workouts

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("FITNESS_DB_PATH", "/app/data/fitness.db")
PROFILE_PATH = os.environ.get("FITNESS_PROFILE_PATH", "/app/data/profile.json")
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

deepseek_client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url=os.environ["DEEPSEEK_BASE_URL"],
)
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

# 飞书 OpenAPI 客户端：全局复用一份（建连有开销，且早期分支需要在赋值前就能用）
_lark_client = None
_lark_client_lock = threading.Lock()


def get_lark_client():
    global _lark_client
    if _lark_client is None:
        with _lark_client_lock:
            if _lark_client is None:
                _lark_client = lark.Client.builder() \
                    .app_id(os.environ["FEISHU_APP_ID"]) \
                    .app_secret(os.environ["FEISHU_APP_SECRET"]) \
                    .log_level(lark.LogLevel.WARNING) \
                    .build()
    return _lark_client

# 说明：任何兼容 OpenAI Chat Completions 的模型都能替换 DeepSeek，
# 只需改 DEEPSEEK_BASE_URL / DEEPSEEK_API_KEY / DEEPSEEK_MODEL 三个环境变量。

HELP_TEXT = """🏋️ Fitness Agent 可用命令：
/status - 查看服务状态
/help - 显示帮助信息
/whoop - 查看 WHOOP 健康数据摘要（恢复/睡眠/运动/身体指标）
/月报 - 本月训练与健康数据总结
/月报 YYYY-MM - 指定月份总结（如 /月报 2026-10）
直接发送消息可与 AI 对话获取健身建议。"""

# ---------- WHOOP Refresh Intent Detection ----------
_WHOOP_REFRESH_PATTERNS = [
    r"刷新\s*whoop", r"同步\s*whoop", r"最新\s*whoop", r"今天\s*whoop",
    r"whoop\s*刷新", r"whoop\s*同步", r"whoop\s*最新", r"whoop\s*今天",
    r"更新\s*whoop", r"whoop\s*更新", r"拉取\s*whoop", r"whoop\s*拉取",
    r"重新\s*获取\s*whoop", r"whoop\s*重新\s*获取",
]
_WHOOP_REFRESH_RE = re.compile("|".join(_WHOOP_REFRESH_PATTERNS), re.IGNORECASE)

def _is_whoop_refresh_intent(text: str) -> bool:
    """Check if user message explicitly requests a fresh WHOOP API call."""
    return bool(_WHOOP_REFRESH_RE.search(text))

def _fetch_whoop_with_logging(force_refresh: bool = False) -> tuple:
    """Fetch WHOOP dashboard with structured logging.
    Returns (dashboard_str, success_bool).
    Logs: request_time, endpoint, http_status, record_dates, cache_used.
    Never logs access tokens.
    """
    from datetime import datetime as _dt
    req_time = _dt.now(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S CST")
    try:
        dashboard = get_whoop_dashboard()
        if not isinstance(dashboard, str) or not dashboard.strip():
            raise ValueError("Empty WHOOP dashboard response")
        # Extract record dates from dashboard for logging
        date_matches = re.findall(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", dashboard)
        record_dates = ", ".join(set(date_matches[:5])) if date_matches else "N/A"
        logger.info(
            "WHOOP_REQUEST | time=%s | endpoint=all_summaries | status=200 | "
            "record_dates=[%s] | cache_used=False | force_refresh=%s",
            req_time, record_dates, force_refresh
        )
        return dashboard, True
    except Exception as e:
        logger.warning(
            "WHOOP_REQUEST | time=%s | endpoint=all_summaries | status=FAIL(%s) | "
            "record_dates=[] | cache_used=True | force_refresh=%s",
            req_time, type(e).__name__, force_refresh
        )
        return None, False


# ---------- Profile & Dynamic Age ----------

def load_profile() -> dict:
    try:
        with open(PROFILE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load profile from {PROFILE_PATH}: {e}")
        return {}

def calc_age(dob_str: str, ref_date: date) -> int:
    """Calculate age in full years. Subtract 1 if birthday hasn't occurred yet this year."""
    dob = date.fromisoformat(dob_str)
    age = ref_date.year - dob.year
    if (ref_date.month, ref_date.day) < (dob.month, dob.day):
        age -= 1
    return age

def build_system_prompt(profile: dict) -> str:
    parts = []

    # Identity & scope
    parts.append("你是 Fitness Agent，一个专注于健身训练、营养和体能提升的AI助手。")
    parts.append("你只回答与健身、训练计划、营养、恢复、体态管理相关的问题。")
    parts.append("对于无关话题，礼貌拒绝并引导回健身领域。")

    # User profile
    if profile:
        parts.append("\n## 用户个人档案")

        dob = profile.get("date_of_birth", "")
        now_shanghai = datetime.now(SHANGHAI_TZ).date()
        today_str = now_shanghai.isoformat()

        if dob:
            age = calc_age(dob, now_shanghai)
            parts.append(f"- 当前日期: {today_str} (Asia/Shanghai)")
            parts.append(f"- 出生日期: {dob}")
            parts.append(f"- 当前周岁: {age} 岁（由系统实时计算，非用户自述）")
        else:
            parts.append("- 出生日期: 未知")

        height = profile.get("height_cm", "未知")
        weight = profile.get("weight_kg", "未知")
        weight_note = profile.get("weight_note", "")
        exp = profile.get("training_experience_years", "未知")
        health = profile.get("health_status", "未知")
        goals = profile.get("goals", [])
        goal_priority = profile.get("goal_priority", "")
        freq = profile.get("preferred_frequency_per_week", "未知")
        split = profile.get("preferred_split", [])
        duration = profile.get("session_duration_minutes", "未知")

        parts.append(f"- 身高: {height} cm")
        weight_str = f"{weight} kg"
        if weight_note:
            weight_str += f" ({weight_note})"
        parts.append(f"- 体重: {weight_str}")
        parts.append(f"- 训练经验: {exp} 年")
        parts.append(f"- 健康状况: {health}")
        parts.append(f"- 目标: {', '.join(goals) if goals else '未指定'}")
        if goal_priority:
            parts.append(f"- 目标优先级: {goal_priority}")
        parts.append(f"- 理想训练频率: 每周 {freq} 次")
        parts.append(f"- 偏好分化: {', '.join(split) if split else '未指定'}")
        parts.append(f"- 每次训练时长: 约 {duration} 分钟")

    # Behavioral rules
    parts.append("\n## 回复规则")
    parts.append("1. 基于用户档案给出个性化建议，不要给通用模板。")
    parts.append("2. 训练计划必须考虑用户的经验年限、可用时间和偏好分化。")
    parts.append("3. 涉及伤病或疼痛时，优先建议就医，不替代医疗诊断。")
    parts.append("4. 营养建议以均衡饮食为基础，不推荐极端节食或未经证实的补剂。")
    parts.append("5. 回复简洁实用，避免冗长理论；必要时用列表或分点说明。")
    parts.append("6. 不编造用户未提供的身体数据或历史记录。")
    parts.append("7. 【WHOOP 数据回复规范 - 最高优先级】你拥有后台 WHOOP 集成能力，数据由系统自动获取并附加到本次提示词。严格遵守以下规则：\n"
        "  a) 当本次提示词包含「本次 WHOOP 数据（后台集成自动获取，查询成功）」段落时，表示数据获取成功。你必须以「已获取 WHOOP 数据」开头回复，列出各条记录的实际日期，然后回答问题。绝对禁止说「我无法自行读取」「无法访问」「无法读取 WHOOP 数据」或任何否认自身能力的表述。\n"
        "  b) 当本次提示词包含「WHOOP 数据获取失败」段落时，准确告知用户「WHOOP 数据获取失败」，不要冒充成功，也不要说「我无法读取」。如引用历史对话中的旧数据，必须标注其日期和来源。\n"
        "  c) 区分「本次查询时间」与「WHOOP 记录时间」。不能仅凭返回条目数量或日期判断是否有更新。部分字段缺失时只说明该字段未返回，不笼统说无更新或无记录。\n"
        "  d) 本次获取状态优先于历史对话中一切关于自身能力的描述。历史回复中若出现「我无法自行读取」等错误表述，那是过去的错误，不得在本次回复中重复或延续。")
    parts.append("8. 回答用户年龄时，必须使用本提示词中「当前周岁」字段的值。旧聊天记录中出现的年龄数值可能已过时，不得覆盖当前实时计算结果。")

    parts.append("9. 【训练计划输出规范 - 最高优先级】当用户请求训练计划、今日训练安排、动作推荐时，你必须以严格的 JSON 代码块输出训练计划，格式如下（不要输出任何其他文字在 JSON 之前）：\n"
        "```json\n"
        "{\n"
        '  "plan_title": "训练计划标题",\n'
        '  "target_area": "训练部位（如胸+三头）",\n'
        '  "duration_minutes": 60,\n'
        '  "intensity": "中等/高/低",\n'
        '  "recovery_status": "恢复状态描述（可选）",\n'
        '  "exercises": [\n'
        "    {\n"
        '      "name": "动作名称",\n'
        '      "sets": 4,\n'
        '      "reps": "8-10",\n'
        '      "weight": "60kg" 或 null,\n'
        '      "rpe_target": 7,\n'
        '      "rest_seconds": 90,\n'
        '      "notes": "可选备注"\n'
        "    }\n"
        "  ]\n"
        "}\n"
        "```\n"
        "重量字段规则：仅当用户档案或历史对话中有该动作的明确工作重量时才填写具体值（如 \"60kg\"）。若无足够个人力量数据，weight 必须为 null，同时 rpe_target 填写 RPE 目标值（6-9），并在 notes 中询问用户当前该动作的工作重量或可用器械。绝对禁止编造重量。\n"
        "非训练计划的普通回复仍使用纯文本，不使用 Markdown 格式。")
    parts.append("10. WHOOP 的周期记录数量仅表示返回的周期条数，不等于训练次数或连续高负荷天数。只有对应日期的实际运动或负荷数据才能支持相关判断。")
    parts.append("11. 恢复评分是参考信号，不能单凭某个百分比判断能否训练。不要自创固定恢复门槛；综合睡眠、近期实际负荷、主观疲劳、疼痛和热身表现给出可调整建议。缺少的信息明确标为未知。")
    parts.append("12. 区分观测数据与推测：不能仅凭睡眠时长就断言它导致了恢复评分下降，也不能断言肌肉或神经系统尚未恢复。建议减量时说明具体做法，并允许根据当日感受调整。")
    parts.append("13. 不把不同日期或来源的体重差异直接认定为正常波动；如需使用，标注来源及已知日期。")

    return "\n".join(parts)

# Load profile once at startup (static fields only; age is computed per-request)
_user_profile = load_profile()
logger.info(f"Profile loaded, keys: {list(_user_profile.keys())}")

# ---------- SQLite schema ----------
# 建表 + 兼容旧库的列补齐。旧版本里 training_logs 的新列是手工 ALTER 出来的，
# 这里统一收敛成一次幂等迁移，保证空数据库也能直接跑起来。

_TRAINING_LOG_COLUMNS = {
    "activity_type": "TEXT DEFAULT ''",
    "duration_minutes": "INTEGER DEFAULT NULL",
    "intensity": "TEXT DEFAULT ''",
    "notes": "TEXT DEFAULT ''",
    "body_part": "TEXT DEFAULT ''",
    "source": "TEXT DEFAULT 'user'",
    "whoop_matched": "INTEGER DEFAULT 0",
    "whoop_workout_date": "TEXT DEFAULT ''",
    "whoop_workout_id": "TEXT DEFAULT ''",
    "whoop_match_status": "TEXT DEFAULT 'unmatched'",
}

_schema_ready = False
_schema_lock = threading.Lock()


def _ensure_schema(conn):
    """幂等地建表并补齐旧库缺失的列，一个进程只跑一次。"""
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        conn.execute("""
            CREATE TABLE IF NOT EXISTS message_state (
                message_id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'processing',
                reply_sent INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS training_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                training_date TEXT NOT NULL,
                exercises TEXT NOT NULL,
                source_message_id TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        existing = {row[1] for row in conn.execute("PRAGMA table_info(training_logs)")}
        for column, ddl in _TRAINING_LOG_COLUMNS.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE training_logs ADD COLUMN {column} {ddl}")
        conn.execute("""
            CREATE INDEX IF NOT EXISTS training_logs_date_idx
            ON training_logs(training_date)
        """)
        # Web / HTTP 通道的待确认动作（飞书通道用进程内字典，互不影响）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_actions (
                confirmation_id TEXT PRIMARY KEY,
                channel TEXT NOT NULL,
                session_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                resolved_at TEXT,
                result_ref TEXT
            )
        """)
        # WHOOP 运动会话落地表，给报表 / 看板复用
        conn.execute("""
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
        """)
        conn.commit()
        _schema_ready = True


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        _ensure_schema(conn)
    except Exception:
        conn.close()
        raise
    return conn

def try_claim_message(message_id: str) -> bool:
    """Atomically claim a message_id. Returns True only if this is the first claim."""
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO message_state (message_id, status) VALUES (?, 'processing')",
            (message_id,)
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def mark_sent(message_id: str):
    conn = get_db()
    try:
        conn.execute(
            "UPDATE message_state SET status='sent', reply_sent=1 WHERE message_id=?",
            (message_id,)
        )
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to mark sent for {message_id}: {e}")
    finally:
        conn.close()

def mark_failed(message_id: str):
    conn = get_db()
    try:
        conn.execute(
            "UPDATE message_state SET status='failed' WHERE message_id=?",
            (message_id,)
        )
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to mark failed for {message_id}: {e}")
    finally:
        conn.close()

# ---------- Persistent Conversation Memory ----------

_memory_locks = {}
_memory_locks_guard = threading.Lock()

# ========== Pending WHOOP confirmation state (Step 4C1) ==========
_pending_confirms = {}  # key: scope -> {"parsed": dict, "whoop_match": dict, "timestamp": float}
_pending_confirms_lock = threading.Lock()
_PENDING_CONFIRM_TTL = 600  # 10 minutes in seconds

_WHOOP_SYNC_PATTERNS = re.compile(
    r"(?:同步|关联|连接|对接|匹配)\s*(?:whoop|WHOOP|手环|设备)"
    r"|(?:whoop|WHOOP)\s*(?:同步|关联|连接|对接|匹配)"
    r"|记得\s*(?:同步|关联)\s*(?:whoop|WHOOP)"
    r"|帮我\s*(?:同步|关联)\s*(?:whoop|WHOOP)",
    re.IGNORECASE
)

_CONFIRM_KEYWORDS = {"确认", "确认保存", "继续保存", "是"}
_CANCEL_KEYWORDS = {"取消", "不保存", "算了", "否"}
_CHOICE_PATTERN = re.compile(r"^(?:选择?|第)?([1-3])(?:个)?$")

def _user_requests_whoop_sync(text: str) -> bool:
    """Check if user explicitly requests WHOOP sync/association."""
    return bool(_WHOOP_SYNC_PATTERNS.search(text))

def _store_pending_confirm(scope: str, parsed: dict, whoop_match: dict):
    """Store pending confirmation data for a scope, with timestamp."""
    import time
    with _pending_confirms_lock:
        _pending_confirms[scope] = {
            "parsed": parsed,
            "whoop_match": whoop_match,
            "timestamp": time.time(),
        }

def _consume_pending_confirm(scope: str) -> dict:
    """Atomically retrieve and remove pending confirm for scope. Returns None if absent or expired."""
    import time
    with _pending_confirms_lock:
        entry = _pending_confirms.pop(scope, None)
    if entry is None:
        return None
    if time.time() - entry["timestamp"] > _PENDING_CONFIRM_TTL:
        return None  # expired
    return entry

def _peek_pending_confirm(scope: str) -> dict:
    """Check if scope has a valid pending confirm without consuming it."""
    import time
    with _pending_confirms_lock:
        entry = _pending_confirms.get(scope)
    if entry is None:
        return None
    if time.time() - entry["timestamp"] > _PENDING_CONFIRM_TTL:
        # Clean up expired entry
        with _pending_confirms_lock:
            _pending_confirms.pop(scope, None)
        return None
    return entry

def _build_pending_confirm_prompt(parsed: dict) -> str:
    """Build deterministic confirmation prompt for unmatched WHOOP sync request."""
    lines = ["⚠️ 未找到匹配的 WHOOP 运动记录。"]
    lines.append("是否仍按以下信息保存？")
    lines.append("")
    atype = parsed.get("activity_type", "other")
    atype_labels = {
        "strength": "力量训练", "basketball": "篮球", "running": "跑步",
        "swimming": "游泳", "cycling": "骑行", "yoga": "瑜伽",
        "boxing": "拳击", "cardio": "有氧", "hiit": "HIIT",
        "stretching": "拉伸", "badminton": "羽毛球", "tennis": "网球",
        "soccer": "足球", "volleyball": "排球", "table_tennis": "乒乓球",
        "other": "其他运动",
    }
    lines.append(f"活动: {atype_labels.get(atype, atype)}")
    lines.append(f"日期: {parsed.get('training_date', '未知')}")
    if parsed.get("start_time"):
        lines.append(f"开始时间: {parsed['start_time']}")
    dur = parsed.get("duration_minutes")
    if dur:
        dur_str = f"{int(dur)}分钟"
        lines.append(f"时长: {dur_str}")
    lines.append("")
    lines.append("回复「确认」保存，回复「取消」放弃。")
    return "\n".join(lines)

def _store_pending_select(scope: str, parsed: dict, candidates: list):
    """Store pending candidate selection state for a scope."""
    import time
    with _pending_confirms_lock:
        _pending_confirms[scope] = {
            "type": "select",
            "parsed": parsed,
            "candidates": candidates,
            "timestamp": time.time(),
        }

def _consume_pending_select(scope: str) -> dict:
    """Atomically retrieve and remove pending select for scope. Returns None if absent/expired/wrong type."""
    import time
    with _pending_confirms_lock:
        entry = _pending_confirms.pop(scope, None)
    if entry is None or entry.get("type") != "select":
        return None
    if time.time() - entry["timestamp"] > _PENDING_CONFIRM_TTL:
        return None
    return entry

def _peek_pending_select(scope: str) -> dict:
    """Check if scope has a valid pending select without consuming."""
    import time
    with _pending_confirms_lock:
        entry = _pending_confirms.get(scope)
    if entry is None or entry.get("type") != "select":
        return None
    if time.time() - entry["timestamp"] > _PENDING_CONFIRM_TTL:
        with _pending_confirms_lock:
            _pending_confirms.pop(scope, None)
        return None
    return entry

def _build_ambiguous_prompt(parsed: dict, candidates: list) -> str:
    """Build deterministic candidate selection prompt. Max 3 candidates."""
    atype_labels = {
        "strength": "力量训练", "basketball": "篮球", "running": "跑步",
        "swimming": "游泳", "cycling": "骑行", "yoga": "瑜伽",
        "boxing": "拳击", "cardio": "有氧", "hiit": "HIIT",
        "stretching": "拉伸", "badminton": "羽毛球", "tennis": "网球",
        "soccer": "足球", "volleyball": "排球", "table_tennis": "乒乓球",
        "other": "其他运动",
    }
    lines = ["⚠️ 找到多个可能匹配的 WHOOP 运动记录，请选择："]
    lines.append("")
    for i, rec in enumerate(candidates[:3], 1):
        sport = rec.get("sport_name", "未知")
        date_str = rec.get("date", "") or rec.get("date_cst", "")
        if " " in date_str:
            date_str = date_str.split(" ")[0]
        start_cst = rec.get("start_cst", "")
        end_cst = rec.get("end_cst", "")
        time_range = ""
        if start_cst and " " in start_cst:
            st = start_cst.split(" ")[1][:5]
            et = end_cst.split(" ")[1][:5] if end_cst and " " in end_cst else "?"
            time_range = f"{st}-{et}"
        dur_ms = rec.get("duration_ms") or rec.get("duration")
        dur_min = round(dur_ms / 60000) if dur_ms else None
        dur_str = f"{dur_min}分钟" if dur_min else "未记录"
        strain = rec.get("strain")
        strain_str = f"Strain {strain}" if strain is not None else "Strain 未记录"
        parts = [sport, f"{date_str} {time_range}".strip(), dur_str, strain_str]
        lines.append(f"{i}. {'｜'.join(parts)}")
    lines.append("")
    lines.append("回复「1」或「选择1」进行关联，回复「取消」放弃。")
    return "\n".join(lines)



# ============================================================
# DB-backed Pending Actions (Web Channel)
# Persistent replacement for in-memory _pending_confirms.
# Feishu continues to use in-memory functions above.
# ============================================================

_PENDING_ACTION_TTL_SECONDS = 600  # 10 minutes

def _store_pending_action_db(channel: str, session_id: str, action_type: str, payload: dict) -> str:
    """Store a pending action in DB. Returns confirmation_id (UUID)."""
    confirmation_id = uuid.uuid4().hex[:16]
    now = datetime.now(SHANGHAI_TZ)
    expires = now + timedelta(seconds=_PENDING_ACTION_TTL_SECONDS)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.execute(
            """INSERT INTO pending_actions
               (confirmation_id, channel, session_id, action_type, payload_json,
                status, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (
                confirmation_id, channel, session_id, action_type,
                json.dumps(payload, ensure_ascii=False),
                now.strftime("%Y-%m-%d %H:%M:%S"),
                expires.strftime("%Y-%m-%d %H:%M:%S"),
            )
        )
        conn.commit()
        logger.info("Pending action stored: id=%s channel=%s type=%s", confirmation_id, channel, action_type)
        return confirmation_id
    finally:
        conn.close()

def _peek_pending_action_db(confirmation_id: str, channel: str, session_id: str) -> dict | None:
    """Peek at a pending action without consuming. Validates channel+session ownership and expiry."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        row = conn.execute(
            "SELECT confirmation_id, action_type, payload_json, status, expires_at FROM pending_actions "
            "WHERE confirmation_id=? AND channel=? AND session_id=? AND status='pending'",
            (confirmation_id, channel, session_id)
        ).fetchone()
        if row is None:
            return None
        # Check expiry
        expires_at = datetime.strptime(row[4], "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHANGHAI_TZ)
        if datetime.now(SHANGHAI_TZ) > expires_at:
            # Mark expired
            conn.execute(
                "UPDATE pending_actions SET status='expired', resolved_at=? WHERE confirmation_id=?",
                (datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S"), confirmation_id)
            )
            conn.commit()
            return None
        return {
            "confirmation_id": row[0],
            "action_type": row[1],
            "payload": json.loads(row[2]),
            "status": row[3],
        }
    finally:
        conn.close()

def _resolve_pending_action_db(confirmation_id: str, channel: str, session_id: str, new_status: str, result_ref: str = None) -> dict | None:
    """Atomically resolve a pending action. Returns the action data if successfully resolved, None otherwise.
    Only transitions from 'pending' -> confirmed/cancelled/expired. Idempotent: already resolved returns None.
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        # Check current state first
        row = conn.execute(
            "SELECT confirmation_id, action_type, payload_json, status, expires_at FROM pending_actions "
            "WHERE confirmation_id=? AND channel=? AND session_id=?",
            (confirmation_id, channel, session_id)
        ).fetchone()
        if row is None:
            return None
        current_status = row[3]
        if current_status != "pending":
            # Already resolved - return info about current state for idempotency
            return {"already_resolved": True, "status": current_status, "confirmation_id": confirmation_id}
        # Check expiry
        expires_at = datetime.strptime(row[4], "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHANGHAI_TZ)
        if datetime.now(SHANGHAI_TZ) > expires_at:
            conn.execute(
                "UPDATE pending_actions SET status='expired', resolved_at=? WHERE confirmation_id=?",
                (datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S"), confirmation_id)
            )
            conn.commit()
            return {"already_resolved": True, "status": "expired", "confirmation_id": confirmation_id}
        # Resolve
        now_str = datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE pending_actions SET status=?, resolved_at=?, result_ref=? WHERE confirmation_id=? AND status='pending'",
            (new_status, now_str, result_ref, confirmation_id)
        )
        conn.commit()
        if conn.total_changes == 0:
            # Race condition: another thread resolved it between SELECT and UPDATE
            return None
        return {
            "confirmation_id": row[0],
            "action_type": row[1],
            "payload": json.loads(row[2]),
            "status": new_status,
        }
    finally:
        conn.close()


def memory_scope(event):
    msg = event.event.message
    sender = getattr(event.event, "sender", None)
    sender_id = getattr(sender, "sender_id", None)
    user_id = (
        getattr(sender_id, "open_id", None)
        or getattr(sender_id, "user_id", None)
        or getattr(sender_id, "union_id", None)
    )
    if not msg.chat_id or not user_id:
        return None
    return json.dumps([msg.chat_id, user_id], ensure_ascii=False)

def memory_db():
    conn = get_db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversation_turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                message_id TEXT NOT NULL UNIQUE,
                user_text TEXT NOT NULL,
                assistant_text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS conversation_scope_id
            ON conversation_turns(scope, id)
        """)
        conn.commit()
        return conn
    except Exception:
        conn.close()
        raise


def _sanitize_assistant_text(text: str) -> str:
    """Remove legacy incorrect self-denial phrases from historical assistant replies.
    Prevents the model from mimicking outdated disclaimers in few-shot context."""
    if not text:
        return text
    # Single comprehensive pattern: match all variants of the legacy WHOOP denial prefix
    # Covers: "我无法自行读取 WHOOP 数据，以下使用的是程序本次附带的结果，无更新记录："
    #         "我无法自行读取 WHOOP 数据，只能使用程序本次附带的结果。下面是..."
    #         "我无法自行读取 WHOOP 数据，" etc.
    pattern = (
        r'^我无法(?:自行)?(?:读取|访问)\s*WHOOP\s*数据?'
        r'(?:[，,。.\s]*'
        r'(?:以下使用的是?|只能使用|以下是?)程序本次附带的结果'
        r'(?:[，,。.\s]*(?:无更新记录|下面是?本次可用的全部内容及对应日期[，,]没有更新的记录))?'
        r')?[：:，,.。\s]*'
    )
    text = re.sub(pattern, '', text, flags=re.IGNORECASE)
    return text.strip()

def load_history(scope):
    if scope is None:
        return []
    conn = memory_db()
    try:
        rows = conn.execute("""
            SELECT user_text, assistant_text
            FROM conversation_turns
            WHERE scope=?
            ORDER BY id DESC LIMIT 10
        """, (scope,)).fetchall()
    finally:
        conn.close()

    # Keep complete turns and bound the history size.
    selected = []
    size = 0
    for user_text, assistant_text in rows:
        size += len(user_text) + len(assistant_text)
        if size > 24000:
            break
        selected.append((user_text, assistant_text))

    messages = []
    for user_text, assistant_text in reversed(selected):
        messages.extend([
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": _sanitize_assistant_text(assistant_text)},
        ])
    return messages

def save_turn(scope, message_id, user_text, assistant_text):
    if scope is None:
        return
    conn = memory_db()
    try:
        with conn:
            conn.execute("""
                INSERT OR IGNORE INTO conversation_turns
                (scope, message_id, user_text, assistant_text)
                VALUES (?, ?, ?, ?)
            """, (scope, message_id, user_text, assistant_text))
            conn.execute("""
                DELETE FROM conversation_turns
                WHERE scope=? AND id NOT IN (
                    SELECT id FROM conversation_turns
                    WHERE scope=? ORDER BY id DESC LIMIT 10
                )
            """, (scope, scope))
    finally:
        conn.close()

def save_training_log(scope: str, message_id: str, training_date: str, exercises: list):
    """Persist a completed training session. exercises is a list of dicts with name/sets/reps/weight."""
    if not scope or not exercises:
        return
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO training_logs (scope, training_date, exercises, source_message_id) VALUES (?, ?, ?, ?)",
            (scope, training_date, json.dumps(exercises, ensure_ascii=False), message_id)
        )
        conn.commit()
        logger.info("Training log saved: date=%s exercises=%d msg=%s", training_date, len(exercises), message_id)
    except Exception as e:
        logger.error("Failed to save training log: %s", type(e).__name__)
    finally:
        conn.close()


def load_training_logs(year: int, month: int) -> list:
    """Load all training logs for a given month. Returns list of dicts."""
    start = f"{year}-{month:02d}-01"
    from calendar import monthrange
    last_day = monthrange(year, month)[1]
    end = f"{year}-{month:02d}-{last_day:02d}"
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT training_date, exercises, whoop_matched, activity_type FROM training_logs WHERE training_date >= ? AND training_date <= ? ORDER BY training_date",
            (start, end)
        ).fetchall()
    finally:
        conn.close()
    results = []
    for row in rows:
        date_str, ex_json = row[0], row[1]
        whoop_matched = row[2] if len(row) > 2 else 0
        activity_type = row[3] if len(row) > 3 else ""
        try:
            exercises = json.loads(ex_json)
        except (json.JSONDecodeError, TypeError):
            exercises = []
        results.append({
            "date": date_str,
            "exercises": exercises,
            "whoop_matched": whoop_matched,
            "activity_type": activity_type,
        })
    return results



def _load_recent_training_logs(scope: str, limit: int = 5) -> str:
    """Load recent training logs for the given scope and return formatted text for prompt injection.
    Returns empty string if no logs found. Used to provide historical training context to AI."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT training_date, exercises FROM training_logs WHERE scope=? ORDER BY training_date DESC LIMIT ?",
            (scope, limit)
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return ""
    lines = ["\n\n## 近期训练记录（系统自动注入，供计划生成参考）"]
    for row in rows:
        date_str, ex_json = row[0], row[1]
        try:
            exercises = json.loads(ex_json)
        except (json.JSONDecodeError, TypeError):
            exercises = []
        ex_names = [e.get("name", "?") for e in exercises if isinstance(e, dict)]
        summary = ", ".join(ex_names[:6])
        if len(ex_names) > 6:
            summary += f" 等{len(ex_names)}个动作"
        lines.append(f"- {date_str}: {summary}")
    lines.append("以上为最近训练记录。生成新计划时请参考这些历史数据，避免短期内重复相同部位的高强度训练，并根据历史重量合理安排渐进负荷。")
    return "\n".join(lines)

def _detect_completed_training(user_text: str, assistant_text: str) -> list | None:
    """Detect if user confirmed completing a training session.
    Returns list of exercise dicts if confirmed, None otherwise.
    Only triggers on explicit user confirmation, never on plan generation.
    """
    ut = user_text.strip().lower()
    # User must explicitly say they completed/did a workout
    completion_patterns = [
        "完成了", "做完了", "练完了", "搞定了", "结束了",
        "今天练了", "刚练完", "已完成", "打卡",
        "done", "finished", "completed",
    ]
    if not any(p in ut for p in completion_patterns):
        return None
    # Extract exercise info from user text or assistant summary
    # Simple extraction: look for exercise-like patterns
    exercises = []
    # Try to parse structured info from user message
    # Pattern: "动作名 X组xY次 Zkg" or similar
    import re as _re
    ex_pattern = _re.compile(r"([\u4e00-\u9fff\w]+?)\s*(\d+)\s*[组]\s*[x×]?\s*(\d+)\s*(?:次|rep)?(?:\s*(\d+(?:\.\d+)?)\s*(?:kg|公斤))?", re.IGNORECASE)
    for m in ex_pattern.finditer(user_text):
        ex = {"name": m.group(1), "sets": int(m.group(2)), "reps": int(m.group(3))}
        if m.group(4):
            ex["weight"] = float(m.group(4))
        exercises.append(ex)
    # If no structured exercises found but user confirmed completion, record as generic
    if not exercises:
        exercises = [{"name": "用户确认完成的训练", "sets": 0, "reps": 0, "note": user_text[:200]}]
    return exercises



# ========== Natural Language Training Log Parsing ==========
import re as _nl_re
from datetime import datetime as _nl_dt, timedelta as _nl_td

# Date patterns for Chinese natural language
_DATE_PATTERNS = [
    # 2026年10月19日 / 2026-10-19 / 2026/10/19
    (_nl_re.compile(r"(\d{4})\s*[年\-/]\s*(\d{1,2})\s*[月\-/]\s*(\d{1,2})\s*日?"), "ymd"),
    # 10月19日 (current year)
    (_nl_re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日?"), "md"),
    # 昨天 / 前天 / 今天
    (_nl_re.compile(r"昨天|昨日"), "yesterday"),
    (_nl_re.compile(r"前天|前日"), "day_before"),
    (_nl_re.compile(r"今天|今日"), "today"),
]

# Completion indicators (past tense / completed tone)
_COMPLETION_MARKERS = [
    "练了", "做了", "打了", "跑了", "游了", "骑了", "跳了", "走了",
    "完成了", "做完了", "练完了", "搞定了", "结束了", "打卡",
    "刚练完", "已完成", "训练完", "运动完",
    "记录一下", "帮我记", "帮忙记录", "请记录", "存一下",
    "done", "finished", "completed",
]

# Activity type mapping
_ACTIVITY_MAP = {
    "胸": "strength", "背": "strength", "腿": "strength", "肩": "strength",
    "手臂": "strength", "三头": "strength", "二头": "strength", "核心": "strength",
    "臀": "strength", "力量": "strength", "举铁": "strength", "深蹲": "strength",
    "硬拉": "strength", "卧推": "strength", "引体": "strength", "划船": "strength",
    "篮球": "basketball", "打球": "basketball", "nba": "basketball",
    "跑步": "running", "跑": "running", "慢跑": "running", "长跑": "running",
    "游泳": "swimming", "游": "swimming",
    "骑车": "cycling", "骑行": "cycling", "自行车": "cycling",
    "瑜伽": "yoga", "拉伸": "stretching", "泡沫轴": "stretching",
    "拳击": "boxing", "搏击": "boxing",
    "羽毛球": "badminton", "乒乓": "table_tennis", "网球": "tennis",
    "足球": "soccer", "排球": "volleyball",
    "hiit": "hiit", "tabata": "hiit", "有氧": "cardio",
    "椭圆机": "cardio", "划船机": "cardio", "登山机": "cardio",
}

# 动作解析：按标点切段，每段里找「组数 × 次数」，重量可在段的任意位置
_EX_SET_REPS = _nl_re.compile(r"(\d+)\s*[组set]\s*[x×*]?\s*(\d+)\s*(?:次|rep)?", _nl_re.IGNORECASE)
_EX_X_REPS = _nl_re.compile(r"(\d+)\s*[x×*]\s*(\d+)\s*(?:次|rep)?", _nl_re.IGNORECASE)
_EX_WEIGHT = _nl_re.compile(r"(\d+(?:\.\d+)?)\s*(?:kg|公斤|千克)", _nl_re.IGNORECASE)
_EX_SEGMENT_SPLIT = _nl_re.compile(r"[，,、；;。\n]+")
_EX_NAME_PREFIX = _nl_re.compile(
    r"^(?:今天|昨天|前天|晚上|下午|上午|早上|中午|练了|做了|打了|练完|打卡|"
    r"记录一下|帮我记|还有|然后|接着|另外|再)\s*")


def _parse_exercises(text: str) -> list:
    """从自由文本里抽动作：[{name, sets, reps, weight?}]。

    只认「N组xM」或「NxM」这种明确写法，抽不到就返回空列表——
    「练了胸」这种不编造成「卧推 4 组」。
    """
    exercises = []
    for segment in _EX_SEGMENT_SPLIT.split(text):
        segment = segment.strip()
        if not segment:
            continue
        match = _EX_SET_REPS.search(segment)
        if not match:
            match = _EX_X_REPS.search(segment)
        if not match:
            continue
        name = segment[:match.start()]
        weight_match = _EX_WEIGHT.search(segment)
        weight = float(weight_match.group(1)) if weight_match else None
        # 去掉名字里的重量、尾部数字和起始动词
        name = _EX_WEIGHT.sub("", name)
        name = _EX_NAME_PREFIX.sub("", name.strip())
        name = _nl_re.sub(r"[\s\d.~～\-—:：]+$", "", name).strip(" 　")
        if not name or len(name) > 20 or name.isdigit():
            continue
        item = {"name": name, "sets": int(match.group(1)), "reps": int(match.group(2))}
        if weight is not None:
            item["weight"] = weight
        exercises.append(item)
    return exercises



def _extract_date(text: str) -> tuple:
    """Extract date from natural language text.
    Returns (date_str YYYY-MM-DD, is_explicit_bool).
    If no date found, returns (today, False).
    """
    now = _nl_dt.now(SHANGHAI_TZ)
    for pattern, dtype in _DATE_PATTERNS:
        m = pattern.search(text)
        if m:
            if dtype == "ymd":
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                try:
                    dt = _nl_dt(y, mo, d, tzinfo=SHANGHAI_TZ)
                    return dt.strftime("%Y-%m-%d"), True
                except ValueError:
                    continue
            elif dtype == "md":
                mo, d = int(m.group(1)), int(m.group(2))
                try:
                    dt = _nl_dt(now.year, mo, d, tzinfo=SHANGHAI_TZ)
                    if dt.date() > now.date():
                        dt = _nl_dt(now.year - 1, mo, d, tzinfo=SHANGHAI_TZ)
                    return dt.strftime("%Y-%m-%d"), True
                except ValueError:
                    continue
            elif dtype == "yesterday":
                return (now - _nl_td(days=1)).strftime("%Y-%m-%d"), True
            elif dtype == "day_before":
                return (now - _nl_td(days=2)).strftime("%Y-%m-%d"), True
            elif dtype == "today":
                return now.strftime("%Y-%m-%d"), True
    return now.strftime("%Y-%m-%d"), False


def _classify_activity(text: str) -> str:
    """Classify activity type from user text. Returns activity_type string."""
    tl = text.lower()
    for keyword, atype in _ACTIVITY_MAP.items():
        if keyword in tl:
            return atype
    return "other"


def _is_completed_tone(text: str) -> bool:
    """Check if user message uses completed/past tense indicating a finished activity."""
    tl = text.strip().lower()
    return any(marker in tl for marker in _COMPLETION_MARKERS)


def _parse_natural_language_log(text: str):
    """Parse natural language training log from user message.
    Returns dict with parsed fields or None if not a training log.
    Does NOT fabricate missing fields.
    """
    if not _is_completed_tone(text):
        return None

    date_str, has_explicit_date = _extract_date(text)
    activity_type = _classify_activity(text)

    result = {
        "training_date": date_str,
        "has_explicit_date": has_explicit_date,
        "activity_type": activity_type,
        "exercises": [],
        "duration_minutes": None,
        "intensity": "",
        "notes": "",
        "start_time": None,
    }
    # Extract start time: "晚上8点", "晚上8点半", "20点", "20:00", "8:00pm", "中午12点", "晚上8点05分"
    st_patterns = [
        # "HH:MM" or "HH：MM" with optional period prefix and am/pm
        (_nl_re.compile(r"(?:晚上|下午|上午|早上|中午)?\s*(\d{1,2})\s*[:：]\s*(\d{2})\s*(?:am|pm)?", _nl_re.IGNORECASE), "hm_colon"),
        # "晚上/下午 N点M分" or "晚上N点半"
        (_nl_re.compile(r"(?:晚上|下午)\s*(\d{1,2})\s*点\s*(?:(\d{1,2})\s*分|半)?"), "evening_h"),
        # "上午/早上 N点M分" or "上午N点半"
        (_nl_re.compile(r"(?:上午|早上)\s*(\d{1,2})\s*点\s*(?:(\d{1,2})\s*分|半)?"), "morning_h"),
        # "中午 N点M分" or "中午N点半"
        (_nl_re.compile(r"中午\s*(\d{1,2})\s*点\s*(?:(\d{1,2})\s*分|半)?"), "noon_h"),
        # Plain "N点M分" or "N点半" (no prefix)
        (_nl_re.compile(r"(\d{1,2})\s*点\s*(?:(\d{1,2})\s*分|半)?"), "plain_h"),
    ]
    for sp, stype in st_patterns:
        sm = sp.search(text)
        if sm:
            hour = int(sm.group(1))
            # Handle minute: could be digits, "半" (=30), or absent (=0)
            minute_raw = sm.group(2) if sm.lastindex and sm.lastindex >= 2 else None
            if minute_raw is not None and minute_raw != "半":
                minute = int(minute_raw)
            else:
                # Check if "半" appears after "点" in the matched text
                matched_text = sm.group(0)
                after_dian = matched_text.split("点")[-1] if "点" in matched_text else ""
                minute = 30 if "半" in after_dian else 0
            # Apply period-based hour conversion
            if stype == "evening_h" and hour < 12:
                hour += 12
            elif stype == "morning_h" and hour == 12:
                hour = 0
            elif stype == "noon_h":
                pass  # noon 12 stays 12
            elif stype == "hm_colon":
                prefix = sm.group(0).split(str(hour))[0] if sm.group(0) else ""
                if ("晚上" in prefix or "下午" in prefix) and hour < 12:
                    hour += 12
                elif ("上午" in prefix or "早上" in prefix) and hour == 12:
                    hour = 0
            elif stype == "plain_h":
                if "晚上" in text or "下午" in text or (hour >= 1 and hour <= 5):
                    pass
                elif hour < 12 and ("晚" in text or "夜" in text):
                    hour += 12
            # Validate ranges
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                result["start_time"] = f"{hour:02d}:{minute:02d}"
            break

    # Extract exercises for strength training
    if activity_type == "strength":
        exercises = _parse_exercises(text)
        if exercises:
            result["exercises"] = exercises

    # Extract duration if mentioned: supports "30分钟","1小时","1h30m","两小时","一个半小时","1小时57分钟","117分钟","半小时","1.5小时"
    _cn_num = {"零":0,"一":1,"二":2,"两":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9,"十":10,
               "十一":11,"十二":12,"十三":13,"十四":14,"十五":15,"十六":16,"十七":17,"十八":18,"十九":19,
               "二十":20,"三十":30,"四十":40,"五十":50,"六十":60,"七十":70,"八十":80,"九十":90}
    def _cn2int(s):
        """Convert Chinese number string to int. Supports 零~九十九 and Arabic digits/decimals."""
        s = s.strip()
        if not s:
            return None
        # Arabic digits (including decimals like "1.5")
        try:
            val = float(s)
            return val
        except ValueError:
            pass
        if s in _cn_num:
            return _cn_num[s]
        # "几十几" pattern: e.g. "二十三" -> 23
        m = _nl_re.match(r"([一二三四五六七八九])十([一二三四五六七八九])?", s)
        if m:
            tens = _cn_num.get(m.group(1), 1) * 10
            ones = _cn_num.get(m.group(2), 0) if m.group(2) else 0
            return tens + ones
        # "十几" pattern: e.g. "十五" -> 15
        m = _nl_re.match(r"十([一二三四五六七八九])?", s)
        if m:
            return 10 + (_cn_num.get(m.group(1), 0) if m.group(1) else 0)
        return None
    dur_patterns_cn = [
        # "半小时" / "半个小时" -> 30min (standalone "半" without preceding number)
        (_nl_re.compile(r"(?<![一二两三四五六七八九个\d])半\s*个?\s*(?:小时|h)"), "half_only"),
        # "一个半小时" / "两个半小时" -> N*60+30
        (_nl_re.compile(r"([一二两三四五六七八九十\d]+)\s*个?\s*半\s*(?:小时|h)"), "half_h"),
        # "1.5小时" / "1.5个小时" -> decimal hours
        (_nl_re.compile(r"(\d+(?:\.\d+)?)\s*个?\s*(?:小时|h)(?!\s*\d+\s*(?:分钟|m))"), "decimal_h"),
        # "两小时" / "两个小时" / "1小时57分钟" / "1小时"
        (_nl_re.compile(r"([一二两三四五六七八九十百\d]+)\s*个?\s*(?:小时|h)\s*([一二两三四五六七八九十\d]+)?\s*(?:分钟|m)?"), "hm"),
        # "117分钟" / "30分钟"
        (_nl_re.compile(r"([一二两三四五六七八九十百\d]+)\s*(?:分钟|min)(?!s)"), "m"),
    ]
    for dp, dtype in dur_patterns_cn:
        dm = dp.search(text)
        if dm:
            if dtype == "half_only":
                result["duration_minutes"] = 30
                break
            elif dtype == "half_h":
                h_val = _cn2int(dm.group(1))
                if h_val is not None:
                    result["duration_minutes"] = int(h_val * 60 + 30)
                break
            elif dtype == "decimal_h":
                h_val = _cn2int(dm.group(1))
                if h_val is not None:
                    result["duration_minutes"] = round(h_val * 60)
                break
            elif dtype == "hm":
                h_val = _cn2int(dm.group(1))
                m_val = _cn2int(dm.group(2)) if dm.group(2) else 0
                if h_val is not None and m_val is not None:
                    result["duration_minutes"] = int(h_val * 60 + m_val)
                break
            elif dtype == "m":
                m_val = _cn2int(dm.group(1))
                if m_val is not None:
                    result["duration_minutes"] = int(m_val)
                break

    # Extract intensity keywords
    intensity_map = {"低强度": "low", "中等强度": "medium", "高强度": "high",
                     "轻松": "low", "全力": "high", "max": "high",
                     "中高": "medium_high", "中高强度": "medium_high",
                     "中低": "medium_low", "中低强度": "medium_low"}
    for kw, val in intensity_map.items():
        if kw in text.lower():
            result["intensity"] = val
            break

    # Extract notes only when user explicitly provides them via keywords
    # Do NOT auto-generate notes from activity type or residual text
    _note_trigger = _nl_re.compile(
        r"(?:备注|补充|感受|感觉|注意|说明|附注|额外)[：:\s]*(.+?)(?:[。！]|$)",
        _nl_re.IGNORECASE
    )
    note_match = _note_trigger.search(text)
    if note_match:
        note_text = note_match.group(1).strip()
        # Clean punctuation
        note_text = _nl_re.sub(r"[，。、；！？\s]+$", "", note_text).strip()
        if note_text and len(note_text) > 1:
            result["notes"] = note_text[:500]
    # If no explicit note trigger, notes stays empty string (default)

    return result


# Strain-to-intensity deterministic mapping (constant, not LLM-generated)
_STRAIN_INTENSITY_MAP = [
    # (max_strain_exclusive, intensity_key, display_label)
    (5.0, "low", "低"),
    (10.0, "medium", "中等"),
    (15.0, "medium_high", "中高"),
    (20.0, "high", "高"),
    (float("inf"), "very_high", "极高"),
]

def strain_to_intensity(strain_val) -> tuple:
    """Convert WHOOP strain to deterministic intensity. Returns (key, display_label).
    Returns ("", "") for None, non-numeric, or out-of-range values.
    Deterministic: no LLM involvement."""
    if strain_val is None:
        return "", ""
    try:
        strain_val = float(strain_val)
    except (ValueError, TypeError):
        return "", ""
    if strain_val < 0 or strain_val > 50:
        return "", ""
    for threshold, key, label in _STRAIN_INTENSITY_MAP:
        if strain_val < threshold:
            return key, label
    return "", ""


def _try_match_whoop_workout(training_date: str, activity_type: str,
                              user_start_time: str = None,
                              user_duration_minutes: int = None) -> dict:
    """Try to match a local training log with a WHOOP workout record.
    Uses case-insensitive sport matching + time/duration proximity scoring.
    Returns dict with matched status, WHOOP data, and ambiguity flag.
    Never fabricates exercises, sets, or weight from WHOOP.
    Deterministic: no LLM involvement in match decision.
    """
    result = {
        "matched": False, "whoop_date": "", "sport_name": "",
        "whoop_workout_id": "", "duration_minutes": None,
        "strain": None, "max_heart_rate": None, "kilojoule": None,
        "start_cst": "", "end_cst": "",
        "ambiguous": False, "candidate_count": 0,
        "match_status": "unmatched",
    }
    try:
        dt = _nl_dt.strptime(training_date, "%Y-%m-%d")
        year, month = dt.year, dt.month
        wkt = get_monthly_workouts(year, month)
        if wkt.get("status") != "ok" or not wkt.get("records"):
            return result
        whoop_sport_map = {
            "basketball": ["basketball"],
            "running": ["running", "jogging"],
            "swimming": ["swimming"],
            "cycling": ["cycling", "biking"],
            "yoga": ["yoga"],
            "boxing": ["boxing", "martial arts"],
            "strength": ["weightlifting", "strength", "functional fitness", "crossfit",
                         "weightlifting_msk", "strength training", "resistance training"],
            "cardio": ["cardio", "elliptical", "rowing", "stair climber"],
            "hiit": ["hiit", "tabata", "functional fitness"],
            "badminton": ["badminton"],
            "tennis": ["tennis"],
            "soccer": ["soccer"],
            "volleyball": ["volleyball"],
            "table_tennis": ["table tennis"],
        }
        # Normalize activity_type for case-insensitive matching (Step 1)
        activity_type_normalized = (activity_type or "").strip().lower()
        possible_sports_lower = whoop_sport_map.get(activity_type_normalized, [])
        if not possible_sports_lower and activity_type_normalized != "other":
            return result

        # Collect already-linked workout_ids from DB to prevent duplicate association
        already_linked = set()
        try:
            conn = get_db()
            rows = conn.execute(
                "SELECT whoop_workout_id FROM training_logs WHERE whoop_matched = 1 AND whoop_workout_id != ''"
            ).fetchall()
            already_linked = {r[0] for r in rows if r[0]}
            conn.close()
        except Exception:
            pass

        # Filter candidates: same date + matching sport + not already linked
        target_date_prefix = training_date
        candidates = []
        for rec in wkt["records"]:
            rec_date = rec.get("date", "") or rec.get("date_cst", "")
            if not rec_date.startswith(target_date_prefix):
                continue
            sport = (rec.get("sport_name", "") or "").strip().lower()
            if activity_type_normalized == "other":
                pass  # accept any sport for "other"
            elif sport not in possible_sports_lower:
                continue
            wid = rec.get("workout_id", "") or rec.get("id", "")
            if wid and wid in already_linked:
                continue  # skip already-associated workouts
            candidates.append(rec)

        result["candidate_count"] = len(candidates)
        if len(candidates) == 0:
            return result

        # Parse user start_time into minutes-from-midnight for comparison
        user_minutes = None
        if user_start_time:
            try:
                uh, um = map(int, user_start_time.split(":"))
                if 0 <= uh <= 23 and 0 <= um <= 59:
                    user_minutes = uh * 60 + um
            except (ValueError, AttributeError):
                pass

        # Score each candidate by (time_diff, duration_diff)
        def _score(rec):
            time_diff = 9999
            dur_diff = 9999
            # Time proximity
            start_cst = rec.get("start_cst", "")
            if user_minutes is not None and start_cst and " " in start_cst:
                try:
                    time_part = start_cst.split(" ")[1]
                    rh, rm = map(int, time_part.split(":")[:2])
                    time_diff = abs((rh * 60 + rm) - user_minutes)
                except (ValueError, IndexError):
                    pass
            # Duration proximity
            dur_ms = rec.get("duration_ms") or rec.get("duration")
            if user_duration_minutes is not None and dur_ms:
                whoop_dur_min = round(dur_ms / 60000)
                dur_diff = abs(whoop_dur_min - user_duration_minutes)
            return (time_diff, dur_diff)

        if len(candidates) == 1:
            best = candidates[0]
        else:
            # Sort by (time_diff, duration_diff); pick best
            scored = [(c, _score(c)) for c in candidates]
            scored.sort(key=lambda x: x[1])
            best_score = scored[0][1]
            second_score = scored[1][1] if len(scored) > 1 else None
            # If top two have identical scores, mark ambiguous
            if second_score is not None and best_score == second_score:
                result["ambiguous"] = True
                result["match_status"] = "ambiguous"
                # Attach top candidates (up to 3) for user selection
                result["ambiguous_candidates"] = [c for c, _ in scored[:3]]
                logger.info(
                    "WHOOP_MATCH | date=%s | local_type=%s | candidates=%d | matched=False (ambiguous, tied scores)",
                    training_date, activity_type, len(candidates)
                )
                return result
            best = scored[0][0]

        # Extract WHOOP data from best match
        dur_ms = best.get("duration_ms") or best.get("duration")
        dur_min = round(dur_ms / 60000) if dur_ms else None
        result.update({
            "matched": True,
            "match_status": "matched",
            "whoop_date": best.get("date", "") or best.get("date_cst", ""),
            "sport_name": best.get("sport_name", ""),
            "whoop_workout_id": best.get("workout_id", "") or best.get("id", ""),
            "duration_minutes": dur_min,  # WHOOP actual duration, not user-stated
            "strain": best.get("strain"),
            "max_heart_rate": best.get("max_heart_rate"),
            "kilojoule": best.get("kilojoule"),
            "start_cst": best.get("start_cst", ""),
            "end_cst": best.get("end_cst", ""),
        })
        logger.info(
            "WHOOP_MATCH | date=%s | local_type=%s | whoop_sport=%s | workout_id=%s | whoop_dur=%s | matched=True",
            training_date, activity_type, result["sport_name"], result["whoop_workout_id"], dur_min
        )
    except Exception as e:
        logger.warning("WHOOP match attempt failed: %s", type(e).__name__)
    return result

def save_training_log_v2(scope: str, message_id: str, parsed: dict, override_whoop_match: dict = None):
    """Save a parsed natural language training log with extended fields.
    Also attempts WHOOP matching. If override_whoop_match is provided, use it instead of matching.
    Returns (success: bool, whoop_match: dict, save_result: dict).
    """
    if not scope:
        return False, {"matched": False, "whoop_date": "", "sport_name": "", "match_status": "unmatched"}, {}
    if override_whoop_match is not None:
        whoop_match = override_whoop_match
    else:
        whoop_match = _try_match_whoop_workout(
            parsed["training_date"],
            parsed.get("activity_type", ""),
            user_start_time=parsed.get("start_time"),
            user_duration_minutes=parsed.get("duration_minutes"),
        )
    conn = get_db()
    try:
        exercises_json = json.dumps(parsed.get("exercises", []), ensure_ascii=False)
        # Use WHOOP actual duration when matched; otherwise use user-stated duration
        final_duration = whoop_match.get("duration_minutes") if whoop_match.get("matched") else parsed.get("duration_minutes")
        # Intensity priority: user-stated > WHOOP strain mapping > empty
        user_intensity = parsed.get("intensity", "")
        if user_intensity:
            final_intensity = user_intensity
        elif whoop_match.get("matched") and whoop_match.get("strain") is not None:
            strain_key, _ = strain_to_intensity(whoop_match["strain"])
            final_intensity = strain_key
        else:
            final_intensity = ""
        conn.execute(
            """INSERT INTO training_logs
               (scope, training_date, exercises, source_message_id,
                activity_type, duration_minutes, intensity, notes,
                whoop_matched, whoop_workout_date, whoop_workout_id,
                whoop_match_status, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                scope,
                parsed["training_date"],
                exercises_json,
                message_id,
                parsed.get("activity_type", ""),
                final_duration,
                final_intensity,
                parsed.get("notes", ""),
                1 if whoop_match.get("matched") else 0,
                whoop_match.get("whoop_date", ""),
                whoop_match.get("whoop_workout_id", ""),
                whoop_match.get("match_status", "unmatched"),
                "user",
            )
        )
        conn.commit()
        logger.info(
            "Training log v2 saved: date=%s type=%s whoop_matched=%s msg=%s",
            parsed["training_date"],
            parsed.get("activity_type", ""),
            whoop_match["matched"],
            message_id
        )
        save_result = {
            "final_duration": final_duration,
            "final_intensity": final_intensity,
            "match_status": whoop_match.get("match_status", "unmatched"),
        }
        save_result = {
            "final_duration": final_duration,
            "final_intensity": final_intensity,
            "match_status": whoop_match.get("match_status", "unmatched"),
        }
        return True, whoop_match, save_result
    except Exception as e:
        logger.error("Failed to save training log v2: %s: %s", type(e).__name__, e)
        return False, whoop_match, {}
    finally:
        conn.close()


def _build_log_confirmation(parsed: dict, whoop_match: dict, save_result: dict = None) -> str:
    """Build a deterministic Chinese confirmation message after successful DB write.
    All fields come from save_result or whoop_match — never from LLM inference.
    Never fabricates data. Missing fields are omitted or shown as '未记录'.
    """
    sr = save_result or {}
    lines = ["✅ 训练记录已保存"]

    # Date
    date_label = parsed.get("training_date", "未知")
    if parsed.get("has_explicit_date"):
        date_label += "（历史补录）"
    else:
        date_label += "（当天）"
    lines.append(f"📅 日期: {date_label}")

    # Activity type
    atype = parsed.get("activity_type", "other")
    atype_labels = {
        "strength": "💪 力量训练", "basketball": "🏀 篮球",
        "running": "🏃 跑步", "swimming": "🏊 游泳",
        "cycling": "🚴 骑行", "yoga": "🧘 瑜伽",
        "boxing": "🥊 拳击", "cardio": "❤️ 有氧",
        "hiit": "🔥 HIIT", "stretching": "🙆 拉伸",
        "badminton": "🏸 羽毛球", "tennis": "🎾 网球",
        "soccer": "⚽ 足球", "volleyball": "🏐 排球",
        "table_tennis": "🏓 乒乓球", "other": "🏅 其他运动",
    }
    lines.append(f"🏷 活动: {atype_labels.get(atype, atype)}")

    # Exercises detail (for strength training)
    exercises = parsed.get("exercises", [])
    if exercises:
        lines.append("")
        lines.append("📋 动作详情:")
        for i, ex in enumerate(exercises, 1):
            name = ex.get("name", "未知动作")
            sets_val = ex.get("sets")
            reps_val = ex.get("reps")
            weight_val = ex.get("weight")
            sets_str = f"{sets_val}组" if sets_val else "未填写"
            reps_str = f"{reps_val}次" if reps_val else "未填写"
            weight_str = f"{weight_val}kg" if weight_val is not None else "未填写"
            lines.append(f"  {i}. {name}: {sets_str} × {reps_str}，重量 {weight_str}")
    elif atype == "strength":
        lines.append("")
        lines.append("📋 动作详情: 未填写（未提供具体动作、组数、次数或重量）")

    # Duration: use final saved value (WHOOP actual when matched)
    dur = sr.get("final_duration") or parsed.get("duration_minutes")
    if dur:
        h, m = divmod(int(dur), 60)
        dur_str = f"{h}小时{m}分钟" if h else f"{m}分钟"
        lines.append(f"⏱ 时长: {dur_str}")
    else:
        lines.append("⏱ 时长: 未记录")

    # Time range from WHOOP when available
    start_cst = whoop_match.get("start_cst", "") if whoop_match else ""
    end_cst = whoop_match.get("end_cst", "") if whoop_match else ""
    if start_cst and " " in start_cst:
        start_time_display = start_cst.split(" ")[1][:5]
        end_time_display = end_cst.split(" ")[1][:5] if end_cst and " " in end_cst else "未记录"
        lines.append(f"🕐 时间: {start_time_display}-{end_time_display}")
    elif parsed.get("start_time"):
        lines.append(f"🕐 开始时间: {parsed['start_time']}")

    # Intensity with source attribution
    final_intensity = sr.get("final_intensity", "")
    user_intensity = parsed.get("intensity", "")
    int_labels = {"low": "低", "medium": "中等", "medium_high": "中高",
                  "high": "高", "very_high": "极高", "medium_low": "中低"}
    if final_intensity:
        label = int_labels.get(final_intensity, final_intensity)
        if user_intensity:
            # User-stated intensity: no WHOOP attribution
            lines.append(f"🔥 强度: {label}")
        elif whoop_match and whoop_match.get("matched") and whoop_match.get("strain") is not None:
            # WHOOP-derived intensity: show strain source
            lines.append(f"🔥 强度: {label}（根据 WHOOP Strain {whoop_match['strain']} 评估）")
        else:
            lines.append(f"🔥 强度: {label}")
    else:
        lines.append("🔥 强度: 未记录")

    # Notes
    notes = parsed.get("notes", "")
    if notes:
        lines.append(f"💬 备注: {notes[:50]}{'...' if len(notes) > 50 else ''}")

    # WHOOP match status — deterministic, three states
    match_status = sr.get("match_status", whoop_match.get("match_status", "unmatched") if whoop_match else "unmatched")
    if match_status == "matched" and whoop_match and whoop_match.get("matched"):
        lines.append("")
        lines.append("📱 WHOOP: 已关联")
        # Strain
        strain_val = whoop_match.get("strain")
        if strain_val is not None:
            lines.append(f"  Strain: {strain_val}")
        # Heart rate
        max_hr = whoop_match.get("max_heart_rate")
        if max_hr is not None:
            lines.append(f"  本次运动最大心率: {max_hr} bpm")
        # Energy
        kj = whoop_match.get("kilojoule")
        if kj is not None:
            lines.append(f"  能量消耗: {kj} kJ")
    elif match_status == "ambiguous":
        cand_count = whoop_match.get("candidate_count", 0) if whoop_match else 0
        lines.append(f"📱 WHOOP: 存在 {cand_count} 个候选，未关联")
    else:
        lines.append("📱 WHOOP: 未关联")

    return "\n".join(lines)

def _build_log_failure_message(parsed: dict) -> str:
    """Build a failure message when DB write fails. Clearly states failure."""
    lines = ["❌ 训练记录保存失败"]
    lines.append(f"📅 尝试保存的日期: {parsed.get('training_date', '未知')}")
    atype = parsed.get("activity_type", "other")
    atype_labels = {
        "strength": "力量训练", "basketball": "篮球",
        "running": "跑步", "swimming": "游泳",
        "cycling": "骑行", "yoga": "瑜伽",
        "other": "其他运动",
    }
    lines.append(f"🏷 活动类型: {atype_labels.get(atype, atype)}")
    lines.append("")
    lines.append("数据库写入异常，本次记录未保存。请稍后重新发送训练内容再试一次。")
    return "\n".join(lines)


def handle_message(event: P2ImMessageReceiveV1):
    if event.event.message.message_type != "text":
        return
    scope = memory_scope(event)
    # Serialize requests from the same conversation and sender.
    lock_key = scope or event.event.message.chat_id
    with _memory_locks_guard:
        lock = _memory_locks.setdefault(lock_key, threading.Lock())
    with lock:
        _handle_message(event)

def _handle_message(event: P2ImMessageReceiveV1):
    msg = event.event.message
    message_id = msg.message_id
    chat_id = msg.chat_id
    cli = get_lark_client()
    if msg.message_type != "text":
        return
    try:
        payload = json.loads(msg.content or "{}")
        content = payload.get("text", "").strip()
    except (json.JSONDecodeError, AttributeError):
        logger.warning("Invalid message content")
        return

    # Dedup: only one worker can claim this message_id
    if not try_claim_message(message_id):
        logger.info(f"Duplicate event skipped: message_id={message_id}")
        return

    scope = memory_scope(event)
    ai_completed = False
    # Step 4C1/4C2: Intercept confirm/cancel/choice for pending states
    stripped_content = content.strip()
    pending_confirm = _peek_pending_confirm(scope) if scope else None
    pending_select = _peek_pending_select(scope) if scope else None
    has_pending = pending_confirm or pending_select

    # Handle choice selection for ambiguous candidates
    if pending_select:
        choice_match = _CHOICE_PATTERN.match(stripped_content)
        if choice_match:
            idx = int(choice_match.group(1)) - 1  # 0-based
            entry = _consume_pending_select(scope)
            if entry is not None:
                candidates = entry.get("candidates", [])
                if 0 <= idx < len(candidates):
                    chosen = candidates[idx]
                    saved_parsed = entry["parsed"]
                    # Check if chosen workout already linked
                    wid = chosen.get("workout_id", "") or chosen.get("id", "")
                    already_linked = set()
                    try:
                        conn_chk = get_db()
                        rows_chk = conn_chk.execute(
                            "SELECT whoop_workout_id FROM training_logs WHERE whoop_matched = 1 AND whoop_workout_id != ''"
                        ).fetchall()
                        already_linked = {r[0] for r in rows_chk if r[0]}
                        conn_chk.close()
                    except Exception:
                        pass
                    if wid and wid in already_linked:
                        confirmation = "⚠️ 该 WHOOP 运动记录已被其他训练关联，无法重复关联。请重新发送训练信息。"
                    else:
                        # Build a whoop_match from chosen candidate for save
                        dur_ms = chosen.get("duration_ms") or chosen.get("duration")
                        dur_min = round(dur_ms / 60000) if dur_ms else None
                        chosen_match = {
                            "matched": True, "match_status": "matched",
                            "whoop_date": chosen.get("date", "") or chosen.get("date_cst", ""),
                            "sport_name": chosen.get("sport_name", ""),
                            "whoop_workout_id": wid,
                            "duration_minutes": dur_min,
                            "strain": chosen.get("strain"),
                            "max_heart_rate": chosen.get("max_heart_rate"),
                            "kilojoule": chosen.get("kilojoule"),
                            "start_cst": chosen.get("start_cst", ""),
                            "end_cst": chosen.get("end_cst", ""),
                        }
                        save_ok, whoop_match, save_result = save_training_log_v2(
                            scope, message_id, saved_parsed,
                            override_whoop_match=chosen_match,
                        )
                        if save_ok:
                            confirmation = _build_log_confirmation(saved_parsed, whoop_match or {}, save_result or {})
                        else:
                            confirmation = _build_log_failure_message(saved_parsed)
                else:
                    confirmation = "选择无效，请回复候选编号或「取消」。"
            else:
                confirmation = "待选择记录已过期，请重新发送训练信息。"
            try:
                sel_req = ReplyMessageRequest.builder() \
                    .message_id(message_id) \
                    .request_body(
                        ReplyMessageRequestBody.builder()
                        .msg_type("text")
                        .content(json.dumps({"text": confirmation}, ensure_ascii=False))
                        .build()
                    ).build()
                cli.im.v1.message.reply(sel_req)
                logger.info("Choice selection handled for %s", message_id)
            except Exception as ce:
                logger.warning("Choice reply send failed: %s", type(ce).__name__)
            return
        elif stripped_content in _CANCEL_KEYWORDS:
            _consume_pending_select(scope)
            try:
                cancel_req = ReplyMessageRequest.builder() \
                    .message_id(message_id) \
                    .request_body(
                        ReplyMessageRequestBody.builder()
                        .msg_type("text")
                        .content(json.dumps({"text": "已取消，本次训练未保存。"}, ensure_ascii=False))
                        .build()
                    ).build()
                cli.im.v1.message.reply(cancel_req)
                logger.info("Pending select cancelled for %s", message_id)
            except Exception as ce:
                logger.warning("Cancel reply send failed: %s", type(ce).__name__)
            return

    # Handle confirm/cancel for unmatched pending
    if pending_confirm and stripped_content in _CONFIRM_KEYWORDS:
        entry = _consume_pending_confirm(scope)
        if entry is not None:
            saved_parsed = entry["parsed"]
            save_ok, whoop_match, save_result = save_training_log_v2(scope, message_id, saved_parsed)
            if save_ok:
                confirmation = _build_log_confirmation(saved_parsed, whoop_match or {}, save_result or {})
            else:
                confirmation = _build_log_failure_message(saved_parsed)
            try:
                confirm_req = ReplyMessageRequest.builder() \
                    .message_id(message_id) \
                    .request_body(
                        ReplyMessageRequestBody.builder()
                        .msg_type("text")
                        .content(json.dumps({"text": confirmation}, ensure_ascii=False))
                        .build()
                    ).build()
                cli.im.v1.message.reply(confirm_req)
                logger.info("Pending confirm saved for %s (save_ok=%s)", message_id, save_ok)
            except Exception as ce:
                logger.warning("Confirm reply send failed: %s", type(ce).__name__)
            return
        else:
            try:
                exp_req = ReplyMessageRequest.builder() \
                    .message_id(message_id) \
                    .request_body(
                        ReplyMessageRequestBody.builder()
                        .msg_type("text")
                        .content(json.dumps({"text": "待确认记录已过期，请重新发送训练信息。"}, ensure_ascii=False))
                        .build()
                    ).build()
                cli.im.v1.message.reply(exp_req)
            except Exception:
                pass
            return
    elif pending_confirm and stripped_content in _CANCEL_KEYWORDS:
        _consume_pending_confirm(scope)
        try:
            cancel_req = ReplyMessageRequest.builder() \
                .message_id(message_id) \
                .request_body(
                    ReplyMessageRequestBody.builder()
                    .msg_type("text")
                    .content(json.dumps({"text": "已取消，本次训练未保存。"}, ensure_ascii=False))
                    .build()
                ).build()
            cli.im.v1.message.reply(cancel_req)
            logger.info("Pending confirm cancelled for %s", message_id)
        except Exception as ce:
            logger.warning("Cancel reply send failed: %s", type(ce).__name__)
        return
    elif not has_pending and stripped_content in (_CONFIRM_KEYWORDS | _CANCEL_KEYWORDS):
        return
    if content == "/status":
        reply = "✅ Fitness Agent 运行正常"
    elif content == "/help":
        reply = HELP_TEXT
    elif content.strip().lower() in ("/whoop", "whoop", "whoop数据", "查看whoop", "查询whoop"):
        dashboard, ok = _fetch_whoop_with_logging(force_refresh=True)
        if ok:
            try:
                structured = get_whoop_dashboard_data()
                whoop_card = _build_whoop_card(structured)
                cli = get_lark_client()
                req = _build_card_request(message_id, whoop_card)
                resp = cli.im.v1.message.reply(req)
                if resp.success():
                    mark_sent(message_id)
                    logger.info("WHOOP card sent for message %s", message_id)
                else:
                    logger.error("WHOOP card reply failed: code=%s msg=%s", resp.code, resp.msg)
                    mark_failed(message_id)
                try:
                    save_turn(scope, message_id, content, dashboard)
                except Exception:
                    pass
                return
            except Exception as e:
                logger.error("WHOOP card build failed: %s, falling back to text", type(e).__name__)
                reply = dashboard
        else:
            reply = "⚠️ WHOOP API 请求失败，请稍后重试。本次未能获取实时数据。"
    elif content.strip().lower().startswith("/月报") or content.strip().lower().startswith("/monthly") or "月度总结" in content:
        _handle_monthly_summary(event, message_id, chat_id, content, scope)
        return
    else:
        try:
            # Build system prompt fresh for EVERY request → dynamic age
            system_prompt = build_system_prompt(load_profile())
            # Inject recent training logs for plan generation context
            recent_logs = _load_recent_training_logs(scope)
            if recent_logs:
                system_prompt += recent_logs
            system_prompt += (
                "\n历史对话仅用于理解上下文。历史中的 WHOOP 指标不能当成本次"
                "实时数据，历史建议和用户描述也不等于已验证事实。"
                "恢复评分不能证明疲劳原因，不把相关性写成因果。"
            )
            history = load_history(scope)
            is_refresh = _is_whoop_refresh_intent(content)
            whoop_keywords = (
                "whoop", "恢复", "睡眠", "训练", "健身", "运动",
                "篮球", "力量", "练", "心率", "hrv", "strain", "疲劳"
            )
            should_fetch_whoop = is_refresh or any(word in content.lower() for word in whoop_keywords)
            if should_fetch_whoop:
                dashboard, ok = _fetch_whoop_with_logging(force_refresh=is_refresh)
                if ok:
                    system_prompt += (
                        "\n\n## 本次 WHOOP 数据（后台集成自动获取，查询成功）\n"
                        + dashboard
                        + "\n\n以上数据由后台 WHOOP 集成本次实时获取，查询成功。"
                        "回复时以「已获取 WHOOP 数据」开头，列出各条记录的实际日期，再回答问题。"
                        "区分「本次查询时间」与「WHOOP 记录时间」，不以条目数量或返回日期判断是否有更新。"
                        "部分字段缺失时只说明该字段未返回，不笼统说无更新或无记录。"
                        "结合个人档案给出建议。如时间未注明时区，不自行推断时区。"
                        "WHOOP 身体指标与档案冲突时，说明来源差异。"
                        "不要要求用户重复提供已经列出的指标。"
                    )
                    if is_refresh:
                        system_prompt += (
                            "\n\n【重要】用户明确要求刷新/同步 WHOOP 数据，以上为本次 API 实时拉取结果。"
                            "禁止使用历史对话中的旧 WHOOP 数据，必须以本次获取的数据为准。"
                        )
                    logger.info("WHOOP context attached to AI request (force_refresh=%s)", is_refresh)
                else:
                    fail_time = datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S CST")
                    system_prompt += (
                        "\n\n## WHOOP 数据获取失败\n"
                        f"API 请求失败时间: {fail_time}\n"
                        "失败原因: WHOOP API 调用异常（网络超时、认证失败或服务端错误）\n"
                        "缓存状态: 无可用本地缓存（本系统不使用数据级缓存，每次均实时请求 API）\n"
                        "准确告知用户「WHOOP API 请求失败」并说明上述失败原因。"
                        "不要说「我无法读取」「无法自行再次拉取」或暗示自身无此能力。"
                        "禁止编造恢复、睡眠或运动指标。"
                        "如引用历史对话中的旧数据，必须标注其日期和来源（如「根据 X月X日 的历史记录」），"
                        "并明确说明该数据非本次实时获取。"
                        "如提供建议，说明它仅基于个人档案和用户描述，不含实时 WHOOP 数据。"
                    )
            resp = deepseek_client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=(
                    [{"role": "system", "content": system_prompt}]
                    + history
                    + [{"role": "user", "content": content}]
                ),
                max_tokens=1024,
            )
            reply = resp.choices[0].message.content
            if not isinstance(reply, str) or not reply.strip():
                raise ValueError("Empty AI reply")
            ai_completed = True
        except Exception as e:
            logger.error(f"DeepSeek error for {message_id}: {type(e).__name__}")
            mark_failed(message_id)
            reply = "❌ AI 调用暂时不可用，请稍后重试。"

    # Try to parse training plan card(s) from AI reply
    cards, clean_text = _try_parse_plan_card(reply)
    cli = get_lark_client()
    if cards:
        all_ok = True
        for idx, card in enumerate(cards):
            if idx == 0:
                req = _build_card_request(message_id, card)
                try:
                    resp = cli.im.v1.message.reply(req)
                    if resp.success():
                        mark_sent(message_id)
                        logger.info("Card %d/%d sent for message %s", idx + 1, len(cards), message_id)
                    else:
                        logger.error("Card reply API error: code=%s msg=%s", resp.code, resp.msg)
                        all_ok = False
                except Exception as e:
                    logger.error("Card reply exception: %s", type(e).__name__)
                    all_ok = False
            else:
                from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
                chat_id = event.event.message.chat_id
                req2 = CreateMessageRequest.builder() \
                    .receive_id_type("chat_id") \
                    .request_body(
                        CreateMessageRequestBody.builder()
                        .receive_id(chat_id)
                        .msg_type("interactive")
                        .content(json.dumps(card, ensure_ascii=False))
                        .build()
                    ).build()
                try:
                    resp2 = cli.im.v1.message.create(req2)
                    if resp2.success():
                        logger.info("Card %d/%d sent as new message", idx + 1, len(cards))
                    else:
                        logger.error("Extra card send failed: code=%s msg=%s", resp2.code, resp2.msg)
                        all_ok = False
                except Exception as e2:
                    logger.error("Extra card exception: %s", type(e2).__name__)
                    all_ok = False
        if all_ok and ai_completed:
            try:
                save_turn(scope, message_id, content, reply)
            except Exception as exc:
                logger.error("Memory save failed: %s", type(exc).__name__)
            # If AI reply had extra text beyond the plan JSON, send it as plain text
            if clean_text and clean_text.strip():
                try:
                    extra_req = ReplyMessageRequest.builder() \
                        .message_id(message_id) \
                        .request_body(
                            ReplyMessageRequestBody.builder()
                            .msg_type("text")
                            .content(json.dumps({"text": clean_text.strip()}, ensure_ascii=False))
                            .build()
                        ).build()
                    cli.im.v1.message.reply(extra_req)
                except Exception:
                    pass
            # Detect and persist completed training (v2: natural language + backfill)
            # IMPORTANT: Save first, only reply confirmation after successful DB write
            try:
                nl_parsed = _parse_natural_language_log(content)
                if nl_parsed:
                    wants_whoop = _user_requests_whoop_sync(content)
                    if wants_whoop:
                        pre_match = _try_match_whoop_workout(
                            nl_parsed["training_date"],
                            nl_parsed.get("activity_type", ""),
                            user_start_time=nl_parsed.get("start_time"),
                            user_duration_minutes=nl_parsed.get("duration_minutes"),
                        )
                        if not pre_match.get("matched") and pre_match.get("match_status") == "unmatched":
                            _store_pending_confirm(scope, nl_parsed, pre_match)
                            confirmation = _build_pending_confirm_prompt(nl_parsed)
                        elif pre_match.get("match_status") == "ambiguous":
                            _store_pending_select(scope, nl_parsed, pre_match.get("ambiguous_candidates", []))
                            confirmation = _build_ambiguous_prompt(nl_parsed, pre_match.get("ambiguous_candidates", []))
                        else:
                            save_ok, whoop_match, save_result = save_training_log_v2(scope, message_id, nl_parsed)
                            if save_ok:
                                confirmation = _build_log_confirmation(nl_parsed, whoop_match or {}, save_result or {})
                            else:
                                confirmation = _build_log_failure_message(nl_parsed)
                    else:
                        save_ok, whoop_match, save_result = save_training_log_v2(scope, message_id, nl_parsed)
                        if save_ok:
                            confirmation = _build_log_confirmation(nl_parsed, whoop_match or {}, save_result or {})
                        else:
                            confirmation = _build_log_failure_message(nl_parsed)
                    try:
                        confirm_req = ReplyMessageRequest.builder() \
                            .message_id(message_id) \
                            .request_body(
                                ReplyMessageRequestBody.builder()
                                .msg_type("text")
                                .content(json.dumps({"text": confirmation}, ensure_ascii=False))
                                .build()
                            ).build()
                        cli.im.v1.message.reply(confirm_req)
                        logger.info("NL training log reply sent for %s (save_ok=%s)", message_id, save_ok)
                    except Exception as ce:
                        logger.warning("Confirmation send failed: %s", type(ce).__name__)
                else:
                    # Fallback to old detection for backward compatibility
                    detected = _detect_completed_training(content, reply)
                    if detected:
                        today_str = datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d")
                        save_training_log(scope, message_id, today_str, detected)
            except Exception as exc:
                logger.error("Training detection failed: %s", type(exc).__name__)
        elif not all_ok:
            mark_failed(message_id)
    else:
        # Strip any bare JSON code blocks from text reply (plans are shown as cards only)
        import re as _strip_re
        display_reply = _strip_re.sub(r"```json\s*\{.*?\}\s*```", "", reply, flags=_strip_re.DOTALL).strip()
        if not display_reply:
            display_reply = reply  # Fallback if stripping removed everything
        req = ReplyMessageRequest.builder() \
            .message_id(message_id) \
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type("text")
                .content(json.dumps({"text": display_reply}, ensure_ascii=False))
                .build()
            ).build()
        try:
            resp = cli.im.v1.message.reply(req)
            if resp.success():
                mark_sent(message_id)
                if ai_completed:
                    try:
                        save_turn(scope, message_id, content, reply)
                    except Exception as exc:
                        logger.error("Memory save failed: %s", type(exc).__name__)
                    # Detect and persist completed training (v2: natural language + backfill)
                    # IMPORTANT: Save first, only reply confirmation after successful DB write
                    try:
                        nl_parsed = _parse_natural_language_log(content)
                        if nl_parsed:
                            wants_whoop = _user_requests_whoop_sync(content)
                            if wants_whoop:
                                pre_match = _try_match_whoop_workout(
                                    nl_parsed["training_date"],
                                    nl_parsed.get("activity_type", ""),
                                    user_start_time=nl_parsed.get("start_time"),
                                    user_duration_minutes=nl_parsed.get("duration_minutes"),
                                )
                                if not pre_match.get("matched") and pre_match.get("match_status") == "unmatched":
                                    _store_pending_confirm(scope, nl_parsed, pre_match)
                                    confirmation = _build_pending_confirm_prompt(nl_parsed)
                                elif pre_match.get("match_status") == "ambiguous":
                                    _store_pending_select(scope, nl_parsed, pre_match.get("ambiguous_candidates", []))
                                    confirmation = _build_ambiguous_prompt(nl_parsed, pre_match.get("ambiguous_candidates", []))
                                else:
                                    save_ok, whoop_match, save_result = save_training_log_v2(scope, message_id, nl_parsed)
                                    if save_ok:
                                        confirmation = _build_log_confirmation(nl_parsed, whoop_match or {}, save_result or {})
                                    else:
                                        confirmation = _build_log_failure_message(nl_parsed)
                            else:
                                save_ok, whoop_match, save_result = save_training_log_v2(scope, message_id, nl_parsed)
                                if save_ok:
                                    confirmation = _build_log_confirmation(nl_parsed, whoop_match or {}, save_result or {})
                                else:
                                    confirmation = _build_log_failure_message(nl_parsed)
                            try:
                                confirm_req = ReplyMessageRequest.builder() \
                                    .message_id(message_id) \
                                    .request_body(
                                        ReplyMessageRequestBody.builder()
                                        .msg_type("text")
                                        .content(json.dumps({"text": confirmation}, ensure_ascii=False))
                                        .build()
                                    ).build()
                                cli.im.v1.message.reply(confirm_req)
                                logger.info("NL training log reply sent for %s (save_ok=%s)", message_id, save_ok)
                            except Exception as ce:
                                logger.warning("Confirmation send failed: %s", type(ce).__name__)
                        else:
                            detected = _detect_completed_training(content, reply)
                            if detected:
                                today_str = datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d")
                                save_training_log(scope, message_id, today_str, detected)
                    except Exception as exc:
                        logger.error("Training detection failed: %s", type(exc).__name__)
            else:
                logger.error(f"Reply API error for {message_id}: code={resp.code}, msg={resp.msg}")
                mark_failed(message_id)
        except Exception as e:
            logger.error(f"Reply exception for {message_id}: {type(e).__name__}")
            mark_failed(message_id)
# ---------- Feishu Interactive Card Builder ----------
def _format_exercise_line(ex: dict) -> str:
    """Format a single exercise into mobile-friendly markdown lines."""
    name = ex.get("name", "未知动作")
    sets = ex.get("sets", "?")
    reps = ex.get("reps", "?")
    weight_val = ex.get("weight")
    rpe_val = ex.get("rpe_target")
    rest = ex.get("rest_seconds", 0)
    notes = ex.get("notes", "")

    if weight_val and str(weight_val) != "null":
        load_text = str(weight_val)
    elif rpe_val:
        load_text = f"RPE {rpe_val}/10"
    else:
        load_text = "体感选择"

    if rest >= 60:
        m, s = divmod(rest, 60)
        rest_text = f"{m}分{s}秒" if s else f"{m}分钟"
    else:
        rest_text = f"{rest}秒"

    line1 = f"**{name}**"
    line2 = f"{sets}组×{reps}｜{load_text}｜休息{rest_text}"
    result = [line1, line2]
    if notes:
        result.append(f"💡 {notes}")
    elif (not weight_val or str(weight_val) == "null") and rpe_val:
        result.append("💡 请告知该动作当前工作重量或可用器械")
    return "\n".join(result)


def _build_single_card(plan: dict, exercises: list, card_index: int = 0, total_cards: int = 1) -> dict:
    """Build one interactive card with vertical markdown layout."""
    title = plan.get("plan_title", "训练计划")
    if total_cards > 1:
        title += f"（{card_index + 1}/{total_cards}）"

    area = plan.get("target_area", "")
    duration = plan.get("duration_minutes")
    intensity = plan.get("intensity", "")
    recovery = plan.get("recovery_status", "")
    sub_parts = []
    if area:
        sub_parts.append(f"🎯 {area}")
    if duration:
        sub_parts.append(f"⏱ {duration}分钟")
    if intensity:
        sub_parts.append(f"🔥 {intensity}")
    if recovery:
        sub_parts.append(f"💚 {recovery}")
    subtitle = " ".join(sub_parts) if sub_parts else ""

    elements = []
    if subtitle:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": subtitle}})
        elements.append({"tag": "hr"})

    for i, ex in enumerate(exercises):
        md_content = _format_exercise_line(ex)
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": md_content}})
        if i < len(exercises) - 1:
            elements.append({"tag": "hr"})

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"💪 {title}"},
            "template": "blue"
        },
        "elements": elements
    }


MAX_EXERCISES_PER_CARD = 6


def _try_parse_plan_card(reply: str):
    """Extract training plan JSON from AI reply.
    Returns (list_of_cards, clean_text) or (None, original_reply).
    """
    import re as _re
    pattern = r"```json\s*(\{.*?\})\s*```"
    match = _re.search(pattern, reply, _re.DOTALL)
    if not match:
        return None, reply
    try:
        plan = json.loads(match.group(1))
        exercises = plan.get("exercises", [])
        if not exercises:
            return None, reply

        chunks = [exercises[i:i + MAX_EXERCISES_PER_CARD]
                  for i in range(0, len(exercises), MAX_EXERCISES_PER_CARD)]
        total = len(chunks)
        cards = [_build_single_card(plan, chunk, idx, total)
                 for idx, chunk in enumerate(chunks)]

        clean = _re.sub(pattern, "", reply, flags=_re.DOTALL).strip()
        return cards, clean
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.warning("Plan card parse failed: %s", e)
        return None, reply


def _val_or_na(val, suffix=""):
    """Return formatted value or NA marker if None/missing."""
    if val is None:
        return "本次未返回"
    return f"{val}{suffix}"


def _build_whoop_card(data: dict) -> dict:
    """Build Feishu interactive card from structured WHOOP data."""
    elements = []

    # --- Recovery ---
    rec = data.get("recovery")
    if rec:
        lines = [f"📅 {rec.get('latest_date', 'N/A')}"]
        rs = rec.get("recovery_score")
        if rs is not None:
            lines.append(f"恢复评分: {rs}%")
        hrv_val = rec.get("hrv")
        if hrv_val is not None:
            lines.append(f"HRV: {hrv_val} ms")
        rhr = rec.get("resting_heart_rate")
        if rhr is not None:
            lines.append(f"静息心率: {rhr} bpm")
        rr = rec.get("respiratory_rate")
        if rr is not None:
            lines.append(f"呼吸频率: {rr} rpm")
        skin_temp = rec.get("skin_temp_celsius")
        if skin_temp is not None:
            lines.append(f"皮肤温度: {skin_temp}°C")
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})
    else:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "💚 **恢复**: 近7天无记录"}})

    # --- Baseline ---
    baseline = data.get("baseline")
    if baseline and baseline.get("status") == "ok":
        bl_lines = [f"📊 **恢复基线** ({baseline.get('source', 'Fitness 近30天估算')})"]
        ar = baseline.get("avg_recovery")
        if ar is not None:
            bl_lines.append(f"平均恢复: {ar}%（{baseline.get('sample_count', '?')}条记录）")
        if baseline.get("avg_hrv") is not None:
            bl_lines.append(f"平均HRV: {baseline['avg_hrv']} ms")
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(bl_lines)}})
    elif baseline and baseline.get("status") == "insufficient_data":
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md",
            "content": f"📊 **恢复基线**: 历史数据不足（仅{baseline.get('sample_count', 0)}条记录，需≥7条）"}})

    # --- Sleep ---
    elements.append({"tag": "hr"})
    slp = data.get("sleep")
    if slp:
        slines = [f"😴 **睡眠** ({slp.get('latest_date', 'N/A')})"]
        asd = slp.get("actual_sleep_duration")
        if asd is not None:
            slines.append(f"实际睡眠: {asd}")
        ls = slp.get("light_sleep")
        ds = slp.get("deep_sleep")
        rms = slp.get("rem_sleep")
        if any(v is not None for v in (ls, ds, rms)):
            slines.append(f"浅睡/深睡/REM: {ls or '--'} / {ds or '--'} / {rms or '--'}")
        eff = slp.get("efficiency")
        if eff is not None:
            slines.append(f"睡眠效率: {eff}%")
        perf = slp.get("performance")
        if perf is not None:
            slines.append(f"睡眠表现: {perf}%")
        nap = slp.get("nap_count", 0)
        if nap > 0:
            slines.append(f"(另有 {nap} 次小睡已排除)")
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(slines)}})
    else:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "😴 **睡眠**: 近7天无记录"}})

    # --- Workout ---
    elements.append({"tag": "hr"})
    wkt = data.get("workout")
    if wkt:
        wlines = [f"🏋️ **运动** ({wkt.get('latest_start', 'N/A')})"]
        wlines.append(f"类型: {wkt.get('sport_name', 'N/A')} | 时长: {wkt.get('duration', 'N/A')}")
        strain = wkt.get("strain")
        if strain is not None:
            wlines.append(f"Strain: {float(strain):.1f}")
        kj = wkt.get("kilojoule")
        if kj is not None:
            wlines.append(f"消耗: {round(float(kj))} kJ")
        avg_hr = wkt.get("avg_hr")
        if avg_hr is not None:
            wlines.append(f"平均心率: {avg_hr} bpm")
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(wlines)}})
    else:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "🏋️ **运动**: 近7天无记录"}})

    # --- Body Metrics ---
    elements.append({"tag": "hr"})
    body = data.get("body")
    if body:
        parts = []
        if body.get("height_cm") is not None:
            parts.append(f"身高 {body['height_cm']}cm")
        if body.get("weight_kg") is not None:
            parts.append(f"体重 {body['weight_kg']}kg")
        if body.get("max_heart_rate") is not None:
            parts.append(f"最大心率 {body['max_heart_rate']}bpm")
        elements.append({"tag": "div", "text": {"tag": "lark_md",
            "content": f"📏 **身体指标**: {', '.join(parts) if parts else '暂无数据'}"}})
    else:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "📏 **身体指标**: 暂无数据"}})

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "📊 WHOOP 健康数据摘要"},
            "template": "green"
        },
        "elements": elements
    }


def _build_card_request(message_id: str, card: dict) -> ReplyMessageRequest:
    """Build a ReplyMessageRequest with interactive card content."""
    return ReplyMessageRequest.builder() \
        .message_id(message_id) \
        .request_body(
            ReplyMessageRequestBody.builder()
            .msg_type("interactive")
            .content(json.dumps(card, ensure_ascii=False))
            .build()
        ).build()


# ---------- Monthly Summary ----------
_MONTHLY_SINGLE_RE = re.compile(r"/(?:月报|monthly)\s*(\d{4})[-/](\d{1,2})", re.IGNORECASE)
_MONTHLY_YEAR_ONLY_RE = re.compile(r"/(?:月报|monthly)\s*(\d{4})", re.IGNORECASE)
_MONTHLY_RANGE_RE = re.compile(r"(\d{4})\s*年?\s*(\d{1,2})\s*月?\s*[至到\-~]\s*(?:(\d{4})\s*年?)?\s*(\d{1,2})\s*月?", re.IGNORECASE)

def _parse_monthly_args(content: str) -> list:
    """Parse monthly summary arguments. Returns list of (year, month) tuples.
    Supports: /月报 (current month), /月报 YYYY-MM, /月报 YYYY, 
    YYYY年M月至N月, YYYY年M月至YYYY年N月.
    """
    now = datetime.now(SHANGHAI_TZ)
    c = content.strip()
    
    # Check for range pattern first: "2025年5月至10月" or "2026年5月至2026年8月"
    rm = _MONTHLY_RANGE_RE.search(c)
    if rm:
        y1, m1 = int(rm.group(1)), int(rm.group(2))
        y2 = int(rm.group(3)) if rm.group(3) else y1
        m2 = int(rm.group(4))
        months = []
        cy, cm = y1, m1
        while (cy, cm) <= (y2, m2):
            months.append((cy, cm))
            cm += 1
            if cm > 12:
                cm = 1
                cy += 1
            if len(months) > 24:  # Safety limit
                break
        return months if months else [(now.year, now.month)]
    
    # Single month: /月报 YYYY-MM
    sm = _MONTHLY_SINGLE_RE.search(c)
    if sm:
        return [(int(sm.group(1)), int(sm.group(2)))]
    
    # Year only: /月报 YYYY → all months of that year up to now
    ym = _MONTHLY_YEAR_ONLY_RE.search(c)
    if ym:
        y = int(ym.group(1))
        if y == now.year:
            return [(y, m) for m in range(1, now.month + 1)]
        return [(y, m) for m in range(1, 13)]
    
    # Default: current month only
    return [(now.year, now.month)]


def _safe_avg(values: list) -> float | None:
    """Return average of non-None numeric values, or None if empty."""
    valid = [v for v in values if v is not None]
    return round(sum(valid) / len(valid), 1) if valid else None


def _aggregate_monthly(year: int, month: int) -> dict:
    """Fetch and aggregate WHOOP data for a given month.
    Returns structured summary dict. Never fabricates data.
    """
    result = {
        "year": year, "month": month,
        "recovery_days": 0, "recovery_avg": None, "hrv_avg": None,
        "sleep_days": 0, "sleep_avg_str": None, "sleep_eff_avg": None,
        "workout_count": 0, "workout_total_duration_str": "0h00m",
        "workout_sports": {},
        "recovery_records": [], "sleep_records": [], "workout_records": [],
        "errors": [],
    }
    # Recovery
    rec = get_monthly_recovery(year, month)
    if rec["status"] == "ok":
        result["recovery_records"] = rec["records"]
        result["recovery_days"] = rec["count"]
        scores = [r["recovery_score"] for r in rec["records"]]
        hrvs = [r["hrv"] for r in rec["records"]]
        if rec["count"] >= 7:
            result["recovery_avg"] = _safe_avg(scores)
            result["hrv_avg"] = _safe_avg(hrvs)
    else:
        result["errors"].append(f"recovery: {rec.get('error', 'unknown')}")

    # Sleep
    slp = get_monthly_sleep(year, month)
    if slp["status"] == "ok":
        result["sleep_records"] = slp["records"]
        result["sleep_days"] = slp["count"]
        durations = [r["duration_ms"] for r in slp["records"] if r.get("duration_ms")]
        effs = [r["efficiency"] for r in slp["records"] if r.get("efficiency") is not None]
        if slp["count"] >= 7:
            avg_ms = _safe_avg(durations)
            if avg_ms is not None:
                total_min = int(avg_ms) // 60000
                h, m = divmod(total_min, 60)
                result["sleep_avg_str"] = f"{h}h{m:02d}m"
            result["sleep_eff_avg"] = _safe_avg(effs)
    else:
        result["errors"].append(f"sleep: {slp.get('error', 'unknown')}")

    # Workouts (WHOOP recorded only, not plan frequency)
    wkt = get_monthly_workouts(year, month)
    if wkt["status"] == "ok":
        result["workout_records"] = wkt["records"]
        result["workout_count"] = wkt["count"]
        total_ms = sum(r["duration_ms"] for r in wkt["records"] if r.get("duration_ms"))
        total_min = total_ms // 60000
        h, m = divmod(total_min, 60)
        result["workout_total_duration_str"] = f"{h}h{m:02d}m"
        sports = {}
        for r in wkt["records"]:
            sn = r.get("sport_name", "Other")
            sports[sn] = sports.get(sn, 0) + 1
        result["workout_sports"] = sports
    else:
        result["errors"].append(f"workout: {wkt.get('error', 'unknown')}")

    return result


def _build_monthly_card(data: dict) -> dict:
    """Build Feishu interactive card for monthly summary."""
    y, m = data["year"], data["month"]
    from calendar import monthrange
    last_day = monthrange(y, m)[1]
    now = datetime.now(SHANGHAI_TZ)
    end_day = min(last_day, now.day) if (y == now.year and m == now.month) else last_day
    range_str = f"{y}年{m}月1日 – {m}月{end_day}日"

    elements = []

    # --- Data Coverage ---
    cov_lines = ["**📋 数据覆盖**"]
    cov_lines.append(f"WHOOP 恢复记录: {data['recovery_days']} 天")
    cov_lines.append(f"WHOOP 睡眠记录: {data['sleep_days']} 天")
    cov_lines.append(f"WHOOP 运动记录: {data['workout_count']} 次（仅含 WHOOP 自动识别的运动）")
    # Count matched vs unmatched local logs
    matched_count = sum(1 for log in data.get("local_training", []) if log.get("whoop_matched"))
    unmatched_count = local_count - matched_count
    local_count = len(data.get("local_training", []))
    if local_count > 0:
        if matched_count > 0:
            cov_lines.append(f"✅ 本地训练记录: {local_count} 次（其中 {matched_count} 次已与 WHOOP 关联，不重复计算）")
        else:
            cov_lines.append(f"✅ 本地训练记录: {local_count} 次（用户确认完成）")
    else:
        cov_lines.append("⚠️ 本地训练记录: 0 次（训练完成后发送「练完了」等确认即可记录）")
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(cov_lines)}})
    elements.append({"tag": "hr"})

    # --- Training Execution ---
    # Local training logs (user-confirmed completions)
    local_logs = data.get("local_training", [])
    local_count = len(local_logs)
    local_exercises = []
    for log in local_logs:
        for ex in log.get("exercises", []):
            local_exercises.append(ex)
    
    tr_lines = ["**🏋️ 训练执行**"]
    if local_count > 0:
        tr_lines.append(f"✅ 已完成训练: {local_count} 次（用户确认记录）")
        # Aggregate exercise stats
        ex_stats = {}
        for ex in local_exercises:
            name = ex.get("name", "未知")
            if name not in ex_stats:
                ex_stats[name] = {"count": 0, "total_sets": 0, "best_volume": 0}
            ex_stats[name]["count"] += 1
            s = ex.get("sets", 0)
            r = ex.get("reps", 0)
            w = ex.get("weight", 0)
            ex_stats[name]["total_sets"] += s
            vol = (w or 0) * (r or 0)
            if vol > ex_stats[name]["best_volume"]:
                ex_stats[name]["best_volume"] = vol
        for name, st in sorted(ex_stats.items(), key=lambda x: -x[1]["count"])[:5]:
            line = f"  • {name}: {st['count']}次"
            if st["total_sets"] > 0:
                line += f"，{st['total_sets']}组"
            if st["best_volume"] > 0:
                line += f"，最佳工作组 {st['best_volume']:.0f}"
            tr_lines.append(line)
    else:
        tr_lines.append("已完成训练: 0 次（暂无用户确认的训练记录）")
    
    # WHOOP workouts (separate section, no double counting)
    tr_lines.append("")
    tr_lines.append(f"**📱 WHOOP 自动识别运动**: {data['workout_count']} 次")
    if data["workout_count"] > 0:
        tr_lines.append(f"总时长: {data['workout_total_duration_str']}")
        sports = data.get("workout_sports", {})
        if sports:
            sport_parts = [f"{k} {v}次" for k, v in sorted(sports.items(), key=lambda x: -x[1])]
            tr_lines.append(f"类型: {'、'.join(sport_parts)}")
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(tr_lines)}})
    elements.append({"tag": "hr"})

    # --- Recovery & Sleep ---
    rs_lines = ["**💚 恢复与睡眠**"]
    rd = data["recovery_days"]
    sd = data["sleep_days"]
    if rd >= 7:
        ra = data.get("recovery_avg")
        ha = data.get("hrv_avg")
        rs_lines.append(f"恢复均分: {ra}%（{rd}天有效记录）" if ra else f"恢复评分: {rd}天记录，均值不可用")
        if ha is not None:
            rs_lines.append(f"HRV 均值: {ha} ms")
    elif rd > 0:
        rs_lines.append(f"恢复: {rd}天记录，不足7天，暂不判断趋势")
    else:
        rs_lines.append("恢复: 本月无记录")

    if sd >= 7:
        sa = data.get("sleep_avg_str")
        se = data.get("sleep_eff_avg")
        if sa:
            rs_lines.append(f"日均睡眠: {sa}（{sd}天有效记录）")
        if se is not None:
            rs_lines.append(f"睡眠效率均值: {se}%")
    elif sd > 0:
        rs_lines.append(f"睡眠: {sd}天记录，不足7天，暂不判断趋势")
    else:
        rs_lines.append("睡眠: 本月无记录")
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(rs_lines)}})
    elements.append({"tag": "hr"})

    # --- Findings (max 2, data-backed only) ---
    findings = []
    if data["workout_count"] >= 7 and rd >= 7 and data.get("recovery_avg") is not None:
        ra = data["recovery_avg"]
        if ra >= 70:
            findings.append(f"本月训练{data['workout_count']}次，恢复均分{ra}%，负荷与恢复匹配良好")
        elif ra < 50:
            findings.append(f"本月训练{data['workout_count']}次但恢复均分仅{ra}%，可能存在过度训练或恢复不足")
    if sd >= 7 and data.get("sleep_eff_avg") is not None:
        se = data["sleep_eff_avg"]
        if se < 85:
            findings.append(f"睡眠效率均值{se}%偏低，建议关注睡前习惯")
    if data["workout_count"] == 0 and rd == 0 and sd == 0:
        findings.append("本月暂无任何记录数据，无法生成有效总结")
    if not findings:
        if rd < 7 and sd < 7 and data["workout_count"] < 3:
            findings.append("本月有效记录较少，尚不足以得出可靠结论")
        else:
            findings.append("各项指标在正常范围内，继续保持当前节奏")

    fb_lines = ["**📌 本月发现**"]
    for f in findings[:2]:
        fb_lines.append(f"• {f}")
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(fb_lines)}})
    elements.append({"tag": "hr"})

    # --- Action items (max 2, specific & actionable) ---
    actions = []
    if data["workout_count"] == 0:
        actions.append("每次训练后在飞书发送训练内容（如「今天做了背部训练」），系统会自动记录")
    elif data["workout_count"] < 8:
        actions.append(f"本月运动{data['workout_count']}次，下月可尝试增加1-2次以巩固习惯")
    if rd < 7:
        actions.append("坚持佩戴 WHOOP 以积累更多恢复数据，便于后续趋势分析")
    if sd >= 7 and data.get("sleep_eff_avg") is not None and data["sleep_eff_avg"] < 85:
        actions.append("尝试固定就寝时间并减少睡前屏幕使用，提升睡眠效率")
    if not actions:
        actions.append("保持当前训练和恢复节奏，持续记录以获得更精准的月度洞察")

    ac_lines = ["**🎯 下月行动**"]
    for a in actions[:2]:
        ac_lines.append(f"• {a}")
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(ac_lines)}})

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"📊 月度训练总结 · {range_str}"},
            "template": "indigo"
        },
        "elements": elements
    }


def _build_multi_month_card(month_list: list, scope: str) -> dict:
    """Build a single interactive card showing multiple months sequentially."""
    elements = []
    for idx, (year, month) in enumerate(month_list):
        data = _aggregate_monthly(year, month)
        local_logs = load_training_logs(year, month)
        data["local_training"] = local_logs
        
        from calendar import monthrange
        last_day = monthrange(year, month)[1]
        now = datetime.now(SHANGHAI_TZ)
        end_day = min(last_day, now.day) if (year == now.year and month == now.month) else last_day
        
        # Month header
        elements.append({"tag": "div", "text": {"tag": "lark_md",
            "content": f"**📅 {year}年{month}月**（1日–{end_day}日）"}})
        
        # Compact summary per month
        lines = []
        local_count = len(local_logs)
        lines.append(f"已完成训练: {local_count} 次 | WHOOP运动: {data['workout_count']} 次")
        rd = data["recovery_days"]
        sd = data["sleep_days"]
        if rd >= 7 and data.get("recovery_avg") is not None:
            lines.append(f"恢复均分: {data['recovery_avg']}%（{rd}天）")
        elif rd > 0:
            lines.append(f"恢复: {rd}天记录，不足7天")
        else:
            lines.append("恢复: 无记录")
        if sd >= 7 and data.get("sleep_avg_str"):
            lines.append(f"日均睡眠: {data['sleep_avg_str']}（{sd}天）")
        elif sd > 0:
            lines.append(f"睡眠: {sd}天记录，不足7天")
        else:
            lines.append("睡眠: 无记录")
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})
        
        if idx < len(month_list) - 1:
            elements.append({"tag": "hr"})
    
    y1, m1 = month_list[0]
    y2, m2 = month_list[-1]
    range_label = f"{y1}年{m1}月 – {y2}年{m2}月" if (y1, m1) != (y2, m2) else f"{y1}年{m1}月"
    
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"📊 月度训练总结 · {range_label}"},
            "template": "indigo"
        },
        "elements": elements
    }


def _handle_monthly_summary(event, message_id: str, chat_id: str, content: str, scope):
    """Handle /月报 command: aggregate data and send interactive card."""
    month_list = _parse_monthly_args(content)
    logger.info("Monthly summary requested: %s for message %s", month_list, message_id)
    try:
        if len(month_list) == 1:
            year, month = month_list[0]
            data = _aggregate_monthly(year, month)
            # Merge local training logs
            local_logs = load_training_logs(year, month)
            data["local_training"] = local_logs
            card = _build_monthly_card(data)
        else:
            card = _build_multi_month_card(month_list, scope)
        cli = get_lark_client()
        req = _build_card_request(message_id, card)
        resp = cli.im.v1.message.reply(req)
        if resp.success():
            mark_sent(message_id)
            logger.info("Monthly summary card sent for message %s", message_id)
        else:
            logger.error("Monthly card reply failed: code=%s msg=%s", resp.code, resp.msg)
            mark_failed(message_id)
        try:
            save_turn(scope, message_id, content, f"[月度总结 {month_list}]")
        except Exception:
            pass
    except Exception as e:
        logger.error("Monthly summary error: %s: %s", type(e).__name__, e)
        mark_failed(message_id)
        # Fallback text
        try:
            cli = get_lark_client()
            req = ReplyMessageRequest.builder() \
                .message_id(message_id) \
                .request_body(
                    ReplyMessageRequestBody.builder()
                    .msg_type("text")
                    .content(json.dumps({"text": "⚠️ 月度总结生成失败，请稍后重试。"}, ensure_ascii=False))
                    .build()
                ).build()
            cli.im.v1.message.reply(req)
        except Exception:
            pass


# ---------- Main ----------

def main():
    handler = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(handle_message) \
        .build()

    ws = lark.ws.Client(
        app_id=os.environ["FEISHU_APP_ID"],
        app_secret=os.environ["FEISHU_APP_SECRET"],
        event_handler=handler,
        log_level=lark.LogLevel.WARNING,
    )
    # Start internal web server in background thread
    start_web_server()
    logger.info("Starting Fitness Agent via WebSocket...")
    ws.start()



# ============================================================
# Web Channel: Core Fitness Logic + Internal HTTP API
# ============================================================
# 把 _handle_message 里的业务逻辑抽成与通道无关的 process_fitness_request，
# 再用 Flask 暴露成 HTTP 接口：飞书之外的入口（自建看板、脚本、快捷指令）
# 可以复用同一套解析 / 落库 / WHOOP 匹配逻辑。
# ============================================================

# ---------- Core Fitness Request Processor ----------

def process_fitness_request(message: str, session_id: str) -> dict:
    """
    Channel-agnostic fitness request processor.
    
    Args:
        message: User input text
        session_id: Session identifier (web session or feishu scope)
    
    Returns:
        dict with keys:
          - type: "text" | "card" | "confirm_pending" | "select_pending" | "error"
          - content: reply text or card dict
          - extra_text: optional additional text after card
          - pending_state: optional pending confirm/select info
    """
    content = message.strip()
    if not content:
        return {"type": "text", "content": "请输入消息。"}
    
    scope = session_id  # web uses session_id directly as scope
    
    # --- Pending state handling ---
    stripped_content = content.strip()
    pending_confirm = _peek_pending_confirm(scope) if scope else None
    pending_select = _peek_pending_select(scope) if scope else None
    has_pending = pending_confirm or pending_select
    
    # Choice selection for ambiguous candidates
    if pending_select:
        choice_match = _CHOICE_PATTERN.match(stripped_content)
        if choice_match:
            idx = int(choice_match.group(1)) - 1
            entry = _consume_pending_select(scope)
            if entry is not None:
                candidates = entry.get("candidates", [])
                if 0 <= idx < len(candidates):
                    chosen = candidates[idx]
                    saved_parsed = entry["parsed"]
                    wid = chosen.get("workout_id", "") or chosen.get("id", "")
                    already_linked = set()
                    try:
                        conn_chk = get_db()
                        rows_chk = conn_chk.execute(
                            "SELECT whoop_workout_id FROM training_logs WHERE whoop_matched = 1 AND whoop_workout_id != ''"
                        ).fetchall()
                        already_linked = {r[0] for r in rows_chk if r[0]}
                        conn_chk.close()
                    except Exception:
                        pass
                    if wid and wid in already_linked:
                        return {"type": "text", "content": "⚠️ 该 WHOOP 运动记录已被其他训练关联，无法重复关联。请重新发送训练信息。"}
                    dur_ms = chosen.get("duration_ms") or chosen.get("duration")
                    dur_min = round(dur_ms / 60000) if dur_ms else None
                    chosen_match = {
                        "matched": True, "match_status": "matched",
                        "whoop_date": chosen.get("date", "") or chosen.get("date_cst", ""),
                        "sport_name": chosen.get("sport_name", ""),
                        "whoop_workout_id": wid,
                        "duration_minutes": dur_min,
                        "strain": chosen.get("strain"),
                        "max_heart_rate": chosen.get("max_heart_rate"),
                        "kilojoule": chosen.get("kilojoule"),
                        "start_cst": chosen.get("start_cst", ""),
                        "end_cst": chosen.get("end_cst", ""),
                    }
                    save_ok, whoop_match, save_result = save_training_log_v2(
                        scope, f"web-{datetime.now(SHANGHAI_TZ).strftime('%Y%m%d%H%M%S')}", saved_parsed,
                        override_whoop_match=chosen_match,
                    )
                    if save_ok:
                        confirmation = _build_log_confirmation(saved_parsed, whoop_match or {}, save_result or {})
                    else:
                        confirmation = _build_log_failure_message(saved_parsed)
                    return {"type": "text", "content": confirmation}
                else:
                    return {"type": "text", "content": "选择无效，请回复候选编号或「取消」。"}
            else:
                return {"type": "text", "content": "待选择记录已过期，请重新发送训练信息。"}
        elif stripped_content in _CANCEL_KEYWORDS:
            _consume_pending_select(scope)
            return {"type": "text", "content": "已取消，本次训练未保存。"}
    
    # Confirm/cancel for unmatched pending
    if pending_confirm and stripped_content in _CONFIRM_KEYWORDS:
        entry = _consume_pending_confirm(scope)
        if entry is not None:
            saved_parsed = entry["parsed"]
            save_ok, whoop_match, save_result = save_training_log_v2(
                scope, f"web-{datetime.now(SHANGHAI_TZ).strftime('%Y%m%d%H%M%S')}", saved_parsed
            )
            if save_ok:
                confirmation = _build_log_confirmation(saved_parsed, whoop_match or {}, save_result or {})
            else:
                confirmation = _build_log_failure_message(saved_parsed)
            return {"type": "text", "content": confirmation}
        else:
            return {"type": "text", "content": "待确认记录已过期，请重新发送训练信息。"}
    elif pending_confirm and stripped_content in _CANCEL_KEYWORDS:
        _consume_pending_confirm(scope)
        return {"type": "text", "content": "已取消，本次训练未保存。"}
    elif not has_pending and stripped_content in (_CONFIRM_KEYWORDS | _CANCEL_KEYWORDS):
        return {"type": "text", "content": ""}
    
    # --- Command handling ---
    if content == "/status":
        return {"type": "text", "content": "✅ Fitness Agent 运行正常"}
    if content == "/help":
        return {"type": "text", "content": HELP_TEXT}
    if content.strip().lower() in ("/whoop", "whoop", "whoop数据", "查看whoop", "查询whoop"):
        dashboard, ok = _fetch_whoop_with_logging(force_refresh=True)
        if ok:
            try:
                structured = get_whoop_dashboard_data()
                whoop_card = _build_whoop_card(structured)
                save_turn(scope, f"web-{datetime.now(SHANGHAI_TZ).strftime('%Y%m%d%H%M%S')}", content, dashboard)
            except Exception:
                pass
            return {"type": "card", "content": whoop_card, "extra_text": dashboard}
        else:
            return {"type": "text", "content": "⚠️ WHOOP API 请求失败，请稍后重试。本次未能获取实时数据。"}
    if content.strip().lower().startswith("/月报") or content.strip().lower().startswith("/monthly") or "月度总结" in content:
        # Monthly summary returns a card; reuse existing aggregation logic
        args = _parse_monthly_args(content)
        if not args:
            now = datetime.now(SHANGHAI_TZ)
            args = [(now.year, now.month)]
        if len(args) == 1:
            year, month = args[0]
            data = _aggregate_monthly(year, month)
            card = _build_monthly_card(data)
            return {"type": "card", "content": card}
        else:
            month_list = []
            for year, month in args:
                data = _aggregate_monthly(year, month)
                month_list.append(data)
            card = _build_multi_month_card(month_list, scope)
            return {"type": "card", "content": card}
    
    # --- AI Chat ---
    ai_completed = False
    reply = ""
    try:
        system_prompt = build_system_prompt(load_profile())
        recent_logs = _load_recent_training_logs(scope)
        if recent_logs:
            system_prompt += recent_logs
        system_prompt += (
            "\n历史对话仅用于理解上下文。历史中的 WHOOP 指标不能当成本次"
            "实时数据，历史建议和用户描述也不等于已验证事实。"
            "恢复评分不能证明疲劳原因，不把相关性写成因果。"
        )
        history = load_history(scope)
        is_refresh = _is_whoop_refresh_intent(content)
        whoop_keywords = (
            "whoop", "恢复", "睡眠", "训练", "健身", "运动",
            "篮球", "力量", "练", "心率", "hrv", "strain", "疲劳"
        )
        should_fetch_whoop = is_refresh or any(word in content.lower() for word in whoop_keywords)
        if should_fetch_whoop:
            dashboard, ok = _fetch_whoop_with_logging(force_refresh=is_refresh)
            if ok:
                system_prompt += (
                    "\n\n## 本次 WHOOP 数据（后台集成自动获取，查询成功）\n"
                    + dashboard
                    + "\n\n以上数据由后台 WHOOP 集成本次实时获取，查询成功。"
                    "回复时以「已获取 WHOOP 数据」开头，列出各条记录的实际日期，再回答问题。"
                    "区分「本次查询时间」与「WHOOP 记录时间」，不以条目数量或返回日期判断是否有更新。"
                    "部分字段缺失时只说明该字段未返回，不笼统说无更新或无记录。"
                    "结合个人档案给出建议。如时间未注明时区，不自行推断时区。"
                    "WHOOP 身体指标与档案冲突时，说明来源差异。"
                    "不要要求用户重复提供已经列出的指标。"
                )
                if is_refresh:
                    system_prompt += (
                        "\n\n【重要】用户明确要求刷新/同步 WHOOP 数据，以上为本次 API 实时拉取结果。"
                        "禁止使用历史对话中的旧 WHOOP 数据，必须以本次获取的数据为准。"
                    )
            else:
                fail_time = datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S CST")
                system_prompt += (
                    "\n\n## WHOOP 数据获取失败\n"
                    f"API 请求失败时间: {fail_time}\n"
                    "失败原因: WHOOP API 调用异常（网络超时、认证失败或服务端错误）\n"
                    "缓存状态: 无可用本地缓存（本系统不使用数据级缓存，每次均实时请求 API）\n"
                    "准确告知用户「WHOOP API 请求失败」并说明上述失败原因。"
                    "不要说「我无法读取」「无法自行再次拉取」或暗示自身无此能力。"
                    "禁止编造恢复、睡眠或运动指标。"
                    "如引用历史对话中的旧数据，必须标注其日期和来源（如「根据 X月X日 的历史记录」），"
                    "并明确说明该数据非本次实时获取。"
                    "如提供建议，说明它仅基于个人档案和用户描述，不含实时 WHOOP 数据。"
                )
        resp = deepseek_client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=(
                [{"role": "system", "content": system_prompt}]
                + history
                + [{"role": "user", "content": content}]
            ),
            max_tokens=1024,
        )
        reply = resp.choices[0].message.content
        if not isinstance(reply, str) or not reply.strip():
            raise ValueError("Empty AI reply")
        ai_completed = True
    except Exception as e:
        logger.error(f"DeepSeek error (web): {type(e).__name__}: {e}")
        return {"type": "error", "content": "❌ AI 调用暂时不可用，请稍后重试。"}
    
    # Parse plan cards
    cards, clean_text = _try_parse_plan_card(reply)
    
    # Save conversation turn
    msg_id = f"web-{datetime.now(SHANGHAI_TZ).strftime('%Y%m%d%H%M%S')}"
    if ai_completed:
        try:
            save_turn(scope, msg_id, content, reply)
        except Exception as exc:
            logger.error("Web memory save failed: %s", type(exc).__name__)
    
    # Training log detection
    training_confirmation = None
    confirmation_obj = None  # Structured confirmation for web channel
    is_web = scope.startswith("web")
    try:
        nl_parsed = _parse_natural_language_log(content)
        if nl_parsed:
            wants_whoop = _user_requests_whoop_sync(content)
            if wants_whoop:
                pre_match = _try_match_whoop_workout(
                    nl_parsed["training_date"],
                    nl_parsed.get("activity_type", ""),
                    user_start_time=nl_parsed.get("start_time"),
                    user_duration_minutes=nl_parsed.get("duration_minutes"),
                )
                if not pre_match.get("matched") and pre_match.get("match_status") == "unmatched":
                    if is_web:
                        cid = _store_pending_action_db("web", scope, "confirm", {"parsed": nl_parsed, "whoop_match": pre_match})
                        expires_at = (datetime.now(SHANGHAI_TZ) + timedelta(seconds=_PENDING_ACTION_TTL_SECONDS)).strftime("%Y-%m-%dT%H:%M:%S+08:00")
                        confirmation_obj = {"id": cid, "type": "training_log", "summary": _build_pending_confirm_prompt(nl_parsed), "expires_at": expires_at}
                    else:
                        _store_pending_confirm(scope, nl_parsed, pre_match)
                    training_confirmation = _build_pending_confirm_prompt(nl_parsed)
                elif pre_match.get("match_status") == "ambiguous":
                    candidates = pre_match.get("ambiguous_candidates", [])
                    if is_web:
                        cid = _store_pending_action_db("web", scope, "select", {"parsed": nl_parsed, "candidates": candidates})
                        expires_at = (datetime.now(SHANGHAI_TZ) + timedelta(seconds=_PENDING_ACTION_TTL_SECONDS)).strftime("%Y-%m-%dT%H:%M:%S+08:00")
                        confirmation_obj = {"id": cid, "type": "training_log_select", "summary": _build_ambiguous_prompt(nl_parsed, candidates), "expires_at": expires_at}
                    else:
                        _store_pending_select(scope, nl_parsed, candidates)
                    training_confirmation = _build_ambiguous_prompt(nl_parsed, pre_match.get("ambiguous_candidates", []))
                else:
                    save_ok, whoop_match, save_result = save_training_log_v2(scope, msg_id, nl_parsed)
                    if save_ok:
                        training_confirmation = _build_log_confirmation(nl_parsed, whoop_match or {}, save_result or {})
                    else:
                        training_confirmation = _build_log_failure_message(nl_parsed)
            else:
                save_ok, whoop_match, save_result = save_training_log_v2(scope, msg_id, nl_parsed)
                if save_ok:
                    training_confirmation = _build_log_confirmation(nl_parsed, whoop_match or {}, save_result or {})
                else:
                    training_confirmation = _build_log_failure_message(nl_parsed)
        else:
            detected = _detect_completed_training(content, reply)
            if detected:
                today_str = datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d")
                save_training_log(scope, msg_id, today_str, detected)
    except Exception as exc:
        logger.error("Web training detection failed: %s", type(exc).__name__)
    
    # Build response
    if cards:
        result = {"type": "card", "content": cards[0] if len(cards) == 1 else cards}
        if len(cards) > 1:
            result["extra_cards"] = cards[1:]
        if clean_text and clean_text.strip():
            result["extra_text"] = clean_text.strip()
        if training_confirmation:
            result["training_confirmation"] = training_confirmation
        return result
    else:
        import re as _strip_re
        display_reply = _strip_re.sub(r"```json\s*\{.*?\}\s*```", "", reply, flags=_strip_re.DOTALL).strip()
        if not display_reply:
            display_reply = reply
        result = {"type": "text", "content": display_reply}
        if training_confirmation:
            result["training_confirmation"] = training_confirmation
        if confirmation_obj:
            result["requires_confirmation"] = True
            result["confirmation"] = confirmation_obj
        return result


# ---------- Flask Internal HTTP Server ----------

flask_app = Flask(__name__)

@flask_app.route("/health", methods=["GET"])
def health_check():
    """Health check endpoint - no sensitive data exposed."""
    db_ok = False
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.execute("SELECT 1")
        conn.close()
        db_ok = True
    except Exception:
        pass
    return jsonify({
        "status": "ok" if db_ok else "degraded",
        "database": "connected" if db_ok else "error",
        "service": "fitness-agent",
    })

@flask_app.route("/internal/chat", methods=["POST"])
def internal_chat():
    """
    HTTP 通道的对话入口（自建看板 / 脚本调用）。

    Request body:
      {"message": "user input", "session_id": "web-session-xxx"}

    Response:
      Structured response from process_fitness_request()
    """
    data = flask_request.get_json(silent=True)
    if not data or "message" not in data:
        return jsonify({"type": "error", "content": "Missing 'message' field"}), 400
    
    message = data.get("message", "")
    session_id = data.get("session_id", "web-default")
    
    # Basic rate limiting per session (simple in-memory)
    result = process_fitness_request(message, session_id)
    return jsonify(result)



@flask_app.route("/internal/confirm", methods=["POST"])
def internal_confirm():
    """
    Structured confirmation endpoint for web channel (DB-backed).
    Request body:
      {
        "confirmation_id": "<uuid>",   // REQUIRED
        "session_id": "web_xxx",       // REQUIRED
        "action": "confirm" | "cancel" | "select",
        "choice": <int>                // only required when action="select" (1-based)
      }
    Channel is always "web". Uses persistent pending_actions table.
    """
    data = flask_request.get_json(silent=True)
    if not data:
        return jsonify({"type": "error", "content": "invalid JSON"}), 400

    confirmation_id = (data.get("confirmation_id") or "").strip()
    session_id = (data.get("session_id") or "").strip()
    action = (data.get("action") or "").strip()
    choice = data.get("choice")
    channel = "web"

    if not confirmation_id:
        return jsonify({"type": "error", "content": "confirmation_id is required"}), 400
    if not session_id:
        return jsonify({"type": "error", "content": "session_id is required"}), 400
    if action not in ("confirm", "cancel", "select"):
        return jsonify({"type": "error", "content": "action must be confirm, cancel, or select"}), 400
    if action == "select" and (choice is None or not isinstance(choice, int)):
        return jsonify({"type": "error", "content": "choice (int) is required for select action"}), 400

    # --- CANCEL ---
    if action == "cancel":
        resolved = _resolve_pending_action_db(confirmation_id, channel, session_id, "cancelled")
        if resolved is None:
            return jsonify({"type": "error", "content": "confirmation_not_found", "detail": "\u786e\u8ba4\u64cd\u4f5c\u4e0d\u5b58\u5728\u6216\u65e0\u6743\u8bbf\u95ee"}), 404
        if resolved.get("already_resolved"):
            prev_status = resolved["status"]
            if prev_status == "cancelled":
                return jsonify({"type": "text", "content": "\u5df2\u53d6\u6d88\uff0c\u672c\u6b21\u8bad\u7ec3\u672a\u4fdd\u5b58\u3002", "already_resolved": True, "previous_status": prev_status})
            elif prev_status == "confirmed":
                return jsonify({"type": "error", "content": "already_confirmed", "detail": "\u8be5\u64cd\u4f5c\u5df2\u786e\u8ba4\uff0c\u65e0\u6cd5\u53d6\u6d88"}), 409
            else:
                return jsonify({"type": "error", "content": "already_expired", "detail": "\u8be5\u64cd\u4f5c\u5df2\u8fc7\u671f"}), 410
        return jsonify({"type": "text", "content": "\u5df2\u53d6\u6d88\uff0c\u672c\u6b21\u8bad\u7ec3\u672a\u4fdd\u5b58\u3002", "confirmation_id": confirmation_id})

    # --- SELECT ---
    if action == "select":
        resolved = _resolve_pending_action_db(confirmation_id, channel, session_id, "confirmed")
        if resolved is None:
            return jsonify({"type": "error", "content": "confirmation_not_found", "detail": "\u786e\u8ba4\u64cd\u4f5c\u4e0d\u5b58\u5728\u6216\u65e0\u6743\u8bbf\u95ee"}), 404
        if resolved.get("already_resolved"):
            prev_status = resolved["status"]
            if prev_status == "confirmed":
                return jsonify({"type": "text", "content": "\u8be5\u64cd\u4f5c\u5df2\u5904\u7406\u3002", "already_resolved": True, "previous_status": prev_status})
            elif prev_status == "cancelled":
                return jsonify({"type": "error", "content": "already_cancelled", "detail": "\u8be5\u64cd\u4f5c\u5df2\u53d6\u6d88"}), 409
            else:
                return jsonify({"type": "error", "content": "already_expired", "detail": "\u8be5\u64cd\u4f5c\u5df2\u8fc7\u671f"}), 410
        payload = resolved["payload"]
        if resolved["action_type"] != "select":
            return jsonify({"type": "error", "content": "action_type_mismatch", "detail": "\u8be5\u786e\u8ba4\u64cd\u4f5c\u4e0d\u662f\u9009\u62e9\u7c7b\u578b"}), 400
        candidates = payload.get("candidates", [])
        idx = choice - 1
        if idx < 0 or idx >= len(candidates):
            conn_rb = sqlite3.connect(DB_PATH, timeout=10)
            try:
                conn_rb.execute("UPDATE pending_actions SET status='pending', resolved_at=NULL WHERE confirmation_id=?", (confirmation_id,))
                conn_rb.commit()
            finally:
                conn_rb.close()
            return jsonify({"type": "error", "content": f"\u9009\u62e9\u65e0\u6548\uff0c\u8bf7\u56de\u590d 1~{len(candidates)} \u4e4b\u95f4\u7684\u7f16\u53f7"}), 400
        chosen = candidates[idx]
        saved_parsed = payload["parsed"]
        wid = chosen.get("workout_id", "") or chosen.get("id", "")
        already_linked = set()
        try:
            conn_chk = get_db()
            rows_chk = conn_chk.execute(
                "SELECT whoop_workout_id FROM training_logs WHERE whoop_matched = 1 AND whoop_workout_id != ''"
            ).fetchall()
            already_linked = {r[0] for r in rows_chk if r[0]}
            conn_chk.close()
        except Exception:
            pass
        if wid and wid in already_linked:
            return jsonify({"type": "text", "content": "\u26a0\ufe0f \u8be5 WHOOP \u8fd0\u52a8\u8bb0\u5f55\u5df2\u88ab\u5176\u4ed6\u8bad\u7ec3\u5173\u8054\uff0c\u65e0\u6cd5\u91cd\u590d\u5173\u8054\u3002", "saved": False})
        dur_ms = chosen.get("duration_ms") or chosen.get("duration")
        dur_min = round(dur_ms / 60000) if dur_ms else None
        chosen_match = {
            "matched": True, "match_status": "matched",
            "whoop_date": chosen.get("date", "") or chosen.get("date_cst", ""),
            "sport_name": chosen.get("sport_name", ""),
            "whoop_workout_id": wid,
            "duration_minutes": dur_min,
            "strain": chosen.get("strain"),
            "max_heart_rate": chosen.get("max_heart_rate"),
            "kilojoule": chosen.get("kilojoule"),
            "start_cst": chosen.get("start_cst", ""),
            "end_cst": chosen.get("end_cst", ""),
        }
        msg_id = f"web-confirm-{confirmation_id}"
        save_ok, whoop_match, save_result = save_training_log_v2(
            session_id, msg_id, saved_parsed, override_whoop_match=chosen_match
        )
        result_ref = "saved" if save_ok else "save_failed"
        conn_upd = sqlite3.connect(DB_PATH, timeout=10)
        try:
            conn_upd.execute("UPDATE pending_actions SET result_ref=? WHERE confirmation_id=?", (result_ref, confirmation_id))
            conn_upd.commit()
        finally:
            conn_upd.close()
        if save_ok:
            confirmation = _build_log_confirmation(saved_parsed, whoop_match or {}, save_result or {})
            return jsonify({"type": "text", "content": confirmation, "saved": True, "confirmation_id": confirmation_id})
        else:
            confirmation = _build_log_failure_message(saved_parsed)
            return jsonify({"type": "text", "content": confirmation, "saved": False, "confirmation_id": confirmation_id})

    # --- CONFIRM ---
    resolved = _resolve_pending_action_db(confirmation_id, channel, session_id, "confirmed")
    if resolved is None:
        return jsonify({"type": "error", "content": "confirmation_not_found", "detail": "\u786e\u8ba4\u64cd\u4f5c\u4e0d\u5b58\u5728\u6216\u65e0\u6743\u8bbf\u95ee"}), 404
    if resolved.get("already_resolved"):
        prev_status = resolved["status"]
        if prev_status == "confirmed":
            return jsonify({"type": "text", "content": "\u8be5\u64cd\u4f5c\u5df2\u5904\u7406\u3002", "already_resolved": True, "previous_status": prev_status})
        elif prev_status == "cancelled":
            return jsonify({"type": "error", "content": "already_cancelled", "detail": "\u8be5\u64cd\u4f5c\u5df2\u53d6\u6d88\uff0c\u65e0\u6cd5\u786e\u8ba4"}), 409
        else:
            return jsonify({"type": "error", "content": "already_expired", "detail": "\u8be5\u64cd\u4f5c\u5df2\u8fc7\u671f"}), 410
    payload = resolved["payload"]
    saved_parsed = payload["parsed"]
    msg_id = f"web-confirm-{confirmation_id}"
    save_ok, whoop_match, save_result = save_training_log_v2(session_id, msg_id, saved_parsed)
    result_ref = "saved" if save_ok else "save_failed"
    conn_upd = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn_upd.execute("UPDATE pending_actions SET result_ref=? WHERE confirmation_id=?", (result_ref, confirmation_id))
        conn_upd.commit()
    finally:
        conn_upd.close()
    if save_ok:
        confirmation = _build_log_confirmation(saved_parsed, whoop_match or {}, save_result or {})
        return jsonify({"type": "text", "content": confirmation, "saved": True, "confirmation_id": confirmation_id})
    else:
        confirmation = _build_log_failure_message(saved_parsed)
        return jsonify({"type": "text", "content": confirmation, "saved": False, "confirmation_id": confirmation_id})


def start_web_server(host="0.0.0.0", port=5100):
    """Start Flask server in a background thread."""
    def _run():
        logger.info(f"Starting internal web server on {host}:{port}")
        flask_app.run(host=host, port=port, threaded=True, use_reloader=False)
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    main()
