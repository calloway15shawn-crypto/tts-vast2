"""Рабочий процесс Qwen3-TTS (ускоренный через faster-qwen3-tts, CUDA Graphs)."""
import os

from worker_common import serve

MODEL_ID = os.environ.get("QWEN_MODEL", "Qwen/Qwen3-TTS-12Hz-1.7B-Base")
model = None


def load(_req=None):
    global model
    if model is not None:
        return {}
    import torch
    from faster_qwen3_tts import FasterQwen3TTS

    model = FasterQwen3TTS.from_pretrained(
        MODEL_ID, device="cuda", dtype=torch.bfloat16, attn_implementation="sdpa", max_seq_len=2048,
    )
    return {"model": MODEL_ID}


def tts(req):
    import numpy as np
    import soundfile as sf
    import torch

    load()
    torch.manual_seed(int(req.get("seed", 0)))
    text = req["text"]
    # Ограничение длины защищает от «зацикливания»: 12 токенов = 1 секунда звука
    max_tokens = min(1800, int(len(text) * 1.8) + 80)
    voice_text = (req.get("voice_text") or "").strip()
    wavs, sr = model.generate_voice_clone(
        text=text,
        language=req["language"],
        ref_audio=req["voice_wav"],
        ref_text=voice_text,
        xvec_only=not voice_text,     # без расшифровки образца — только тембр
        max_new_tokens=max_tokens,
        temperature=float(req.get("temperature", 0.9)),
    )
    audio = np.asarray(wavs[0], dtype=np.float32).flatten()
    sf.write(req["out_path"], audio, sr)
    return {"sr": sr, "duration": len(audio) / sr}


if __name__ == "__main__":
    serve({"load": load, "tts": tts})
