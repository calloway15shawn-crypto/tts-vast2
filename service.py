"""Локальный API поверх аренды Vast: принять текст запросом — вернуть готовый MP3.

    py service.py        — запустить сервис (по умолчанию http://127.0.0.1:8800)

Машина арендуется, когда появляется первая задача, и удаляется после простоя (idle_minutes).
Пока машина жива, новые запросы уходят на неё же — установка моделей не повторяется.
Поэтому десять запросов подряд платят за установку один раз, а не десять.

Озвучка занимает десятки минут, поэтому API асинхронный: POST /tts возвращает номер задачи,
готовность спрашивается через GET /tts/{id}, аудио забирается через GET /tts/{id}/audio.
"""
import argparse
import atexit
import json
import sys
import threading
import time
import uuid
from pathlib import Path

import requests
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "server"))

import run as cli  # noqa: E402  — конфиг, проверка репозитория, аренда, ожидание сервера
import textproc  # noqa: E402
from langs import resolve_engine  # noqa: E402
from settings import validate as validate_settings  # noqa: E402
from vast_api import Vast, VastError  # noqa: E402

API_DEFAULTS = {
    "host": "127.0.0.1",       # 0.0.0.0 — открыть для других компьютеров (тогда нужен token)
    "port": 8800,
    "token": "",
    "idle_minutes": 25,        # сколько держать машину без работы, прежде чем удалить
    "max_session_hours": 10,   # предохранитель: сессия дольше — машина удаляется
    "max_tries": 3,            # сколько раз пробовать задачу на новых машинах
}


def say(msg=""):
    print(msg, flush=True)


# ---------------------------------------------------------------- очередь и аренда

