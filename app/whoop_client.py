"""WHOOP API v2 client for Fitness Agent.
- Reads token from /app/data/whoop_token.json
- Auto-refresh with concurrency lock & atomic save (mode 600)
- Supports: recovery, sleep, cycles, workout, body_measurement
- Handles pagination, score expansion, nap filtering, sleep stage duration
- Network timeouts to avoid blocking Feishu message handling
- Never logs credentials, tokens, or auth codes
"""
import json, os, time, threading, tempfile, logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

TOKEN_FILE = Path(os.environ.get("WHOOP_TOKEN_PATH", "/app/data/whoop_token.json"))
ENV_FILE = Path(os.environ.get("WHOOP_ENV_PATH", "/app/data/../.env"))
BASE_URL = "https://api.prod.whoop.com/developer/v2"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
CST = timezone(timedelta(hours=8))

# Concurrency lock for token refresh
_refresh_lock = threading.Lock()

# Network timeouts: (connect, read) seconds
_TIMEOUT = (5, 15)


def _load_env() -> dict:
    """Load config from env vars first, then .env file as fallback."""
    cfg = {}
    # Priority 1: OS environment variables (injected by compose/env)
    for k in ("WHOOP_CLIENT_ID", "WHOOP_CLIENT_SECRET"):
        v = os.environ.get(k)
        if v:
            cfg[k] = v
    # Priority 2: .env file (if not already set)
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                if k not in cfg:
                    cfg[k] = v.strip()
    return cfg


def _read_token() -> dict:
    with open(TOKEN_FILE) as f:
        return json.load(f)


def _save_token(td: dict) -> None:
    """Atomic write with mode 600."""
    td["saved_at"] = int(time.time())
    if "expires_in" in td:
        td["expires_at"] = td["saved_at"] + int(td["expires_in"])
    data_dir = str(TOKEN_FILE.parent)
    fd, tmp = tempfile.mkstemp(dir=data_dir, prefix=".whoop_token_", suffix=".tmp")
    try:
        os.write(fd, json.dumps(td, indent=2).encode())
        os.fsync(fd)
        os.close(fd)
        os.chmod(tmp, 0o600)
        os.rename(tmp, str(TOKEN_FILE))
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _do_refresh(tok: dict) -> dict:
    """Perform token refresh. Caller must hold _refresh_lock."""
    env = _load_env()
    cid = env.get("WHOOP_CLIENT_ID", "")
    csec = env.get("WHOOP_CLIENT_SECRET", "")
    if not cid or not csec:
        raise RuntimeError("WHOOP credentials missing")
    resp = requests.post(TOKEN_URL, data={
        "grant_type": "refresh_token",
        "refresh_token": tok["refresh_token"],
        "client_id": cid,
        "client_secret": csec,
    }, timeout=_TIMEOUT)
    if resp.status_code != 200:
        raise RuntimeError(f"Token refresh failed: HTTP {resp.status_code}")
    new_tok = resp.json()
    _save_token(new_tok)
    logger.info("WHOOP token refreshed successfully")
    return new_tok


def get_valid_token() -> str:
    """Get a valid access_token, refreshing if needed. Thread-safe."""
    tok = _read_token()
    # Check expiry with 60s buffer
    if tok.get("expires_at", 0) > time.time() + 60:
        return tok["access_token"]
    # Need refresh - acquire lock
    with _refresh_lock:
        # Re-read after acquiring lock (another thread may have refreshed)
        tok = _read_token()
        if tok.get("expires_at", 0) > time.time() + 60:
            return tok["access_token"]
        new_tok = _do_refresh(tok)
        return new_tok["access_token"]


def _headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


