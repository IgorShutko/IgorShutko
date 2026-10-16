// Код для блока "Выполнение" (Execution) в конструкторе сценариев Dolphin{anty}.
// Сценарий: Старт -> Перейти по URL (https://adsmanager.facebook.com/adsmanager/manage/campaigns)
//           -> Ожидание 8-10 сек -> Выполнение (этот код).
// Всё создаётся в PAUSED. Режим DRY_RUN = true только читает кабинеты и страницы.

(async () => {
  const CFG = {
    DRY_RUN: true,               // false - создать кампанию
    GRAPH: 'v23.0',
    ACCOUNT: '',                 // ID кабинета без act_
    PAGE: '',                    // ID фан-страницы
    LINK: 'https://example.com',
    IMAGE_URL: '',               // прямая ссылка на jpg/png креатива
    BUDGET: 5,                   // дневной бюджет в валюте кабинета
    MAX_BUDGET: 10,              // предохранитель
    COUNTRY: 'UA',
    TEXT: 'Тестовое объявление',
    HEADLINE: 'Тест',
  };

  const log = [];
  const say = (m) => { log.push(m); console.log('[ads]', m); };
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  const token = window.__accessToken
    || (document.documentElement.innerHTML.match(/EAA[A-Za-z0-9]{30,}/) || [])[0];
  if (!token) { alert('Токен не найден: Ads Manager не прогрузился или чекпоинт'); return; }

  async function g(path, method = 'GET', params = {}) {
    const p = { ...params, access_token: token };
    for (const k in p) if (typeof p[k] === 'object') p[k] = JSON.stringify(p[k]);
    const url = `https://graph.facebook.com/${CFG.GRAPH}/${path}`;
    let res;
    if (method === 'GET') {
      res = await fetch(url + '?' + new URLSearchParams(p), { credentials: 'include' });
    } else {
      const body = new FormData();
      for (const k in p) body.append(k, p[k]);
      res = await fetch(url, { method, body, credentials: 'include' });
    }
    const j = await res.json();
    await sleep(1500);
    if (j.error) throw new Error(`${method} ${path}: ${j.error.message} | ${j.error.error_user_msg || ''} | code=${j.error.code}/${j.error.error_subcode}`);
    return j;
  }

  try {
    const accs = (await g('me/adaccounts', 'GET', { fields: 'account_id,name,account_status,currency', limit: 50 })).data;
    const pages = (await g('me/accounts', 'GET', { fields: 'id,name', limit: 50 })).data;
    accs.forEach((a) => say(`Кабинет ${a.account_id} ${a.name} status=${a.account_status} ${a.currency}`));
    pages.forEach((p) => say(`Страница ${p.id} ${p.name}`));

    if (CFG.DRY_RUN) { alert('DRY RUN\n\n' + log.join('\n')); return; }

    if (CFG.BUDGET > CFG.MAX_BUDGET) throw new Error('Бюджет выше предохранителя');
    const acc = accs.find((a) => a.account_id === CFG.ACCOUNT);
    if (!acc) throw new Error('Кабинет не найден');
    if (acc.account_status !== 1) throw new Error(`Кабинет не активен: ${acc.account_status}`);

    const act = `act_${CFG.ACCOUNT}`;
    const ts = new Date().toISOString().slice(0, 16);

    const camp = await g(`${act}/campaigns`, 'POST', {
      name: `TEST ${ts}`, objective: 'OUTCOME_TRAFFIC', status: 'PAUSED',
      special_ad_categories: [], is_adset_budget_sharing_enabled: 'false',
    });
    say(`Кампания ${camp.id}`);

    const adset = await g(`${act}/adsets`, 'POST', {
      name: `TEST adset ${ts}`, campaign_id: camp.id,
      daily_budget: Math.round(CFG.BUDGET * 100),
      billing_event: 'IMPRESSIONS', optimization_goal: 'LINK_CLICKS',
      bid_strategy: 'LOWEST_COST_WITHOUT_CAP',
      targeting: {
        geo_locations: { countries: [CFG.COUNTRY] }, age_min: 18, age_max: 65,
        targeting_automation: { advantage_audience: 0 },
      },
      status: 'PAUSED',
    });
    say(`Адсет ${adset.id}`);

    const blob = await (await fetch(CFG.IMAGE_URL)).blob();
    const b64 = await new Promise((r) => { const fr = new FileReader(); fr.onload = () => r(fr.result.split(',')[1]); fr.readAsDataURL(blob); });
    const img = await g(`${act}/adimages`, 'POST', { bytes: b64 });
    const hash = Object.values(img.images)[0].hash;
    say(`Картинка ${hash}`);

    const cr = await g(`${act}/adcreatives`, 'POST', {
      name: `TEST creative ${ts}`,
      object_story_spec: {
        page_id: CFG.PAGE,
        link_data: {
          link: CFG.LINK, message: CFG.TEXT, name: CFG.HEADLINE, image_hash: hash,
          call_to_action: { type: 'LEARN_MORE', value: { link: CFG.LINK } },
        },
      },
    });
    say(`Креатив ${cr.id}`);

    const ad = await g(`${act}/ads`, 'POST', {
      name: `TEST ad ${ts}`, adset_id: adset.id, creative: { creative_id: cr.id }, status: 'PAUSED',
    });
    say(`Объявление ${ad.id}`);

    alert('ГОТОВО, всё в PAUSED\n\n' + log.join('\n'));
  } catch (e) {
    alert('ОШИБКА\n\n' + e.message + '\n\n' + log.join('\n'));
  }
})();
