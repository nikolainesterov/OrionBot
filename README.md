# Amazon Price Tracker — Telegram Bot

A personal Telegram bot that tracks Amazon product prices and messages you
when they drop. Built with Python (Flask), deployed on Render via webhooks,
with a free Neon Postgres database for storage and a free GitHub Actions
cron job to trigger the daily price check.

## Commands

| Command | What it does |
|---|---|
| `/add <amazon link>` | Start tracking a product |
| `/list` | Show everything you're tracking, with current prices |
| `/check <number or ASIN>` | Look up a product's price right now |
| `/remove <number or ASIN>` | Stop tracking a product |
| `/help` | Show the command list |

`<number>` refers to the position shown in `/list` (e.g. `/remove 2`). You
can also use the Amazon ASIN directly (the 10-character code in the URL,
e.g. `/remove B08N5WRWNW`).

Every day, a GitHub Actions workflow pings the bot, which re-checks every
tracked product. If a price has dropped since the last check, you get a
message. This also "wakes up" the free Render instance if it had spun down.

## How it's built

- **`app.py`** — Flask app with three things: a `/webhook` route Telegram
  calls on every message, a `/check-prices` route the daily cron job calls,
  and a couple of `/admin/*` helper routes.
- **`bot/amazon.py`** — turns an Amazon link into an ASIN and scrapes the
  product page for title/price.
- **`bot/db.py`** — Postgres data access (tracked products per chat).
- **`bot/telegram.py`** — thin wrapper around the Telegram Bot API.
- **`bot/handlers.py`** — command parsing and the reply logic for each command.
- **`.github/workflows/daily-price-check.yml`** — calls `/check-prices` once a day.
- **`.python-version`** — pins Render's Python version to 3.12. Without this,
  Render uses whatever its current default is for new services, which can
  outpace prebuilt wheels for some dependencies (this is what caused the
  `psycopg2` import crash some deploys hit — its binary wheels lag behind
  brand-new Python releases). If you ever bump this, make sure
  `psycopg2-binary` in `requirements.txt` has a matching wheel on PyPI first.

## Important limitations — please read before relying on this

**Amazon scraping is inherently fragile.** There's no free official API for
this kind of casual price lookup, so this bot reads the public product page's
HTML directly. This means:
- Amazon may show a CAPTCHA instead of the real page, especially from
  cloud-hosting IPs like Render's. The bot detects this and tells you it
  failed rather than guessing — but on a bad day, checks may simply fail.
- Amazon changes its page markup periodically, which can break price/title
  detection until you update the CSS selectors in `bot/amazon.py`.
- Scraping product pages may be against Amazon's Terms of Service. This is
  intended for light personal use (one user, a handful of products, one
  check a day) — not for high-frequency or large-scale use.

