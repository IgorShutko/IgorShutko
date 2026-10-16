"""MCP-сервер: даёт Claude (Desktop / Code) инструменты автозалива через Dolphin.

Подключение (Claude Desktop -> Settings -> Developer -> Edit config):
{
  "mcpServers": {
    "dolphin-ads": {
      "command": "python",
      "args": ["C:/path/to/dolphin-autolaunch/mcp_server.py"]
    }
  }
}
"""

import contextlib
import io
import json
import sys
from pathlib import Path

import anyio

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from dolphin_ads import ops
from dolphin_ads.brief import BriefError, parse
from dolphin_ads.config import load_config
from dolphin_ads.dolphin import Dolphin
from dolphin_ads.launcher import Run, budget_check, launch, plan

mcp = _Server(
    "dolphin-ads",
    instructions=(
        "Автозалив Meta Ads через профили Dolphin{anty}. Порядок: list_profiles -> inspect_profile -> "
        "validate_brief -> launch_brief(dry_run=True) -> launch_brief(dry_run=False). Всё создаётся в PAUSED. "
        "activate_run только после явного подтверждения пользователя в чате."
    ),
)


async def _run(fn, *args, **kw):
    """Playwright sync API нельзя звать из event loop - уводим в поток, stdout в лог (stdio занят протоколом)."""
    def call():
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = fn(*args, **kw)
        return result, buf.getvalue()
    try:
        result, out = await anyio.to_thread.run_sync(call)
    except BriefError as e:
        return {"error": str(e), "errors": e.errors}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    if out:
        print(out, file=sys.stderr)
        if isinstance(result, dict):
            result = {**result, "log": out[-4000:]}
        else:
            result = {"result": result, "log": out[-4000:]}
    return result


@mcp.tool()
async def list_profiles(query: str = "") -> dict:
    """Профили Dolphin (id, имя, статус). query - фильтр по имени."""
    cfg = load_config()
    return await _run(lambda: {"profiles": Dolphin(cfg).list_profiles(query)})


@mcp.tool()
async def inspect_profile(profile: str) -> dict:
    """Запускает профиль и возвращает его кабинеты (status 1 = активен), страницы, Instagram, пиксели."""
    return await _run(ops.inspect_profile, load_config(), profile)


@mcp.tool()
async def search_targeting(profile: str, query: str, kind: str = "interest") -> dict:
    """Поиск ID интересов (kind=interest) или городов (kind=city) для колонок interests/cities брифа."""
    k = "adinterest" if kind == "interest" else "adgeolocation"
    return await _run(lambda: {"results": ops.search_targeting(load_config(), profile, query, k)})


@mcp.tool()
async def validate_brief(brief: str) -> dict:
    """Проверяет бриф (путь к CSV или ссылка Google Sheets) без браузера: ошибки, структура, бюджеты."""
    def go():
        cfg = load_config()
        ads = parse(brief)
        return {"ads": len(ads), "plan": plan(ads), "budget_errors": budget_check(ads, cfg)}
    return await _run(go)


@mcp.tool()
async def write_brief(path: str, rows: list[dict]) -> dict:
    """Пишет бриф CSV из списка строк (ключи = колонки брифа). Удобно собирать залив прямо из чата."""
    def go():
        import csv
        cols = []
        for r in rows:
            cols += [k for k in r if k not in cols]
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
        return {"path": str(p.resolve()), "rows": len(rows)}
    return await _run(go)


@mcp.tool()
async def launch_brief(brief: str, dry_run: bool = True, run_name: str = "", profiles: list[str] | None = None,
                       activate: bool = False) -> dict:
    """Заливает бриф. dry_run=True: проверить профили/кабинеты без создания. По умолчанию всё в PAUSED.
    activate=True - сразу включить созданное (только после явного "включай" от пользователя; нужен allow_activate).
    Повторный вызов с тем же run_name продолжает с места ошибки."""
    def go():
        run = launch(load_config(), brief, run_name or None, dry_run, profiles, activate)
        return {"run": run.name, "results_csv": str(run.results_path), "rows": run.state["rows"],
                "campaigns": run.state["campaigns"], "ads_created": len(run.state["ads"])}
    return await _run(go)


@mcp.tool()
async def run_state(run_name: str) -> dict:
    """Состояние прогона: созданные кампании/адсеты/объявления и ошибки по строкам."""
    def go():
        run = Run(load_config(), run_name)
        return {k: run.state[k] for k in ("campaigns", "adsets", "ads", "rows", "meta")}
    return await _run(go)


@mcp.tool()
async def get_stats(run_name: str, level: str = "campaign", date_preset: str = "today", result: str = "purchase") -> dict:
    """Статистика прогона: spend, CTR, CPC, CPM, результаты, CPA, ROAS. level: campaign/adset/ad."""
    return await _run(lambda: {"rows": ops.stats(load_config(), run_name, level, date_preset, result)})


@mcp.tool()
async def activate_run(run_name: str, confirm: bool = False, profiles: list[str] | None = None) -> dict:
    """Включает всё из прогона. Только после явного "да, включай" от пользователя: confirm=True.
    Требует allow_activate=true в config.json."""
    if not confirm:
        return {"error": "Нужно явное подтверждение пользователя: confirm=True"}
    return await _run(lambda: {"changed": ops.set_status(load_config(), run_name, "ACTIVE", profiles)})


@mcp.tool()
async def pause_run(run_name: str, profiles: list[str] | None = None) -> dict:
    """Ставит кампании прогона на паузу."""
    return await _run(lambda: {"changed": ops.set_status(load_config(), run_name, "PAUSED", profiles)})


@mcp.tool()
async def apply_rules(run_name: str, result: str = "purchase", date_preset: str = "today",
                      max_cpa: float | None = None, max_spend_no_result: float | None = None,
                      min_ctr: float | None = None, min_impressions: int = 1000, dry_run: bool = True) -> dict:
    """Автоотключение объявлений: без результата при spend >= порога, CPA выше max_cpa, CTR ниже min_ctr.
    dry_run=True только показывает, что будет выключено."""
    return await _run(lambda: {"actions": ops.apply_rules(load_config(), run_name, result, date_preset, max_cpa,
                                                          max_spend_no_result, min_ctr, min_impressions, dry_run)})


@mcp.tool()
async def graph_get(profile: str, path: str, params_json: str = "{}") -> dict:
    """Произвольный GET к Graph API из профиля (только чтение). Пример: path='act_123/campaigns',
    params_json='{"fields":"name,status,effective_status"}'."""
    return await _run(ops.graph_get, load_config(), profile, path, json.loads(params_json or "{}"))


if __name__ == "__main__":
    mcp.run()