def _get(url: str, params: dict = None, retry_on_401: bool = True, max_retries: int = 3) -> dict:
    """GET with Retry-After respect, bounded 429 retries, and 401 auto-refresh.
    Never logs credentials. Raises after exhausting retries."""
    token = get_valid_token()
    last_resp = None
    for attempt in range(max_retries):
        resp = requests.get(url, headers=_headers(token), params=params, timeout=_TIMEOUT)
        last_resp = resp
        if resp.status_code == 429:
            # Respect Retry-After header; fallback to capped exponential backoff
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    wait = min(int(retry_after), 120)
                except ValueError:
                    wait = min(30 * (attempt + 1), 120)
            else:
                wait = min(30 * (attempt + 1), 120)
            logger.warning(f"WHOOP API 429, waiting {wait}s (attempt {attempt+1}/{max_retries})")
            time.sleep(wait)
            continue
        # Non-429: exit retry loop
        break
    else:
        # Exhausted all retries on 429
        raise RuntimeError(f"WHOOP API 429 persisted after {max_retries} retries for {url}")

    if resp.status_code == 401 and retry_on_401:
        logger.info("WHOOP API 401, refreshing token and retrying once")
        with _refresh_lock:
            tok = _read_token()
            # Refresh the rejected token even if its local expiry is in the future.
            # Reuse a newer valid token if another thread already refreshed it.
            if (
                tok.get("access_token") == token
                or tok.get("expires_at", 0) <= time.time() + 60
            ):
                tok = _do_refresh(tok)
            token = tok["access_token"]
        resp = requests.get(url, headers=_headers(token), params=params, timeout=_TIMEOUT)

    if resp.status_code != 200:
        raise RuntimeError(f"WHOOP API error: HTTP {resp.status_code} for {url}")
    return resp.json()


def _fetch_all_pages(url: str, params: dict = None) -> list:
    """Fetch all pages via next_token pagination."""
    all_records = []
    p = dict(params or {})
    while True:
        data = _get(url, p)
        records = data.get("records", [])
        all_records.extend(records)
        nt = data.get("next_token")
        if not nt:
            break
        p["next_token"] = nt
    return all_records


def _get_single(url: str, params: dict = None) -> list:
    """Fetch first page only (no pagination traversal). For single-record queries."""
    data = _get(url, params)
    return data.get("records", [])


def _to_cst(iso_str: Optional[str]) -> str:
    if not iso_str:
        return "N/A"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.astimezone(CST).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso_str[:16]


def _r1(value):
    """数值统一保留 1 位小数。

    WHOOP 的百分比/温度是浮点原值，直接插进文案会出现
    「皮肤温度: 33.8266668°C」「睡眠效率: 91.28212%」这种噪音。
    None 与非数值原样返回，避免把缺失数据变成 0。
    """
    if value is None:
        return None
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return value


def _expand_score(score: Optional[dict]) -> dict:
    """Flatten nested score object into readable metrics."""
    if not score or not isinstance(score, dict):
        return {}
    result = {}
    # Recovery score fields
    for k in ("recovery_score", "hrv", "resting_heart_rate", "spo2_percentage",
              "skin_temp_celsius", "respiratory_rate"):
        if k in score:
            result[k] = _r1(score[k])
    # Sleep score fields (flat + nested stage_summary)
    for k in ("sleep_performance_percentage", "sleep_consistency_percentage",
              "sleep_efficiency_percentage", "sleep_duration_milli",
              "total_in_bed_milli",
              "light_sleep_milli", "deep_sleep_milli", "rem_sleep_milli",
              "awake_milli", "arousal_count", "no_breath_events_per_hour",
              "possible_breath_events_per_hour", "disturbance_count"):
        if k in score:
            result[k] = _r1(score[k])
    # Handle nested stage_summary with WHOOP v2 field names
    stage = score.get("stage_summary", {})
    if isinstance(stage, dict):
        # Map WHOOP v2 stage_summary keys to our canonical names
        stage_map = {
            "total_light_sleep_time_milli": "light_sleep_milli",
            "total_slow_wave_sleep_time_milli": "deep_sleep_milli",
            "total_rem_sleep_time_milli": "rem_sleep_milli",
            "total_awake_time_milli": "awake_milli",
            "total_in_bed_time_milli": "total_in_bed_milli",
            "disturbance_count": "disturbance_count",
            "sleep_cycle_count": "sleep_cycle_count",
        }
        for src_key, dst_key in stage_map.items():
            if src_key in stage and dst_key not in result:
                result[dst_key] = _r1(stage[src_key])
    # Workout score fields
    for k in ("strain", "kilojoule", "average_heart_rate", "max_heart_rate",
              "heart_rate_zone_one_milli", "heart_rate_zone_two_milli",
              "heart_rate_zone_three_milli", "heart_rate_zone_four_milli",
              "heart_rate_zone_five_milli"):
        if k in score:
            result[k] = _r1(score[k])
    return result


