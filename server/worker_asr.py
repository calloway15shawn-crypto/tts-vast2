"""Рабочий процесс проверки: распознаёт озвученный фрагмент обратно в текст (Whisper)."""
import os

from worker_common import serve

MODEL_ID = os.environ.get("ASR_MODEL", "openai/whisper-large-v3-turbo")
pipe = None


def load(_req=None):
    global pipe
    if pipe is not None:
        return {}
    import torch
    from transformers import pipeline

    pipe = pipeline("automatic-speech-recognition", model=MODEL_ID, dtype=torch.float16, device="cuda:0")
    return {"model": MODEL_ID}


def asr(req):
    load()
    kwargs = {"task": "transcribe"}
    if req.get("language"):
        kwargs["language"] = req["language"]
    result = pipe(req["path"], generate_kwargs=kwargs)
    return {"text": (result.get("text") or "").strip()}


def transcribe_voice(req):
    """Расшифровка образца голоса (нужна только для режима клонирования icl)."""
    return asr(req)


if __name__ == "__main__":
    serve({"load": load, "asr": asr, "transcribe_voice": transcribe_voice})
