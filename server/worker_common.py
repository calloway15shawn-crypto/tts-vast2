"""Протокол рабочих процессов: запрос и ответ — по одной JSON-строке.

Весь посторонний вывод библиотек уходит в stderr (в лог), stdout занят только протоколом.
"""
import json
import os
import sys
import traceback


def serve(handlers):
    proto = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
    os.dup2(2, 1)            # всё, что библиотеки печатают в stdout, — в stderr
    sys.stdout = sys.stderr
    proto.write(json.dumps({"id": None, "ok": True, "hello": True}) + "\n")
    proto.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        try:
            handler = handlers[req["op"]]
            result = handler(req) or {}
            reply = {"id": req.get("id"), "ok": True, **result}
        except Exception as exc:  # noqa: BLE001 — ошибку отдаём наверх текстом
            traceback.print_exc()
            reply = {"id": req.get("id"), "ok": False, "error": f"{type(exc).__name__}: {exc}"}
        proto.write(json.dumps(reply, ensure_ascii=False) + "\n")
        proto.flush()