**Free-tier data persistence.** Render's free web service has an *ephemeral*
filesystem — anything written to disk (like a SQLite file) is wiped on every
restart or redeploy. That's why this bot uses Postgres (hosted on
[Neon](https://neon.tech)) instead of SQLite. Neon's free tier doesn't
expire and gives you 0.5 GB storage and 100 compute-hours/month, far more
than tracking a handful of products needs. The database "scales to zero"
after 5 minutes of inactivity and wakes up in under a second on the next
query — you won't notice this happening.

**Free-tier cold starts.** A free Render web service spins down after 15
minutes of no traffic. The first message you send after it's been idle will
take 30-60 seconds to get a reply while it spins back up — that's normal,
not a bug.

---

## Setup

### 1. Create your Telegram bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram, send `/newbot`,
   and follow the prompts.
2. Save the token it gives you (looks like `123456789:AA...`).
3. Message [@userinfobot](https://t.me/userinfobot) to get your own numeric
   Telegram user ID. Save it — this restricts the bot to only respond to you.

### 2. Create your free Neon database

1. Sign up at [neon.tech](https://neon.tech) (no credit card required) and
   create a new project — any name/region is fine.
2. On the project's Dashboard tab, copy the **Connection string**. It looks
   like:
   `postgresql://user:password@ep-xxxx-xxxx.region.aws.neon.tech/neondb?sslmode=require`
3. Save it — you'll paste it into Render as `DATABASE_URL` in a moment.

This is a permanent free database (no 30-day expiry like Render's own
Postgres). You don't need to do anything else here; `app.py` creates the
`products` table automatically the first time it runs.

### 3. Push this code to your own GitHub repo

```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/<your-username>/<your-repo>.git
git push -u origin main
```

### 4. Deploy to Render

**Option A — Blueprint (recommended):**

Before deploying, generate three secrets now by running this in your
terminal (each command gives you one value to copy):

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"   # TELEGRAM_WEBHOOK_SECRET
python3 -c "import secrets; print(secrets.token_hex(32))"   # CRON_SECRET
python3 -c "import secrets; print(secrets.token_hex(32))"   # ADMIN_SECRET
```

`token_hex` produces hex strings (only `0-9` and `a-f`) which are safe
everywhere: in URLs, in Telegram's API, and in environment variables.
**Do not use base64 or random-password generators** for these — many produce
`/`, `+`, and `=` characters that break Telegram's webhook API.

**Option A — Blueprint (recommended):**

1. In the [Render Dashboard](https://dashboard.render.com), click
   **New > Blueprint** and select your GitHub repo. Render will read
   `render.yaml` and propose a single free web service.
2. When prompted, fill in all five environment variables:
   - `TELEGRAM_BOT_TOKEN` — from BotFather
   - `TELEGRAM_USER_ID` — your numeric Telegram ID
   - `DATABASE_URL` — the Neon connection string from step 2
   - `TELEGRAM_WEBHOOK_SECRET` — the first hex string you generated above
   - `CRON_SECRET` — the second hex string
   - `ADMIN_SECRET` — the third hex string
3. Click **Apply**.

**Option B — Manual setup:**

1. **New > Web Service**, connect your repo.
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn app:app`
   - Plan: Free
2. Under **Environment**, add all six variables listed in Option A step 2.
3. Deploy.

### 5. Register the webhook with Telegram

Once your service is live (you'll have a URL like
`https://amazon-price-bot.onrender.com`), tell Telegram where to send
messages by visiting, in your browser:

```
https://<your-app>.onrender.com/admin/set-webhook?token=<your ADMIN_SECRET>
```

You should see a JSON response with `"ok": true`. You only need to do this
once (and again if your Render URL ever changes).

To double check it worked:

```
https://<your-app>.onrender.com/admin/webhook-info?token=<your ADMIN_SECRET>
```

**If a page just spins forever ("Application loading...") and never
responds:** that's not a normal free-tier cold start (those resolve in well
under a minute). It almost always means `DATABASE_URL` is wrong or
unreachable — check it first with:

```
https://<your-app>.onrender.com/admin/db-check?token=<your ADMIN_SECRET>
```

This endpoint tries a real database connection with a 10-second timeout and
returns a clear error message instead of hanging — e.g. wrong host, wrong
password, or missing `sslmode=require`. Compare whatever it reports against
the connection string on your Neon project's Dashboard tab. (`/admin/set-webhook`
and `/admin/webhook-info` never touch the database at all, so they should
respond quickly regardless of whether your database is configured
correctly — if those also hang, the problem is elsewhere, e.g. the service
itself failing to start; check the Render logs.)

### 6. Set up the daily check (GitHub Actions)

1. In your GitHub repo, go to **Settings > Secrets and variables > Actions**.
2. Add two repository secrets:
   - `RENDER_APP_URL` — e.g. `https://amazon-price-bot.onrender.com`
   - `CRON_SECRET` — the same value you set on Render
3. That's it — `.github/workflows/daily-price-check.yml` is already in the
   repo and will run automatically once a day. You can also trigger it
   manually from the **Actions** tab (**Run workflow**) to test it
   immediately rather than waiting for the schedule.

### 7. Test it

Message your bot on Telegram: `/start`, then `/add` with a real Amazon
product link. If the first message takes a minute to reply, that's the free
instance spinning up — totally normal.

---

## Local development

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in real values
python3 -m dotenv run -- python3 app.py
```

You won't get real Telegram messages locally unless you expose your machine
with something like `ngrok` and point `setWebhook` at that URL — for casual
testing it's usually easier to just deploy to Render and iterate there.

Run the offline test suite (no network or database needed):

```bash
pip install pytest
pytest
```

## Adjusting the daily check time

Edit the `cron:` line in `.github/workflows/daily-price-check.yml`. It uses
standard cron syntax in UTC, e.g. `0 13 * * *` runs at 13:00 UTC daily.