class Service:
    """Очередь задач и одна арендованная машина на всю очередь."""

    def __init__(self, cfg, api_cfg):
        self.cfg, self.api_cfg = cfg, api_cfg
        self.jobs = {}                      # id -> запись задачи
        self.order = []                     # порядок поступления
        # Рекурсивный: _set вызывается из блоков, которые уже держат замок,
        # а сохранение очереди берёт его снова.
        self.lock = threading.RLock()
        self.machine = None                 # что сейчас арендовано
        self.state_file = ROOT / "output" / ".queue.json"
        self.bad_hosts = cli.load_bad_hosts()   # переживает перезапуск сервиса
        self.rental = None                  # активная аренда, чтобы удалить её при остановке
        self.rental_lock = threading.Lock()
        self.repo_checked = False
        self.stopping = threading.Event()
        self._restore()
        threading.Thread(target=self._worker, daemon=True).start()

    # ---------- очередь на диске ----------
    def _save(self) -> None:
        """Сложить очередь на диск: пачка из десяти роликов идёт часами, и
        перезапуск сервиса не должен означать потерю всего сделанного."""
        try:
            with self.lock:
                данные = [self.jobs[i] for i in self.order]
            tmp = self.state_file.with_suffix(".tmp")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(данные, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.state_file)
        except OSError as exc:
            say(f"  (не удалось сохранить очередь: {exc})")

    def _restore(self) -> None:
        """Поднять очередь с диска. Незаконченное возвращается в очередь: сервер
        на машине считает номер задачи от содержимого, поэтому повторная отправка
        того же текста не создаёт дубля и не переозвучивает заново."""
        try:
            данные = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        готово = снова = 0
        for j in данные if isinstance(данные, list) else []:
            jid = j.get("id")
            if not jid:
                continue
            if j.get("status") == "running":
                j["status"], j["stage"] = "queued", "в очереди (после перезапуска)"
                снова += 1
            elif j.get("status") == "done":
                готово += 1
            self.jobs[jid] = j
            self.order.append(jid)
        if self.jobs:
            say(f"  Очередь восстановлена: {len(self.jobs)} задач "
                f"(готово {готово}, вернулось в очередь {снова})")

    # ---------- то, что видит HTTP-слой ----------
    def add(self, name, lang, text, voice, settings):
        job_id = uuid.uuid4().hex[:12]
        with self.lock:
            self.jobs[job_id] = {
                "id": job_id, "name": name, "lang": lang, "voice": voice,
                "text": text, "settings": settings,
                "status": "queued", "stage": "в очереди", "total": 0, "done": 0,
                "flagged": 0, "duration": None, "error": None, "tries": 0,
                "created": time.time(), "updated": time.time(),
                "audio": None, "report": None,
            }
            self.order.append(job_id)
        self._save()
        return self.public(job_id)

    def public(self, job_id):
        """Запись задачи без текста и настроек — то, что отдаём наружу."""
        with self.lock:
            j = self.jobs.get(job_id)
            if not j:
                return None
            hidden = ("text", "settings", "audio", "report", "tries")
            out = {k: v for k, v in j.items() if k not in hidden}
        out["audio_ready"] = bool(j["audio"] and Path(j["audio"]).exists())
        return out

    def listing(self):
        return [self.public(i) for i in list(self.order)]

    def cancel(self, job_id):
        with self.lock:
            j = self.jobs.get(job_id)
            if not j:
                return None
            if j["status"] != "queued":
                return False
            self._set(j, status="cancelled", stage="отменена")
        return True

    def paths(self, job_id, kind):
        with self.lock:
            j = self.jobs.get(job_id)
        if not j or not j.get(kind):
            return None
        path = Path(j[kind])
        return path if path.exists() else None

    def status(self):
        m = self.machine
        with self.lock:
            counts = {}
            for j in self.jobs.values():
                counts[j["status"]] = counts.get(j["status"], 0) + 1
        if not m:
            return {"machine": None, "jobs": counts,
                    "note": "машина не арендована — поднимется при первом запросе"}
        hours = (time.time() - m["started"]) / 3600
        return {"machine": {"id": m["id"], "gpu": m["gpu"], "stage": m["stage"],
                            "rate_per_hour": round(m["rate"], 3),
                            "hours": round(hours, 2),
                            "spent_approx": round(hours * m["rate"], 2)},
                "jobs": counts}

    # ---------- внутреннее ----------
    def _set(self, job, **fields):
        job.update(fields, updated=time.time())
        self._save()

    def _queued_ids(self):
        with self.lock:
            return [i for i in self.order if self.jobs[i]["status"] == "queued"]

    def _worker(self):
        while not self.stopping.is_set():
            if not self._queued_ids():
                time.sleep(2)
                continue
            try:
                self._session()
            except cli.AuthError as exc:
                self._fail_queued(f"сервер не принял токен: {exc}")
            except VastError as exc:
                self._fail_queued(f"Vast: {exc}")
            except Exception as exc:  # noqa: BLE001 — сервис не должен падать из-за одной сессии
                say(f"  Сессия прервана: {type(exc).__name__}: {exc}")
                self._return_to_queue(f"{type(exc).__name__}: {exc}")
            time.sleep(3)

    def _fail_queued(self, error):
        """Ошибка, которую не исправит другая машина: задачи в брак."""
        with self.lock:
            for i in self.order:
                j = self.jobs[i]
                if j["status"] in ("queued", "running"):
                    self._set(j, status="failed", stage="ошибка", error=error)
        say(f"  Задачи отменены: {error}")

    def _return_to_queue(self, error):
        """Машина умерла — вернуть незаконченное в очередь, исчерпавшее попытки в брак."""
        with self.lock:
            for i in self.order:
                j = self.jobs[i]
                if j["status"] != "running":
                    continue
                if j["tries"] >= self.api_cfg["max_tries"]:
                    self._set(j, status="failed", stage="ошибка",
                              error=f"не удалось за {j['tries']} попыток. {error}")
                else:
                    self._set(j, status="queued", stage="в очереди (новая машина)")

    def _session(self):
        """Одна аренда: поднять машину и работать, пока есть задачи."""
        cfg = self.cfg
        if not self.repo_checked:
            cli.check_repo(cfg)          # при ошибке выходит из процесса — до аренды, бесплатно
            self.repo_checked = True

        vast = Vast(cli.find_api_key(cfg.get("vast_api_key", "")))
        token = uuid.uuid4().hex + uuid.uuid4().hex
        instance_id, rate, gpu, host = self._rent(vast, token)
        started = time.time()
        with self.rental_lock:
            self.rental = {"vast": vast, "id": instance_id, "rate": rate, "started": started}
        self.machine = {"id": instance_id, "rate": rate, "gpu": gpu,
                        "started": started, "stage": "установка"}
        try:
            ready = cli.wait_ready(vast, instance_id, token)
            if isinstance(ready, cli._SetupFailed):
                cli.save_logs(ready.api, ROOT / "output", str(instance_id))
                self._fail_queued("установка на сервере не удалась, логи в папке output")
                return
            if ready is None:
                if host:
                    self.bad_hosts.add(host)
                    cli.remember_bad_host(host, "машина не поднялась")
                self._return_to_queue("машина не поднялась")
                return
            self.machine["stage"] = "работает"
            self._pump(ready, vast, instance_id, started)
        finally:
            self.machine = None
            self._destroy()

    def _rent(self, vast, token):
        g = self.cfg["gpu"]
        offers = vast.search_offers(g["names"], g["max_price_per_hour"], g["min_reliability"],
                                    g["disk_gb"], interruptible=g["interruptible"])
        # Поиск всегда возвращает самую дешёвую первой — без отсева битый хост берётся снова
        offers = [o for o in offers if o.get("machine_id") not in self.bad_hosts]
        if not offers:
            raise VastError(f"нет свободных машин {', '.join(g['names'])} дешевле "
                            f"${g['max_price_per_hour']}/ч")
        env = {"-p 8000:8000": "1", "API_TOKEN": token, "ENGINES": "qwen,voxcpm",
               "GITHUB_REPO": self.cfg["github_repo"], "GITHUB_BRANCH": self.cfg["github_branch"],
               "GITHUB_TOKEN": self.cfg["github_token"] or "", "PYTHONUNBUFFERED": "1"}
        for offer in offers[:8]:
            price = None
            if g["interruptible"]:
                price = round(min(g["max_price_per_hour"], (offer.get("min_bid") or 0.1) * 1.25), 3)
            try:
                instance_id = vast.create_instance(offer["id"], cli.IMAGE, env, cli.ONSTART,
                                                   g["disk_gb"], cli.LABEL, price)
            except VastError as exc:
                say(f"  Предложение {offer['id']} недоступно: {exc}")
                continue
            rate = price if g["interruptible"] else offer.get("dph_total", 0)
            say(f"  Арендована машина {instance_id}: {offer.get('gpu_name')}, ${rate:.3f}/ч")
            return instance_id, rate, offer.get("gpu_name"), offer.get("machine_id")
        raise VastError("не удалось арендовать ни одну из подходящих машин")

    def _pump(self, api, vast, instance_id, started):
        """Слать задачи на машину и забирать результаты, пока есть работа."""
        out_dir = ROOT / "output"
        uploaded, remote = set(), {}      # remote_id -> [локальные id]
        idle_since, lost_since = None, None
        idle_sec = self.api_cfg["idle_minutes"] * 60
        limit_sec = self.api_cfg["max_session_hours"] * 3600

        while not self.stopping.is_set():
            try:
                self._submit_new(api, uploaded, remote)
                state = {j["id"]: j for j in api.get("/jobs").json()}
                lost_since = None
            except requests.RequestException:
                lost_since = lost_since or time.time()
                if time.time() - lost_since > 300:
                    inst = vast.instance(instance_id) or {}
                    if inst.get("actual_status") != "running" or time.time() - lost_since > 1800:
                        raise RuntimeError("связь с машиной потеряна")
                time.sleep(15)
                continue

            for remote_id, local_ids in list(remote.items()):
                rj = state.get(remote_id)
                if not rj:
                    continue
                self._absorb(api, rj, local_ids, out_dir)
                if rj["status"] in ("done", "failed"):
                    del remote[remote_id]

            if remote or self._queued_ids():
                idle_since = None
            else:
                idle_since = idle_since or time.time()
                left = idle_sec - (time.time() - idle_since)
                self.machine["stage"] = f"простой, удалю через {left / 60:.0f} мин"
                if left <= 0:
                    say(f"  Простой {self.api_cfg['idle_minutes']} мин — удаляю машину.")
                    return
            if time.time() - started > limit_sec:
                say(f"  Сессия длится дольше {self.api_cfg['max_session_hours']} ч — удаляю машину.")
                self._return_to_queue("предел длительности сессии")
                return
            time.sleep(5)

    def _submit_new(self, api, uploaded, remote):
        for job_id in self._queued_ids():
            with self.lock:
                j = self.jobs[job_id]
                if j["status"] != "queued":
                    continue
                voice, text, settings = j["voice"], j["text"], j["settings"]
                name, lang = j["name"], j["lang"]
            if voice not in uploaded:
                audio_path, txt_path = voice_files(voice)
                api.put(f"/voices/{voice}{audio_path.suffix.lower()}", audio_path.read_bytes())
                if txt_path:
                    api.put(f"/voices/{voice}.txt", txt_path.read_bytes())
                uploaded.add(voice)
            rj = api.post("/jobs", {"name": name, "lang": lang, "text": text,
                                    "voice": voice, "settings": settings})
            with self.lock:
                j = self.jobs[job_id]
                self._set(j, status="running", stage="отправлена", tries=j["tries"] + 1)
            # Сервер склеивает одинаковые тексты в одну задачу — держим список
            remote.setdefault(rj["id"], []).append(job_id)
            say(f"  → {job_id} ({name}_{lang}) отправлена на машину")

    def _absorb(self, api, rj, local_ids, out_dir):
        """Перенести состояние удалённой задачи в локальные и скачать готовое."""
        audio = report = None
        if rj["status"] == "done":
            audio = api.get(f"/jobs/{rj['id']}/audio", timeout=600).content
            report = api.get(f"/jobs/{rj['id']}/report").json()
        for job_id in local_ids:
            with self.lock:
                j = self.jobs.get(job_id)
                if not j or j["status"] in ("done", "failed", "cancelled"):
                    continue
                self._set(j, stage=rj["stage"], total=rj["total"], done=rj["done"],
                          flagged=rj["flagged"])
                name, lang = j["name"], j["lang"]
            if rj["status"] == "failed":
                with self.lock:
                    self._set(self.jobs[job_id], status="failed", stage="ошибка", error=rj["error"])
                say(f"  ✖ {job_id}: {rj['error']}")
                cli.save_logs(api, out_dir, f"{name}_{lang}")   # версии библиотек видны только там
            elif rj["status"] == "done":
                out = out_dir / f"{name}_{lang}.{self.cfg['output_format']}"
                out.write_bytes(audio)
                rep = out.with_name(out.stem + "_отчёт.txt")
                cli.write_report(report, rep)
                with self.lock:
                    self._set(self.jobs[job_id], status="done", stage="готово",
                              duration=report["duration"], audio=str(out), report=str(rep))
                say(f"  ✔ {job_id}: {out.name} ({report['duration']}), "
                    f"под вопросом: {report['flagged_count']}")

    def _destroy(self):
        """Удалить арендованную машину. Безопасно вызывать повторно и из обработчика выхода."""
        with self.rental_lock:
            r, self.rental = self.rental, None
        if not r:
            return
        hours = (time.time() - r["started"]) / 3600
        for _ in range(5):
            try:
                r["vast"].destroy(r["id"])
                say(f"  Машина {r['id']} удалена. {hours:.2f} ч, примерно ${hours * r['rate']:.2f}")
                return
            except Exception as exc:  # noqa: BLE001
                say(f"  Не удалось удалить машину ({exc}), повтор…")
                time.sleep(5)
        say(f"  !!! Машину {r['id']} удалить не удалось. Запустите 5_cleanup.bat")

    def shutdown(self):
        """Остановка сервиса: машину надо снять с аренды, иначе она тратит деньги дальше.

        Рабочий поток — демон, при выходе процесса его finally может не успеть выполниться,
        поэтому удаление вызывается ещё и отсюда: из atexit и после остановки uvicorn.
        """
        self.stopping.set()
        with self.rental_lock:
            pending = self.rental is not None
        if pending:
            say("")
            say("Остановка сервиса: снимаю машину с аренды…")
        self._destroy()


