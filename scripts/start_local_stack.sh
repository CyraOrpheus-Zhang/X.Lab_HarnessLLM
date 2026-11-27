#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"
HARNESS="$PROJECT_DIR/.venv/bin/xue-harness"
LOG_DIR="$PROJECT_DIR/logs"

if [[ ! -x "$HARNESS" || ! -x "$VENV_PYTHON" ]]; then
  echo "项目虚拟环境还未安装。请先按 README 完成 pip install -e ." >&2
  exit 1
fi

cd "$PROJECT_DIR"
mkdir -p "$LOG_DIR"

if [[ -f "$PROJECT_DIR/.env.txt" ]]; then
  echo "检测到 .env.txt：程序读取的是 .env；macOS 文本编辑器可能自动追加了 .txt。"
fi

# 预先检查 Slack 配置，避免模型载入数分钟后才提示缺少凭据。
"$VENV_PYTHON" - <<'PY'
from dotenv import dotenv_values
from pathlib import Path
settings = dotenv_values(Path('.env'))
missing = [key for key in ('SLACK_BOT_TOKEN', 'SLACK_APP_TOKEN', 'SLACK_ALLOWED_USER_IDS') if not settings.get(key)]
if missing:
    raise SystemExit('请先配置 .env：' + ', '.join(missing))
PY

MODEL_PID=""
WEB_PID=""
if "$VENV_PYTHON" - <<'PY' >/dev/null 2>&1
import urllib.request
with urllib.request.urlopen('http://127.0.0.1:8080/v1/models', timeout=2) as response:
    assert response.status == 200
PY
then
  echo "检测到已有 MLX API，复用 127.0.0.1:8080。"
else
  "$HARNESS" models serve >"$LOG_DIR/mlx-server.log" 2>&1 &
  MODEL_PID=$!
fi

cleanup() {
  if [[ -n "$WEB_PID" ]]; then
    kill "$WEB_PID" 2>/dev/null || true
    wait "$WEB_PID" 2>/dev/null || true
  fi
  if [[ -n "$MODEL_PID" ]]; then
    pkill -TERM -P "$MODEL_PID" 2>/dev/null || true
    kill "$MODEL_PID" 2>/dev/null || true
    wait "$MODEL_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

if [[ -n "$MODEL_PID" ]]; then
  echo "等待本地 MLX 模型启动；首次载入可能需要数分钟。日志：$LOG_DIR/mlx-server.log"
fi
ready=0
for _ in $(seq 1 180); do
  if [[ -n "$MODEL_PID" ]] && ! kill -0 "$MODEL_PID" 2>/dev/null; then
    echo "MLX 服务已退出，请查看 $LOG_DIR/mlx-server.log" >&2
    exit 1
  fi
  if "$VENV_PYTHON" - <<'PY' >/dev/null 2>&1
import urllib.request
with urllib.request.urlopen('http://127.0.0.1:8080/v1/models', timeout=2) as response:
    assert response.status == 200
PY
  then
    ready=1
    break
  fi
  sleep 2
done

if [[ "$ready" != 1 ]]; then
  echo "等待 MLX 服务超时，请查看 $LOG_DIR/mlx-server.log" >&2
  exit 1
fi

echo "本地模型 API 已就绪，启动控制台和 Slack Socket Mode。按 Ctrl-C 停止由此脚本启动的服务。"
if ! "$VENV_PYTHON" - <<'PY' >/dev/null 2>&1
import urllib.request
with urllib.request.urlopen('http://127.0.0.1:8090/api/dashboard', timeout=2) as response:
    assert response.status == 200
PY
then
  "$HARNESS" serve-web >"$LOG_DIR/control-panel.log" 2>&1 &
  WEB_PID=$!
  for _ in $(seq 1 30); do
    if ! kill -0 "$WEB_PID" 2>/dev/null; then
      echo "控制台无法启动，请查看 $LOG_DIR/control-panel.log" >&2
      exit 1
    fi
    if "$VENV_PYTHON" - <<'PY' >/dev/null 2>&1
import urllib.request
with urllib.request.urlopen('http://127.0.0.1:8090/api/dashboard', timeout=2) as response:
    assert response.status == 200
PY
    then break; fi
    sleep 1
  done
fi
echo "控制台地址：http://127.0.0.1:8090"
"$HARNESS" serve-slack
