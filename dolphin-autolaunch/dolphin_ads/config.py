"""Настройки: config.json рядом с проектом + переменные окружения поверх."""

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    # Dolphin
    dolphin_token: str = ""                                   # JWT из панели Dolphin -> API
    local_api: str = "http://localhost:3001/v1.0"
    remote_api: str = "https://dolphin-anty-api.com"
    # Meta
    graph_base: str = "https://graph.facebook.com"
    graph_version: str = "v23.0"
    ads_manager_url: str = "https://adsmanager.facebook.com/adsmanager/manage/campaigns"
    # Предохранители
    max_daily_budget: float = 50.0       # на один адсет/кампанию, в валюте кабинета
    max_total_daily_budget: float = 300.0  # на весь запуск
    allow_activate: bool = False         # без true команда activate не сработает
    # Поведение
    request_delay: float = 1.5           # пауза между запросами к Graph, сек
    retry_backoff: float = 5.0           # база экспоненциального бэкоффа при rate limit, сек
    token_wait: int = 60                 # сколько ждать токен в Ads Manager, сек
    stop_profile_after: bool = True      # закрывать профиль после работы
    runs_dir: str = "runs"
    extra: dict = field(default_factory=dict)

    @property
    def graph(self) -> str:
        return f"{self.graph_base.rstrip('/')}/{self.graph_version}"

    @property
    def runs_path(self) -> Path:
        p = Path(self.runs_dir)
        if not p.is_absolute():
            p = ROOT / p
        p.mkdir(parents=True, exist_ok=True)
        return p


def load_config(path: str | None = None) -> Config:
    cfg = Config()
    path = path or os.getenv("DOLPHIN_ADS_CONFIG") or str(ROOT / "config.json")
    if Path(path).exists():
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        known = {f.name for f in fields(Config)}
        for k, v in data.items():
            if k in known:
                setattr(cfg, k, v)
            else:
                cfg.extra[k] = v
    env_map = {
        "DOLPHIN_TOKEN": "dolphin_token",
        "DOLPHIN_LOCAL_API": "local_api",
        "GRAPH_VERSION": "graph_version",
        "GRAPH_BASE": "graph_base",
        "ADS_MANAGER_URL": "ads_manager_url",
    }
    for env, attr in env_map.items():
        if os.getenv(env):
            setattr(cfg, attr, os.getenv(env))
    return cfg
