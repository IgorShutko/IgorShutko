"""Фейковое окружение для e2e: Dolphin Local/Remote API + Ads Manager + Graph API на одном порту.

Профиль "стартует" настоящим Chromium с --remote-debugging-port, как это делает Dolphin.
"""

import email
import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import requests

CHROME = os.getenv("CHROME_PATH", "/opt/pw-browsers/chromium")
TOKEN = "EAAB" + "x" * 60


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeEnv:
    def __init__(self):
        self.port = free_port()
        self.calls: list[dict] = []
        self.browsers: dict[str, subprocess.Popen] = {}
        self.counter = 0
        self.fail_rules: list[dict] = []   # [{"method":..., "path_re":..., "error":{...}, "times": n}]
        self.lock = threading.Lock()
        env = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="application/json"):
                data = body if isinstance(body, bytes) else (json.dumps(body) if not isinstance(body, str) else body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                code, body, ctype = env.handle("GET", u.path, q)
                self._send(code, body, ctype)

            def do_POST(self):
                u = urlparse(self.path)
                n = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(n)
                ct = self.headers.get("Content-Type", "")
                params = {}
                if ct.startswith("multipart/form-data"):
                    msg = email.message_from_bytes(b"Content-Type: " + ct.encode() + b"\r\n\r\n" + raw)
                    for part in msg.get_payload():
                        name = part.get_param("name", header="content-disposition")
                        payload = part.get_payload(decode=True)
                        if part.get_filename():
                            params[name] = {"__file__": part.get_filename(), "size": len(payload)}
                        else:
                            params[name] = payload.decode()
                elif ct.startswith("application/json"):
                    params = json.loads(raw or b"{}")
                code, body, ctype = env.handle("POST", u.path, params)
                self._send(code, body, ctype)

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    # ---------- жизненный цикл ----------

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        for p in self.browsers.values():
            p.kill()
        self.server.shutdown()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"

    def config_dict(self, runs_dir: str) -> dict:
        return {
            "dolphin_token": "test-jwt",
            "local_api": f"{self.base}/v1.0",
            "remote_api": self.base,
            "graph_base": self.base,
            "graph_version": "v23.0",
            "ads_manager_url": f"{self.base}/adsmanager",
            "request_delay": 0,
            "token_wait": 20,
            "runs_dir": runs_dir,
        }

    # ---------- хелперы для тестов ----------

    def graph_calls(self, method=None, path_re=None):
        out = [c for c in self.calls if c["path"].startswith("/v23.0/")]
        if method:
            out = [c for c in out if c["method"] == method]
        if path_re:
            out = [c for c in out if re.search(path_re, c["path"])]
        return out

    def fail(self, method, path_re, error, times=1):
        self.fail_rules.append({"method": method, "path_re": path_re, "error": error, "times": times})

    def _new_id(self, prefix):
        with self.lock:
            self.counter += 1
            return f"{prefix}{self.counter}"

    # ---------- Dolphin ----------

    def _start_browser(self, pid):
        if pid in self.browsers and self.browsers[pid].poll() is None:
            return {"success": False, "error": "profile already running"}
        port = free_port()
        udd = tempfile.mkdtemp(prefix="fakeprofile")
        proc = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={udd}",
                                 "--no-first-run", "--no-sandbox", "about:blank"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.browsers[pid] = proc
        for _ in range(100):
            try:
                ws = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=1).json()["webSocketDebuggerUrl"]
                return {"success": True, "automation": {"port": port, "wsEndpoint": urlparse(ws).path}}
            except Exception:
                time.sleep(0.1)
        return {"success": False, "error": "chrome did not start"}

    # ---------- маршрутизация ----------

    def handle(self, method, path, params):
        self.calls.append({"method": method, "path": path, "params": params})
        j = "application/json"

        # Dolphin local
        if path == "/v1.0/auth/login-with-token":
            return 200, {"success": params.get("token") == "test-jwt"}, j
        m = re.fullmatch(r"/v1.0/browser_profiles/(\w+)/start", path)
        if m:
            return 200, self._start_browser(m.group(1)), j
        m = re.fullmatch(r"/v1.0/browser_profiles/(\w+)/stop", path)
        if m:
            p = self.browsers.pop(m.group(1), None)
            if p:
                p.kill()
            return 200, {"success": True}, j
        # Dolphin remote
        if path == "/browser_profiles":
            profiles = [{"id": 101, "name": "Shop_UA", "status": None}, {"id": 102, "name": "Shop_PL", "status": None}]
            qn = params.get("query", "")
            return 200, {"data": [p for p in profiles if qn in p["name"]]}, j
        # Ads Manager
        if path == "/adsmanager":
            return 200, f"<html><body>Ads Manager<script>window.__accessToken='{TOKEN}';</script></body></html>", "text/html"

        if not path.startswith("/v23.0/"):
            return 404, {"error": "not found"}, j
        if params.get("access_token") != TOKEN:
            return 400, {"error": {"message": "Invalid OAuth access token", "code": 190}}, j

        gpath = path[len("/v23.0/"):]
        for rule in self.fail_rules:
            if rule["times"] > 0 and rule["method"] == method and re.search(rule["path_re"], gpath):
                rule["times"] -= 1
                return 400, {"error": rule["error"]}, j

        if method == "GET":
            if gpath == "me/adaccounts":
                return 200, {"data": [
                    {"account_id": "111", "name": "UA main", "account_status": 1, "currency": "UAH"},
                    {"account_id": "222", "name": "PL", "account_status": 1, "currency": "PLN"},
                    {"account_id": "333", "name": "banned", "account_status": 2, "currency": "USD", "disable_reason": 1},
                ]}, j
            if gpath == "me/accounts":
                return 200, {"data": [{"id": "900", "name": "Brand page"}]}, j
            if re.fullmatch(r"act_\d+/adspixels", gpath):
                return 200, {"data": [{"id": "777", "name": "Pixel"}]}, j
            if re.fullmatch(r"v\d+", gpath):
                return 200, {"id": gpath, "status": {"video_status": "ready"}}, j
            if re.fullmatch(r"v\d+/thumbnails", gpath):
                return 200, {"data": [{"uri": "https://cdn/thumb.jpg", "is_preferred": True}]}, j
            if gpath.endswith("/insights"):
                level = params.get("level", "campaign")
                return 200, {"data": [
                    {"campaign_name": "C", "adset_name": "S", "ad_name": "bad ad", "ad_id": "a_bad", "spend": "300",
                     "impressions": "5000", "clicks": "40", "inline_link_clicks": "30", "ctr": "0.6", "cpc": "10", "cpm": "60",
                     "frequency": "1.2", "actions": [{"action_type": "link_click", "value": "30"}]},
                    {"campaign_name": "C", "adset_name": "S", "ad_name": "good ad", "ad_id": "a_good", "spend": "200",
                     "impressions": "4000", "clicks": "80", "inline_link_clicks": "70", "ctr": "2.0", "cpc": "2.8", "cpm": "50",
                     "frequency": "1.1", "actions": [{"action_type": "offsite_conversion.fb_pixel_purchase", "value": "4"}],
                     "purchase_roas": [{"action_type": "omni_purchase", "value": "3.1"}]},
                ][: 2 if level == "ad" else 1]}, j
            if gpath == "search":
                return 200, {"data": [{"id": "6003139266461", "name": "Fitness", "audience_size_lower_bound": 1}]}, j
            return 404, {"error": {"message": f"unknown GET {gpath}", "code": 100}}, j

        # POST
        m = re.fullmatch(r"act_\d+/(campaigns|adsets|adcreatives|ads|advideos)", gpath)
        if m:
            prefix = {"campaigns": "c", "adsets": "s", "adcreatives": "cr", "ads": "a", "advideos": "v"}[m.group(1)]
            return 200, {"id": self._new_id(prefix)}, j
        if re.fullmatch(r"act_\d+/adimages", gpath):
            f = params.get("filename") or {}
            name = f.get("__file__", "bytes") if isinstance(f, dict) else "bytes"
            return 200, {"images": {name: {"hash": self._new_id("h"), "url": "https://cdn/x"}}}, j
        if "status" in params:
            return 200, {"success": True}, j
        return 404, {"error": {"message": f"unknown POST {gpath}", "code": 100}}, j