def _ms_to_hm(ms: Optional[int]) -> str:
    """Convert milliseconds to Hh Mm format."""
    if ms is None:
        return "N/A"
    total_min = int(ms) // 60000
    h, m = divmod(total_min, 60)
    return f"{h}h{m:02d}m"


def _time_range_7d() -> tuple:
    now = datetime.now(CST)
    start = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S.000+08:00")
    end = now.strftime("%Y-%m-%dT%H:%M:%S.000+08:00")
    return start, end

def _time_range_30d() -> tuple:
    now = datetime.now(CST)
    start = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S.000+08:00")
    end = now.strftime("%Y-%m-%dT%H:%M:%S.000+08:00")
    return start, end


# ==================== Public API ====================

def get_recovery_summary(days: int = 7) -> dict:
    """Get recovery data for recent N days."""
    start, end = _time_range_7d()
    records = _fetch_all_pages(f"{BASE_URL}/recovery", {"start": start, "end": end, "limit": 25})
    if not records:
        return {"status": "no_records", "count": 0}
    latest = records[0]
    score = _expand_score(latest.get("score"))
    return {
        "status": "ok",
        "count": len(records),
        "latest_date": _to_cst(latest.get("created_at")),
        "cycle_id": latest.get("cycle_id"),
        "sleep_id": latest.get("sleep_id"),
        "score_state": latest.get("score_state"),
        "recovery_score": score.get("recovery_score"),
        "hrv": score.get("hrv"),
        "resting_heart_rate": score.get("resting_heart_rate"),
        "spo2": score.get("spo2_percentage"),
        "skin_temp": score.get("skin_temp_celsius"),
        "respiratory_rate": score.get("respiratory_rate"),
    }


def get_sleep_summary(days: int = 7) -> dict:
    """Get sleep data, excluding naps. Uses stage durations for actual sleep time."""
    start, end = _time_range_7d()
    records = _get_single(f"{BASE_URL}/activity/sleep", {"start": start, "end": end, "limit": 10})
    # Filter out naps
    main_sleeps = [r for r in records if not r.get("nap", False)]
    if not main_sleeps:
        return {"status": "no_records", "count": 0, "nap_count": len(records) - len(main_sleeps)}
    latest = main_sleeps[0]
    score = _expand_score(latest.get("score"))
    # Actual sleep duration from stages (not start-end difference)
    # WHOOP v2 may nest stages under stage_summary or expose them flat
    stage_data = score.get("stage_summary", {})
    if not isinstance(stage_data, dict):
        stage_data = {}
    light = score.get("light_sleep_milli") or stage_data.get("light_sleep_milli")
    deep = score.get("deep_sleep_milli") or stage_data.get("deep_sleep_milli")
    rem = score.get("rem_sleep_milli") or stage_data.get("rem_sleep_milli")
    stage_total = None
    if any(v is not None for v in (light, deep, rem)):
        stage_total = sum(int(v) for v in (light, deep, rem) if v is not None)
    # Fallback to sleep_duration_milli or total_in_bed_milli
    if stage_total is None:
        dur = score.get("sleep_duration_milli") or score.get("total_in_bed_milli")
        if dur is not None:
            stage_total = int(dur)
    return {
        "status": "ok",
        "count": len(main_sleeps),
        "nap_count": len(records) - len(main_sleeps),
        "latest_date": _to_cst(latest.get("start")),
        "cycle_id": latest.get("cycle_id"),
        "score_state": latest.get("score_state"),
        "sleep_start": _to_cst(latest.get("start")),
        "sleep_end": _to_cst(latest.get("end")),
        "actual_sleep_duration": _ms_to_hm(stage_total),
        "light_sleep": _ms_to_hm(light),
        "deep_sleep": _ms_to_hm(deep),
        "rem_sleep": _ms_to_hm(rem),
        "efficiency": score.get("sleep_efficiency_percentage"),
        "performance": score.get("sleep_performance_percentage"),
        "consistency": score.get("sleep_consistency_percentage"),
        "arousal_count": score.get("arousal_count"),
        "breath_events": score.get("no_breath_events_per_hour"),
    }


