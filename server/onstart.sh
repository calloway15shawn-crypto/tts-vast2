#!/bin/bash
# Запускается на машине Vast после скачивания кода. Ставит системные пакеты и запускает API.
# Установка моделей идёт уже внутри API в фоне — прогресс виден через /health.
#
# Без set -e: одна неудачная команда (чаще всего apt на хосте без доступа к зеркалам)
# не должна молча оставлять машину без API. Всё, что критично, проверяется явно.
set -uo pipefail

step() { echo "[onstart] $(date -u +%H:%M:%S) $*"; }

die() {
  echo "[onstart] ОШИБКА: $*"
  echo "[onstart] API не запущен. Строки выше объясняют, почему."
  exit 1
}

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
export TTS_HOME=/workspace/tts
export VENVS_DIR=/workspace/venvs
export HF_HOME=/workspace/hf
mkdir -p "$TTS_HOME/logs" "$HF_HOME"

step "старт, код в $APP_DIR"
[ -n "${API_TOKEN:-}" ] || die "в контейнер не пришла переменная API_TOKEN — без неё сервер не стартует"

PY="$(command -v python || echo /opt/conda/bin/python)"
[ -x "$PY" ] || die "в образе нет python (искал $PY)"
step "python: $PY ($("$PY" --version 2>&1))"

# ffmpeg нужен для mp3 и подготовки образца голоса. apt на хосте может быть недоступен,
# поэтому есть запасной путь: пакет imageio-ffmpeg приносит готовый статический бинарник.
if ! command -v ffmpeg >/dev/null 2>&1; then
  step "ставлю ffmpeg через apt"
  export DEBIAN_FRONTEND=noninteractive
  if apt-get update -qq && apt-get install -y -qq ffmpeg; then
    step "ffmpeg из apt: $(command -v ffmpeg)"
  else
    step "apt не сработал — беру ffmpeg из пакета imageio-ffmpeg"
    "$PY" -m pip install -q imageio-ffmpeg
    FF="$("$PY" -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())" 2>/dev/null)"
    if [ -n "$FF" ] && [ -x "$FF" ]; then
      ln -sf "$FF" /usr/local/bin/ffmpeg
      step "ffmpeg: $FF"
    else
      die "ffmpeg не удалось поставить ни через apt, ни через pip"
    fi
  fi
fi

step "ставлю пакеты API"
"$PY" -m pip install -q "fastapi>=0.110" "uvicorn>=0.29" soundfile numpy pydantic \
  || die "не удалось поставить пакеты API (строки выше — вывод pip)"

# Параметры, которые клиент передал при создании машины, сохраняем для ручных SSH-сессий
env | grep -E '^(API_TOKEN|ENGINES|QWEN_MODEL|VOXCPM_MODEL|ASR_MODEL|FAKE_ENGINES)=' > "$TTS_HOME/env" || true

step "запуск API на порту 8000"
cd "$APP_DIR" || die "нет каталога $APP_DIR"
nohup "$PY" -m uvicorn app:app --host 0.0.0.0 --port 8000 >> "$TTS_HOME/logs/api.log" 2>&1 &
API_PID=$!

# Убедиться, что API действительно слушает порт. Без этой проверки падение uvicorn
# (например, из-за ошибки импорта) видно только в api.log на самой машине.
for _ in $(seq 1 45); do
  sleep 2
  if ! kill -0 "$API_PID" 2>/dev/null; then
    echo "[onstart] API упал сразу после запуска, последние строки api.log:"
    tail -n 80 "$TTS_HOME/logs/api.log"
    die "uvicorn завершился, pid $API_PID"
  fi
  if "$PY" -c "import socket; socket.create_connection(('127.0.0.1', 8000), 2).close()" 2>/dev/null; then
    step "готово: API слушает порт 8000, pid $API_PID"
    exit 0
  fi
done

echo "[onstart] API не занял порт 8000 за 90 с, последние строки api.log:"
tail -n 80 "$TTS_HOME/logs/api.log"
die "сервер не поднялся"
