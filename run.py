"""Озвучка сценариев на арендованной видеокарте Vast.ai.

    py run.py              — озвучить всё из папки scripts
    py run.py --check      — только проверить сценарии и посчитать стоимость, без аренды
    py run.py --test       — пробный запуск на дешёвой машине без моделей (проверка связки)
"""
import argparse
import json
import re
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests
import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "server"))

import textproc  # noqa: E402
from langs import LANGS, resolve_engine  # noqa: E402
from settings import validate as validate_settings  # noqa: E402
from vast_api import Vast, VastError, api_endpoint, find_api_key  # noqa: E402

IMAGE = "pytorch/pytorch:2.8.0-cuda12.8-cudnn9-runtime"
LABEL = "tts-vast"
FILE_RE = re.compile(r"^(?P<name>.+)_(?P<lang>[a-z]{2})$", re.IGNORECASE)
VOICE_EXT = (".wav", ".mp3", ".flac", ".m4a", ".ogg")
# Скорость озвучки относительно реального времени (с запасом) — только для оценки стоимости
SPEED = {"qwen": 3.0, "voxcpm": 1.2}
CHARS_PER_SEC = 14.0

ONSTART = (
    "bash -c 'mkdir -p /workspace && cd /workspace && "
    "(command -v git >/dev/null || (apt-get update -qq && apt-get install -y -qq git)) && "
    "if [ ! -d app ]; then "
    "if [ -n \"$GITHUB_TOKEN\" ]; then "
    "git clone --depth 1 -b \"$GITHUB_BRANCH\" \"https://x-access-token:$GITHUB_TOKEN@github.com/$GITHUB_REPO.git\" app; "
    "else git clone --depth 1 -b \"$GITHUB_BRANCH\" \"https://github.com/$GITHUB_REPO.git\" app; fi; fi && "
    "bash app/server/onstart.sh' > /workspace/onstart.log 2>&1"
)


def say(msg=""):
    print(msg, flush=True)


def fail(msg):
    say(f"\nОШИБКА: {msg}")
    sys.exit(1)


# ---------------------------------------------------------------- настройки и сценарии

def load_config(path):
    if not path.exists():
        fail(f"нет файла {path.name}. Скопируйте config.example.yaml в config.yaml и заполните его.")
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    repo = str(cfg.get("github_repo") or "").strip().removeprefix("https://github.com/").strip("/")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo) or repo == "ВАШ_ЛОГИН/tts-vast":
        fail("в config.yaml не указан github_repo в виде ЛОГИН/РЕПОЗИТОРИЙ")
    cfg["github_repo"] = repo
    cfg.setdefault("github_branch", "main")
    cfg.setdefault("github_token", "")
    cfg.setdefault("voice", "narrator")
    cfg.setdefault("engines", {})
    cfg.setdefault("output_format", "mp3")
    cfg["gpu"] = {"names": ["RTX 4090"], "max_price_per_hour": 0.6, "min_reliability": 0.95,
                  "interruptible": False, "disk_gb": 60, **(cfg.get("gpu") or {})}
    try:
        cfg["check"] = validate_settings(cfg.get("check"), format=cfg["output_format"],
                                         engines=cfg["engines"])
    except ValueError as exc:
        fail(f"в config.yaml: {exc}")
    cfg["output_format"] = cfg["check"]["format"]
    return cfg


def read_text_file(path):
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1251"):   # cp1251 — старый «Блокнот» Windows
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    fail(f"{path.name}: не удалось прочитать кодировку, сохраните файл в UTF-8")


