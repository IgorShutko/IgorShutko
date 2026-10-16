"""Сессия профиля: Dolphin -> Playwright (CDP) -> Ads Manager -> Graph API через fetch внутри браузера.

Все запросы к Meta идут из самого профиля: тот же IP/прокси, куки и отпечаток.
"""

import base64
import json
import time

from playwright.sync_api import sync_playwright

from .config import Config
from .dolphin import Dolphin

FETCH_JS = """
async ({url, method, params, cred}) => {
  let res;
  try {
    if (method === 'GET') {
      res = await fetch(url + '?' + new URLSearchParams(params).toString(), {credentials: cred});
    } else {
      const body = new FormData();
      for (const [k, v] of Object.entries(params)) {
        if (v && typeof v === 'object' && v.__b64 !== undefined) {
          const bin = Uint8Array.from(atob(v.__b64), c => c.charCodeAt(0));
          body.append(k, new Blob([bin], {type: v.type || 'application/octet-stream'}), v.name || 'file');
        } else {
          body.append(k, v);
        }
      }
      res = await fetch(url, {method, body, credentials: cred});
    }
    const text = await res.text();
    try { return JSON.parse(text); }
    catch (e) { return {error: {message: 'Не JSON ответ: ' + text.slice(0, 300), code: -1}}; }
  } catch (e) {
    return {error: {message: 'fetch упал: ' + e.message, code: -2}};
  }
}
"""

TOKEN_JS = """
() => {
  const out = [];
  if (typeof window.__accessToken === 'string' && window.__accessToken) out.push(window.__accessToken);
  const html = document.documentElement.innerHTML;
  for (const m of html.matchAll(/EAA[A-Za-z0-9]{30,}/g)) if (!out.includes(m[0])) out.push(m[0]);
  return out.slice(0, 6);
}
"""

# коды Graph API, при которых имеет смысл повторить запрос
RETRY_CODES = {1, 2, 4, 17, 32, 341, 613, 80000, 80004, -2}


class GraphError(RuntimeError):
    def __init__(self, method, path, err: dict):
        self.err = err
        self.code = err.get("code")
        self.subcode = err.get("error_subcode")
        msg = err.get("message", "")
        user = err.get("error_user_msg") or err.get("error_user_title") or ""
        super().__init__(f"{method} {path}: {msg} {('| ' + user) if user else ''} (code={self.code}, sub={self.subcode})")


def file_param(data: bytes, name: str, mime: str) -> dict:
    """Параметр-файл для multipart загрузки из браузера."""
    return {"__b64": base64.b64encode(data).decode(), "name": name, "type": mime}


