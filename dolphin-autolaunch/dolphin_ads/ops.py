"""Операции над уже залитым: статистика, включение/пауза, правила автоотключения."""

import csv
from datetime import datetime

from .config import Config
from .dolphin import Dolphin
from .launcher import Run
from .session import ProfileSession

INSIGHT_FIELDS = "campaign_name,adset_name,ad_name,spend,impressions,reach,clicks,inline_link_clicks,ctr,cpc,cpm,frequency,actions,cost_per_action_type,purchase_roas"

RESULT_ACTIONS = {
    "purchase": ["offsite_conversion.fb_pixel_purchase", "purchase", "omni_purchase"],
    "lead": ["offsite_conversion.fb_pixel_lead", "lead", "onsite_conversion.lead_grouped"],
    "link_click": ["link_click"],
    "landing_page_view": ["landing_page_view"],
    "add_to_cart": ["offsite_conversion.fb_pixel_add_to_cart", "add_to_cart"],
}


def _profile_of(key: str) -> str:
    return key.split("|", 1)[0]


def _group(run: Run, kind: str) -> dict[str, dict[str, str]]:
    """{profile: {key: id}} по типу сущности."""
    out: dict[str, dict[str, str]] = {}
    for key, oid in run.state[kind].items():
        out.setdefault(_profile_of(key), {})[key] = oid
    return out


def _action(row: dict, names: list[str], field="actions") -> float:
    for a in row.get(field) or []:
        if a.get("action_type") in names:
            return float(a.get("value", 0))
    return 0.0


def stats(cfg: Config, run_name: str, level: str = "campaign", date_preset: str = "today",
          result: str = "purchase") -> list[dict]:
    run = Run(cfg, run_name)
    dolphin = Dolphin(cfg)
    names = RESULT_ACTIONS.get(result, [result])
    rows = []
    for profile, camps in _group(run, "campaigns").items():
        with ProfileSession(cfg, dolphin, profile, log=run.log) as s:
            for key, cid in camps.items():
                data = s.get_all(f"{cid}/insights", level=level, date_preset=date_preset, fields=INSIGHT_FIELDS)
                for d in data:
                    spend = float(d.get("spend", 0))
                    res = _action(d, names)
                    roas = _action(d, names, "purchase_roas") if result == "purchase" else 0
                    rows.append({
                        "profile": profile, "campaign": d.get("campaign_name"), "adset": d.get("adset_name", ""),
                        "ad": d.get("ad_name", ""), "spend": round(spend, 2), "impressions": int(d.get("impressions", 0)),
                        "clicks": int(d.get("inline_link_clicks") or d.get("clicks") or 0),
                        "ctr": round(float(d.get("ctr", 0)), 2), "cpc": round(float(d.get("cpc", 0)), 2),
                        "cpm": round(float(d.get("cpm", 0)), 2), "frequency": round(float(d.get("frequency", 0)), 2),
                        "results": res, "cpa": round(spend / res, 2) if res else None, "roas": round(roas, 2) if roas else None,
                    })
    if rows:
        path = cfg.runs_path / f"{run_name}.stats_{level}_{date_preset}_{datetime.now():%Y%m%d-%H%M}.csv"
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        run.log(f"статистика сохранена: {path}")
    return rows


def set_status(cfg: Config, run_name: str, status: str, profiles: list[str] | None = None) -> int:
    """ACTIVE: включает объявления -> адсеты -> кампании. PAUSED: ставит кампании на паузу."""
    status = status.upper()
    if status not in ("ACTIVE", "PAUSED"):
        raise ValueError("status: ACTIVE или PAUSED")
    if status == "ACTIVE" and not cfg.allow_activate:
        raise PermissionError("Включение запрещено: поставь \"allow_activate\": true в config.json")
    run = Run(cfg, run_name)
    dolphin = Dolphin(cfg)
    kinds = ["ads", "adsets", "campaigns"] if status == "ACTIVE" else ["campaigns"]
    all_profiles = {_profile_of(k) for kind in kinds for k in run.state[kind]}
    if profiles:
        all_profiles &= set(profiles)
    changed = 0
    for profile in sorted(all_profiles):
        with ProfileSession(cfg, dolphin, profile, log=run.log) as s:
            for kind in kinds:
                for key, oid in _group(run, kind).get(profile, {}).items():
                    s.post(oid, status=status)
                    changed += 1
                    run.log(f"  {kind[:-1]} {oid} -> {status}")
    return changed


