"""HTTP API сервера озвучки. Запуск: uvicorn app:app --host 0.0.0.0 --port 8000"""
import hmac
import os
import re
import tempfile
import threading
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

import audio
from jobs import HOME, VOICES_DIR, JobManager
from setup_env import SetupState, run_setup

API_TOKEN = os.environ.get("API_TOKEN", "")
if len(API_TOKEN) < 16:
    raise SystemExit("API_TOKEN не задан или слишком короткий — сервер не запускается без защиты")

ENGINES = [e.strip() for e in os.environ.get("ENGINES", "qwen").split(",") if e.strip()]

app = FastAPI(title="tts-vast")
setup_state = SetupState()
threading.Thread(target=run_setup, args=(setup_state, ENGINES), daemon=True).start()
manager = JobManager(setup_state)


def auth(authorization: str = Header(default="")):
    expected = f"Bearer {API_TOKEN}"
    if not hmac.compare_digest(authorization.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="неверный токен")


class JobIn(BaseModel):
    name: str
    lang: str
    text: str
    voice: str
    settings: dict = {}


@app.get("/health", dependencies=[Depends(auth)])
def health():
    return {"ready": setup_state.ready, "stage": setup_state.stage, "error": setup_state.error}


@app.put("/voices/{filename}", dependencies=[Depends(auth)])
async def upload_voice(filename: str, request: Request):
    m = re.fullmatch(r"([A-Za-z0-9_\-]{1,64})\.(wav|mp3|flac|m4a|ogg|txt)", filename)
    if not m:
        raise HTTPException(400, "имя файла: латиница, цифры, _ или -, расширение wav/mp3/flac/m4a/ogg/txt")
    name, ext = m.groups()
    body = await request.body()
    if not body or len(body) > 50 * 1024 * 1024:
        raise HTTPException(400, "файл пустой или больше 50 МБ")
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    if ext == "txt":
        (VOICES_DIR / f"{name}.txt").write_text(body.decode("utf-8-sig"), encoding="utf-8")
        return {"ok": True}
    with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
        tmp.write(body)
    try:
        audio.prepare_voice(tmp.name, str(VOICES_DIR / f"{name}.wav"))
    except RuntimeError as exc:
        raise HTTPException(400, f"не удалось прочитать аудио: {exc}") from exc
    finally:
        Path(tmp.name).unlink(missing_ok=True)
    (VOICES_DIR / f"{name}.auto.txt").unlink(missing_ok=True)
    return {"ok": True}


@app.post("/jobs", dependencies=[Depends(auth)])
def create_job(job: JobIn):
    try:
        return manager.submit(job.name, job.lang, job.text, job.voice, job.settings)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/jobs", dependencies=[Depends(auth)])
def list_jobs():
    return manager.list()


@app.get("/jobs/{job_id}", dependencies=[Depends(auth)])
def get_job(job_id: str):
    job = manager.get(job_id)
    if not job:
        raise HTTPException(404, "задача не найдена")
    return job


@app.get("/jobs/{job_id}/audio", dependencies=[Depends(auth)])
def get_audio(job_id: str):
    path = manager.result_path(job_id)
    if not path or not path.exists():
        raise HTTPException(404, "аудио ещё не готово")
    return FileResponse(path)


@app.get("/jobs/{job_id}/report", dependencies=[Depends(auth)])
def get_report(job_id: str):
    path = manager.report_path(job_id)
    if not path:
        raise HTTPException(404, "отчёт ещё не готов")
    return FileResponse(path, media_type="application/json")


@app.get("/logs", dependencies=[Depends(auth)])
def logs():
    parts = []
    for p in sorted((HOME / "logs").glob("*.log")) + [Path("/workspace/onstart.log")]:
        if p.exists():
            parts.append(f"===== {p.name} =====\n" + p.read_text(encoding="utf-8", errors="replace")[-20000:])
    return PlainTextResponse("\n\n".join(parts) or "логов нет")


@app.exception_handler(Exception)
def unhandled(_request, exc):
    return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})