def voice_files(name):
    """Файл образца голоса и его расшифровка (если есть)."""
    voices = ROOT / "voices"
    for ext in cli.VOICE_EXT:
        p = voices / f"{name}{ext}"
        if p.exists():
            txt = voices / f"{name}.txt"
            return p, (txt if txt.exists() else None)
    raise HTTPException(400, f"нет образца голоса voices/{name}.wav (или .mp3)")


# ---------------------------------------------------------------- HTTP

class TtsIn(BaseModel):
    text: str
    lang: str
    name: str | None = None
    voice: str | None = None
    settings: dict = {}


def build_app(service, api_cfg):
    app = FastAPI(title="tts-vast: локальный API")
    token = api_cfg["token"]

    def auth(authorization: str = Header(default="")):
        if token and authorization != f"Bearer {token}":
            raise HTTPException(401, "неверный токен")

    guard = [Depends(auth)]

    @app.post("/tts", dependencies=guard, status_code=202)
    def create(req: TtsIn):
        lang = req.lang.lower()
        try:
            resolve_engine(lang, service.cfg["engines"])
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not req.text.strip():
            raise HTTPException(400, "пустой текст")
        errors, warnings = textproc.preflight(req.text, lang)
        if errors:
            raise HTTPException(400, {"error": "текст не прошёл проверку", "problems": errors[:15]})
        try:
            settings = validate_settings({**service.cfg["check"], **(req.settings or {})})
        except ValueError as exc:
            raise HTTPException(400, f"настройки: {exc}") from exc
        voice = req.voice or service.cfg["voice"]
        voice_files(voice)               # проверить, что образец есть, до постановки в очередь
        name = req.name or f"api_{uuid.uuid4().hex[:8]}"
        job = service.add(name, lang, req.text, voice, settings)
        job["warnings"] = warnings[:10]
        return job

    @app.get("/tts", dependencies=guard)
    def listing():
        return service.listing()

    @app.get("/tts/{job_id}", dependencies=guard)
    def one(job_id: str):
        job = service.public(job_id)
        if not job:
            raise HTTPException(404, "задача не найдена")
        return job

    @app.get("/tts/{job_id}/audio", dependencies=guard)
    def audio(job_id: str):
        path = service.paths(job_id, "audio")
        if not path:
            raise HTTPException(404, "аудио ещё не готово")
        return FileResponse(path, filename=path.name)

    @app.get("/tts/{job_id}/report", dependencies=guard)
    def report(job_id: str):
        path = service.paths(job_id, "report")
        if not path:
            raise HTTPException(404, "отчёт ещё не готов")
        return FileResponse(path, filename=path.name)

    @app.delete("/tts/{job_id}", dependencies=guard)
    def cancel(job_id: str):
        res = service.cancel(job_id)
        if res is None:
            raise HTTPException(404, "задача не найдена")
        if res is False:
            raise HTTPException(409, "задача уже выполняется — отменить нельзя")
        return {"ok": True}

    @app.get("/status", dependencies=guard)
    def status():
        return service.status()

    @app.exception_handler(Exception)
    def unhandled(_request, exc):
        return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})

    return app