def apply_rules(cfg: Config, run_name: str, result: str = "purchase", date_preset: str = "today",
                max_cpa: float | None = None, max_spend_no_result: float | None = None,
                min_ctr: float | None = None, min_impressions: int = 1000, dry_run: bool = True) -> list[dict]:
    """Правила на уровне объявлений:
       - spend >= max_spend_no_result и 0 результатов -> пауза
       - CPA > max_cpa -> пауза
       - CTR < min_ctr при показах >= min_impressions -> пауза
    """
    run = Run(cfg, run_name)
    dolphin = Dolphin(cfg)
    names = RESULT_ACTIONS.get(result, [result])
    actions = []
    for profile, camps in _group(run, "campaigns").items():
        with ProfileSession(cfg, dolphin, profile, log=run.log) as s:
            for cid in camps.values():
                for d in s.get_all(f"{cid}/insights", level="ad", date_preset=date_preset,
                                   fields="ad_id,ad_name,spend,impressions,ctr,actions"):
                    spend, res = float(d.get("spend", 0)), _action(d, names)
                    impr, ctr = int(d.get("impressions", 0)), float(d.get("ctr", 0))
                    reason = None
                    if max_spend_no_result and res == 0 and spend >= max_spend_no_result:
                        reason = f"spend {spend} без результата"
                    elif max_cpa and res and spend / res > max_cpa:
                        reason = f"CPA {spend / res:.2f} > {max_cpa}"
                    elif min_ctr and impr >= min_impressions and ctr < min_ctr:
                        reason = f"CTR {ctr:.2f}% < {min_ctr}%"
                    if not reason:
                        continue
                    actions.append({"profile": profile, "ad_id": d["ad_id"], "ad": d.get("ad_name"), "reason": reason})
                    if dry_run:
                        run.log(f"  [правило, dry] пауза {d.get('ad_name')}: {reason}")
                    else:
                        s.post(d["ad_id"], status="PAUSED")
                        run.log(f"  [правило] пауза {d.get('ad_name')}: {reason}")
    return actions


def inspect_profile(cfg: Config, profile: str) -> dict:
    """Кабинеты, страницы, пиксели профиля - для заполнения брифа."""
    dolphin = Dolphin(cfg)
    with ProfileSession(cfg, dolphin, profile) as s:
        accounts = s.accounts()
        pixels = {}
        for a in accounts:
            if a["account_status"] == 1:
                try:
                    pixels[a["account_id"]] = s.pixels(a["account_id"])
                except Exception as e:
                    pixels[a["account_id"]] = [{"error": str(e)}]
        return {"accounts": accounts, "pages": s.pages(), "pixels": pixels}


def graph_get(cfg: Config, profile: str, path: str, params: dict | None = None) -> dict:
    """Произвольный GET к Graph API из профиля (только чтение)."""
    dolphin = Dolphin(cfg)
    with ProfileSession(cfg, dolphin, profile) as s:
        return s.get(path, **(params or {}))


def search_targeting(cfg: Config, profile: str, query: str, kind: str = "adinterest") -> list[dict]:
    """Поиск интересов (adinterest) или городов (adgeolocation) для брифа."""
    dolphin = Dolphin(cfg)
    with ProfileSession(cfg, dolphin, profile) as s:
        params = {"type": kind, "q": query, "limit": 25}
        if kind == "adgeolocation":
            params["location_types"] = ["city"]
        return s.get("search", **params).get("data", [])


def diagnose(cfg: Config, profile: str) -> dict:
    """Какие токены нашлись и какие связки хост/куки работают (для отладки)."""
    dolphin = Dolphin(cfg)
    s = ProfileSession(cfg, dolphin, profile)
    try:
        s.__enter__()
        return {"ok": True, "url": s.page.url, "chosen": {"host": s.graph_base, "cookies": s.cred},
                "report": s.probe_report}
    except Exception as e:
        return {"ok": False, "error": str(e), "report": s.probe_report,
                "url": s.page.url if s.page else ""}
    finally:
        s.__exit__(None, None, None)
