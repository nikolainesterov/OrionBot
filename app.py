"""
Flask app with two jobs:

1. POST /webhook — Telegram sends every message here. The handler returns
   200 immediately for every request. Commands that call Apify (/add, /check)
   are dispatched to a background thread so the webhook never times out —
   Apify takes ~40 seconds and gunicorn's worker timeout would kill a
   synchronous handler long before that.

2. GET/POST /check-prices — re-checks every tracked product and DMs anyone
   whose price dropped. A GitHub Actions workflow hits this once a day.
   Protected by a shared-secret query parameter (CRON_SECRET).
"""

import os
import threading
import time

from flask import Flask, jsonify, request

from bot import db, telegram
from bot.amazon import AmazonFetchError, fetch_product_info, polite_delay
from bot.handlers import handle_update

app = Flask(__name__)

TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
CRON_SECRET = os.environ.get("CRON_SECRET", "")
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "")
ALLOWED_CHAT_ID = os.environ.get("TELEGRAM_USER_ID")

_db_ready = False


def ensure_db():
    global _db_ready
    if not _db_ready:
        db.init_db()
        _db_ready = True


def _run_in_background(chat_id, update):
    """
    Called in a daemon thread for slow commands (/add, /check).
    Runs handle_update and sends the result when it's ready — regardless of
    how long Apify takes. The webhook handler has already returned 200 by
    the time this runs.
    """
    try:
        reply_text = handle_update(update, allowed_chat_id=ALLOWED_CHAT_ID)
        if reply_text and chat_id is not None:
            telegram.send_message(chat_id, reply_text)
    except Exception:
        # Best effort — if something crashes in the thread, don't let it
        # propagate silently. Could add logging here in future.
        pass


@app.get("/")
def health():
    return jsonify(status="ok", service="orion-bot")


@app.post("/webhook")
def webhook():
    incoming_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not TELEGRAM_WEBHOOK_SECRET or incoming_secret != TELEGRAM_WEBHOOK_SECRET:
        return jsonify(error="unauthorized"), 401

    ensure_db()
    update = request.get_json(silent=True) or {}
    message = update.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    text = (message.get("text") or "").strip()

    # Detect slow commands (/add and /check with an argument both call Apify
    # and take ~40 seconds). For these, send an immediate "please wait" ack
    # and dispatch the real work to a background thread, then return 200 right
    # away. This prevents gunicorn worker timeouts and Telegram's retry loop.
    if text and chat_id:
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower().lstrip("/").split("@")[0]
        has_arg = len(parts) > 1

        if cmd in ("add", "check") and has_arg:
            telegram.send_message(chat_id, "⏳ Checking Amazon, please wait...")
            thread = threading.Thread(
                target=_run_in_background,
                args=(chat_id, update),
                daemon=True,
            )
            thread.start()
            return jsonify(ok=True)  # Return immediately — thread handles the rest

    # Fast commands (/list, /remove, /help, /start, and bare /add or /check
    # with no argument) are handled synchronously — they don't call Apify.
    reply_text = handle_update(update, allowed_chat_id=ALLOWED_CHAT_ID)
    if reply_text and chat_id is not None:
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


@app.get("/admin/apify-check")
def admin_apify_check():
    if not ADMIN_SECRET or request.args.get("token") != ADMIN_SECRET:
        return jsonify(error="unauthorized"), 401

    apify_token = os.environ.get("APIFY_API_TOKEN", "").strip()
    if not apify_token:
        return jsonify(ok=False, error="APIFY_API_TOKEN is not set in environment."), 400

    # Test with a well-known, always-available product (Echo Dot on amazon.com).
    test_asin = "B08N5WRWNW"
    test_domain = "amazon.com"
    try:
        from bot.amazon import _fetch_via_apify
        result = _fetch_via_apify(test_asin, test_domain, apify_token)
        return jsonify(ok=True, result=result)
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 500


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
