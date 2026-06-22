"""Thin wrapper around the Telegram Bot HTTP API."""

import os

import requests

TELEGRAM_API_BASE = "https://api.telegram.org"
REQUEST_TIMEOUT = 10


def _token():
    return os.environ["TELEGRAM_BOT_TOKEN"]


def _api_url(method):
    return f"{TELEGRAM_API_BASE}/bot{_token()}/{method}"


def send_message(chat_id, text, disable_web_page_preview=True):
    try:
        requests.post(
            _api_url("sendMessage"),
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": disable_web_page_preview,
            },
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException:
        # Best-effort notification; don't let a Telegram hiccup crash a
        # batch job that's checking many products.
        pass


def set_webhook(url, secret_token):
    resp = requests.post(
        _api_url("setWebhook"),
        json={
            "url": url,
            "secret_token": secret_token,
            "allowed_updates": ["message"],
        },
        timeout=REQUEST_TIMEOUT,
    )
    return resp.json()


def delete_webhook():
    resp = requests.post(_api_url("deleteWebhook"), timeout=REQUEST_TIMEOUT)
    return resp.json()


def get_webhook_info():
    resp = requests.get(_api_url("getWebhookInfo"), timeout=REQUEST_TIMEOUT)
    return resp.json()


# The list of commands shown in Telegram's command menu (the / button next
# to the message input). Keep these in sync with the handlers in handlers.py.
BOT_COMMANDS = [
    {"command": "add",    "description": "Track a new Amazon product (paste a link)"},
    {"command": "list",   "description": "Show all your tracked products"},
    {"command": "check",  "description": "Check a product's price right now"},
    {"command": "remove", "description": "Stop tracking a product"},
    {"command": "help",   "description": "Show all commands"},
]


def set_my_commands():
    resp = requests.post(
        _api_url("setMyCommands"),
        json={"commands": BOT_COMMANDS},
        timeout=REQUEST_TIMEOUT,
    )
    return resp.json()
