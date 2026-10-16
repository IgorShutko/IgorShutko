"""Бриф залива: CSV-файл или ссылка на Google Sheets. Одна строка = одно объявление.

Строки с одинаковыми profile+account_id+campaign собираются в одну кампанию,
с одинаковым adset внутри кампании - в один адсет.
"""

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

import requests

OBJECTIVES = {
    # ключ: (objective, optimization_goal по умолчанию, нужен пиксель, событие по умолчанию)
    "traffic": ("OUTCOME_TRAFFIC", "LINK_CLICKS", False, None),
    "sales": ("OUTCOME_SALES", "OFFSITE_CONVERSIONS", True, "PURCHASE"),
    "leads": ("OUTCOME_LEADS", "OFFSITE_CONVERSIONS", True, "LEAD"),
    "awareness": ("OUTCOME_AWARENESS", "REACH", False, None),
    "engagement": ("OUTCOME_ENGAGEMENT", "POST_ENGAGEMENT", False, None),
}

PLACEMENTS = {
    "fb_feed": ("facebook", "facebook_positions", "feed"),
    "fb_reels": ("facebook", "facebook_positions", "facebook_reels"),
    "fb_stories": ("facebook", "facebook_positions", "story"),
    "fb_marketplace": ("facebook", "facebook_positions", "marketplace"),
    "fb_video_feeds": ("facebook", "facebook_positions", "video_feeds"),
    "fb_search": ("facebook", "facebook_positions", "search"),
    "ig_feed": ("instagram", "instagram_positions", "stream"),
    "ig_stories": ("instagram", "instagram_positions", "story"),
    "ig_reels": ("instagram", "instagram_positions", "reels"),
    "ig_explore": ("instagram", "instagram_positions", "explore"),
    "ig_profile_feed": ("instagram", "instagram_positions", "profile_feed"),
    "an": ("audience_network", "audience_network_positions", "classic"),
    "messenger": ("messenger", "messenger_positions", "messenger_home"),
}

CTA = {"LEARN_MORE", "SHOP_NOW", "SIGN_UP", "ORDER_NOW", "BUY_NOW", "CONTACT_US", "GET_OFFER",
       "BOOK_TRAVEL", "DOWNLOAD", "APPLY_NOW", "SUBSCRIBE", "GET_QUOTE", "SEND_MESSAGE",
       "WHATSAPP_MESSAGE", "MESSAGE_PAGE", "SEE_MORE", "NO_BUTTON"}

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

REQUIRED = ["profile", "account_id", "page_id", "campaign", "adset", "media", "primary_text", "link"]

# русские/альтернативные заголовки -> ключи
ALIASES = {
    "профиль": "profile", "кабинет": "account_id", "account": "account_id", "act": "account_id",
    "страница": "page_id", "page": "page_id", "instagram": "instagram_id", "инстаграм": "instagram_id",
    "кампания": "campaign", "цель": "objective", "бюджет_кампании": "campaign_budget",
    "адсет": "adset", "группа": "adset", "бюджет": "daily_budget", "budget": "daily_budget",
    "гео": "countries", "страны": "countries", "country": "countries",
    "возраст_от": "age_min", "возраст_до": "age_max", "пол": "gender",
    "интересы": "interests", "аудитории": "custom_audiences", "исключить": "excluded_audiences",
    "плейсменты": "placements", "пиксель": "pixel_id", "pixel": "pixel_id", "событие": "event",
    "старт": "start_time", "объявление": "ad_name", "ad": "ad_name", "креатив": "media",
    "текст": "primary_text", "text": "primary_text", "заголовок": "headline", "описание": "description",
    "ссылка": "link", "url": "link", "кнопка": "cta", "utm": "url_tags",
}


