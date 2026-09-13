"""Подготовка окружений на машине Vast. Выполняется в фоне после старта API.

Каждая модель ставится в своё виртуальное окружение: у Qwen и VoxCPM несовместимые версии transformers.
Окружения видят системный PyTorch из образа, поэтому torch повторно не скачивается.
"""
import os
import subprocess
import sys
from pathlib import Path

VENVS = Path(os.environ.get("VENVS_DIR", "/workspace/venvs"))
LOG_DIR = Path(os.environ.get("TTS_HOME", "/workspace/tts")) / "logs"

PACKAGES = {
    # transformers пиньуем жёстко: faster-qwen3-tts 0.4.0 собрана 2026-08-25 против 5.15.1,
    # а в 5.16+ у MimiConfig убрали rope_theta и модель перестаёт загружаться:
    # AttributeError: 'MimiConfig' object has no attribute 'rope_theta'
    "qwen": ["faster-qwen3-tts==0.4.0", "transformers==5.15.1", "soundfile"],
    "voxcpm": ["voxcpm==2.0.3", "soundfile"],
}

MODELS = {
    "qwen": [os.environ.get("QWEN_MODEL", "Qwen/Qwen3-TTS-12Hz-1.7B-Base"),
             os.environ.get("ASR_MODEL", "openai/whisper-large-v3-turbo")],
    "voxcpm": [os.environ.get("VOXCPM_MODEL", "openbmb/VoxCPM2")],
}


class SetupState:
    def __init__(self):
        self.stage = "ожидание"
        self.ready = False
        self.error = None


def _run(cmd, log_name):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / log_name, "a", encoding="utf-8") as log:
        log.write(f"\n$ {' '.join(cmd)}\n")
        log.flush()
        proc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)
    if proc.returncode != 0:
        tail = (LOG_DIR / log_name).read_text(encoding="utf-8", errors="replace")[-1500:]
        raise RuntimeError(f"команда завершилась с ошибкой: {' '.join(cmd[:4])}…\n{tail}")


def _constraints_file():
    """Запретить pip менять torch/torchaudio из образа."""
    import importlib.metadata as md

    lines = []
    for pkg in ("torch", "torchaudio", "torchvision"):
        try:
            lines.append(f"{pkg}=={md.version(pkg)}")
        except md.PackageNotFoundError:
            pass
    path = VENVS / "constraints.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run_setup(state, engines):
    if os.environ.get("FAKE_ENGINES") == "1":
        state.stage, state.ready = "готово (тестовый режим)", True
        return
    try:
        engines = ["qwen"] + [e for e in engines if e != "qwen"]  # qwen нужен всегда: там Whisper
        constraints = _constraints_file()
        for engine in engines:
            venv = VENVS / engine
            py = venv / "bin" / "python"
            marker = venv / ".installed"
            if not marker.exists():
                state.stage = f"установка окружения {engine}"
                _run([sys.executable, "-m", "venv", "--system-site-packages", str(venv)], f"setup_{engine}.log")
                _run([str(py), "-m", "pip", "install", "-q", "--upgrade", "pip"], f"setup_{engine}.log")
                _run([str(py), "-m", "pip", "install", "-c", str(constraints), *PACKAGES[engine]],
                     f"setup_{engine}.log")
                marker.write_text("ok", encoding="utf-8")
            for model_id in MODELS[engine]:
                state.stage = f"скачивание модели {model_id}"
                _run([str(py), "-c",
                      f"from huggingface_hub import snapshot_download; snapshot_download('{model_id}')"],
                     f"setup_{engine}.log")
        state.stage, state.ready = "готово", True
    except Exception as exc:  # noqa: BLE001
        state.error = str(exc)
        state.stage = "ошибка установки"