def get_cycles_summary(days: int = 7) -> dict:
    """Get cycle data for recent N days."""
    start, end = _time_range_7d()
    records = _fetch_all_pages(f"{BASE_URL}/cycle", {"start": start, "end": end, "limit": 25})
    if not records:
        return {"status": "no_records", "count": 0}
    latest = records[0]
    score = _expand_score(latest.get("score"))
    return {
        "status": "ok",
        "count": len(records),
        "latest_start": _to_cst(latest.get("start")),
        "latest_end": _to_cst(latest.get("end")),
        "cycle_id": latest.get("id"),
        "score_state": latest.get("score_state"),
        "step_count": latest.get("step_count"),
        "timezone_offset": latest.get("timezone_offset"),
    }


def get_workout_summary(days: int = 7) -> dict:
    """Get workout data for recent N days."""
    start, end = _time_range_7d()
    records = _fetch_all_pages(f"{BASE_URL}/activity/workout", {"start": start, "end": end, "limit": 25})
    if not records:
        return {"status": "no_records", "count": 0}
    latest = records[0]
    score = _expand_score(latest.get("score"))
    return {
        "status": "ok",
        "count": len(records),
        "latest_start": _to_cst(latest.get("start")),
        "latest_end": _to_cst(latest.get("end")),
        "sport_name": latest.get("sport_name"),
        "sport_id": latest.get("sport_id"),
        "score_state": latest.get("score_state"),
        "strain": score.get("strain"),
        "kilojoule": score.get("kilojoule"),
        "avg_hr": score.get("average_heart_rate"),
        "max_hr": score.get("max_heart_rate"),
        "duration": _ms_to_hm(
            (datetime.fromisoformat(latest["end"].replace("Z", "+00:00")) -
             datetime.fromisoformat(latest["start"].replace("Z", "+00:00"))).total_seconds() * 1000
        ) if latest.get("start") and latest.get("end") else "N/A",
    }


def get_body_measurement() -> dict:
    """Get current body measurement (direct object, not paginated)."""
    data = _get(f"{BASE_URL}/user/measurement/body")
    # This endpoint returns a direct object, not {"records": [...]}
    has_data = any(data.get(k) is not None for k in ("height_meter", "weight_kilogram", "max_heart_rate"))
    if not has_data:
        return {"status": "no_data"}
    result = {"status": "ok"}
    if data.get("height_meter") is not None:
        result["height_cm"] = round(data["height_meter"] * 100, 1)
    if data.get("weight_kilogram") is not None:
        result["weight_kg"] = round(data["weight_kilogram"], 1)
    if data.get("max_heart_rate") is not None:
        result["max_heart_rate"] = data["max_heart_rate"]
    result["updated_at"] = _to_cst(data.get("created_at"))
    return result



