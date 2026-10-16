"""Залив по брифу: кампании -> адсеты -> медиа -> креативы -> объявления. Всё в PAUSED.

Состояние пишется в runs/<run>.state.json после каждого созданного объекта,
поэтому повторный запуск того же брифа продолжит с места падения и не задублит.
"""

import csv
import json
import mimetypes
import time
from datetime import datetime
from pathlib import Path

import requests

from .brief import OBJECTIVES, PLACEMENTS, AdRow, parse
from .config import Config
from .dolphin import Dolphin
from .session import GraphError, ProfileSession, file_param

# валюты без копеек (offset 1) по справке Meta
ZERO_DECIMAL = {"CLP", "COP", "CRC", "HUF", "ISK", "IDR", "JPY", "KRW", "PYG", "TWD", "VND"}

BID = {"lowest": "LOWEST_COST_WITHOUT_CAP", "cost_cap": "COST_CAP", "bid_cap": "LOWEST_COST_WITH_BID_CAP"}


def minor(amount: float, currency: str) -> int:
    return int(round(amount if currency in ZERO_DECIMAL else amount * 100))


class Run:
    """Один прогон брифа: состояние, лог, результаты."""

    def __init__(self, cfg: Config, name: str):
        self.cfg = cfg
        self.name = name
        d = cfg.runs_path
        self.state_path = d / f"{name}.state.json"
        self.log_path = d / f"{name}.log"
        self.results_path = d / f"{name}.results.csv"
        self.state = {"campaigns": {}, "adsets": {}, "ads": {}, "media": {}, "creatives": {}, "rows": {}, "meta": {}}
        if self.state_path.exists():
            self.state.update(json.loads(self.state_path.read_text(encoding="utf-8")))

    def save(self):
        self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")

    def log(self, msg: str):
        line = f"{datetime.now():%H:%M:%S} {msg}"
        print(line, flush=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def write_results(self, ads: list[AdRow]):
        with self.results_path.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["row", "profile", "account_id", "campaign", "campaign_id", "adset", "adset_id", "ad_name", "ad_id", "status", "error"])
            for a in ads:
                r = self.state["rows"].get(str(a.row), {})
                w.writerow([a.row, a.profile, a.account_id, a.campaign, self.state["campaigns"].get(a.campaign_key, ""),
                            a.adset, self.state["adsets"].get(a.adset_key, ""), a.ad_name,
                            self.state["ads"].get(a.ad_key, ""), r.get("status", "pending"), r.get("error", "")])


# ---------- проверки до запуска ----------

def plan(ads: list[AdRow]) -> dict:
    """Структура залива + бюджеты (в валюте кабинета)."""
    out = {}
    for a in ads:
        p = out.setdefault(a.profile, {})
        c = p.setdefault(a.campaign_key, {"campaign": a.campaign, "account_id": a.account_id, "objective": a.objective,
                                          "campaign_budget": a.campaign_budget, "adsets": {}})
        s = c["adsets"].setdefault(a.adset_key, {"adset": a.adset, "daily_budget": a.daily_budget, "ads": []})
        s["ads"].append({"row": a.row, "ad_name": a.ad_name, "media": Path(a.media).name, "type": a.media_type})
    return out


def budget_check(ads: list[AdRow], cfg: Config) -> list[str]:
    errors, total, seen = [], 0.0, set()
    for a in ads:
        if a.campaign_budget:
            if a.campaign_key in seen:
                continue
            seen.add(a.campaign_key)
            amount, what = a.campaign_budget, f"кампания '{a.campaign}'"
        else:
            if a.adset_key in seen:
                continue
            seen.add(a.adset_key)
            amount, what = a.daily_budget, f"адсет '{a.adset}'"
        if amount > cfg.max_daily_budget:
            errors.append(f"{what}: бюджет {amount} > max_daily_budget {cfg.max_daily_budget}")
        total += amount
    if total > cfg.max_total_daily_budget:
        errors.append(f"суммарный дневной бюджет {total} > max_total_daily_budget {cfg.max_total_daily_budget}")
    return errors


