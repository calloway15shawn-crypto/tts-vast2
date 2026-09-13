"""Аудио: чтение, обрезка тишины, склейка с паузами, экспорт через ffmpeg."""
import subprocess

import numpy as np
import soundfile as sf


def load_mono(path):
    data, sr = sf.read(path, dtype="float32", always_2d=True)
    return data.mean(axis=1), sr


def trim_silence(x, sr, threshold_db=-45.0, pad_ms=60, frame_ms=20):
    """Обрезать тишину по краям фрагмента, оставив небольшой запас."""
    if len(x) == 0:
        return x
    frame = max(1, int(sr * frame_ms / 1000))
    n = len(x) // frame
    if n == 0:
        return x
    rms = np.sqrt(np.mean(x[: n * frame].reshape(n, frame) ** 2, axis=1) + 1e-12)
    loud = np.where(20 * np.log10(rms) > threshold_db)[0]
    if len(loud) == 0:
        return x[:0]
    pad = int(sr * pad_ms / 1000)
    start = max(0, loud[0] * frame - pad)
    end = min(len(x), (loud[-1] + 1) * frame + pad)
    return x[start:end]


def speech_seconds(x, sr):
    """Длительность без тишины по краям (для проверки темпа)."""
    return len(trim_silence(x, sr, pad_ms=0)) / sr if sr else 0.0


def concat(chunk_paths, pauses, out_wav):
    """Склеить фрагменты с паузами. Возвращает (длительность, [время начала каждого фрагмента])."""
    parts, starts, t, sr_ref = [], [], 0.0, None
    for path, pause in zip(chunk_paths, pauses):
        x, sr = load_mono(path)
        if sr_ref is None:
            sr_ref = sr
        elif sr != sr_ref:
            raise RuntimeError(f"разная частота фрагментов: {sr} и {sr_ref}")
        x = trim_silence(x, sr)
        starts.append(t)
        parts.append(x)
        t += len(x) / sr
        if pause > 0:
            parts.append(np.zeros(int(sr * pause), dtype=np.float32))
            t += pause
    if not parts:
        raise RuntimeError("нет фрагментов для склейки")
    audio = np.concatenate(parts)
    sf.write(out_wav, audio, sr_ref, subtype="PCM_16")
    return len(audio) / sr_ref, starts


def run_ffmpeg(args):
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg: {proc.stderr.strip()[-800:]}")


def prepare_voice(src, dst, max_seconds=20):
    """Привести образец голоса к WAV 24 кГц моно, не длиннее max_seconds."""
    run_ffmpeg(["-i", src, "-t", str(max_seconds), "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", dst])


def export(full_wav, out_path, fmt="mp3", loudness=-16.0, sample_rate=48000):
    """Нормализовать громкость и сохранить в mp3 или wav."""
    af = f"loudnorm=I={loudness}:TP=-1.5:LRA=11"
    codec = ["-c:a", "libmp3lame", "-b:a", "192k"] if fmt == "mp3" else ["-c:a", "pcm_s16le"]
    run_ffmpeg(["-i", full_wav, "-af", af, "-ar", str(sample_rate), "-ac", "1", *codec, out_path])
