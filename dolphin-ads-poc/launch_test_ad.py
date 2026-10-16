"""
PoC: тестовая реклама в Meta через профиль Dolphin{anty}.

Схема:
  Dolphin Local API -> старт профиля с automation=1 -> Playwright по CDP ->
  Ads Manager -> access token из сессии -> Graph API через fetch ВНУТРИ браузера
  (тот же IP/прокси/отпечаток, что и у профиля).

Безопасность:
  - по умолчанию режим --dry-run: только чтение (кабинеты, страницы);
  - с --create всё создаётся в статусе PAUSED, скрипт никогда ничего не включает;
  - жёсткий потолок дневного бюджета MAX_DAILY_BUDGET.
"""

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

LOCAL_API = os.getenv("DOLPHIN_LOCAL_API", "http://localhost:3001/v1.0")
DOLPHIN_TOKEN = os.getenv("DOLPHIN_TOKEN", "")  # JWT из панели Dolphin -> API
GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v23.0")
MAX_DAILY_BUDGET = 10  # в валюте кабинета, предохранитель

ADS_MANAGER_URL = "https://adsmanager.facebook.com/adsmanager/manage/campaigns"


# ---------- Dolphin ----------

def dolphin_login():
    if not DOLPHIN_TOKEN:
        return
    r = requests.post(f"{LOCAL_API}/auth/login-with-token", json={"token": DOLPHIN_TOKEN}, timeout=30)
    r.raise_for_status()


def dolphin_start(profile_id: str) -> str:
    r = requests.get(f"{LOCAL_API}/browser_profiles/{profile_id}/start", params={"automation": 1}, timeout=120)
    data = r.json()
    if not data.get("success"):
        sys.exit(f"Dolphin не запустил профиль: {data}")
    auto = data["automation"]
    return f"ws://127.0.0.1:{auto['port']}{auto['wsEndpoint']}"


def dolphin_stop(profile_id: str):
    try:
        requests.get(f"{LOCAL_API}/browser_profiles/{profile_id}/stop", timeout=30)
    except requests.RequestException:
        pass


# ---------- Graph API внутри браузера ----------

FETCH_JS = """
async ({url, method, params}) => {
  let res;
  if (method === 'GET') {
    const qs = new URLSearchParams(params).toString();
    res = await fetch(url + '?' + qs, {credentials: 'omit'});
  } else {
    const body = new FormData();
    for (const [k, v] of Object.entries(params)) body.append(k, v);
    res = await fetch(url, {method, body, credentials: 'omit'});
  }
  return await res.json();
}
"""

TOKEN_JS = """
() => {
  if (window.__accessToken) return window.__accessToken;
  const m = document.documentElement.innerHTML.match(/EAA[A-Za-z0-9]{30,}/);
  return m ? m[0] : null;
}
"""


class Graph:
    def __init__(self, page, token):
        self.page, self.token = page, token

    def call(self, path, method="GET", **params):
        params = {k: (json.dumps(v) if isinstance(v, (dict, list)) else str(v)) for k, v in params.items()}
        params["access_token"] = self.token
        url = f"https://graph.facebook.com/{GRAPH_VERSION}/{path.lstrip('/')}"
        res = self.page.evaluate(FETCH_JS, {"url": url, "method": method, "params": params})
        if "error" in res:
            err = res["error"]
            sys.exit(f"Graph API ошибка на {method} {path}:\n"
                     f"  {err.get('message')}\n  {err.get('error_user_msg', '')}\n  code={err.get('code')} sub={err.get('error_subcode')}")
        time.sleep(1.5)  # не долбим API пачкой
        return res