# ---------- построение параметров ----------

def targeting(a: AdRow) -> dict:
    t = {"age_min": a.age_min, "age_max": a.age_max,
         "targeting_automation": {"advantage_audience": int(a.advantage_audience)}}
    if a.cities:
        cities = []
        for c in a.cities:
            key, _, radius = c.partition(":")
            item = {"key": key}
            if radius:
                item.update(radius=int(radius), distance_unit="kilometer")
            cities.append(item)
        t["geo_locations"] = {"cities": cities}
    else:
        t["geo_locations"] = {"countries": a.countries}
    if a.gender == "male":
        t["genders"] = [1]
    elif a.gender == "female":
        t["genders"] = [2]
    if a.interests:
        t["flexible_spec"] = [{"interests": [{"id": i.split(":")[0]} for i in a.interests]}]
    if a.custom_audiences:
        t["custom_audiences"] = [{"id": i} for i in a.custom_audiences]
    if a.excluded_audiences:
        t["excluded_custom_audiences"] = [{"id": i} for i in a.excluded_audiences]
    if a.placements:
        platforms = []
        for p in a.placements:
            platform, field, value = PLACEMENTS[p]
            if platform not in platforms:
                platforms.append(platform)
            t.setdefault(field, [])
            if value not in t[field]:
                t[field].append(value)
        t["publisher_platforms"] = platforms
        t["device_platforms"] = ["mobile", "desktop"]
    return t


def campaign_params(a: AdRow, currency: str) -> dict:
    objective = OBJECTIVES[a.objective][0]
    p = {"name": a.campaign, "objective": objective, "status": "PAUSED",
         "special_ad_categories": [], "buying_type": "AUCTION"}
    if a.campaign_budget:
        p["daily_budget"] = minor(a.campaign_budget, currency)
        p["bid_strategy"] = BID[a.bid_strategy]
    else:
        p["is_adset_budget_sharing_enabled"] = False
    return p


def adset_params(a: AdRow, campaign_id: str, currency: str) -> dict:
    objective, opt, needs_pixel, default_event = OBJECTIVES[a.objective]
    p = {"name": a.adset, "campaign_id": campaign_id, "status": "PAUSED",
         "billing_event": "IMPRESSIONS", "optimization_goal": a.optimization or opt,
         "targeting": targeting(a)}
    if a.objective in ("traffic", "sales", "leads"):
        p["destination_type"] = "WEBSITE"
    if needs_pixel:
        p["promoted_object"] = {"pixel_id": a.pixel_id, "custom_event_type": a.event or default_event}
    if not a.campaign_budget:
        p["daily_budget"] = minor(a.daily_budget, currency)
        p["bid_strategy"] = BID[a.bid_strategy]
    if a.bid_amount:
        p["bid_amount"] = minor(a.bid_amount, currency)
    if a.start_time:
        p["start_time"] = a.start_time
    return p


def creative_params(a: AdRow, media: dict) -> dict:
    cta = None if a.cta == "NO_BUTTON" else {"type": a.cta, "value": {"link": a.link}}
    spec = {"page_id": a.page_id}
    if a.instagram_id:
        spec["instagram_user_id"] = a.instagram_id
    if media["type"] == "image":
        data = {"link": a.link, "message": a.primary_text, "image_hash": media["hash"]}
        if a.headline:
            data["name"] = a.headline
        if a.description:
            data["description"] = a.description
        if cta:
            data["call_to_action"] = cta
        spec["link_data"] = data
    else:
        data = {"video_id": media["video_id"], "image_url": media["thumb"], "message": a.primary_text,
                "call_to_action": cta or {"type": "LEARN_MORE", "value": {"link": a.link}}}
        if a.headline:
            data["title"] = a.headline
        if a.description:
            data["link_description"] = a.description
        spec["video_data"] = data
    p = {"name": f"{a.ad_name} creative", "object_story_spec": spec}
    if a.url_tags:
        p["url_tags"] = a.url_tags
    return p


