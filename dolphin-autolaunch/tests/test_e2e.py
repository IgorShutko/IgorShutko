import json
import struct
import sys
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dolphin_ads import ops  # noqa: E402
from dolphin_ads.brief import BriefError, parse  # noqa: E402
from dolphin_ads.config import load_config  # noqa: E402
from dolphin_ads.launcher import adset_params, budget_check, campaign_params, launch  # noqa: E402
from fake_env import FakeEnv  # noqa: E402


def png_bytes():
    raw = b"\x00\xff\x00\x00"
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


HEADER = ("profile,account_id,page_id,instagram_id,campaign,objective,campaign_budget,adset,daily_budget,countries,"
          "age_min,age_max,gender,interests,placements,pixel_id,event,ad_name,media,primary_text,headline,link,cta,url_tags")

ROWS = [
    # ABO, трафик, 2 объявления в одном адсете (одна картинка дважды -> один upload)
    "Shop_UA,111,900,,Traffic UA,traffic,,Women 25-45,10,UA,25,45,female,6003139266461,ig_feed;ig_reels,,,ad1,img.png,Текст 1,Заголовок,https://site.com,SHOP_NOW,utm_source=fb",
    "Shop_UA,111,900,,Traffic UA,traffic,,Women 25-45,10,UA,25,45,female,6003139266461,ig_feed;ig_reels,,,ad2,img.png,Текст 2,Заголовок,https://site.com,LEARN_MORE,",
    # CBO, продажи, видео
    "Shop_UA,111,900,555,Sales UA,sales,20,Broad,,UA,18,65,all,,auto,777,PURCHASE,vid1,clip.mp4,Видео текст,Купи,https://site.com/p,SHOP_NOW,",
    # второй профиль по ID
    "102,222,900,,Traffic PL,traffic,,All,15,PL,18,65,all,,,,,pl1,img.png,Tekst,Nagłówek,https://site.pl,LEARN_MORE,",
]


@pytest.fixture
def env(tmp_path):
    with FakeEnv() as e:
        (tmp_path / "img.png").write_bytes(png_bytes())
        (tmp_path / "clip.mp4").write_bytes(b"\x00" * 2048)
        (tmp_path / "config.json").write_text(json.dumps(e.config_dict(str(tmp_path / "runs"))))
        yield e, tmp_path


