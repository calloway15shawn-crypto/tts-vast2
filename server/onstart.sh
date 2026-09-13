#!/bin/bash
# Запускается на машине Vast после скачивания кода. Ставит системные пакеты и запускает API.
# Установка моделей идёт уже внутри API в фоне — прогресс виден через /health.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
export TTS_HOME=/workspace/tts
export VENVS_DIR=/workspace/venvs
export HF_HOME=/workspace/hf
mkdir -p "$TTS_HOME/logs" "$HF_HOME"

echo "[onstart] $(date) системные пакеты"
if ! command -v ffmpeg >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq ffmpeg >/dev/null
fi

PY="$(command -v python || echo /opt/conda/bin/python)"
echo "[onstart] python: $PY ($($PY --version 2>&1))"
"$PY" -m pip install -q "fastapi>=0.110" "uvicorn>=0.29" soundfile numpy pydantic

# Параметры, которые клиент передал при создании машины, сохраняем для ручных SSH-сессий
env | grep -E '^(API_TOKEN|ENGINES|QWEN_MODEL|VOXCPM_MODEL|ASR_MODEL|FAKE_ENGINES)=' > "$TTS_HOME/env" || true

echo "[onstart] $(date) запуск API на порту 8000"
cd "$APP_DIR"
nohup "$PY" -m uvicorn app:app --host 0.0.0.0 --port 8000 >> "$TTS_HOME/logs/api.log" 2>&1 &
echo "[onstart] готово, pid $!"