class ProfileSession:
    """with ProfileSession(cfg, dolphin, '123') as s: s.get('me/adaccounts')"""

    def __init__(self, cfg: Config, dolphin: Dolphin, profile_ref: str, log=print):
        self.cfg, self.dolphin, self.profile_ref, self.log = cfg, dolphin, profile_ref, log
        self.profile_id = None
        self.token = None
        self.graph_base = None
        self.cred = "include"
        self.probe_report: list[dict] = []
        self._pw = self._browser = self.page = None

    # ---------- жизненный цикл ----------

    def __enter__(self):
        self.profile_id = self.dolphin.resolve_profile(self.profile_ref)
        ws = self.dolphin.start(self.profile_id)
        self.log(f"[{self.profile_ref}] профиль запущен")
        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.connect_over_cdp(ws)
            ctx = self._browser.contexts[0] if self._browser.contexts else self._browser.new_context()
            self.page = ctx.new_page()
            self._load_token()
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc):
        try:
            if self.page:
                self.page.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        if self.cfg.stop_profile_after and self.profile_id:
            self.dolphin.stop(self.profile_id)
        return False

    def _load_token(self):
        self.page.goto(self.cfg.ads_manager_url, wait_until="domcontentloaded", timeout=120_000)
        deadline = time.time() + self.cfg.token_wait
        tokens = []
        while time.time() < deadline:
            try:
                tokens = self.page.evaluate(TOKEN_JS) or []
            except Exception:
                tokens = []  # страница ещё грузится/редиректит
            if tokens:
                break
            time.sleep(2)
        if not tokens:
            url = self.page.url
            hint = "чекпоинт/логин" if any(x in url for x in ("checkpoint", "login")) else "Ads Manager не прогрузился"
            raise RuntimeError(f"[{self.profile_ref}] токен не найден ({hint}), URL: {url}")
        self.log(f"[{self.profile_ref}] найдено токенов: {len(tokens)}, подбираю рабочий способ запросов")
        self._probe(tokens)

    def _probe(self, tokens: list[str]):
        """Перебирает токены x хосты x режим кук на GET me/adaccounts, закрепляет первую рабочую связку."""
        self.probe_report = []
        for ti, tok in enumerate(tokens):
            for base, cred in self.cfg.graph_candidates():
                url = f"{base}/{self.cfg.graph_version}/me/adaccounts"
                res = self.page.evaluate(FETCH_JS, {"url": url, "method": "GET", "cred": cred,
                                                    "params": {"fields": "account_id", "limit": "200", "access_token": tok}})
                ok = isinstance(res, dict) and "error" not in res
                err = "" if ok else (res.get("error", {}) if isinstance(res, dict) else {})
                self.probe_report.append({
                    "token": f"#{ti + 1} {tok[:6]}..{tok[-4:]} ({len(tok)})", "host": base.split("//")[-1],
                    "cookies": cred, "ok": ok,
                    "result": (f"кабинетов: {len(res.get('data', []))}" if ok
                               else f"{err.get('message', err)} (code={err.get('code')})")[:90],
                })
                if ok:
                    self.token, self.graph_base, self.cred = tok, base, cred
                    self.log(f"[{self.profile_ref}] ок: {base.split('//')[-1]}, куки={cred}, кабинетов {len(res.get('data', []))}")
                    return
        lines = "\n".join(f"  {r['token']} | {r['host']} | {r['cookies']} | {r['result']}" for r in self.probe_report)
        raise RuntimeError(f"[{self.profile_ref}] ни один способ запросов не сработал:\n{lines}")

    # ---------- Graph API ----------

    def call(self, path: str, method: str = "GET", retries: int = 3, **params):
        prepared = {}
        for k, v in params.items():
            if v is None:
                continue
            if isinstance(v, dict) and "__b64" in v:
                prepared[k] = v
            elif isinstance(v, (dict, list)):
                prepared[k] = json.dumps(v, ensure_ascii=False)
            elif isinstance(v, bool):
                prepared[k] = "true" if v else "false"
            else:
                prepared[k] = str(v)
        attempt = 0
        while True:
            url = f"{self.graph_base}/{self.cfg.graph_version}/{path.lstrip('/')}"
            prepared["access_token"] = self.token
            res = self.page.evaluate(FETCH_JS, {"url": url, "method": method, "params": prepared, "cred": self.cred})
            time.sleep(self.cfg.request_delay)
            if not isinstance(res, dict) or "error" not in res:
                return res
            err = GraphError(method, path, res["error"])
            attempt += 1
            if err.code == 190 and attempt <= 1:
                self.log(f"[{self.profile_ref}] токен протух, обновляю")
                self._load_token()
                continue
            if err.code in RETRY_CODES and attempt <= retries:
                wait = self.cfg.retry_backoff * 2 ** (attempt - 1)
                self.log(f"[{self.profile_ref}] {err} -> повтор через {wait}с")
                time.sleep(wait)
                continue
            raise err

    def get(self, path, **params):
        return self.call(path, "GET", **params)

    def post(self, path, **params):
        return self.call(path, "POST", **params)

    def get_all(self, path, limit=100, **params) -> list:
        """Пагинация по cursor'ам."""
        out, after = [], None
        while True:
            res = self.get(path, limit=limit, after=after, **params)
            out.extend(res.get("data", []))
            after = (res.get("paging") or {}).get("cursors", {}).get("after")
            if not after or not (res.get("paging") or {}).get("next"):
                return out

    # ---------- справочники ----------

    def accounts(self) -> list[dict]:
        return self.get_all("me/adaccounts", fields="account_id,name,account_status,currency,disable_reason,amount_spent,spend_cap,timezone_name")

    def pages(self) -> list[dict]:
        return self.get_all("me/accounts", fields="id,name,instagram_business_account{id,username}")

    def pixels(self, account_id: str) -> list[dict]:
        return self.get_all(f"act_{account_id}/adspixels", fields="id,name,last_fired_time")
