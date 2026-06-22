"""
Flask app with two jobs:

1. POST /webhook — Telegram sends every message here. Render's free web
   service spins up on request, so a Telegram message effectively "wakes
   the bot up". Protected by the Telegram secret-token header (set via
   set_webhook.py), not by anything in the URL.

2. GET/POST /check-prices — re-checks every tracked product and DMs anyone
   whose price dropped. Nothing on Render's free tier calls this on a
   schedule for free, so a GitHub Actions workflow
   (.github/workflows/daily-price-check.yml) hits this endpoint once a day.
   Protected by a shared-secret query parameter (CRON_SECRET).

Also exposes a couple of small admin helpers (/admin/set-webhook,
/admin/webhook-info) so you don't need to write a separate script to
register the webhook with Telegram after each deploy.
"""

import os
import time

from flask import Flask, jsonify, request

from bot import db, telegram
from bot.amazon import AmazonFetchError, fetch_product_info, polite_delay
from bot.handlers import handle_update

app = Flask(__name__)

TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
CRON_SECRET = os.environ.get("CRON_SECRET", "")
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "")
ALLOWED_CHAT_ID = os.environ.get("TELEGRAM_USER_ID")  # optional but recommended

_db_ready = False


def ensure_db():
    global _db_ready
    if not _db_ready:
        db.init_db()
        _db_ready = True


# NOTE: deliberately not a @app.before_request hook. That used to run on
# every single request — including /admin/* routes that don't touch the
# database — so a bad DATABASE_URL would hang admin/health requests too.
# Instead, only the two routes that actually need the database call this.


@app.get("/")
def health():
    return jsonify(status="ok", service="amazon-price-bot")


@app.post("/webhook")
def webhook():
    # Telegram includes this header on every webhook request when a
    # secret_token was set via setWebhook — reject anything else.
    incoming_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not TELEGRAM_WEBHOOK_SECRET or incoming_secret != TELEGRAM_WEBHOOK_SECRET:
        return jsonify(error="unauthorized"), 401

    ensure_db()
    update = request.get_json(silent=True) or {}
    reply_text = handle_update(update, allowed_chat_id=ALLOWED_CHAT_ID)

    if reply_text:
        message = update.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        if chat_id is not None:
            telegram.send_message(chat_id, reply_text)

    return jsonify(ok=True)


@app.route("/check-prices", methods=["GET", "POST"])
def check_prices():
    if not CRON_SECRET or request.args.get("token") != CRON_SECRET:
        return jsonify(error="unauthorized"), 401

    ensure_db()
    products = db.get_all_products()
    checked, dropped, failed = 0, 0, 0

    for product in products:
        try:
            info = fetch_product_info(product["asin"], product["domain"])
        except AmazonFetchError:
            db.touch_checked(product["id"])
            failed += 1
            polite_delay()
            continue

        checked += 1
        new_price = info["price"]
        old_price = product["last_price"]

        if (
            new_price is not None
            and old_price is not None
            and float(new_price) < float(old_price)
        ):
            dropped += 1
            telegram.send_message(
                product["chat_id"],
                (
                    f"📉 Price drop! <b>{info['title']}</b>\n"
                    f"{info['currency']}{old_price} → {info['currency']}{new_price}\n"
                    f"{product['url']}"
                ),
            )

        if new_price is not None:
            db.update_price(product["id"], new_price)
        else:
            db.touch_checked(product["id"])

        polite_delay()  # be gentle with Amazon between requests

    return jsonify(
        total=len(products), checked=checked, dropped=dropped, failed=failed
    )


@app.get("/admin/db-check")
def admin_db_check():
    if not ADMIN_SECRET or request.args.get("token") != ADMIN_SECRET:
        return jsonify(error="unauthorized"), 401

    try:
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return jsonify(ok=True, message="Database connection succeeded.")
    except Exception as exc:  # noqa: BLE001 — surface any DB error to the caller
        return jsonify(ok=False, error=str(exc)), 500


@app.get("/admin/set-webhook")
def admin_set_webhook():
    if not ADMIN_SECRET or request.args.get("token") != ADMIN_SECRET:
        return jsonify(error="unauthorized"), 401

    base_url = os.environ.get("RENDER_EXTERNAL_URL") or request.url_root.rstrip("/")
    webhook_url = f"{base_url}/webhook"
    webhook_result = telegram.set_webhook(webhook_url, TELEGRAM_WEBHOOK_SECRET)
    commands_result = telegram.set_my_commands()
    return jsonify(
        webhook_url=webhook_url,
        webhook=webhook_result,
        commands=commands_result,
    )


@app.get("/admin/webhook-info")
def admin_webhook_info():
    if not ADMIN_SECRET or request.args.get("token") != ADMIN_SECRET:
        return jsonify(error="unauthorized"), 401
    return jsonify(telegram.get_webhook_info())


if __name__ == "__main__":
    # Local development only. In production, Render runs this with gunicorn
    # (see the start command in render.yaml / README).
    ensure_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), debug=True)