def get_recovery_baseline() -> dict:
    """Calculate Fitness 30-day estimated recovery baseline from WHOOP API history.
    Returns dict with avg_recovery, avg_hrv, sample_count, or status='insufficient_data'.
    This is NOT a WHOOP official baseline; it is computed locally from recent records.
    """
    start, end = _time_range_30d()
    try:
        records = _fetch_all_pages(
            f"{BASE_URL}/recovery",
            {"start": start, "end": end, "limit": 25}
        )
    except Exception as e:
        logger.warning("Baseline fetch failed: %s", type(e).__name__)
        return {"status": "error", "error": type(e).__name__}

    scores = []
    hrvs = []
    for r in records:
        s = _expand_score(r.get("score"))
        if s.get("recovery_score") is not None:
            scores.append(s["recovery_score"])
        if s.get("hrv") is not None:
            hrvs.append(s["hrv"])

    if len(scores) < 7:
        return {"status": "insufficient_data", "sample_count": len(scores)}

    return {
        "status": "ok",
        "avg_recovery": round(sum(scores) / len(scores), 1),
        "avg_hrv": round(sum(hrvs) / len(hrvs), 1) if hrvs else None,
        "sample_count": len(scores),
        "source": "Fitness 近30天估算",
    }


def get_whoop_dashboard_data() -> dict:
    """Return structured WHOOP dashboard data as a dict (no formatting)."""
    result = {
        "status": "ok",
        "recovery": None,
        "sleep": None,
        "cycles": None,
        "workout": None,
        "body": None,
        "baseline": None,
        "error": None,
    }
    try:
        rec = get_recovery_summary()
        if rec["status"] == "ok":
            result["recovery"] = {
                "latest_date": rec.get("latest_date"),
                "recovery_score": rec.get("recovery_score"),
                "hrv": rec.get("hrv"),
                "resting_heart_rate": rec.get("resting_heart_rate"),
                "respiratory_rate": rec.get("respiratory_rate"),
                "spo2_percentage": rec.get("spo2_percentage"),
                "skin_temp_celsius": rec.get("skin_temp"),
            }
        # Fetch baseline separately
        result["baseline"] = get_recovery_baseline()
        slp = get_sleep_summary()
        if slp["status"] == "ok":
            result["sleep"] = {
                "latest_date": slp.get("latest_date"),
                "actual_sleep_duration": slp.get("actual_sleep_duration"),
                "light_sleep": slp.get("light_sleep"),
                "deep_sleep": slp.get("deep_sleep"),
                "rem_sleep": slp.get("rem_sleep"),
                "efficiency": slp.get("efficiency"),
                "performance": slp.get("performance"),
                "nap_count": slp.get("nap_count", 0),
            }
        cyc = get_cycles_summary()
        if cyc["status"] == "ok":
            result["cycles"] = {
                "count": cyc.get("count"),
                "latest_start": cyc.get("latest_start"),
                "latest_end": cyc.get("latest_end"),
                "step_count": cyc.get("step_count"),
            }
        wkt = get_workout_summary()
        if wkt["status"] == "ok":
            result["workout"] = {
                "latest_start": wkt.get("latest_start"),
                "sport_name": wkt.get("sport_name"),
                "duration": wkt.get("duration"),
                "strain": wkt.get("strain"),
                "kilojoule": wkt.get("kilojoule"),
                "avg_hr": wkt.get("avg_hr"),
            }
        body = get_body_measurement()
        if body["status"] == "ok":
            result["body"] = {
                "height_cm": body.get("height_cm"),
                "weight_kg": body.get("weight_kg"),
                "max_heart_rate": body.get("max_heart_rate"),
            }
    except Exception as e:
        logger.error(f"WHOOP dashboard_data error: {type(e).__name__}: {e}")
        result["status"] = "error"
        result["error"] = f"{type(e).__name__}: {e}"
    return result



def _time_range_month(year: int, month: int) -> tuple:
    """Return (start, end) ISO strings for a given month in CST.
    End is the earlier of month-end or now (for current month).
    """
    from calendar import monthrange
    now = datetime.now(CST)
    start_dt = datetime(year, month, 1, tzinfo=CST)
    last_day = monthrange(year, month)[1]
    end_dt = datetime(year, month, last_day, 23, 59, 59, tzinfo=CST)
    if end_dt > now:
        end_dt = now
    start = start_dt.strftime("%Y-%m-%dT%H:%M:%S.000+08:00")
    end = end_dt.strftime("%Y-%m-%dT%H:%M:%S.000+08:00")
    return start, end