def collect_scripts(folder, cfg, out_dir, overwrite):
    if not folder.is_dir():
        fail(f"папка со сценариями не найдена: {folder}")
    items, skipped, has_errors = [], [], False
    for path in sorted(folder.glob("*.txt")):
        m = FILE_RE.match(path.stem)
        if not m:
            say(f"  пропуск {path.name}: имя должно заканчиваться на _язык, например tema01_ru.txt")
            continue
        name, lang = m.group("name"), m.group("lang").lower()
        try:
            engine = resolve_engine(lang, cfg["engines"])
        except ValueError as exc:
            say(f"  пропуск {path.name}: {exc}")
            continue
        out_file = out_dir / f"{name}_{lang}.{cfg['output_format']}"
        if out_file.exists() and not overwrite:
            skipped.append(path.name)
            continue
        text = read_text_file(path)
        errors, warnings = textproc.preflight(text, lang)
        if errors or warnings:
            say(f"\n  {path.name}:")
            for e in errors[:15]:
                say(f"    ✖ {e}")
            for w in warnings[:10]:
                say(f"    ! {w}")
            extra = len(errors) - 15 + max(0, len(warnings) - 10)
            if extra > 0:
                say(f"    … и ещё {extra}")
        has_errors |= bool(errors)
        items.append({"file": path.name, "name": name, "lang": lang, "engine": engine,
                      "text": text, "chars": len(textproc.clean_text(text)), "out": out_file})
    if skipped:
        say(f"\n  Уже озвучены, пропускаю ({len(skipped)}): {', '.join(skipped)}  (заново: --overwrite)")
    return items, has_errors


def find_voice(cfg):
    voices = ROOT / "voices"
    for ext in VOICE_EXT:
        p = voices / f"{cfg['voice']}{ext}"
        if p.exists():
            return p, (voices / f"{cfg['voice']}.txt" if (voices / f"{cfg['voice']}.txt").exists() else None)
    fail(f"нет образца голоса voices/{cfg['voice']}.wav (или .mp3). Положите 10–20 секунд чистой речи.")


def estimate(items, cfg):
    say("\n  Файл                              Язык  Символов   Аудио")
    audio_min = gpu_min = 0.0
    for it in items:
        minutes = it["chars"] / CHARS_PER_SEC / 60
        audio_min += minutes
        gpu_min += minutes / SPEED[it["engine"]] * 1.3   # 1.3 — проверка и перегенерации
        say(f"  {it['file'][:32]:<34}{it['lang']:<6}{it['chars']:>8}  ~{minutes:>5.1f} мин")
    setup_min = 25
    hours = (gpu_min + setup_min) / 60
    price = cfg["gpu"]["max_price_per_hour"]
    say(f"\n  Всего аудио: ~{audio_min:.0f} мин. Работа машины: ~{hours:.1f} ч (включая ~{setup_min} мин установки).")
    say(f"  Стоимость: не больше ~${hours * price:.2f} при лимите ${price}/ч (обычно дешевле).")


# ---------------------------------------------------------------- память о плохих хостах

BAD_HOSTS_FILE = ROOT / ".bad_hosts.json"
BAD_HOST_TTL_DAYS = 7      # через неделю хост снова считается годным: у Vast всё чинится


