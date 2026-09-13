"""Очередь задач озвучки: нарезка, синтез, проверка распознаванием, перегенерация, склейка, отчёт."""
import hashlib
import json
import os
import threading
import time
import traceback
from pathlib import Path

import audio
import textproc
from langs import ENGINE_MAX_CHARS, LANGS, resolve_engine
from settings import validate as validate_settings
from workers import WorkerError, WorkerPool

HOME = Path(os.environ.get("TTS_HOME", "/workspace/tts"))
JOBS_DIR = HOME / "jobs"
VOICES_DIR = HOME / "voices"


def _write_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _remove(directory, name):
    """Удалить файл попытки (и служебный файл тестового режима)."""
    for p in (directory / name, directory / (name + ".heard")):
        p.unlink(missing_ok=True)


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


class JobManager:
    def __init__(self, setup_state):
        self.setup = setup_state
        self.pool = WorkerPool()
        self.lock = threading.Lock()
        self.wakeup = threading.Event()
        JOBS_DIR.mkdir(parents=True, exist_ok=True)
        VOICES_DIR.mkdir(parents=True, exist_ok=True)
        self._resume_interrupted()
        threading.Thread(target=self._loop, daemon=True).start()

    # ---------- API ----------
    def submit(self, name, lang, text, voice, settings=None):
        settings = validate_settings(settings)
        engine = resolve_engine(lang, settings.get("engines"))
        if not (VOICES_DIR / f"{voice}.wav").exists():
            raise ValueError(f"образец голоса '{voice}' не загружен")
        errors, _ = textproc.preflight(text, lang)
        if errors:
            raise ValueError("текст не прошёл проверку: " + "; ".join(errors[:5]))
        key = json.dumps([name, lang, engine, voice, text, settings], ensure_ascii=False, sort_keys=True)
        job_id = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
        job_dir = JOBS_DIR / job_id
        with self.lock:
            if (job_dir / "job.json").exists():
                job = _read_json(job_dir / "job.json")
                if job["status"] == "failed":
                    job.update(status="queued", error=None, stage="в очереди")
                    _write_json(job_dir / "job.json", job)
                    self.wakeup.set()
                return job
            job_dir.mkdir(parents=True)
            chunks = textproc.split_chunks(text, max_chars=ENGINE_MAX_CHARS[engine])
            _write_json(job_dir / "chunks.json", [
                {"i": i, "text": c["text"], "pause": c["pause"], "status": "pending"} for i, c in enumerate(chunks)
            ])
            job = {
                "id": job_id, "name": name, "lang": lang, "engine": engine, "voice": voice,
                "settings": settings, "status": "queued", "stage": "в очереди",
                "total": len(chunks), "done": 0, "flagged": 0, "error": None,
                "created": time.time(), "updated": time.time(), "duration": None,
            }
            _write_json(job_dir / "job.json", job)
        self.wakeup.set()
        return job

    def get(self, job_id):
        path = JOBS_DIR / job_id / "job.json"
        return _read_json(path) if path.exists() else None

    def list(self):
        jobs = [_read_json(p) for p in JOBS_DIR.glob("*/job.json")]
        return sorted(jobs, key=lambda j: j["created"])

    def result_path(self, job_id):
        job = self.get(job_id)
        if not job or job["status"] != "done":
            return None
        return JOBS_DIR / job_id / f"result.{job['settings']['format']}"

    def report_path(self, job_id):
        path = JOBS_DIR / job_id / "report.json"
        return path if path.exists() else None

    # ---------- обработка ----------
    def _resume_interrupted(self):
        for p in JOBS_DIR.glob("*/job.json"):
            job = _read_json(p)
            if job["status"] == "running":
                job.update(status="queued", stage="в очереди (после перезапуска)")
                _write_json(p, job)

    def _next_job(self):
        queued = [j for j in self.list() if j["status"] == "queued"]
        return queued[0] if queued else None

    def _loop(self):
        while True:
            if not self.setup.ready:
                time.sleep(3)
                continue
            job = self._next_job()
            if job is None:
                self.wakeup.wait(timeout=5)
                self.wakeup.clear()
                continue
            self._run_job(job)

    def _update(self, job, **fields):
        job.update(fields, updated=time.time())
        _write_json(JOBS_DIR / job["id"] / "job.json", job)

    def _run_job(self, job):
        job_dir = JOBS_DIR / job["id"]
        try:
            self._update(job, status="running", stage="загрузка моделей")
            s = job["settings"]
            engine = self.pool.get(job["engine"])
            asr = self.pool.get("asr")
            engine.call("load", timeout=3600)
            asr.call("load", timeout=3600)
            voice_wav = str(VOICES_DIR / f"{job['voice']}.wav")
            voice_text = self._voice_text(job["voice"], asr) if s["clone_mode"] == "icl" else ""

            chunks = _read_json(job_dir / "chunks.json")
            for ch in chunks:
                if ch["status"] != "pending":
                    continue
                self._update(job, stage=f"фрагмент {ch['i'] + 1} из {job['total']}")
                self._synth_chunk(job, ch, engine, asr, voice_wav, voice_text, job_dir)
                _write_json(job_dir / "chunks.json", chunks)
                self._update(job, done=sum(c["status"] != "pending" for c in chunks),
                             flagged=sum(c["status"] == "flagged" for c in chunks))

            self._update(job, stage="склейка и нормализация громкости")
            self._assemble(job, chunks, job_dir)
            self._update(job, status="done", stage="готово")
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._update(job, status="failed", stage="ошибка", error=str(exc)[-1500:])

    def _voice_text(self, voice, asr):
        manual = VOICES_DIR / f"{voice}.txt"
        if manual.exists():
            return manual.read_text(encoding="utf-8-sig").strip()
        auto = VOICES_DIR / f"{voice}.auto.txt"
        if not auto.exists():
            text = asr.call("transcribe_voice", path=str(VOICES_DIR / f"{voice}.wav"), language=None)["text"]
            auto.write_text(text, encoding="utf-8")
        return auto.read_text(encoding="utf-8").strip()

    def _synth_chunk(self, job, ch, engine, asr, voice_wav, voice_text, job_dir):
        s, lang = job["settings"], job["lang"]
        chunk_dir = job_dir / "chunks"
        chunk_dir.mkdir(exist_ok=True)
        best = None
        for attempt in range(s["max_attempts"]):   # не меньше 1, проверено в settings.validate
            out = chunk_dir / f"{ch['i']:05d}_a{attempt}.wav"
            try:
                engine.call("tts", timeout=600, text=ch["text"], language=LANGS[lang]["qwen"],
                            voice_wav=voice_wav, voice_text=voice_text, out_path=str(out),
                            seed=ch["i"] * 10 + attempt, temperature=s["temperature"])
                x, sr = audio.load_mono(str(out))
                speech = audio.speech_seconds(x, sr)
                heard = asr.call("asr", timeout=300, path=str(out), language=LANGS[lang]["whisper"])["text"]
            except WorkerError as exc:
                cand = {"file": None, "score": 99.0, "wer": None, "asr": "", "reason": f"сбой модели: {exc}"}
            else:
                score = textproc.wer(ch["text"], heard)
                cps = len(ch["text"]) / max(speech, 0.01)
                reason, penalty = None, 0.0
                if speech < 0.3:
                    reason, penalty = "тишина вместо речи", 10.0
                elif cps > s["max_cps"] or cps < s["min_cps"]:
                    reason, penalty = f"странный темп ({cps:.1f} симв/с)", 1.0
                elif score > s["max_wer"]:
                    reason = f"расхождение с текстом {score:.0%}"
                cand = {"file": out.name, "score": score + penalty, "wer": round(score, 3),
                        "asr": heard, "reason": reason}
            if best is None or cand["score"] < best["score"]:
                if best and best["file"]:
                    _remove(chunk_dir, best["file"])
                best = cand
            elif cand["file"]:
                _remove(chunk_dir, cand["file"])
            if cand["file"] and cand["reason"] is None:
                break

        ch["attempts"] = attempt + 1
        ch["wer"], ch["asr"], ch["reason"] = best["wer"], best["asr"], best["reason"]
        if best["file"] is None:
            raise RuntimeError(f"фрагмент {ch['i'] + 1}: модель не смогла озвучить. {best['reason']}")
        final = chunk_dir / f"{ch['i']:05d}.wav"
        os.replace(chunk_dir / best["file"], final)
        _remove(chunk_dir, best["file"])
        ch["file"] = final.name
        ch["status"] = "ok" if best["reason"] is None else "flagged"

    def _assemble(self, job, chunks, job_dir):
        paths = [str(job_dir / "chunks" / c["file"]) for c in chunks]
        full_wav = job_dir / "full.wav"
        duration, starts = audio.concat(paths, [c["pause"] for c in chunks], str(full_wav))
        fmt = job["settings"]["format"]
        audio.export(str(full_wav), str(job_dir / f"result.{fmt}"), fmt=fmt)
        full_wav.unlink(missing_ok=True)
        flagged = [
            {"fragment": c["i"] + 1, "time": textproc.format_tc(starts[c["i"]]), "reason": c["reason"],
             "text": c["text"], "heard": c.get("asr", ""), "attempts": c.get("attempts")}
            for c in chunks if c["status"] == "flagged"
        ]
        report = {
            "name": job["name"], "lang": job["lang"], "engine": job["engine"],
            "duration": textproc.format_tc(duration), "fragments": len(chunks),
            "flagged_count": len(flagged), "flagged": flagged,
        }
        _write_json(job_dir / "report.json", report)
        self._update(job, duration=round(duration, 1))