def get_monthly_recovery(year: int, month: int) -> dict:
    """Get all recovery records for a specific month."""
    start, end = _time_range_month(year, month)
    try:
        records = _fetch_all_pages(f"{BASE_URL}/recovery", {"start": start, "end": end, "limit": 25})
    except Exception as e:
        logger.warning("Monthly recovery fetch failed: %s", type(e).__name__)
        return {"status": "error", "error": type(e).__name__, "records": []}
    results = []
    for r in records:
        s = _expand_score(r.get("score"))
        results.append({
            "date": _to_cst(r.get("created_at")),
            "recovery_score": s.get("recovery_score"),
            "hrv": s.get("hrv"),
            "resting_heart_rate": s.get("resting_heart_rate"),
            "respiratory_rate": s.get("respiratory_rate"),
        })
    return {"status": "ok", "count": len(results), "records": results}


def get_monthly_sleep(year: int, month: int) -> dict:
    """Get all sleep records for a specific month (excluding naps)."""
    start, end = _time_range_month(year, month)
    try:
        records = _fetch_all_pages(f"{BASE_URL}/activity/sleep", {"start": start, "end": end, "limit": 25})
    except Exception as e:
        logger.warning("Monthly sleep fetch failed: %s", type(e).__name__)
        return {"status": "error", "error": type(e).__name__, "records": []}
    main_sleeps = [r for r in records if not r.get("nap", False)]
    results = []
    for r in main_sleeps:
        s = _expand_score(r.get("score"))
        stage = s.get("stage_summary", {})
        if not isinstance(stage, dict):
            stage = {}
        light = s.get("light_sleep_milli") or stage.get("total_light_sleep_time_milli")
        deep = s.get("deep_sleep_milli") or stage.get("total_slow_wave_sleep_time_milli")
        rem = s.get("rem_sleep_milli") or stage.get("total_rem_sleep_time_milli")
        stage_total = None
        if any(v is not None for v in (light, deep, rem)):
            stage_total = sum(int(v) for v in (light, deep, rem) if v is not None)
        if stage_total is None:
            dur = s.get("sleep_duration_milli") or s.get("total_in_bed_milli")
            if dur is not None:
                stage_total = int(dur)
        results.append({
            "date": _to_cst(r.get("start")),
            "duration_ms": stage_total,
            "duration_str": _ms_to_hm(stage_total),
            "efficiency": s.get("sleep_efficiency_percentage"),
        })
    return {"status": "ok", "count": len(results), "records": results}


def get_monthly_workouts(year: int, month: int) -> dict:
    """Get all workout records for a specific month.
    Returns enriched fields including workout_id, raw start/end times, avg HR.
    Uses workout_id as stable key for upsert to handle time corrections on WHOOP side.
    """
    start, end = _time_range_month(year, month)
    try:
        records = _fetch_all_pages(f"{BASE_URL}/activity/workout", {"start": start, "end": end, "limit": 25})
    except Exception as e:
        logger.warning("Monthly workout fetch failed: %s", type(e).__name__)
        return {"status": "error", "error": type(e).__name__, "records": []}
    results = []
    for r in records:
        s = _expand_score(r.get("score"))
        dur_ms = None
        raw_start = r.get("start", "")
        raw_end = r.get("end", "")
        if raw_start and raw_end:
            try:
                dur_ms = int((
                    datetime.fromisoformat(raw_end.replace("Z", "+00:00")) -
                    datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
                ).total_seconds() * 1000)
            except Exception:
                pass
        results.append({
            "workout_id": str(r.get("id", "")),
            "date": _to_cst(raw_start),
            "start_utc": raw_start,
            "end_utc": raw_end,
            "start_cst": _to_cst(raw_start),
            "end_cst": _to_cst(raw_end),
            "sport_name": r.get("sport_name", ""),
            "duration_ms": dur_ms,
            "duration_str": _ms_to_hm(dur_ms),
            "strain": s.get("strain"),
            "average_heart_rate": s.get("average_heart_rate"),
            "max_heart_rate": s.get("max_heart_rate"),
            "kilojoule": s.get("kilojoule"),
        })
    return {"status": "ok", "count": len(results), "records": results}