# ---------- медиа ----------

def _read_media(src: str) -> tuple[bytes, str, str]:
    if src.startswith("http"):
        r = requests.get(src, timeout=300)
        r.raise_for_status()
        name = Path(src.split("?")[0]).name or "file"
        return r.content, name, r.headers.get("content-type") or mimetypes.guess_type(name)[0] or "application/octet-stream"
    p = Path(src)
    return p.read_bytes(), p.name, mimetypes.guess_type(p.name)[0] or "application/octet-stream"


def upload_media(s: ProfileSession, run: Run, a: AdRow) -> dict:
    key = f"{a.account_id}|{a.media}"
    if key in run.state["media"]:
        return run.state["media"][key]
    act = f"act_{a.account_id}"
    data, name, mime = _read_media(a.media)
    if a.media_type == "image":
        res = s.post(f"{act}/adimages", **{"filename": file_param(data, name, mime)})
        img = next(iter(res["images"].values()))
        media = {"type": "image", "hash": img["hash"]}
        run.log(f"  картинка {name} -> {img['hash']}")
    else:
        if a.media.startswith("http"):
            res = s.post(f"{act}/advideos", file_url=a.media, name=name)
        else:
            res = s.post(f"{act}/advideos", source=file_param(data, name, mime), name=name)
        vid = res["id"]
        run.log(f"  видео {name} -> {vid}, жду обработку")
        deadline = time.time() + 900
        while True:
            st = s.get(vid, fields="status").get("status", {})
            if st.get("video_status") == "ready":
                break
            if st.get("video_status") == "error":
                raise RuntimeError(f"Meta не обработала видео {name}: {st}")
            if time.time() > deadline:
                raise RuntimeError(f"видео {name} обрабатывается >15 минут")
            time.sleep(10)
        thumbs = s.get(f"{vid}/thumbnails").get("data", [])
        thumb = next((t["uri"] for t in thumbs if t.get("is_preferred")), thumbs[0]["uri"] if thumbs else None)
        if not thumb:
            raise RuntimeError(f"у видео {name} нет превью")
        media = {"type": "video", "video_id": vid, "thumb": thumb}
    run.state["media"][key] = media
    run.save()
    return media


# ---------- запуск ----------

def check_profile(s: ProfileSession, ads: list[AdRow], log) -> dict:
    """Проверяет кабинеты/страницы/пиксели профиля. Возвращает {account_id: currency}."""
    accounts = {a["account_id"]: a for a in s.accounts()}
    pages = {p["id"] for p in s.pages()}
    currencies, problems = {}, []
    for acc_id in sorted({a.account_id for a in ads}):
        acc = accounts.get(acc_id)
        if not acc:
            problems.append(f"кабинет {acc_id} недоступен в профиле")
            continue
        if acc["account_status"] != 1:
            problems.append(f"кабинет {acc_id} не активен (status={acc['account_status']}, reason={acc.get('disable_reason')})")
            continue
        currencies[acc_id] = acc["currency"]
        log(f"  кабинет {acc_id} '{acc['name']}' {acc['currency']} ok")
        need_pixels = {a.pixel_id for a in ads if a.account_id == acc_id and a.pixel_id}
        if need_pixels:
            have = {p["id"] for p in s.pixels(acc_id)}
            for px in need_pixels - have:
                log(f"  ВНИМАНИЕ: пиксель {px} не найден в кабинете {acc_id} (может быть расшарен через BM)")
    for pg in sorted({a.page_id for a in ads}):
        if pg not in pages:
            log(f"  ВНИМАНИЕ: страница {pg} не найдена в me/accounts (ок, если доступ через BM)")
    if problems:
        raise RuntimeError("; ".join(problems))
    return currencies


