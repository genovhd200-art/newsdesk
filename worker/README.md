# Cloudflare Worker за NewsDesk (безплатно)

Прави две неща: **вход с Google + синхронизация между устройства** и **реле** за източници, които блокират GitHub.
Безплатният план стига (100 000 заявки/ден за Worker, 100 000 записа/ден за базата D1).

## Пускане през сайта на Cloudflare (без инсталиране)

1. **Workers & Pages → Create → Create Worker** → име `newsdesk-api` → Deploy → **Edit code** → изтрий всичко, постави съдържанието на `worker/src/index.js` → Deploy.
2. ~~D1 база~~ — вече е създадена (`newsdesk`, id `fb476de1-91c3-4229-bd40-b6c34328f0bc`) и таблицата е готова. Пропусни тази стъпка.
3. Worker `newsdesk-api` → **Settings → Bindings → Add → D1 database** → Variable name: `DB` → избери `newsdesk` → Save.
4. Същото място → **Variables and Secrets → Add**:
   - Text `GOOGLE_CLIENT_ID` = `1020354337477-b99a8v22upo09iil85sqn0ks1ka9onn8.apps.googleusercontent.com`
   - Text `ALLOW_ORIGIN` = `https://genovhd200-art.github.io`
   - Text `RELAY_HOSTS` = `basketnews.com,bgtennis.bg,bgvolleyball.com,bloodyelbow.com,corrieredellosport.it,dailymail.com,dsport.bg,espn.com,eurohoops.net,feeds.bbci.co.uk,football-italia.net,football365.com,formula1.com,gazzetta.it,gong.bg,heavy.com,insideworldfootball.com,keep.md,marca.com,mmamania.com,motorsport.com,motorsportweek.com,news.google.com,planetf1.com,racingnews365.com,skysports.com,sportal.bg,sportbible.com,tennis365.com,tenniskafe.com,tennisworlditalia.com,tuttosport.com,volleyweek.bg`
   - **Secret** `RELAY_KEY` = дълга случайна парола (поне 32 знака, от генератор на пароли). Запиши си я, трябва ти и в GitHub.
   - (по желание) Secret `USER_SALT` = друг случаен низ.
5. Копирай адреса на Worker-а (`https://newsdesk-api.ИМЕ.workers.dev`).

## Свързване

6. **GitHub → хранилището → Settings → Secrets and variables → Actions → New repository secret**: `RELAY_URL` = адресът от стъпка 5 (без `/` накрая), `RELAY_KEY` = същата парола.
7. В хранилището отвори `config.js` → молив → `window.NEWSDESK_API = 'ТОЗИ-АДРЕС'` → Commit.
8. **Google Cloud → APIs & Services → Credentials → твоят OAuth клиент → Authorized JavaScript origins → Add URI** `https://genovhd200-art.github.io` → Save. (Без това бутонът на Google не работи.)

Проверка: отвори сайта → ☁ Вход → Влез с Google. После отвори сайта от друго устройство и влез със същия акаунт: мониторингите трябва да се появят.

## Какво прави релето
Ако източник не отговори на GitHub, сборката опитва втори път през Worker-а. Той приема само заявки с твоя `RELAY_KEY` и само за сайтовете от `RELAY_HOSTS`. Някои сайтове (напр. ESPN) блокират и Cloudflare и там релето може да не помогне.

## За разработчици
`wrangler.toml` е готов: `wrangler d1 create newsdesk` (сложи id в файла), `wrangler secret put RELAY_KEY`, `wrangler deploy`.