def load_api_cfg(cfg):
    api_cfg = {**API_DEFAULTS, **(cfg.get("api") or {})}
    if api_cfg["host"] not in ("127.0.0.1", "localhost") and not api_cfg["token"]:
        cli.fail("api.host открыт наружу, но api.token пустой — любой в сети сможет тратить "
                 "ваши деньги на аренду. Задайте api.token в config.yaml.")
    for key in ("idle_minutes", "max_session_hours", "max_tries"):
        try:
            api_cfg[key] = float(api_cfg[key]) if key != "max_tries" else int(api_cfg[key])
        except (TypeError, ValueError):
            cli.fail(f"api.{key}: нужно число, а не «{api_cfg[key]}»")
        if api_cfg[key] <= 0:
            cli.fail(f"api.{key}: должно быть больше нуля")
    return api_cfg


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(description="Локальный API озвучки поверх аренды Vast")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    args = p.parse_args()

    cfg = cli.load_config(ROOT / args.config)
    api_cfg = load_api_cfg(cfg)
    if args.host:
        api_cfg["host"] = args.host
    if args.port:
        api_cfg["port"] = args.port

    # Если с прошлого раза осталась машина — сказать сразу, это деньги
    try:
        vast = Vast(cli.find_api_key(cfg.get("vast_api_key", "")))
        alive = [i for i in vast.list_instances() if (i.get("label") or "").strip() == cli.LABEL]
        if alive:
            say(f"ВНИМАНИЕ: уже арендовано машин проекта: {len(alive)} "
                f"({', '.join(str(i['id']) for i in alive)}). Запустите 5_cleanup.bat, если они лишние.")
    except (VastError, requests.RequestException) as exc:
        say(f"(не удалось проверить старые машины: {exc})")

    service = Service(cfg, api_cfg)
    app = build_app(service, api_cfg)
    atexit.register(service.shutdown)   # страховка на случай выхода мимо uvicorn
    say(f"\nAPI озвучки: http://{api_cfg['host']}:{api_cfg['port']}")
    say(f"  токен: {'задан' if api_cfg['token'] else 'не нужен (только этот компьютер)'}")
    say(f"  машина удаляется после {api_cfg['idle_minutes']:.0f} мин простоя")
    if service.bad_hosts:
        say(f"  пропускаю {len(service.bad_hosts)} хостов, подводивших за последнюю неделю")
    say("  документация: /docs\n")
    try:
        uvicorn.run(app, host=api_cfg["host"], port=int(api_cfg["port"]), log_level="warning")
    finally:
        service.shutdown()              # Ctrl+C: uvicorn гасится, машину снимаем здесь


if __name__ == "__main__":
    main()