@dataclass
class AdRow:
    row: int
    profile: str
    account_id: str
    page_id: str
    campaign: str
    adset: str
    media: str
    primary_text: str
    link: str
    instagram_id: str = ""
    objective: str = "traffic"
    campaign_budget: float | None = None
    daily_budget: float | None = None
    countries: list = field(default_factory=lambda: ["UA"])
    cities: list = field(default_factory=list)
    age_min: int = 18
    age_max: int = 65
    gender: str = "all"
    interests: list = field(default_factory=list)
    custom_audiences: list = field(default_factory=list)
    excluded_audiences: list = field(default_factory=list)
    advantage_audience: int = 0
    placements: list = field(default_factory=list)  # пусто = автоплейсменты
    pixel_id: str = ""
    event: str = ""
    optimization: str = ""
    bid_strategy: str = "lowest"
    bid_amount: float | None = None
    start_time: str = ""
    ad_name: str = ""
    headline: str = ""
    description: str = ""
    cta: str = "LEARN_MORE"
    url_tags: str = ""

    @property
    def media_type(self) -> str:
        ext = Path(self.media.split("?")[0]).suffix.lower()
        return "video" if ext in VIDEO_EXT else "image"

    @property
    def campaign_key(self) -> str:
        return f"{self.profile}|{self.account_id}|{self.campaign}"

    @property
    def adset_key(self) -> str:
        return f"{self.campaign_key}|{self.adset}"

    @property
    def ad_key(self) -> str:
        return f"{self.adset_key}|{self.ad_name}"


class BriefError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("Ошибки в брифе:\n" + "\n".join(f"  - {e}" for e in errors))


def _sheet_csv_url(url: str) -> str:
    m = re.search(r"/spreadsheets/d/([\w-]+)", url)
    if not m:
        return url
    gid = re.search(r"[#&?]gid=(\d+)", url)
    return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv" + (f"&gid={gid.group(1)}" if gid else "")


def read_rows(source: str) -> list[dict]:
    if source.startswith("http"):
        r = requests.get(_sheet_csv_url(source), timeout=60)
        r.raise_for_status()
        if "text/html" in r.headers.get("content-type", ""):
            raise BriefError(["Google Sheets вернул HTML: открой доступ 'Все, у кого есть ссылка' (просмотр)"])
        text = r.content.decode("utf-8-sig")
    else:
        text = Path(source).read_text(encoding="utf-8-sig")
    sample = text[:4096]
    delim = ";" if sample.count(";") > sample.count(",") else ","
    rows = []
    for raw in csv.DictReader(io.StringIO(text), delimiter=delim):
        row = {}
        for k, v in raw.items():
            if k is None:
                continue
            key = k.strip().lower().replace(" ", "_")
            row[ALIASES.get(key, key)] = (v or "").strip()
        if any(row.values()):
            rows.append(row)
    return rows


def _split(v: str) -> list[str]:
    return [x.strip() for x in re.split(r"[,;|\n]", v or "") if x.strip()]


def _num(v: str, kind=float):
    if v in ("", None):
        return None
    return kind(str(v).replace(" ", "").replace(",", "."))


