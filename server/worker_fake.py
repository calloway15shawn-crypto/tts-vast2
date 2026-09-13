"""Тестовый рабочий процесс без видеокарты: генерирует тон вместо речи.

Нужен, чтобы проверить весь конвейер (API, нарезку, проверку, склейку) локально или на дешёвой машине.
Включается переменной FAKE_ENGINES=1.
"""
import hashlib
import os

import numpy as np
import soundfile as sf

from worker_common import serve


def tts(req):
    text, seed = req["text"], int(req.get("seed", 0))
    sr = 24000
    seconds = max(0.5, len(text) / 15.0)
    t = np.arange(int(sr * seconds)) / sr
    audio = 0.2 * np.sin(2 * np.pi * 220 * t).astype(np.float32)
    audio = np.concatenate([np.zeros(sr // 5, np.float32), audio, np.zeros(sr // 5, np.float32)])
    sf.write(req["out_path"], audio, sr)
    # Имитация ошибки: у каждого пятого фрагмента первая попытка «неудачная»
    bad = int(hashlib.md5(text.encode()).hexdigest(), 16) % 5 == 0 and seed % 10 == 0
    heard = "совсем другой текст" if bad else text
    if os.environ.get("FAKE_ALWAYS_BAD") == "1":
        heard = "ошибка"
    with open(req["out_path"] + ".heard", "w", encoding="utf-8") as f:
        f.write(heard)
    return {"sr": sr, "duration": len(audio) / sr}


def asr(req):
    try:
        with open(req["path"] + ".heard", encoding="utf-8") as f:
            return {"text": f.read()}
    except FileNotFoundError:
        return {"text": ""}


if __name__ == "__main__":
    serve({"load": lambda r: {}, "tts": tts, "asr": asr, "transcribe_voice": lambda r: {"text": "образец"}})
