# Copy this to .env for local development (never commit the real .env file).

# From @BotFather on Telegram after creating your bot.
TELEGRAM_BOT_TOKEN=123456789:AAExampleTokenFromBotFather

# Your personal numeric Telegram user ID (get it from @userinfobot).
# When set, only messages from this user ID get a response.
TELEGRAM_USER_ID=123456789

# Any long random string — used to verify that webhook requests really
# come from Telegram. IMPORTANT: Telegram only allows letters, numbers,
# hyphens and underscores. Generate one safely with:
#   python3 -c "import secrets; print(secrets.token_hex(32))"
# (token_hex uses only 0-9 and a-f, so it's always safe)
TELEGRAM_WEBHOOK_SECRET=change-me-to-a-random-hex-string

# Any long random string — used to verify that /check-prices requests
# really come from your GitHub Actions workflow. Same character rules apply.
#   python3 -c "import secrets; print(secrets.token_hex(32))"
CRON_SECRET=change-me-to-a-random-hex-string

# Any long random string — used to protect the /admin/* helper endpoints.
# Same character rules apply.
#   python3 -c "import secrets; print(secrets.token_hex(32))"
ADMIN_SECRET=change-me-to-a-random-hex-string

# Postgres connection string from your Neon project (neon.tech).
# Find it on your Neon project's Dashboard tab — copy the "Connection string"
# for the branch you want to use. It looks like:
# postgresql://user:password@ep-xxxx-xxxx.region.aws.neon.tech/neondb?sslmode=require
DATABASE_URL=postgresql://user:password@ep-example-123.us-east-2.aws.neon.tech/neondb?sslmode=require
