"""Рабочий процесс VoxCPM2 (по умолчанию — для польского)."""
import os

from worker_common import serve

MODEL_ID = os.environ.get("VOXCPM_MODEL", "openbmb/VoxCPM2")
# torch.compile ускоряет, но требует компилятора; по умолчанию выключен ради надёжности
OPTIMIZE = os.environ.get("VOXCPM_OPTIMIZE", "0") == "1"
model = None


def load(_req=None):
    global model
    if model is not None:
        return {}
    from voxcpm import VoxCPM

    model = VoxCPM.from_pretrained(MODEL_ID, load_denoiser=False, optimize=OPTIMIZE)
    return {"model": MODEL_ID}


def tts(req):
    import numpy as np
    import soundfile as sf
    import torch

    load()
    torch.manual_seed(int(req.get("seed", 0)))
    audio = model.generate(
        text=req["text"],
        reference_wav_path=req["voice_wav"],
        cfg_value=float(req.get("cfg_value", 2.0)),
        inference_timesteps=int(req.get("timesteps", 10)),
    )
    audio = np.asarray(audio, dtype=np.float32).flatten()
    sr = model.tts_model.sample_rate
    sf.write(req["out_path"], audio, sr)
    return {"sr": sr, "duration": len(audio) / sr}


if __name__ == "__main__":
    serve({"load": load, "tts": tts})