def write_brief(tmp_path, rows, name="brief.csv"):
    p = tmp_path / name
    p.write_text(HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return str(p)


# ---------- бриф ----------

def test_brief_parse_and_validation(tmp_path):
    (tmp_path / "img.png").write_bytes(png_bytes())
    (tmp_path / "clip.mp4").write_bytes(b"0")
    ads = parse(write_brief(tmp_path, ROWS))
    assert len(ads) == 4
    assert ads[0].placements == ["ig_feed", "ig_reels"] and ads[0].gender == "female"
    assert ads[2].media_type == "video" and ads[2].campaign_budget == 20 and ads[2].placements == []

    bad = [
        "Shop_UA,111,900,,C,sales,,S,10,UA,18,65,all,,,,,x,img.png,t,h,https://s.com,LEARN_MORE,",   # sales без пикселя
        "Shop_UA,111,900,,C2,traffic,,S,10,UA,18,65,all,,ig_tv,,,x,img.png,t,h,https://s.com,LEARN_MORE,",  # плейсмент
        "Shop_UA,111,900,,C3,traffic,,S,,UA,18,65,all,,,,,x,img.png,t,h,https://s.com,LEARN_MORE,",  # нет бюджета
        "Shop_UA,111,900,,C4,traffic,,S,10,UA,18,65,all,,,,,x,missing.png,t,h,https://s.com,LEARN_MORE,",  # нет файла
    ]
    with pytest.raises(BriefError) as e:
        parse(write_brief(tmp_path, bad, "bad.csv"))
    text = str(e.value)
    assert "pixel_id" in text and "ig_tv" in text and "нет ни campaign_budget" in text and "не найден" in text


def test_russian_headers_and_semicolon(tmp_path):
    (tmp_path / "img.png").write_bytes(png_bytes())
    p = tmp_path / "ru.csv"
    p.write_text("профиль;кабинет;страница;кампания;адсет;бюджет;креатив;текст;ссылка;гео\n"
                 "Shop_UA;act_111;900;К1;А1;7,5;img.png;Привет;https://s.com;UA,PL\n", encoding="utf-8")
    ad = parse(str(p))[0]
    assert ad.account_id == "111" and ad.daily_budget == 7.5 and ad.countries == ["UA", "PL"]


def test_params_and_budget_guard(tmp_path):
    (tmp_path / "img.png").write_bytes(png_bytes())
    (tmp_path / "clip.mp4").write_bytes(b"0")
    ads = parse(write_brief(tmp_path, ROWS))
    c = campaign_params(ads[0], "UAH")
    assert c["status"] == "PAUSED" and c["is_adset_budget_sharing_enabled"] is False and "daily_budget" not in c
    s = adset_params(ads[0], "c1", "UAH")
    assert s["daily_budget"] == 1000 and s["targeting"]["genders"] == [2]
    assert s["targeting"]["publisher_platforms"] == ["instagram"]
    assert s["targeting"]["instagram_positions"] == ["stream", "reels"]
    cbo = campaign_params(ads[2], "UAH")
    assert cbo["daily_budget"] == 2000 and cbo["bid_strategy"] == "LOWEST_COST_WITHOUT_CAP"
    s2 = adset_params(ads[2], "c2", "UAH")
    assert "daily_budget" not in s2 and s2["promoted_object"] == {"pixel_id": "777", "custom_event_type": "PURCHASE"}
    assert campaign_params(ads[2], "JPY")["daily_budget"] == 20

    cfg = load_config("/nonexistent")
    cfg.max_daily_budget, cfg.max_total_daily_budget = 12, 40
    errs = budget_check(ads, cfg)
    assert any("Sales UA" in e for e in errs) and any("PL" not in e for e in errs)


# ---------- e2e ----------

def test_full_launch_resume_and_ops(env):
    fake, tmp = env
    cfg = load_config(str(tmp / "config.json"))
    brief = write_brief(tmp, ROWS)

    # dry run: ничего не создаёт
    launch(cfg, brief, "t1", dry_run=True)
    assert not fake.graph_calls("POST")

    # падение креатива на 2-й строке (не ретраится: code 100)
    fake.fail("POST", r"adcreatives$", {"message": "Invalid parameter", "code": 100, "error_user_msg": "bad link"}, times=1)
    fake.fail("POST", r"act_111/campaigns$", {"message": "rate", "code": 17}, times=1)  # ретраится
    cfg.retry_backoff = 0
    run = launch(cfg, brief, "t1")

    rows = run.state["rows"]
    assert rows["2"]["status"] == "error" and "bad link" in rows["2"]["error"]
    assert sum(1 for r in rows.values() if r["status"] == "created") == 3

    posts = fake.graph_calls("POST")
    camps = [c for c in posts if c["path"].endswith("/campaigns")]
    assert len(camps) == 4  # 1 ретрай + 3 кампании
    assert all(c["params"]["status"] == "PAUSED" for c in posts if "status" in c["params"])
    # картинка на кабинет 111 загружена один раз, на 222 - отдельно
    imgs = [c for c in posts if c["path"].endswith("/adimages")]
    assert len(imgs) == 2 and imgs[0]["params"]["filename"]["__file__"] == "img.png"
    vids = [c for c in posts if c["path"].endswith("/advideos")]
    assert len(vids) == 1 and vids[0]["params"]["source"]["size"] == 2048
    cr = [json.loads(c["params"]["object_story_spec"]) for c in posts if c["path"].endswith("/adcreatives")]
    video_cr = next(x for x in cr if "video_data" in x)
    assert video_cr["instagram_user_id"] == "555" and video_cr["video_data"]["image_url"] == "https://cdn/thumb.jpg"
    adset_post = next(c for c in posts if c["path"].endswith("/adsets"))
    t = json.loads(adset_post["params"]["targeting"])
    assert t["flexible_spec"][0]["interests"][0]["id"] == "6003139266461"
    # профиль по имени резолвится через remote API, оба профиля стартовали и остановились
    assert any(c["path"] == "/v1.0/browser_profiles/101/start" for c in fake.calls)
    assert any(c["path"] == "/v1.0/browser_profiles/102/stop" for c in fake.calls)

    # повтор: досоздаёт только упавшую строку, без дублей кампаний/адсетов/картинок
    n_before = len(fake.graph_calls("POST"))
    run = launch(cfg, brief, "t1")
    new = fake.graph_calls("POST")[n_before:]
    assert [c["path"].rsplit("/", 1)[-1] for c in new] == ["adcreatives", "ads"]
    assert all(r["status"] == "created" for r in run.state["rows"].values())
    assert Path(run.results_path).read_text(encoding="utf-8-sig").count("created") == 4

    # третий запуск - ничего не делает и даже не стартует профили
    n_calls = len(fake.calls)
    launch(cfg, brief, "t1")
    assert not [c for c in fake.calls[n_calls:] if "start" in c["path"]]

    # статистика
    st = ops.stats(cfg, "t1", level="ad", date_preset="today", result="purchase")
    good = next(r for r in st if r["ad"] == "good ad")
    assert good["cpa"] == 50.0 and good["roas"] == 3.1

    # включение запрещено без allow_activate
    with pytest.raises(PermissionError):
        ops.set_status(cfg, "t1", "ACTIVE")
    cfg.allow_activate = True
    n_before = len(fake.graph_calls("POST"))
    changed = ops.set_status(cfg, "t1", "ACTIVE")
    acts = fake.graph_calls("POST")[n_before:]
    assert changed == 4 + 3 + 3  # ads + adsets + campaigns
    assert all(c["params"]["status"] == "ACTIVE" for c in acts)
    # кампании включаются последними в рамках профиля
    paths = [c["path"].rsplit("/", 1)[-1] for c in acts]
    assert paths[:3] == ["a15", "s12", "c11"]  # профиль 102: ad -> adset -> campaign
    assert [p[0] for p in paths[3:]] == ["a"] * 3 + ["s"] * 2 + ["c"] * 2

    assert ops.set_status(cfg, "t1", "PAUSED") == 3

    # правила: превью и применение
    acts = ops.apply_rules(cfg, "t1", result="purchase", max_spend_no_result=250, dry_run=True)
    assert {a["ad_id"] for a in acts} == {"a_bad"}
    n_before = len(fake.graph_calls("POST"))
    ops.apply_rules(cfg, "t1", result="purchase", max_cpa=40, dry_run=False)
    paused = [c["path"] for c in fake.graph_calls("POST")[n_before:]]
    assert paused and all(p.endswith("a_good") for p in paused)  # CPA 50 > 40


def test_inactive_account_and_token_refresh(env):
    fake, tmp = env
    cfg = load_config(str(tmp / "config.json"))
    rows = ["Shop_UA,333,900,,Banned,traffic,,S,10,UA,18,65,all,,,,,b1,img.png,t,h,https://s.com,LEARN_MORE,"]
    run = launch(cfg, write_brief(tmp, rows, "banned.csv"), "t2")
    assert run.state["rows"]["2"]["status"] == "error" and "не активен" in run.state["rows"]["2"]["error"]
    assert not fake.graph_calls("POST")

    # протухший токен -> перезагрузка Ads Manager и повтор
    fake.fail("GET", r"me/adaccounts", {"message": "Session expired", "code": 190}, times=1)
    info = ops.inspect_profile(cfg, "Shop_UA")
    assert {a["account_id"] for a in info["accounts"]} == {"111", "222", "333"}
    assert info["pixels"]["111"][0]["id"] == "777"
    assert sum(1 for c in fake.calls if c["path"] == "/adsmanager") >= 2


def test_cli_and_mcp(env, capsys):
    fake, tmp = env
    import os
    os.environ["DOLPHIN_ADS_CONFIG"] = str(tmp / "config.json")
    try:
        from dolphin_ads.__main__ import main
        brief = write_brief(tmp, ROWS[:1])
        assert main(["validate", brief]) == 0
        assert main(["profiles"]) == 0
        assert "Shop_UA" in capsys.readouterr().out
        assert main(["launch", brief, "--run", "cli"]) == 0
        assert main(["activate", "--run", "cli"]) == 1  # без --yes

        import anyio
        import mcp_server
        res = anyio.run(mcp_server.validate_brief, brief)
        assert res["ads"] == 1 and res["budget_errors"] == []
        res = anyio.run(mcp_server.inspect_profile, "Shop_UA")
        assert len(res["accounts"]) == 3
        res = anyio.run(mcp_server.activate_run, "cli")
        assert "подтверждение" in res["error"]
        res = anyio.run(mcp_server.run_state, "cli")
        assert len(res["ads"]) == 1
    finally:
        os.environ.pop("DOLPHIN_ADS_CONFIG", None)


def test_launch_with_activate(env):
    fake, tmp = env
    cfg = load_config(str(tmp / "config.json"))
    brief = write_brief(tmp, ROWS[:2] + ROWS[3:], "act.csv")  # UA: 1 кампания/1 адсет/2 ad; PL: 1/1/1

    with pytest.raises(PermissionError):
        launch(cfg, brief, "a1", activate=True)
    assert not fake.graph_calls("POST")

    cfg.allow_activate = True
    fake.fail("POST", r"act_222/ads$", {"message": "Invalid", "code": 100}, times=1)  # PL объявление упадёт
    run = launch(cfg, brief, "a1", activate=True)
    acts = [c for c in fake.graph_calls("POST") if c["params"].get("status") == "ACTIVE"]
    kinds = [c["path"].rsplit("/", 1)[-1][0] for c in acts]
    assert kinds == ["a", "a", "s", "c"]  # только UA: объявления -> адсет -> кампания; PL не включён
    assert len(run.state["active"]) == 4

    # повтор: досоздаёт PL и включает только его, UA повторно не трогает
    n = len(fake.graph_calls("POST"))
    launch(cfg, brief, "a1", activate=True)
    new = fake.graph_calls("POST")[n:]
    assert [c["path"].rsplit("/", 1)[-1] for c in new][0] == "ads"
    assert [c["path"].rsplit("/", 1)[-1][0] for c in new if c["params"].get("status") == "ACTIVE"] == ["a", "s", "c"]

    # третий запуск - ничего
    n = len(fake.calls)
    launch(cfg, brief, "a1", activate=True)
    assert not [c for c in fake.calls[n:] if "start" in c["path"]]


def test_probe_picks_working_token(env, capsys):
    fake, tmp = env
    cfg = load_config(str(tmp / "config.json"))
    fake.decoy_first = True
    d = ops.diagnose(cfg, "Shop_UA")
    assert d["ok"] and len(d["report"]) == 3  # decoy x 2 режима кук -> провал, затем валидный
    assert [r["ok"] for r in d["report"]] == [False, False, True]
    info = ops.inspect_profile(cfg, "Shop_UA")
    assert len(info["accounts"]) == 3

    import os
    os.environ["DOLPHIN_ADS_CONFIG"] = str(tmp / "config.json")
    try:
        from dolphin_ads.__main__ import main
        assert main(["diag", "--profile", "Shop_UA"]) == 0
        assert "ИТОГ: работает" in capsys.readouterr().out
    finally:
        os.environ.pop("DOLPHIN_ADS_CONFIG", None)
