# Dolphin Autolaunch - автозалив Meta Ads через профили Dolphin{anty}

Бриф в Google Sheets/CSV -> профили Dolphin -> кампании, адсеты, креативы, объявления в нужных кабинетах.
Всё создаётся **в PAUSED**, включение - отдельной командой после проверки.

Работает двумя способами:
- **CLI** - `python -m dolphin_ads ...` (или `run.bat ...` на Windows);
- **MCP для Claude Desktop/Code** - пишешь в чат "залей бриф X", Claude сам вызывает инструменты.

## Как это устроено

```
Бриф (CSV / Google Sheets)
   -> валидация + предохранители бюджета
   -> Dolphin Local API: старт профиля (automation=1)
   -> Playwright по CDP -> Ads Manager -> access token из сессии
   -> Graph API через fetch ВНУТРИ профиля (тот же IP/прокси/отпечаток)
   -> campaigns -> adsets -> adimages/advideos -> adcreatives -> ads (PAUSED)
   -> runs/<run>.state.json + .log + .results.csv
```

Повторный запуск того же брифа **продолжает с места ошибки**: уже созданное не дублируется.

## Установка (Windows/Mac, на ПК где стоит Dolphin)

```bash
cd dolphin-autolaunch
pip install -r requirements.txt
copy config.example.json config.json     # mac: cp
```

`playwright install` **не нужен**: подключаемся к браузеру Dolphin.

В `config.json` впиши `dolphin_token`: панель Dolphin -> API -> создать токен.

## Порядок работы

```bash
# 1. профили
python -m dolphin_ads profiles

# 2. что есть в профиле: кабинеты (status 1 = активен), страницы, IG, пиксели
python -m dolphin_ads inspect --profile Shop_UA

# 3. ID интересов / городов для брифа
python -m dolphin_ads search --profile Shop_UA --q "фитнес"
python -m dolphin_ads search --profile Shop_UA --q "Днепр" --type city

# 4. проверка брифа без браузера
python -m dolphin_ads validate examples/brief.csv

# 5. прогон без создания: стартует профили, проверяет кабинеты/пиксели/страницы
python -m dolphin_ads launch examples/brief.csv --dry-run

# 6. залив (всё в PAUSED)
python -m dolphin_ads launch examples/brief.csv --run drop_oct16

# 7. проверил в Ads Manager -> включил (нужно "allow_activate": true в config.json)
python -m dolphin_ads activate --run drop_oct16 --yes

# или залить и сразу включить одной командой (тоже нужен allow_activate: true)
python -m dolphin_ads launch examples/brief.csv --run drop_oct16 --activate

# 8. статистика (CSV сохраняется в runs/)
python -m dolphin_ads stats --run drop_oct16 --level ad --date today --result purchase

# 9. автоотключение: без покупок при spend >= 300, CPA > 250, CTR < 0.8%
python -m dolphin_ads rules --run drop_oct16 --max-spend-no-result 300 --max-cpa 250 --min-ctr 0.8
python -m dolphin_ads rules --run drop_oct16 --max-spend-no-result 300 --apply

# стоп-кран
python -m dolphin_ads pause --run drop_oct16
```

Google Sheets вместо CSV: открой доступ "Все, у кого есть ссылка - читатель" и передай ссылку целиком
(`launch "https://docs.google.com/spreadsheets/d/.../edit#gid=0"`). Пути к креативам тогда - абсолютные или URL.

## Бриф

Одна строка = одно объявление. Одинаковые `profile + account_id + campaign` -> одна кампания,
одинаковый `adset` внутри неё -> один адсет. Шаблон: `examples/brief_template.csv`.
Разделитель `,` или `;`, русские заголовки тоже понимаются (профиль, кабинет, бюджет, текст, ссылка...).

