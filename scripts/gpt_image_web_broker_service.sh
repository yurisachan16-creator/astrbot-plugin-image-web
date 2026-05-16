#!/usr/bin/env bash
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${OPENCLAW_REPO:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
ENV_FILE="${IMAGE_WEB_ENV_FILE:-${GPT_IMAGE_WEB_ENV_FILE:-$HOME/.openclaw/gpt-image-web.env}}"

if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

cd "$REPO_DIR"

BROKER_TOKEN="${IMAGE_WEB_BROKER_TOKEN:-${GPT_IMAGE_WEB_BROKER_TOKEN:-}}"
if [ -z "$BROKER_TOKEN" ]; then
  echo "IMAGE_WEB_BROKER_TOKEN is required; set it in $ENV_FILE or the LaunchAgent environment." >&2
  exit 2
fi
export IMAGE_WEB_BROKER_TOKEN="$BROKER_TOKEN"

if [ -n "${GPT_IMAGE_WEB_PYTHON:-}" ]; then
  PYTHON_CMD=("$GPT_IMAGE_WEB_PYTHON")
elif command -v uv >/dev/null 2>&1; then
  PYTHON_CMD=(uv run python)
elif [ -x "$HOME/AstrBot/.venv/bin/python" ]; then
  PYTHON_CMD=("$HOME/AstrBot/.venv/bin/python")
else
  PYTHON_CMD=(python3)
fi

PROVIDER_MODE="${IMAGE_WEB_PROVIDER_MODE:-${GPT_IMAGE_WEB_BACKEND_MODE:-gpt_browser}}"
case "$PROVIDER_MODE" in
  auto)
    "${PYTHON_CMD[@]}" scripts/astrbot/gpt_image_web_broker.py launch-browser --provider gpt_browser
    "${PYTHON_CMD[@]}" scripts/astrbot/gpt_image_web_broker.py launch-browser --provider ai_studio_browser
    ;;
  browser|gpt_browser)
    "${PYTHON_CMD[@]}" scripts/astrbot/gpt_image_web_broker.py launch-browser --provider gpt_browser
    ;;
  ai_studio_browser)
    "${PYTHON_CMD[@]}" scripts/astrbot/gpt_image_web_broker.py launch-browser --provider ai_studio_browser
    ;;
  *)
    echo "Unknown IMAGE_WEB_PROVIDER_MODE=$PROVIDER_MODE" >&2
    exit 2
    ;;
esac
exec "${PYTHON_CMD[@]}" scripts/astrbot/gpt_image_web_broker.py serve