def launch(cfg: Config, brief: str, run_name: str | None = None, dry_run: bool = False,
           only_profiles: list[str] | None = None) -> Run:
    ads = parse(brief)
    if only_profiles:
        ads = [a for a in ads if a.profile in only_profiles]
    run_name = run_name or Path(brief.split("?")[0]).stem
    run = Run(cfg, run_name)
    run.state["meta"].update(brief=brief, updated=datetime.now().isoformat(timespec="seconds"))

    errors = budget_check(ads, cfg)
    if errors:
        raise RuntimeError("Предохранители бюджета:\n  " + "\n  ".join(errors))

    structure = plan(ads)
    n_c = sum(len(v) for v in structure.values())
    n_s = sum(len(c["adsets"]) for v in structure.values() for c in v.values())
    run.log(f"=== {run_name}: профилей {len(structure)}, кампаний {n_c}, адсетов {n_s}, объявлений {len(ads)}"
            f"{' [DRY RUN]' if dry_run else ''}")

    dolphin = Dolphin(cfg)
    by_profile: dict[str, list[AdRow]] = {}
    for a in ads:
        by_profile.setdefault(a.profile, []).append(a)

    for profile, rows in by_profile.items():
        todo = [a for a in rows if a.ad_key not in run.state["ads"]]
        if not todo:
            run.log(f"[{profile}] всё уже создано, пропуск")
            continue
        run.log(f"[{profile}] старт, объявлений к созданию: {len(todo)}")
        try:
            with ProfileSession(cfg, dolphin, profile, log=run.log) as s:
                currencies = check_profile(s, todo, run.log)
                if dry_run:
                    for a in todo:
                        run.log(f"  [план] {a.campaign} / {a.adset} / {a.ad_name} ({a.media_type})")
                    continue
                for a in todo:
                    _create_row(s, run, a, currencies[a.account_id])
        except Exception as e:  # профиль целиком упал - идём к следующему
            run.log(f"[{profile}] ОШИБКА: {e}")
            for a in todo:
                st = run.state["rows"].setdefault(str(a.row), {})
                if st.get("status") != "created":
                    st.update(status="error", error=str(e))
            run.save()
    run.write_results(ads)
    created = sum(1 for a in ads if a.ad_key in run.state["ads"])
    run.log(f"=== итог: создано {created}/{len(ads)} объявлений. Результаты: {run.results_path}")
    return run


def _create_row(s: ProfileSession, run: Run, a: AdRow, currency: str):
    act = f"act_{a.account_id}"
    st = run.state["rows"].setdefault(str(a.row), {})
    try:
        if a.campaign_key not in run.state["campaigns"]:
            res = s.post(f"{act}/campaigns", **campaign_params(a, currency))
            run.state["campaigns"][a.campaign_key] = res["id"]
            run.save()
            run.log(f"  + кампания '{a.campaign}' {res['id']}")
        camp_id = run.state["campaigns"][a.campaign_key]

        if a.adset_key not in run.state["adsets"]:
            res = s.post(f"{act}/adsets", **adset_params(a, camp_id, currency))
            run.state["adsets"][a.adset_key] = res["id"]
            run.save()
            run.log(f"  + адсет '{a.adset}' {res['id']}")
        adset_id = run.state["adsets"][a.adset_key]

        media = upload_media(s, run, a)
        if a.ad_key not in run.state["creatives"]:
            res = s.post(f"{act}/adcreatives", **creative_params(a, media))
            run.state["creatives"][a.ad_key] = res["id"]
            run.save()
        creative_id = run.state["creatives"][a.ad_key]

        res = s.post(f"{act}/ads", name=a.ad_name, adset_id=adset_id, creative={"creative_id": creative_id}, status="PAUSED")
        run.state["ads"][a.ad_key] = res["id"]
        st.update(status="created", error="")
        run.save()
        run.log(f"  + объявление '{a.ad_name}' {res['id']} (строка {a.row})")
    except (GraphError, RuntimeError, requests.RequestException, OSError) as e:
        st.update(status="error", error=str(e))
        run.save()
        run.log(f"  ! строка {a.row}: {e}")