def parse(source: str, base_dir: Path | None = None) -> list[AdRow]:
    raw_rows = read_rows(source)
    base_dir = base_dir or (Path(source).parent if not source.startswith("http") else Path.cwd())
    ads, errors = [], []
    if not raw_rows:
        raise BriefError(["Бриф пустой"])

    for i, r in enumerate(raw_rows, start=2):  # 2 = первая строка данных в таблице
        miss = [k for k in REQUIRED if not r.get(k)]
        if miss:
            errors.append(f"строка {i}: не заполнено {', '.join(miss)}")
            continue
        try:
            objective = (r.get("objective") or "traffic").lower()
            if objective not in OBJECTIVES:
                raise ValueError(f"цель '{objective}', допустимо: {', '.join(OBJECTIVES)}")
            media = r["media"]
            if not media.startswith("http"):
                p = Path(media)
                if not p.is_absolute():
                    p = base_dir / p
                if not p.exists():
                    raise ValueError(f"файл креатива не найден: {p}")
                media = str(p)
            placements = [x.lower() for x in _split(r.get("placements", ""))]
            if placements == ["auto"]:
                placements = []
            bad = [x for x in placements if x not in PLACEMENTS]
            if bad:
                raise ValueError(f"плейсменты {bad}, допустимо: auto или {', '.join(PLACEMENTS)}")
            cta = (r.get("cta") or "LEARN_MORE").upper()
            if cta not in CTA:
                raise ValueError(f"кнопка '{cta}', допустимо: {', '.join(sorted(CTA))}")
            gender = (r.get("gender") or "all").lower()
            gender = {"м": "male", "ж": "female", "муж": "male", "жен": "female", "все": "all"}.get(gender, gender)
            if gender not in ("all", "male", "female"):
                raise ValueError(f"пол '{gender}': all/male/female")
            ad = AdRow(
                row=i,
                profile=r["profile"],
                account_id=r["account_id"].replace("act_", ""),
                page_id=r["page_id"],
                instagram_id=r.get("instagram_id", ""),
                campaign=r["campaign"],
                objective=objective,
                campaign_budget=_num(r.get("campaign_budget")),
                adset=r["adset"],
                daily_budget=_num(r.get("daily_budget")),
                countries=[c.upper() for c in _split(r.get("countries", ""))] or ["UA"],
                cities=_split(r.get("cities", "")),
                age_min=_num(r.get("age_min"), int) or 18,
                age_max=_num(r.get("age_max"), int) or 65,
                gender=gender,
                interests=_split(r.get("interests", "")),
                custom_audiences=_split(r.get("custom_audiences", "")),
                excluded_audiences=_split(r.get("excluded_audiences", "")),
                advantage_audience=_num(r.get("advantage_audience"), int) or 0,
                placements=placements,
                pixel_id=r.get("pixel_id", ""),
                event=(r.get("event") or "").upper(),
                optimization=(r.get("optimization") or "").upper(),
                bid_strategy=(r.get("bid_strategy") or "lowest").lower(),
                bid_amount=_num(r.get("bid_amount")),
                start_time=r.get("start_time", ""),
                ad_name=r.get("ad_name") or f"{Path(media).stem[:40]} r{i}",
                media=media,
                primary_text=r["primary_text"].replace("\\n", "\n"),
                headline=r.get("headline", ""),
                description=r.get("description", ""),
                link=r["link"],
                cta=cta,
                url_tags=r.get("url_tags", ""),
            )
            if not 13 <= ad.age_min <= ad.age_max <= 65:
                raise ValueError("возраст: 13 <= age_min <= age_max <= 65")
            if ad.advantage_audience and ad.age_max < 65:
                raise ValueError("Advantage+ аудитория (advantage_audience=1) требует age_max=65")
            if OBJECTIVES[objective][2] and not ad.pixel_id:
                raise ValueError(f"для цели {objective} нужен pixel_id")
            if ad.bid_strategy not in ("lowest", "cost_cap", "bid_cap"):
                raise ValueError("bid_strategy: lowest / cost_cap / bid_cap")
            if ad.bid_strategy != "lowest" and not ad.bid_amount:
                raise ValueError("для cost_cap/bid_cap нужен bid_amount")
            if not ad.link.startswith("http"):
                raise ValueError("link должен начинаться с http")
            ads.append(ad)
        except (ValueError, TypeError) as e:
            errors.append(f"строка {i}: {e}")

    # согласованность: бюджет либо на кампании (CBO), либо на каждом адсете (ABO)
    by_campaign: dict[str, list[AdRow]] = {}
    for a in ads:
        by_campaign.setdefault(a.campaign_key, []).append(a)
    for key, rows in by_campaign.items():
        cbo = {a.campaign_budget for a in rows if a.campaign_budget}
        objectives = {a.objective for a in rows}
        if len(objectives) > 1:
            errors.append(f"кампания '{rows[0].campaign}': разные цели в строках {[a.row for a in rows]}")
        if len(cbo) > 1:
            errors.append(f"кампания '{rows[0].campaign}': разные campaign_budget")
        if not cbo:
            no_budget = [a.row for a in rows if not a.daily_budget]
            if no_budget:
                errors.append(f"кампания '{rows[0].campaign}': нет ни campaign_budget, ни daily_budget (строки {no_budget})")
        names = [a.ad_key for a in rows]
        dups = {n for n in names if names.count(n) > 1}
        if dups:
            errors.append(f"кампания '{rows[0].campaign}': повторяются ad_name {[d.split('|')[-1] for d in dups]}")

    if errors:
        raise BriefError(errors)
    return ads