def get_whoop_dashboard() -> str:
    """Generate a formatted WHOOP dashboard summary for bot reply."""
    lines = ["📊 **WHOOP 健康数据摘要**\n"]
    try:
        rec = get_recovery_summary()
        if rec["status"] == "ok":
            lines.append(f"💚 **恢复** ({rec['latest_date']})")
            if rec.get("recovery_score") is not None:
                lines.append(f"  恢复评分: {rec['recovery_score']}%")
            if rec.get("hrv") is not None:
                lines.append(f"  HRV: {rec['hrv']} ms")
            if rec.get("resting_heart_rate") is not None:
                lines.append(f"  静息心率: {rec['resting_heart_rate']} bpm")
            if rec.get("respiratory_rate") is not None:
                lines.append(f"  呼吸频率: {rec['respiratory_rate']} rpm")
            lines.append("")
        else:
            lines.append("💚 **恢复**: 近7天无记录\n")

        slp = get_sleep_summary()
        if slp["status"] == "ok":
            lines.append(f"😴 **睡眠** ({slp['latest_date']})")
            lines.append(f"  实际睡眠: {slp['actual_sleep_duration']}")
            lines.append(f"  浅睡/深睡/REM: {slp['light_sleep']} / {slp['deep_sleep']} / {slp['rem_sleep']}")
            if slp.get("efficiency") is not None:
                lines.append(f"  睡眠效率: {slp['efficiency']}%")
            if slp.get("performance") is not None:
                lines.append(f"  睡眠表现: {slp['performance']}%")
            if slp.get("nap_count", 0) > 0:
                lines.append(f"  (另有 {slp['nap_count']} 次小睡已排除)")
            lines.append("")
        else:
            lines.append("😴 **睡眠**: 近7天无记录\n")

        cyc = get_cycles_summary()
        if cyc["status"] == "ok":
            lines.append(f"🔄 **周期**: 近7天 {cyc['count']} 个周期")
            lines.append(f"  最新: {cyc['latest_start']} ~ {cyc['latest_end']}")
            if cyc.get("step_count") is not None:
                lines.append(f"  步数: {cyc['step_count']}")
            lines.append("")

        wkt = get_workout_summary()
        if wkt["status"] == "ok":
            lines.append(f"🏋️ **运动** ({wkt['latest_start']})")
            lines.append(f"  类型: {wkt.get('sport_name', 'N/A')} | 时长: {wkt['duration']}")
            if wkt.get("strain") is not None:
                lines.append(f"  Strain: {float(wkt['strain']):.1f}")
            if wkt.get("kilojoule") is not None:
                lines.append(f"  消耗: {round(float(wkt['kilojoule']))} kJ")
            if wkt.get("avg_hr") is not None:
                lines.append(f"  平均心率: {wkt['avg_hr']} bpm")
            lines.append("")
        else:
            lines.append("🏋️ **运动**: 近7天无记录\n")

        body = get_body_measurement()
        if body["status"] == "ok":
            parts = []
            if "height_cm" in body:
                parts.append(f"身高 {body['height_cm']}cm")
            if "weight_kg" in body:
                parts.append(f"体重 {body['weight_kg']}kg")
            if "max_heart_rate" in body:
                parts.append(f"最大心率 {body['max_heart_rate']}bpm")
            lines.append(f"📏 **身体指标**: {', '.join(parts)}")
        else:
            lines.append("📏 **身体指标**: 暂无数据")

    except Exception as e:
        logger.error(f"WHOOP dashboard error: {type(e).__name__}: {e}")
        lines.append(f"\n⚠️ 数据获取异常: {type(e).__name__}")

    return "\n".join(lines)
