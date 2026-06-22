"""
Parses incoming Telegram messages and runs the right command.

The functions here are split so the "pure" parsing logic (parse_command)
can be unit tested without needing a database or network access.
"""

from bot import db
from bot.amazon import AmazonFetchError, extract_asin, fetch_product_info

HELP_TEXT = (
    "<b>Amazon Price Tracker</b>\n\n"
    "/add &lt;amazon link&gt; — track a new product\n"
    "/list — show everything you're tracking\n"
    "/check &lt;number or ASIN&gt; — look up the current price right now\n"
    "/remove &lt;number or ASIN&gt; — stop tracking a product\n"
    "/help — show this message\n\n"
    "I'll automatically check your tracked prices once a day and message "
    "you here if any of them drop."
)


def parse_command(text: str):
    """
    Turn raw message text into (command, argument).
    '/add https://...' -> ('add', 'https://...')
    'hello'             -> (None, None)
    """
    if not text or not text.startswith("/"):
        return None, None
    parts = text.strip().split(maxsplit=1)
    command = parts[0][1:].lower().split("@")[0]  # strip /cmd@BotName
    argument = parts[1].strip() if len(parts) > 1 else ""
    return command, argument


def _format_product_line(index, product):
    price = product["last_price"]
    currency = product.get("currency") or "$"
    price_text = f"{currency}{price}" if price is not None else "price unknown"
    return f"{index}. {product['title']} — {price_text} (ASIN {product['asin']})"


def handle_start(chat_id):
    return HELP_TEXT


def handle_help(chat_id):
    return HELP_TEXT


def handle_add(chat_id, argument):
    if not argument:
        return (
            "Please send me the Amazon link you want to track:\n\n"
            "<code>/add https://www.amazon.ca/dp/XXXXXXXXXX</code>\n\n"
            "You can paste any Amazon product link — full URLs, short "
            "a.co links, and amazon.ca / amazon.com / amazon.co.uk all work."
        )

    parsed = extract_asin(argument)
    if not parsed:
        return (
            "That doesn't look like an Amazon product link. Send me a link "
            "like https://www.amazon.com/dp/XXXXXXXXXX"
        )
    asin, domain = parsed

    try:
        info = fetch_product_info(asin, domain)
    except AmazonFetchError as exc:
        # Still track it even if we couldn't get a price yet — better than
        # making the user re-paste the link. We'll pick up the price on the
        # next daily check.
        # Use the canonical Amazon URL (built from ASIN+domain), NOT the
        # original argument — which could be an a.co short link that would
        # show up as the title in /list.
        canonical_url = f"https://www.{domain}/dp/{asin}"
        added = db.add_product(
            chat_id, asin, domain, canonical_url, title=f"Product {asin}",
            price=None, currency="$",
        )
        if not added:
            return "You're already tracking that product."
        return (
            f"Added it, but I couldn't fetch the current price just now "
            f"({exc}). I'll keep trying on the next daily check."
        )

    added = db.add_product(
        chat_id, asin, domain, info["url"], info["title"],
        info["price"], info["currency"],
    )
    if not added:
        return f"You're already tracking <b>{info['title']}</b>."

    price_text = (
        f"{info['currency']}{info['price']}"
        if info["price"] is not None
        else "price unknown right now"
    )
    return (
        f"✅ Now tracking <b>{info['title']}</b>\n"
        f"Current price: {price_text}\n\n"
        f"I'll check daily and let you know if it drops."
    )


def handle_list(chat_id):
    products = db.list_products(chat_id)
    if not products:
        return "You're not tracking anything yet. Use /add <amazon link> to start."
    lines = [_format_product_line(i, p) for i, p in enumerate(products, start=1)]
    return "Your tracked products:\n\n" + "\n".join(lines)


def handle_check(chat_id, argument):
    if not argument:
        return (
            "Which product do you want to check? Use the number from /list:\n\n"
            "<code>/check 1</code>"
        )

    product = db.find_product(chat_id, argument)
    if not product:
        return "I couldn't find that in your tracked list. Try /list to see numbers."

    try:
        info = fetch_product_info(product["asin"], product["domain"])
    except AmazonFetchError as exc:
        db.touch_checked(product["id"])
        return f"Couldn't fetch the price right now: {exc}"

    db.update_price(product["id"], info["price"])
    price_text = (
        f"{info['currency']}{info['price']}"
        if info["price"] is not None
        else "price unknown"
    )
    return f"<b>{info['title']}</b>\nCurrent price: {price_text}"


def handle_remove(chat_id, argument):
    if not argument:
        return (
            "Which product do you want to remove? Use the number from /list:\n\n"
            "<code>/remove 1</code>"
        )

    product = db.find_product(chat_id, argument)
    if not product:
        return "I couldn't find that in your tracked list. Try /list to see numbers."

    db.remove_product(chat_id, product["id"])
    return f"Removed <b>{product['title']}</b> from your tracking list."


COMMANDS = {
    "start": handle_start,
    "help": handle_help,
    "list": handle_list,
}

COMMANDS_WITH_ARGS = {
    "add": handle_add,
    "check": handle_check,
    "remove": handle_remove,
}


def handle_update(update: dict, allowed_chat_id=None):
    """
    Top-level entry point: takes a raw Telegram update dict, returns the
    text to send back, or None if no reply is needed.
    """
    message = update.get("message")
    if not message or "text" not in message:
        return None

    chat_id = message["chat"]["id"]

    if allowed_chat_id is not None and str(chat_id) != str(allowed_chat_id):
        return "This is a private bot and isn't available to other users."

    command, argument = parse_command(message["text"])
    if command is None:
        return None  # not a command — stay quiet rather than echo chat

    if command in COMMANDS:
        return COMMANDS[command](chat_id)
    if command in COMMANDS_WITH_ARGS:
        return COMMANDS_WITH_ARGS[command](chat_id, argument)

    return "Unknown command. Send /help to see what I can do."
