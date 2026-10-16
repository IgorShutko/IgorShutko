"""CLI: python -m dolphin_ads <команда> ..."""

import argparse
import json
import sys

from .brief import BriefError, parse
from .config import load_config
from .launcher import budget_check, launch, plan
from . import ops


def _table(rows: list[dict], cols: list[str] | None = None):
    if not rows:
        print("(пусто)")
        return
    cols = cols or list(rows[0])
    w = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    print("  ".join(c.ljust(w[c]) for c in cols))
    for r in rows:
        print("  ".join(str(r.get(c, "") if r.get(c) is not None else "").ljust(w[c]) for c in cols))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dolphin_ads", description="Автозалив Meta Ads через профили Dolphin{anty}")
    ap.add_argument("--config", help="путь к config.json")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("profiles", help="список профилей Dolphin")
    p.add_argument("--query", default="")

    p = sub.add_parser("inspect", help="кабинеты, страницы, пиксели профиля")
    p.add_argument("--profile", required=True)

    p = sub.add_parser("search", help="поиск интересов/городов для брифа")
    p.add_argument("--profile", required=True)
    p.add_argument("--q", required=True)
    p.add_argument("--type", choices=["interest", "city"], default="interest")

    p = sub.add_parser("validate", help="проверить бриф без браузера")
    p.add_argument("brief")

    p = sub.add_parser("launch", help="залить бриф (всё в PAUSED)")
    p.add_argument("brief")
    p.add_argument("--run", help="имя прогона (по умолчанию имя файла брифа)")
    p.add_argument("--dry-run", action="store_true", help="проверить профили и кабинеты, ничего не создавать")
    p.add_argument("--profile", action="append", help="только эти профили (можно несколько)")

    p = sub.add_parser("stats", help="статистика по прогону")
    p.add_argument("--run", required=True)
    p.add_argument("--level", choices=["campaign", "adset", "ad"], default="campaign")
    p.add_argument("--date", default="today", help="today, yesterday, last_3d, last_7d, maximum ...")
    p.add_argument("--result", default="purchase", help="purchase, lead, link_click, landing_page_view, add_to_cart")

    p = sub.add_parser("activate", help="включить всё из прогона")
    p.add_argument("--run", required=True)
    p.add_argument("--profile", action="append")
    p.add_argument("--yes", action="store_true")

    p = sub.add_parser("pause", help="поставить кампании прогона на паузу")
    p.add_argument("--run", required=True)
    p.add_argument("--profile", action="append")

    p = sub.add_parser("rules", help="автоотключение плохих объявлений")
    p.add_argument("--run", required=True)
    p.add_argument("--result", default="purchase")
    p.add_argument("--date", default="today")
    p.add_argument("--max-cpa", type=float)
    p.add_argument("--max-spend-no-result", type=float)
    p.add_argument("--min-ctr", type=float)
    p.add_argument("--min-impressions", type=int, default=1000)
    p.add_argument("--apply", action="store_true", help="реально ставить на паузу (иначе только показать)")

    a = ap.parse_args(argv)
    cfg = load_config(a.config)

    try:
        if a.cmd == "profiles":
            from .dolphin import Dolphin
            _table(Dolphin(cfg).list_profiles(a.query), ["id", "name", "status"])

        elif a.cmd == "inspect":
            info = ops.inspect_profile(cfg, a.profile)
            print("\nКАБИНЕТЫ (status 1 = активен)")
            _table(info["accounts"], ["account_id", "name", "account_status", "currency", "amount_spent", "disable_reason"])
            print("\nСТРАНИЦЫ")
            _table([{"id": p["id"], "name": p["name"],
                     "instagram_id": (p.get("instagram_business_account") or {}).get("id", ""),
                     "instagram": (p.get("instagram_business_account") or {}).get("username", "")} for p in info["pages"]])
            print("\nПИКСЕЛИ")
            for acc, px in info["pixels"].items():
                for x in px:
                    print(f"  {acc}: {x.get('id', '')} {x.get('name', x.get('error', ''))} last_fired={x.get('last_fired_time', '')}")

        elif a.cmd == "search":
            kind = "adinterest" if a.type == "interest" else "adgeolocation"
            res = ops.search_targeting(cfg, a.profile, a.q, kind)
            cols = ["id", "name", "audience_size_lower_bound", "audience_size_upper_bound", "path"] if kind == "adinterest" \
                else ["key", "name", "region", "country_code"]
            _table(res, cols)

        elif a.cmd == "validate":
            ads = parse(a.brief)
            errs = budget_check(ads, cfg)
            print(json.dumps(plan(ads), ensure_ascii=False, indent=2))
            if errs:
                print("\nПРЕДОХРАНИТЕЛИ:\n  " + "\n  ".join(errs))
                return 1
            print(f"\nOK: {len(ads)} объявлений")

        elif a.cmd == "launch":
            run = launch(cfg, a.brief, a.run, a.dry_run, a.profile)
            print(f"\nСостояние: {run.state_path}\nЛог: {run.log_path}")

        elif a.cmd == "stats":
            _table(ops.stats(cfg, a.run, a.level, a.date, a.result))

        elif a.cmd == "activate":
            if not a.yes:
                print("Добавь --yes, если точно включаем. Перед этим проверь всё в Ads Manager.")
                return 1
            print(f"Включено объектов: {ops.set_status(cfg, a.run, 'ACTIVE', a.profile)}")

        elif a.cmd == "pause":
            print(f"На паузе кампаний: {ops.set_status(cfg, a.run, 'PAUSED', a.profile)}")

        elif a.cmd == "rules":
            res = ops.apply_rules(cfg, a.run, a.result, a.date, a.max_cpa, a.max_spend_no_result,
                                  a.min_ctr, a.min_impressions, dry_run=not a.apply)
            _table(res)
            if not a.apply and res:
                print("\nЭто предпросмотр. Добавь --apply, чтобы поставить на паузу.")
    except BriefError as e:
        print(e)
        return 1
    except Exception as e:
        print(f"ОШИБКА: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
