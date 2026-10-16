"""Клиент Dolphin{anty}: remote API (список профилей) + local API (старт/стоп)."""

import time

import requests

from .config import Config


class DolphinError(RuntimeError):
    pass


class Dolphin:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._logged_in = False

    # ---------- remote API ----------

    def _remote(self, path: str, **params):
        if not self.cfg.dolphin_token:
            raise DolphinError("Нужен dolphin_token в config.json, чтобы искать профили по имени")
        r = requests.get(
            f"{self.cfg.remote_api.rstrip('/')}/{path.lstrip('/')}",
            params=params,
            headers={"Authorization": f"Bearer {self.cfg.dolphin_token}"},
            timeout=30,
        )
        if r.status_code >= 400:
            raise DolphinError(f"Remote API {r.status_code}: {r.text[:300]}")
        return r.json()

    def list_profiles(self, query: str = "", limit: int = 100) -> list[dict]:
        params = {"limit": limit, "page": 1}
        if query:
            params["query"] = query
        data = self._remote("browser_profiles", **params)
        items = data.get("data", data if isinstance(data, list) else [])
        return [{"id": str(p.get("id")), "name": p.get("name", ""),
                 "status": (p.get("status") or {}).get("name", "") if isinstance(p.get("status"), dict) else p.get("status", ""),
                 "tags": p.get("tags", [])} for p in items]

    def resolve_profile(self, ref: str) -> str:
        """Профиль можно указать ID или точным именем."""
        ref = str(ref).strip()
        if ref.isdigit():
            return ref
        found = [p for p in self.list_profiles(ref) if p["name"] == ref]
        if not found:
            raise DolphinError(f"Профиль '{ref}' не найден")
        if len(found) > 1:
            raise DolphinError(f"Несколько профилей с именем '{ref}', укажи ID")
        return found[0]["id"]

    # ---------- local API ----------

    def _local(self, path: str, method: str = "GET", **kw):
        try:
            r = requests.request(method, f"{self.cfg.local_api.rstrip('/')}/{path.lstrip('/')}", timeout=120, **kw)
        except requests.ConnectionError as e:
            raise DolphinError(f"Dolphin не отвечает на {self.cfg.local_api}. Приложение открыто?") from e
        try:
            return r.json()
        except ValueError:
            raise DolphinError(f"Local API вернул не JSON ({r.status_code}): {r.text[:300]}")

    def login(self):
        if self._logged_in or not self.cfg.dolphin_token:
            return
        data = self._local("auth/login-with-token", "POST", json={"token": self.cfg.dolphin_token})
        if data.get("success") is False:
            raise DolphinError(f"Local API не принял токен: {data}")
        self._logged_in = True

    def start(self, profile_id: str) -> str:
        """Стартует профиль, возвращает ws:// для CDP."""
        self.login()
        data = self._local(f"browser_profiles/{profile_id}/start", params={"automation": 1})
        if not data.get("success"):
            msg = str(data)
            if "already" in msg.lower() or "running" in msg.lower():
                self.stop(profile_id)
                time.sleep(3)
                data = self._local(f"browser_profiles/{profile_id}/start", params={"automation": 1})
            if not data.get("success"):
                raise DolphinError(f"Профиль {profile_id} не запустился: {data}")
        auto = data["automation"]
        ws = auto["wsEndpoint"]
        if not ws.startswith("/"):
            ws = "/" + ws
        return f"ws://127.0.0.1:{auto['port']}{ws}"

    def stop(self, profile_id: str):
        try:
            self._local(f"browser_profiles/{profile_id}/stop")
        except DolphinError:
            pass