# ---------- сценарий ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True, help="ID профиля Dolphin")
    ap.add_argument("--create", action="store_true", help="реально создать кампанию (PAUSED). Без флага - только чтение")
    ap.add_argument("--account", help="ID кабинета без act_")
    ap.add_argument("--page", help="ID фан-страницы")
    ap.add_argument("--link", default="https://example.com")
    ap.add_argument("--image", help="путь к картинке для креатива (jpg/png)")
    ap.add_argument("--budget", type=float, default=5, help="дневной бюджет в валюте кабинета")
    ap.add_argument("--country", default="UA")
    ap.add_argument("--text", default="Тестовое объявление")
    ap.add_argument("--headline", default="Тест")
    args = ap.parse_args()

    if args.budget > MAX_DAILY_BUDGET:
        sys.exit(f"Бюджет {args.budget} > предохранителя {MAX_DAILY_BUDGET}. Поменяй MAX_DAILY_BUDGET осознанно.")

    dolphin_login()
    ws = dolphin_start(args.profile)
    print(f"[ok] профиль запущен: {ws}")

    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(ws)
            ctx = browser.contexts[0]
            page = ctx.new_page()
            page.goto(ADS_MANAGER_URL, wait_until="domcontentloaded", timeout=90_000)
            page.wait_for_timeout(8000)

            token = page.evaluate(TOKEN_JS)
            if not token:
                sys.exit("Токен не найден. Проверь, что в профиле залогинен FB и Ads Manager открывается без чекпоинта.")
            print(f"[ok] токен найден: {token[:12]}...")
            g = Graph(page, token)

            accounts = g.call("me/adaccounts", fields="account_id,name,account_status,currency,disable_reason", limit=50)["data"]
            pages = g.call("me/accounts", fields="id,name", limit=50)["data"]

            print("\nКабинеты (account_status 1 = активен):")
            for a in accounts:
                print(f"  {a['account_id']}  {a['name']}  status={a['account_status']}  {a['currency']}")
            print("\nСтраницы:")
            for pg in pages:
                print(f"  {pg['id']}  {pg['name']}")

            if not args.create:
                print("\n[dry-run] Ничего не создано. Повтори с --create --account ... --page ... --image ...")
                return

            if not (args.account and args.page and args.image):
                sys.exit("Для --create нужны --account, --page и --image")

            acc = next((a for a in accounts if a["account_id"] == args.account), None)
            if not acc:
                sys.exit("Кабинет не найден среди доступных")
            if acc["account_status"] != 1:
                sys.exit(f"Кабинет не активен (status={acc['account_status']}), стоп")

            act = f"act_{args.account}"
            ts = time.strftime("%Y%m%d-%H%M")

            # 1. кампания
            camp = g.call(f"{act}/campaigns", "POST",
                          name=f"TEST {ts}",
                          objective="OUTCOME_TRAFFIC",
                          status="PAUSED",
                          special_ad_categories=[],
                          is_adset_budget_sharing_enabled="false")
            print(f"[ok] кампания {camp['id']}")

            # 2. адсет (бюджет в минорных единицах: центы/копейки)
            adset = g.call(f"{act}/adsets", "POST",
                           name=f"TEST adset {ts}",
                           campaign_id=camp["id"],
                           daily_budget=int(args.budget * 100),
                           billing_event="IMPRESSIONS",
                           optimization_goal="LINK_CLICKS",
                           bid_strategy="LOWEST_COST_WITHOUT_CAP",
                           targeting={
                               "geo_locations": {"countries": [args.country]},
                               "age_min": 18, "age_max": 65,
                               "targeting_automation": {"advantage_audience": 0},
                           },
                           status="PAUSED")
            print(f"[ok] адсет {adset['id']}")

            # 3. картинка
            img_b64 = base64.b64encode(Path(args.image).read_bytes()).decode()
            img = g.call(f"{act}/adimages", "POST", bytes=img_b64)
            image_hash = next(iter(img["images"].values()))["hash"]
            print(f"[ok] картинка {image_hash}")

            # 4. креатив
            creative = g.call(f"{act}/adcreatives", "POST",
                              name=f"TEST creative {ts}",
                              object_story_spec={
                                  "page_id": args.page,
                                  "link_data": {
                                      "link": args.link,
                                      "message": args.text,
                                      "name": args.headline,
                                      "image_hash": image_hash,
                                      "call_to_action": {"type": "LEARN_MORE", "value": {"link": args.link}},
                                  },
                              })
            print(f"[ok] креатив {creative['id']}")

            # 5. объявление
            ad = g.call(f"{act}/ads", "POST",
                        name=f"TEST ad {ts}",
                        adset_id=adset["id"],
                        creative={"creative_id": creative["id"]},
                        status="PAUSED")
            print(f"[ok] объявление {ad['id']}")

            print(f"\nГотово. Всё в PAUSED. Проверь и включи руками:\n"
                  f"{ADS_MANAGER_URL}?act={args.account}&selected_campaign_ids={camp['id']}")
            page.wait_for_timeout(3000)
    finally:
        dolphin_stop(args.profile)


if __name__ == "__main__":
    main()