def load_bad_hosts():
    """Хосты, подводившие в прошлые запуски. Память переживает перезапуск программы."""
    try:
        data = json.loads(BAD_HOSTS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    fresh, now = set(), time.time()
    for host, rec in (data if isinstance(data, dict) else {}).items():
        if now - ((rec or {}).get("when") or 0) < BAD_HOST_TTL_DAYS * 86400:
            try:
                fresh.add(int(host))
            except (TypeError, ValueError):
                continue
    return fresh


def remember_bad_host(host, reason=""):
    """Записать хост на диск, чтобы он не выбирался и после перезапуска."""
    if not host:
        return
    try:
        data = json.loads(BAD_HOSTS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data[str(host)] = {"when": time.time(), "reason": (reason or "")[:200]}
    try:
        tmp = BAD_HOSTS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(BAD_HOSTS_FILE)
    except OSError as exc:
        say(f"  (не удалось запомнить хост {host}: {exc})")


# ---------------------------------------------------------------- проверка репозитория

GITHUB_API = "https://api.github.com"
SERVER_EXT = (".py", ".sh")   # что машина скачивает с GitHub и запускает


def _branch_names(r):
    """Список веток репозитория или None, если GitHub ответил не списком."""
    try:
        data = r.json() if r.status_code == 200 else None
    except ValueError:
        return None
    return [b.get("name") for b in data] if isinstance(data, list) else None


def check_repo(cfg):
    """Убедиться, что машина сможет скачать код: репозиторий, ветка и файлы server/ на месте.

    Останавливает работу только при ошибке, которая точно сорвёт запуск: пустой репозиторий,
    нет ветки, не хватает файлов. Если GitHub недоступен — предупреждение, работа продолжается.
    """
    repo, branch = cfg["github_repo"], cfg["github_branch"]
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if cfg["github_token"]:
        headers["Authorization"] = f"Bearer {cfg['github_token']}"

    def api(path):
        return requests.get(f"{GITHUB_API}/repos/{repo}{path}", headers=headers, timeout=20)

    def skip(reason):
        say(f"  ! {reason} — пропускаю проверку, код проверится уже на машине")

    try:
        r = api("")
        if r.status_code == 404:
            fail(f"репозиторий github.com/{repo} не найден. Проверьте github_repo в config.yaml"
                 + (" и права токена github_token" if cfg["github_token"]
                    else ". Если репозиторий приватный, заполните github_token"))
        if r.status_code in (401, 403):
            if r.headers.get("X-RateLimit-Remaining") == "0":
                return skip("GitHub временно ограничил число запросов")
            fail(f"GitHub отклонил github_token (HTTP {r.status_code}). Нужен fine-grained токен "
                 "с доступом Contents: Read-only к этому репозиторию.")
        if r.status_code >= 400:
            return skip(f"GitHub ответил {r.status_code}")

        r = api(f"/contents/server?ref={quote(branch, safe='')}")
        if r.status_code == 404:
            names = _branch_names(api("/branches"))
            if names is not None and not names:
                fail(f"репозиторий github.com/{repo} пустой — файлы проекта не загружены. "
                     "Откройте его на GitHub, нажмите Add file → Upload files и перетащите "
                     "СОДЕРЖИМОЕ папки проекта (run.py, vast_api.py, папку server), кроме config.yaml.")
            if names and branch not in names:
                fail(f"в репозитории {repo} нет ветки «{branch}» (есть: {', '.join(names[:5])}). "
                     "Поправьте github_branch в config.yaml.")
            fail(f"в репозитории {repo}, ветка {branch}: нет папки server. "
                 "Загружать нужно СОДЕРЖИМОЕ папки проекта, а не саму папку.")
        if r.status_code >= 400:
            return skip(f"GitHub ответил {r.status_code} на чтение server/")
        listing = r.json()
        if not isinstance(listing, list):
            return skip("server в репозитории — не папка")
    except requests.RequestException as exc:
        return skip(f"GitHub не отвечает ({type(exc).__name__})")

    remote = {x.get("name") for x in listing}
    local = {p.name for p in (ROOT / "server").iterdir() if p.suffix in SERVER_EXT}
    missing = sorted(local - remote)
    if missing:
        fail(f"в репозитории {repo} не хватает файлов server/: {', '.join(missing)}. "
             "Загрузите папку server заново — без них сервер на машине не запустится.")
    say(f"  GitHub: {repo}, ветка {branch} — код на месте ({len(local)} файлов в server/)")


# ---------------------------------------------------------------- работа с сервером

class AuthError(RuntimeError):
    """Сервер не принял токен. Ждать бессмысленно: само это не исправится."""


class Api:
    def __init__(self, base, token):
        self.base = base
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"

    def _check_auth(self, r):
        """Отделить «токен не подошёл» от «сервер ещё не поднялся»."""
        if r.status_code in (401, 403):
            raise AuthError(
                f"сервер по адресу {self.base} не принял токен доступа (HTTP {r.status_code}). "
                "Похоже, на этом порту отвечает не наш сервер. Запустите снова — "
                "будет арендована другая машина."
            )
        return r

    def get(self, path, **kw):
        r = self._check_auth(self.s.get(self.base + path, timeout=kw.pop("timeout", 20), **kw))
        r.raise_for_status()
        return r

    def put(self, path, data):
        r = self._check_auth(self.s.put(self.base + path, data=data, timeout=300))
        if r.status_code >= 400:
            raise RuntimeError(r.json().get("detail", r.text))
        return r.json()

    def post(self, path, payload):
        r = self._check_auth(self.s.post(self.base + path, json=payload, timeout=120))
        if r.status_code >= 400:
            raise RuntimeError(r.json().get("detail", r.text))
        return r.json()


def save_logs(api, out_dir, tag):
    try:
        text = api.get("/logs", timeout=60).text
        path = out_dir / f"логи_сервера_{tag}.txt"
        path.write_text(text, encoding="utf-8")
        say(f"  Логи сервера сохранены: {path}")
    except Exception as exc:  # noqa: BLE001
        say(f"  Не удалось скачать логи: {exc}")


def write_report(report, path):
    lines = [f"Озвучка: {report['name']}_{report['lang']} (движок {report['engine']})",
             f"Длительность: {report['duration']}, фрагментов: {report['fragments']}",
             f"Проверить вручную: {report['flagged_count']}", ""]
    if not report["flagged"]:
        lines.append("Все фрагменты прошли автоматическую проверку.")
    for n, f in enumerate(report["flagged"], 1):
        lines += [f"{n}) {f['time']} — фрагмент {f['fragment']}: {f['reason']}",
                  f"   Текст:    {f['text']}", f"   Услышано: {f['heard']}", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


# Сообщения Vast, означающие, что машина не запустится: беда хоста, ждать бессмысленно
DEAD_MSG = (
    "error response from daemon",        # docker не смог: образ, сеть, демон
    "failed to resolve reference",       # хост не достучался до docker.io
    "oci runtime create failed",         # сломан рантайм
    "failed to create task for container",
    "failed to inject cdi devices",      # драйвер nvidia на хосте
    "no space left on device",
)


def wait_ready(vast, instance_id, token, max_setup_min=50, max_loading_min=20, max_silent_min=12):
    """Дождаться, пока машина запустится и сервер установит модели. Возвращает Api или None.

    max_setup_min  — общий предел: сервер отвечает, но долго ставит модели.
    max_silent_min — предел молчания: сервер не отвечает совсем. Установка моделей идёт уже
    после запуска API, поэтому молчание дольше нескольких минут означает, что код не скачался
    или onstart.sh упал, — ждать полный max_setup_min бессмысленно и дорого.
    """
    start, last_msg, api = time.time(), "", None
    silent_since, ever_answered, dead_polls = None, False, 0
    while True:
        elapsed = (time.time() - start) / 60
        try:
            inst = vast.instance(instance_id) or {}
        except (VastError, requests.RequestException) as exc:
            say(f"  (Vast не ответил: {exc}, повторяю)")
            time.sleep(15)
            continue
        status = inst.get("actual_status") or "created"
        if status in ("exited", "offline") or inst.get("intended_status") == "stopped":
            say(f"  Машина остановилась ({status}): {inst.get('status_msg') or ''}")
            return None
        if status != "running":
            silent_since = None
            status_msg = (inst.get("status_msg") or "").strip()
            if any(p in status_msg.lower() for p in DEAD_MSG):
                dead_polls += 1
                if dead_polls >= 3:      # три опроса подряд, около минуты
                    say(f"  Машина не смогла запустить образ: {status_msg[:160]}")
                    say("  Это отказ хоста, а не ваша настройка — беру другую машину.")
                    return None
            else:
                dead_polls = 0
            msg = f"  [{elapsed:4.0f} мин] машина: {status} {status_msg[:80]}"
            if elapsed > max_loading_min:
                say(f"  Машина не запустилась за {max_loading_min} мин — беру другую.")
                return None
        else:
            endpoint = api_endpoint(inst)
            if not endpoint:
                msg = f"  [{elapsed:4.0f} мин] машина запущена, жду проброс порта"
            else:
                api = api or Api(endpoint, token)
                try:
                    h = api.get("/health").json()
                except requests.RequestException:
                    silent_since = silent_since or time.time()
                    silent = (time.time() - silent_since) / 60
                    if silent > max_silent_min:
                        if ever_answered:
                            say(f"  Сервер молчит больше {max_silent_min} мин — беру другую машину.")
                        else:
                            say(f"  Сервер не ответил ни разу за {max_silent_min} мин. Обычно это значит, "
                                "что код с GitHub не скачался или onstart.sh упал на установке пакетов. "
                                "Логи остались на машине в /workspace/onstart.log, но API не поднялся, "
                                "поэтому скачать их нельзя: чтобы разобраться по SSH, запустите "
                                "с ключом --keep (машина не будет удалена).")
                        return None
                    msg = f"  [{elapsed:4.0f} мин] запуск сервера, молчит {silent:.0f} из {max_silent_min} мин"
                else:
                    silent_since, ever_answered = None, True
                    if h.get("error"):
                        say(f"  Ошибка установки на сервере:\n{h['error'][-1200:]}")
                        return _SetupFailed(api)
                    if h.get("ready"):
                        say(f"  [{elapsed:4.0f} мин] сервер готов")
                        return api
                    msg = f"  [{elapsed:4.0f} мин] сервер: {h.get('stage')}"
        if msg != last_msg:
            say(msg)
            last_msg = msg
        if elapsed > max_setup_min:
            say(f"  Установка идёт дольше {max_setup_min} мин — прерываю.")
            return _SetupFailed(api) if api else None
        time.sleep(15)


class _SetupFailed:
    """Сервер ответил, но установка не удалась: логи ещё можно скачать."""

    def __init__(self, api):
        self.api = api


def run_session(vast, cfg, items, voice, voice_txt, args, out_dir, failed, skip_hosts):
    """Одна аренда: запустить машину, озвучить что успеем. Возвращает список неозвученных.

    skip_hosts — хосты, уже подводившие в этом запуске. Без них самая дешёвая, но битая
    машина выбиралась бы снова и снова: поиск всегда возвращает её первой.
    """
    g = cfg["gpu"]
    offers = vast.search_offers(g["names"], g["max_price_per_hour"], g["min_reliability"], g["disk_gb"],
                                interruptible=g["interruptible"])
    if skip_hosts:
        before = len(offers)
        offers = [o for o in offers if o.get("machine_id") not in skip_hosts]
        if before != len(offers):
            say(f"  Пропускаю {before - len(offers)} машин на хостах, уже подводивших в этом запуске.")
    if not offers:
        fail(f"нет свободных машин {', '.join(g['names'])} дешевле ${g['max_price_per_hour']}/ч "
             "с надёжностью от {:.2f}. Поднимите max_price_per_hour или добавьте модели GPU в config.yaml."
             .format(g["min_reliability"]))

    token = secrets.token_urlsafe(32)
    engines = sorted({it["engine"] for it in items})
    env = {"-p 8000:8000": "1", "API_TOKEN": token, "ENGINES": ",".join(engines),
           "GITHUB_REPO": cfg["github_repo"], "GITHUB_BRANCH": cfg["github_branch"],
           "GITHUB_TOKEN": cfg["github_token"] or "", "PYTHONUNBUFFERED": "1"}
    if args.test:
        env["FAKE_ENGINES"] = "1"

    instance_id, offer = None, None
    for offer in offers[:8]:
        price = None
        if g["interruptible"]:
            price = round(min(g["max_price_per_hour"], (offer.get("min_bid") or 0.1) * 1.25), 3)
        try:
            instance_id = vast.create_instance(offer["id"], IMAGE, env, ONSTART, g["disk_gb"], LABEL, price)
            break
        except VastError as exc:
            say(f"  Предложение {offer['id']} недоступно: {exc}")
    if instance_id is None:
        fail("не удалось арендовать ни одну из подходящих машин, попробуйте позже")

    rate = price if g["interruptible"] else offer.get("dph_total", 0)
    say(f"\n  Арендована машина {instance_id}: {offer.get('gpu_name')}, "
        f"{(offer.get('geolocation') or '').strip()}, ${rate:.3f}/ч")
    started = time.time()
    remaining = list(items)
    try:
        ready = wait_ready(vast, instance_id, token)
        if isinstance(ready, _SetupFailed):
            save_logs(ready.api, out_dir, instance_id)
            fail("установка на сервере не удалась. Пришлите файл с логами — по нему видно, что исправить.")
        if ready is None:
            host = offer.get("machine_id")
            if host:
                skip_hosts.add(host)
                remember_bad_host(host, "машина не поднялась")
            return remaining, False
        api = ready

        say("\n  Загружаю образец голоса")
        api.put(f"/voices/{cfg['voice']}{voice.suffix.lower()}", voice.read_bytes())
        if voice_txt:
            api.put(f"/voices/{cfg['voice']}.txt", voice_txt.read_bytes())

        settings = cfg["check"]   # уже проверены и дополнены в load_config
        jobs = {}
        for it in items:
            job = api.post("/jobs", {"name": it["name"], "lang": it["lang"], "text": it["text"],
                                     "voice": cfg["voice"], "settings": settings})
            jobs[job["id"]] = it
        say(f"  Отправлено сценариев: {len(jobs)}\n")

        last_line, lost_since = {}, None
        while jobs:
            try:
                state = {j["id"]: j for j in api.get("/jobs").json()}
                lost_since = None
            except requests.RequestException:
                lost_since = lost_since or time.time()
                if time.time() - lost_since > 300:
                    inst = vast.instance(instance_id) or {}
                    if inst.get("actual_status") != "running" or time.time() - lost_since > 1800:
                        say("  Связь с машиной потеряна (возможно, прерываемый инстанс отключён).")
                        return remaining, False
                time.sleep(20)
                continue
            for job_id, it in list(jobs.items()):
                j = state.get(job_id)
                if not j:
                    continue
                line = f"  {it['file']}: {j['stage']} ({j['done']}/{j['total']}, под вопросом: {j['flagged']})"
                if last_line.get(job_id) != line:
                    say(line)
                    last_line[job_id] = line
                if j["status"] == "done":
                    it["out"].write_bytes(api.get(f"/jobs/{job_id}/audio", timeout=600).content)
                    report = api.get(f"/jobs/{job_id}/report").json()
                    report_path = it["out"].with_name(it["out"].stem + "_отчёт.txt")
                    write_report(report, report_path)
                    say(f"  ✔ Готово: {it['out'].name} ({report['duration']}), "
                        f"проверить вручную: {report['flagged_count']} → {report_path.name}")
                    remaining.remove(it)
                    del jobs[job_id]
                elif j["status"] == "failed":
                    say(f"  ✖ {it['file']}: ошибка — {j['error']}")
                    save_logs(api, out_dir, f"{it['name']}_{it['lang']}")
                    failed.append(it)
                    remaining.remove(it)
                    del jobs[job_id]
            time.sleep(10)
        return remaining, True
    finally:
        hours = (time.time() - started) / 3600
        if args.keep:
            say(f"\n  Машина {instance_id} ОСТАВЛЕНА включённой (--keep). Не забудьте: py cleanup.py")
        else:
            for attempt in range(5):
                try:
                    vast.destroy(instance_id)
                    say(f"\n  Машина {instance_id} удалена. Время аренды {hours:.2f} ч, примерно ${hours * rate:.2f}")
                    break
                except Exception as exc:  # noqa: BLE001
                    say(f"  Не удалось удалить машину ({exc}), повтор…")
                    time.sleep(5)
            else:
                say(f"\n  !!! Машину {instance_id} удалить не удалось. Запустите py cleanup.py или удалите в консоли Vast.")


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(description="Озвучка сценариев на Vast.ai")
    p.add_argument("folder", nargs="?", default="scripts", help="папка со сценариями (по умолчанию scripts)")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--check", action="store_true", help="только проверить сценарии и оценить стоимость")
    p.add_argument("--test", action="store_true", help="пробный запуск без моделей (тон вместо речи)")
    p.add_argument("--yes", "-y", action="store_true", help="не спрашивать подтверждение")
    p.add_argument("--keep", action="store_true", help="не удалять машину после работы")
    p.add_argument("--overwrite", action="store_true", help="озвучить заново уже готовые файлы")
    args = p.parse_args()

    cfg = load_config(ROOT / args.config)
    out_dir = ROOT / "output"
    out_dir.mkdir(exist_ok=True)
    folder = Path(args.folder)
    folder = folder if folder.is_absolute() else ROOT / folder

    say("Проверка сценариев…")
    items, has_errors = collect_scripts(folder, cfg, out_dir, args.overwrite)
    if not items:
        say("\nНечего озвучивать.")
        return
    voice, voice_txt = find_voice(cfg)
    estimate(items, cfg)
    if has_errors:
        fail("исправьте ошибки (✖) в сценариях и запустите снова. Предупреждения (!) не мешают запуску.")
    say()
    say("Проверка репозитория на GitHub…")
    check_repo(cfg)
    if args.check:
        say("\nПроверка пройдена. Для запуска: py run.py")
        return
    if args.test:
        say("\n  ТЕСТОВЫЙ РЕЖИМ: модели не ставятся, вместо речи будет тон. Проверяется связка Vast + GitHub + API.")
    if not args.yes and input("\nАрендовать машину и запустить? [y/N]: ").strip().lower() not in ("y", "д", "yes", "да"):
        say("Отменено.")
        return

    vast = Vast(find_api_key(cfg.get("vast_api_key", "")))
    remaining, failed, skip_hosts = items, [], load_bad_hosts()
    if skip_hosts:
        say()
        say(f"Пропущу {len(skip_hosts)} хостов, подводивших за последние {BAD_HOST_TTL_DAYS} дней.")
    try:
        for session in range(1, 4):
            if session > 1:
                say(f"\n=== Попытка {session}: беру другую машину для {len(remaining)} сценариев ===")
            remaining, _finished = run_session(vast, cfg, remaining, voice, voice_txt, args,
                                               out_dir, failed, skip_hosts)
            if not remaining:
                break
    except KeyboardInterrupt:
        say("\nОстановлено пользователем.")
    except VastError as exc:
        fail(str(exc))
    except AuthError as exc:
        fail(str(exc))
    except requests.RequestException as exc:
        fail(f"нет связи с Vast.ai ({type(exc).__name__}). Проверьте интернет и попробуйте снова.")

    remaining = [it for it in remaining if not it["out"].exists() and it not in failed]
    if failed:
        say(f"\nС ошибкой: {', '.join(it['file'] for it in failed)} — логи в папке output.")
    if remaining:
        say(f"\nНе озвучено: {', '.join(it['file'] for it in remaining)}. Запустите снова — готовые файлы пропускаются.")
    elif not failed:
        say(f"\nВсё готово. Результаты в папке {out_dir}")


if __name__ == "__main__":
    main()
