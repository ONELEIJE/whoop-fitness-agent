#!/usr/bin/env python3
"""WHOOP OAuth2 授权小工具（跑在宿主机或本地即可）。

监听 127.0.0.1:9876：
  GET /          -> 302 跳转到 WHOOP 授权页（带随机 state）
  GET /callback  -> 用 code 换 token，原子写入 data/whoop_token.json（权限 600）
  GET /health    -> 探活

redirect_uri 不在代码里写死，而是读 data/whoop_auth_config.json：
授权前先把公网 HTTPS 地址写进那个文件（WHOOP 后台的 Redirect URI 要一字不差）。
"""
import json, os, secrets, sys, time, urllib.parse, tempfile
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent

# 数据目录：容器内挂载 ./app/data，宿主机直接跑默认也是它
DATA_DIR = Path(os.environ.get("WHOOP_DATA_DIR", str(BASE_DIR / "data")))
TOKEN_FILE = DATA_DIR / "whoop_token.json"
# .env 可能在 app/ 下，也可能在仓库根目录下，两处都找
_ENV_CANDIDATES = [BASE_DIR / ".env", BASE_DIR.parent / ".env"]
ENV_FILE = next((p for p in _ENV_CANDIDATES if p.exists()), _ENV_CANDIDATES[0])

AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"

# redirect_uri is loaded from env or defaults; updated at runtime via config file
CONFIG_FILE = DATA_DIR / "whoop_auth_config.json"

SCOPES = "offline read:recovery read:cycles read:workout read:sleep read:body_measurement"

# In-memory state store: {state_str: created_timestamp}
_pending_states: dict[str, float] = {}
STATE_TTL = 600  # 10 minutes


def load_env() -> dict:
    cfg = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    return cfg


def get_credentials() -> tuple[str, str]:
    cfg = load_env()
    cid = cfg.get("WHOOP_CLIENT_ID", "")
    csec = cfg.get("WHOOP_CLIENT_SECRET", "")
    if not cid:
        raise RuntimeError("WHOOP_CLIENT_ID missing in .env")
    if not csec:
        raise RuntimeError("WHOOP_CLIENT_SECRET missing in .env")
    return cid, csec


def get_redirect_uri() -> str:
    """Load redirect_uri from config file (set after tunnel is established)."""
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text())
            uri = cfg.get("redirect_uri", "")
            if uri:
                return uri
        except (json.JSONDecodeError, OSError):
            pass
    # Fallback: should not be used in production
    return "http://127.0.0.1:9876/callback"


def save_token(token_data: dict) -> None:
    """Atomically save token with expires_at and restricted permissions."""
    now = int(time.time())
    token_data["saved_at"] = now
    if "expires_in" in token_data:
        token_data["expires_at"] = now + int(token_data["expires_in"])
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Atomic write: write to temp file then rename
    fd, tmp_path = tempfile.mkstemp(dir=str(DATA_DIR), prefix=".whoop_token_", suffix=".tmp")
    try:
        os.write(fd, json.dumps(token_data, indent=2).encode())
        os.fsync(fd)
        os.close(fd)
        os.chmod(tmp_path, 0o600)
        os.rename(tmp_path, str(TOKEN_FILE))
        print(f"[whoop_auth] Token saved (expires_at={token_data.get('expires_at', 'N/A')})")
    except Exception:
        os.close(fd) if not os.get_inheritable(fd) else None
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def generate_state() -> str:
    state = secrets.token_urlsafe(32)
    _pending_states[state] = time.time()
    # Prune expired states
    cutoff = time.time() - STATE_TTL
    expired = [s for s, t in _pending_states.items() if t < cutoff]
    for s in expired:
        del _pending_states[s]
    return state


def validate_state(state: str) -> bool:
    if not state or state not in _pending_states:
        return False
    created = _pending_states.pop(state, 0)
    return (time.time() - created) <= STATE_TTL


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Suppress default logging to avoid leaking sensitive query params
        pass

    def _safe_log(self, msg: str):
        """Log without tokens, secrets, codes, or callback query params."""
        # Redact known sensitive patterns
        import re
        msg = re.sub(r'(code|token|secret|access_token|refresh_token)=\S+', r'\1=***', msg, flags=re.I)
        print(f"[whoop_auth] {msg}", flush=True)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self._handle_index()
        elif parsed.path == "/callback":
            self._handle_callback(parsed)
        elif parsed.path == "/health":
            self._send(200, "ok")
        else:
            self._send(404, "Not found")

    def _handle_index(self):
        try:
            cid, _ = get_credentials()
        except RuntimeError as e:
            self._safe_log(f"Index error: {e}")
            self._send(500, str(e))
            return
        state = generate_state()
        redirect_uri = get_redirect_uri()
        params = {
            "client_id": cid,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPES,
            "state": state,
        }
        url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
        self._safe_log(f"Redirecting to WHOOP auth (state generated, redirect_uri={redirect_uri})")
        self.send_response(302)
        self.send_header("Location", url)
        self.end_headers()

    def _handle_callback(self, parsed):
        qs = urllib.parse.parse_qs(parsed.query)
        error = qs.get("error", [None])[0]
        if error:
            self._safe_log(f"OAuth error received: {error}")
            self._send(400, f"OAuth error: {error}")
            return

        code = qs.get("code", [None])[0]
        state = qs.get("state", [None])[0]

        if not state or not validate_state(state):
            self._safe_log("Callback rejected: invalid or expired state")
            self._send(400, "Invalid or expired state parameter")
            return

        if not code:
            self._safe_log("Callback rejected: missing code")
            self._send(400, "Missing code parameter")
            return

        try:
            cid, csec = get_credentials()
            redirect_uri = get_redirect_uri()
            resp = requests.post(TOKEN_URL, data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": cid,
                "client_secret": csec,
            }, timeout=15)
            if resp.status_code != 200:
                self._safe_log(f"Token exchange failed: HTTP {resp.status_code}")
                self._send(502, f"Token exchange failed: {resp.status_code}")
                return
            token_data = resp.json()
            save_token(token_data)
            self._safe_log("Token exchange successful, token saved")
            self._send(200, "Authorization successful! Token saved. You may close this page.")
        except RuntimeError as e:
            self._safe_log(f"Token exchange error: {e}")
            self._send(500, str(e))
        except Exception as e:
            self._safe_log(f"Unexpected error during token exchange: {type(e).__name__}")
            self._send(500, "Internal error during token exchange")

    def _send(self, code: int, body: str):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())


def main():
    host, port = "127.0.0.1", 9876
    server = HTTPServer((host, port), Handler)
    print(f"[whoop_auth] Listening on {host}:{port}", flush=True)
    print(f"[whoop_auth] Data dir: {DATA_DIR}", flush=True)
    print(f"[whoop_auth] Redirect URI: {get_redirect_uri()}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[whoop_auth] Shutting down", flush=True)
        server.shutdown()


if __name__ == "__main__":
    main()
