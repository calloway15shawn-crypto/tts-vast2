"""Показать и удалить все машины Vast, созданные этим проектом (метка tts-vast).

    py cleanup.py          — показать и спросить подтверждение
    py cleanup.py --all    — удалить ВСЕ ваши машины на Vast, не только созданные проектом
"""
import argparse
import sys
from pathlib import Path

import yaml

from vast_api import Vast, VastError, find_api_key

ROOT = Path(__file__).resolve().parent


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser()
    p.add_argument("--all", action="store_true", help="удалить все машины аккаунта")
    p.add_argument("--yes", "-y", action="store_true")
    args = p.parse_args()

    cfg_path = ROOT / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    try:
        vast = Vast(find_api_key((cfg or {}).get("vast_api_key", "")))
        instances = vast.list_instances()
    except VastError as exc:
        print(f"ОШИБКА: {exc}")
        sys.exit(1)

    targets = [i for i in instances if args.all or (i.get("label") or "").strip() == "tts-vast"]
    if not targets:
        print("Машин проекта нет — ничего не списывается." if not args.all else "У вас нет машин на Vast.")
        return
    for i in targets:
        print(f"  {i['id']}: {i.get('gpu_name')} статус={i.get('actual_status')} "
              f"${(i.get('dph_total') or 0):.3f}/ч метка={i.get('label')}")
    if not args.yes and input(f"Удалить {len(targets)} шт.? [y/N]: ").strip().lower() not in ("y", "д", "yes", "да"):
        return
    for i in targets:
        try:
            vast.destroy(i["id"])
            print(f"  удалена {i['id']}")
        except VastError as exc:
            print(f"  не удалось удалить {i['id']}: {exc}")


if __name__ == "__main__":
    main()
