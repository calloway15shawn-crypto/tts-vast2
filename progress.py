"""Прогресс озвучки: что в очереди, что делает машина, сколько потрачено.

    py progress.py           — показать один раз
    py progress.py --watch   — обновлять, пока пачка не доозвучится

Полезно при пакетной работе: пять-десять сценариев идут на одной машине часами,
и надо видеть, где очередь сейчас и не простаивает ли аренда впустую.
"""
import argparse
import sys
import time
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent

ЗНАЧКИ = {"queued": "…", "running": "▶", "done": "✔", "failed": "✖", "cancelled": "—"}


def say(msg=""):
    print(msg, flush=True)


def настройки():
    cfg = {}
    path = ROOT / "config.yaml"
    if path.exists():
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    api = {"host": "127.0.0.1", "port": 8800, "token": "", **(cfg.get("api") or {})}
    host = api["host"] if api["host"] != "0.0.0.0" else "127.0.0.1"
    return f"http://{host}:{api['port']}", api.get("token") or ""


def показать(base, token):
    """Одна страница состояния. Возвращает True, когда работы не осталось."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        jobs = requests.get(f"{base}/tts", headers=headers, timeout=15).json()
        state = requests.get(f"{base}/status", headers=headers, timeout=15).json()
    except requests.RequestException as exc:
        say(f"сервис не отвечает ({type(exc).__name__}) — запущен ли 6_api.bat?")
        return False

    m = state.get("machine")
    if m:
        say(f"Машина {m['id']}: {m['gpu']}, {m['stage']}")
        say(f"  работает {m['hours'] * 60:.0f} мин, ${m['rate_per_hour']}/ч, "
            f"потрачено ~${m['spent_approx']}")
    else:
        say(f"Машина: {state.get('note', 'нет')}")

    if not jobs:
        say("\nОчередь пуста.")
        return True

    say("")
    say(f"  {'':2} {'задача':<14}{'имя':<20}{'фрагменты':>11}  {'стадия'}")
    осталось = 0
    под_вопросом = 0
    for j in jobs:
        статус = j.get("status", "?")
        if статус in ("queued", "running"):
            осталось += 1
        под_вопросом += int(j.get("flagged") or 0)
        всего = j.get("total") or 0
        готово = j.get("done") or 0
        доля = f"{готово}/{всего}" if всего else "—"
        say(f"  {ЗНАЧКИ.get(статус, '?'):2} {j['id']:<14}{str(j.get('name'))[:18]:<20}"
            f"{доля:>11}  {str(j.get('stage'))[:40]}")
        if статус == "failed" and j.get("error"):
            say(f"     ошибка: {str(j['error'])[:90]}")

    сделано = sum(1 for j in jobs if j.get("status") == "done")
    брак = sum(1 for j in jobs if j.get("status") == "failed")
    say("")
    say(f"  готово {сделано}, в работе {осталось}, с ошибкой {брак}, "
        f"фрагментов под вопросом {под_вопросом}")
    return осталось == 0


def main():
    for поток in (sys.stdout, sys.stderr):
        try:
            поток.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(description="Прогресс пакетной озвучки")
    p.add_argument("--watch", action="store_true", help="обновлять до конца работы")
    p.add_argument("--every", type=int, default=30, help="период обновления, секунд")
    args = p.parse_args()

    base, token = настройки()
    while True:
        say("=" * 74)
        say(time.strftime("%H:%M:%S"))
        всё = показать(base, token)
        if not args.watch or всё:
            if всё and args.watch:
                say("\nВся очередь обработана.")
            return
        time.sleep(max(5, args.every))


if __name__ == "__main__":
    main()
