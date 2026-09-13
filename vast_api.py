"""Минимальный клиент REST API Vast.ai (без установки vastai CLI)."""
import json
import os
from pathlib import Path
from urllib.parse import quote_plus

import requests

BASE_URL = os.environ.get("VAST_URL", "https://console.vast.ai")


class VastError(RuntimeError):
    pass


def find_api_key(config_key=""):
    """Ключ из config.yaml, переменной VAST_API_KEY или файла, куда его сохраняет `vastai set api-key`."""
    if config_key and config_key.strip():
        return config_key.strip()
    if os.environ.get("VAST_API_KEY"):
        return os.environ["VAST_API_KEY"].strip()
    for path in (Path.home() / ".config" / "vastai" / "vast_api_key", Path.home() / ".vast_api_key"):
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    return ""


class Vast:
    def __init__(self, api_key):
        if not api_key:
            raise VastError("не найден API-ключ Vast: впишите его в config.yaml (vast_api_key)")
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {api_key}", "User-Agent": "tts-vast/1.0"})

    def _url(self, path, query=None):
        url = BASE_URL + path
        if query:
            url += "?" + "&".join(
                f"{k}={quote_plus(v if isinstance(v, str) else json.dumps(v))}" for k, v in query.items()
            )
        return url

    def _check(self, r):
        if r.status_code == 401 or r.status_code == 403:
            raise VastError("Vast отклонил API-ключ (проверьте vast_api_key в config.yaml)")
        if r.status_code >= 400:
            raise VastError(f"Vast API {r.status_code}: {r.text[:500]}")
        return r.json()

    def search_offers(self, gpu_names, max_price, min_reliability, disk_gb, interruptible=False,
                      min_cuda=12.8, limit=40):
        q = {
            "verified": {"eq": True}, "external": {"eq": False},
            "rentable": {"eq": True}, "rented": {"eq": False},
            "gpu_name": {"in": list(gpu_names)}, "num_gpus": {"eq": 1},
            "reliability": {"gte": min_reliability}, "cuda_max_good": {"gte": min_cuda},
            "direct_port_count": {"gte": 1}, "disk_space": {"gte": disk_gb}, "inet_down": {"gte": 200},
            "order": [["min_bid" if interruptible else "dph_total", "asc"]],
            "type": "bid" if interruptible else "on-demand",
            "limit": limit, "allocated_storage": disk_gb,
        }
        if not interruptible:
            q["dph_total"] = {"lte": max_price}
        offers = self._check(self.s.post(self._url("/api/v0/bundles/"), json=q, timeout=60)).get("offers", [])
        if interruptible:
            offers = [o for o in offers if (o.get("min_bid") or 99) <= max_price]
        return offers

    def create_instance(self, offer_id, image, env, onstart, disk_gb, label, price=None):
        payload = {
            "client_id": "me", "image": image, "env": env, "price": price, "disk": disk_gb,
            "label": label, "extra": None, "onstart": onstart, "image_login": None,
            "python_utf8": False, "lang_utf8": False, "use_jupyter_lab": False, "jupyter_dir": None,
            "force": False, "cancel_unavail": True, "template_hash_id": None, "user": None,
            "runtype": "ssh_direc ssh_proxy",
        }
        data = self._check(self.s.put(self._url(f"/api/v0/asks/{offer_id}/"), json=payload, timeout=60))
        if not data.get("success") or not data.get("new_contract"):
            raise VastError(f"не удалось арендовать машину: {data}")
        return int(data["new_contract"])

    def instance(self, instance_id):
        r = self.s.get(self._url(f"/api/v0/instances/{instance_id}/", {"owner": "me"}), timeout=30)
        return self._check(r).get("instances")

    def destroy(self, instance_id):
        return self._check(self.s.delete(self._url(f"/api/v0/instances/{instance_id}/"), json={}, timeout=30))

    def list_instances(self):
        rows, params = [], {"select_filters": {}, "order_by": [{"col": "id", "dir": "asc"}], "limit": 25}
        while True:
            data = self._check(self.s.get(self._url("/api/v1/instances/", params), timeout=30))
            rows.extend(data.get("instances") or [])
            if not data.get("next_token"):
                return rows
            params["after_token"] = data["next_token"]


def api_endpoint(inst, internal_port=8000):
    """Внешний адрес API на машине: http://IP:порт или None, если порт ещё не проброшен."""
    ip = (inst.get("public_ipaddr") or "").strip()
    mapping = (inst.get("ports") or {}).get(f"{internal_port}/tcp") or []
    if not ip or not mapping:
        return None
    return f"http://{ip}:{mapping[0].get('HostPort')}"
