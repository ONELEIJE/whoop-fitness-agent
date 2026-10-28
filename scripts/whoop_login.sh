#!/usr/bin/env bash
# WHOOP 首次授权：起本地授权服务 + Cloudflare 快速隧道，拿到公网 HTTPS 回调地址。
#
# 用它的原因：WHOOP 要求 redirect_uri 是 https，而你的服务器/本机没有域名和证书时，
# 用 cloudflared 快速隧道可以白嫖一个临时 HTTPS 域名（重启会变，但授权只需做一次）。
#
# 用法：bash scripts/whoop_login.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PORT=${WHOOP_AUTH_PORT:-9876}
LOG=/tmp/whoop-tunnel.log
mkdir -p app/data

command -v python3 >/dev/null || { echo "缺少 python3"; exit 1; }
command -v cloudflared >/dev/null || {
  cat <<'EOF'
缺少 cloudflared，先装一个：
  # macOS
  brew install cloudflared
  # Ubuntu / Debian
  curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o /tmp/cf.deb && sudo dpkg -i /tmp/cf.deb
EOF
  exit 1
}

echo "== 1/4 启动隧道 =="
: > "$LOG"
cloudflared tunnel --url "http://127.0.0.1:$PORT" --no-autoupdate >"$LOG" 2>&1 &
TUNNEL_PID=$!
trap 'kill "$TUNNEL_PID" 2>/dev/null || true' EXIT

URL=""
for _ in $(seq 1 30); do
  URL=$(grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" 2>/dev/null | head -1 || true)
  [ -n "$URL" ] && break
  sleep 1
done
if [ -z "$URL" ]; then
  echo "隧道没起来，看一眼 $LOG"; exit 1
fi

echo "== 2/4 写入 redirect_uri =="
python3 - "$URL" <<'PY'
import json, sys, pathlib
url = sys.argv[1].rstrip("/")
p = pathlib.Path("app/data/whoop_auth_config.json")
p.write_text(json.dumps({"redirect_uri": url + "/callback", "tunnel_url": url},
                        ensure_ascii=False, indent=2), encoding="utf-8")
print("   redirect_uri =", url + "/callback")
PY

cat <<EOF
== 3/4 去 WHOOP 后台登记回调地址 ==
   WHOOP Developer Dashboard → 你的 App → Redirect URI 填：
   $URL/callback
   （保存失败说明地址没对上；这个域名每次重启隧道都会变，但 token 拿到后会自己续期）

== 4/4 授权服务已启动，浏览器打开 ==
   $URL
   授权成功后 app/data/whoop_token.json 会生成（权限 600）。
   按 Ctrl+C 结束。
EOF

# 不用 exec：让 EXIT trap 在退出时能关掉隧道，避免留一个孤儿 cloudflared 进程
python3 app/whoop_auth.py