| Колонка | Обяз. | Пример | Комментарий |
|---|---|---|---|
| profile | да | `Shop_UA` / `412345` | имя или ID профиля Dolphin |
| account_id | да | `1234567890` | без `act_` |
| page_id | да | `1122334455` | фан-страница |
| instagram_id | | `1784...` | IG-аккаунт (из `inspect`) |
| campaign | да | `Traffic UA` | |
| objective | | `traffic` | traffic / sales / leads / awareness / engagement |
| campaign_budget | | `20` | заполнено = CBO, бюджет на кампанию |
| adset | да | `Women 25-45` | |
| daily_budget | * | `5` | бюджет адсета (ABO), если нет campaign_budget |
| countries | | `UA,PL` | по умолчанию UA |
| cities | | `2421836:25` | key города (из `search --type city`) : радиус км. Заменяет countries |
| age_min / age_max | | `25` / `45` | 13-65 |
| gender | | `female` | all / male / female |
| interests | | `6003139266461,600310...` | ID интересов (из `search`) |
| custom_audiences / excluded_audiences | | `238...` | ID аудиторий |
| advantage_audience | | `0` | 1 = Advantage+ аудитория |
| placements | | `ig_feed;ig_reels` | пусто/auto = автоплейсменты. fb_feed, fb_reels, fb_stories, fb_marketplace, fb_video_feeds, fb_search, ig_feed, ig_stories, ig_reels, ig_explore, ig_profile_feed, an, messenger |
| pixel_id | ** | `987654321` | обязателен для sales/leads |
| event | | `PURCHASE` | PURCHASE, LEAD, ADD_TO_CART, INITIATED_CHECKOUT, COMPLETE_REGISTRATION ... |
| optimization | | `LANDING_PAGE_VIEWS` | переопределить цель оптимизации |
| bid_strategy / bid_amount | | `cost_cap` / `150` | lowest / cost_cap / bid_cap |
| start_time | | `2026-10-20T08:00:00+0300` | |
| ad_name | | `ad_1` | уникально в адсете |
| media | да | `creatives/b1.jpg` / URL | картинка или видео (по расширению). Путь относительно брифа |
| primary_text | да | | `\n` = перенос строки |
| headline / description | | | |
| link | да | `https://site.com` | |
| cta | | `SHOP_NOW` | LEARN_MORE, SHOP_NOW, ORDER_NOW, BUY_NOW, SIGN_UP, CONTACT_US, GET_OFFER, SUBSCRIBE, SEND_MESSAGE, WHATSAPP_MESSAGE, NO_BUTTON ... |
| url_tags | | `utm_source=facebook&utm_content={{ad.name}}` | UTM на уровне креатива |

## Предохранители

- Всё создаётся в **PAUSED**. Включение - `activate --yes` или `launch --activate`, и только при `"allow_activate": true`.
- При `--activate` включается только то, что создалось целиком: объявления -> адсеты -> кампании (кампания последней, чтобы не крутилось полуготовое). Упавшие строки не включаются; повторный запуск досоздаёт и включает их.
- `max_daily_budget` - потолок на один адсет/кампанию, `max_total_daily_budget` - на весь залив. Проверяется до старта браузера.
- Неактивный кабинет (status != 1) - профиль пропускается, остальные идут дальше.
- Rate limit (коды 4, 17, 613, 80004 ...) - повтор с бэкоффом. Протух токен (190) - перезагрузка Ads Manager и повтор.
- Одна картинка/видео грузится в кабинет один раз и переиспользуется.
- Всё логируется в `runs/<run>.log`, результат по строкам - `runs/<run>.results.csv`.

## Подключение к Claude Desktop (MCP)

Settings -> Developer -> Edit Config, добавь (путь свой):

```json
{
  "mcpServers": {
    "dolphin-ads": {
      "command": "python",
      "args": ["C:/Users/you/dolphin-autolaunch/mcp_server.py"]
    }
  }
}
```

Перезапусти Claude Desktop. Инструменты: `list_profiles`, `inspect_profile`, `search_targeting`, `validate_brief`,
`write_brief`, `launch_brief`, `run_state`, `get_stats`, `activate_run`, `pause_run`, `apply_rules`, `graph_get`.

Примеры запросов в чат:
- "Покажи кабинеты профиля Shop_UA"
- "Собери бриф: профиль Shop_UA, кабинет 123, 3 адсета по интересам фитнес/йога/бег, по 5 грн, креативы из C:/creo, сначала dry run"
- "Покажи статистику drop_oct16 по объявлениям за сегодня и предложи, что выключить"

Claude не включит рекламу без твоего явного "да" (`activate_run` требует `confirm=True` + `allow_activate`).

## Тесты

```bash
pip install pytest
python -m pytest -q tests
```

e2e-тест поднимает фейковый Dolphin + фейковый Graph API и настоящий Chromium (`CHROME_PATH`):
залив 4 объявлений в 2 профиля (картинки, видео, CBO/ABO), ошибка и докачка, ретраи, протухший токен,
статистика, включение, правила, CLI и MCP.

## Ограничения и риски

- Токен берётся из сессии Ads Manager, а не из своего app - это **серая зона по ToS Meta**. Тестируй на кабинетах, которые не жалко.
- Если Meta поменяет, где лежит токен в Ads Manager (`window.__accessToken`), - поправить `TOKEN_JS` в `dolphin_ads/session.py`.
- Meta периодически меняет обязательные поля API. Ошибка придёт с текстом `error_user_msg` в лог и results.csv.
- Lead-формы (instant forms), каталоги/DPA и карусели пока не поддерживаются.
