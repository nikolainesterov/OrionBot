"""
Handles everything related to talking to Amazon: turning a product URL into
an ASIN, and fetching the current title/price for that ASIN.

Amazon does not offer a free public API for this kind of casual price
lookup, so this module scrapes the product page's HTML instead. That means
it is inherently fragile:
  - Amazon frequently changes its page markup.
  - Amazon actively tries to detect and block automated requests, especially
    from cloud-hosting IP ranges (which is exactly what Render uses). You may
    see this work fine for a while and then start failing as Amazon's
    detection updates change, or simply because Render's IP range is flagged.
  - Amazon may show a CAPTCHA ("Sorry, we just need to make sure you're not
    a robot") instead of the real page. This module detects that case and
    raises a clear error rather than silently returning garbage.

If scraping stops working, the fix is almost always to update the CSS
selectors in PRICE_SELECTORS / TITLE_SELECTORS below to match Amazon's
current markup (inspect the page in your browser's dev tools).
"""

import re
import time
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

# Amazon shows different markup to different requests, so we try several
# selectors in order and use the first one that matches.
PRICE_SELECTORS = [
    "span.a-price span.a-offscreen",
    "#corePrice_feature_div span.a-offscreen",
    "#corePriceDisplay_desktop_feature_div span.a-offscreen",
    "#priceblock_ourprice",
    "#priceblock_dealprice",
    "#tp_price_block_total_price_ww span.a-offscreen",
]

TITLE_SELECTORS = [
    "#productTitle",
    "#title span#productTitle",
]

# A realistic browser User-Agent. Amazon is much more likely to block
# requests that look like they come from a script (e.g. "python-requests").
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# Mobile User-Agent as a fallback — Amazon sometimes returns a simpler page
# structure for mobile clients that is easier to parse.
MOBILE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
        "Mobile/15E148 Safari/604.1"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

REQUEST_TIMEOUT = 15

ASIN_PATTERNS = [
    re.compile(r"/dp/([A-Z0-9]{10})"),
    re.compile(r"/gp/product/([A-Z0-9]{10})"),
    re.compile(r"/gp/aw/d/([A-Z0-9]{10})"),
    re.compile(r"/product/([A-Z0-9]{10})"),
    re.compile(r"[?&]asin=([A-Z0-9]{10})", re.IGNORECASE),
]

# Hosts that are short-link redirectors and need to be resolved first.
SHORT_LINK_HOSTS = {"amzn.to", "a.co", "amzn.eu", "amzn.asia"}


class AmazonFetchError(Exception):
    """Raised when we can't get a clean product page from Amazon."""


def _looks_like_amazon(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return "amazon." in host or host in SHORT_LINK_HOSTS


def resolve_short_link(url: str) -> str:
    """Follow redirects for shortened Amazon links (amzn.to, a.co, ...)."""
    host = urlparse(url).netloc.lower()
    if host not in SHORT_LINK_HOSTS:
        return url
    resp = requests.get(
        url, headers=HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True
    )
    return resp.url


def extract_asin(url: str):
    """
    Given an Amazon product URL (full or shortened), return (asin, domain).
    Returns None if the URL doesn't look like an Amazon product link.
    """
    if not url or not _looks_like_amazon(url):
        return None

    try:
        url = resolve_short_link(url)
    except requests.RequestException:
        # If we can't resolve the short link we simply can't extract an ASIN.
        return None

    parsed = urlparse(url)
    domain = parsed.netloc.lower().replace("www.", "")

    for pattern in ASIN_PATTERNS:
        match = pattern.search(parsed.path) or pattern.search(url)
        if match:
            return match.group(1).upper(), domain

    return None


def _parse_price_text(text: str):
    """Turn '$19.99', '£1,234.56', 'CDN$ 19.99' etc into (float, currency_symbol)."""
    if not text:
        return None, None
    text = text.strip()
    # Grab a leading run of non-digit characters as the currency symbol.
    m = re.match(r"^([^\d]*)([\d.,]+)", text)
    if not m:
        return None, None
    symbol = m.group(1).strip() or "$"
    number = m.group(2)
    # Handle both "1,234.56" and "1.234,56" style separators.
    if "," in number and "." in number:
        if number.rfind(",") > number.rfind("."):
            number = number.replace(".", "").replace(",", ".")
        else:
            number = number.replace(",", "")
    elif "," in number:
        # Could be a thousands separator (1,234) or a decimal comma (19,99).
        if len(number.split(",")[-1]) == 2:
            number = number.replace(",", ".")
        else:
            number = number.replace(",", "")
    try:
        return round(float(number), 2), symbol
    except ValueError:
        return None, None


def _is_blocked_page(soup: BeautifulSoup, status_code: int) -> bool:
    if status_code in (503, 403):
        return True
    text = soup.get_text(" ", strip=True).lower()
    blocked_markers = [
        "type the characters you see",
        "to discuss automated access to amazon data",
        "enter the characters you see below",
        "sorry, we just need to make sure you're not a robot",
        "robot check",
        "verify your identity",
        "sign in for the best experience",
    ]
    return any(marker in text for marker in blocked_markers)


def _extract_title_and_price(soup: BeautifulSoup):
    """Return (title, price, currency) from a parsed page, or (None, None, None)."""
    title_el = None
    for selector in TITLE_SELECTORS:
        title_el = soup.select_one(selector)
        if title_el:
            break
    title = title_el.get_text(strip=True) if title_el else None

    price_value, currency = None, "$"
    for selector in PRICE_SELECTORS:
        price_el = soup.select_one(selector)
        if price_el:
            price_value, currency = _parse_price_text(price_el.get_text())
            if price_value is not None:
                break

    return title, price_value, currency


def fetch_product_info(asin: str, domain: str = "amazon.com"):
    """
    Fetch the current title and price for a product.
    Returns {"title": str, "price": float|None, "currency": str, "url": str}.
    Raises AmazonFetchError if the page can't be read after all attempts.

    Tries three strategies in order, moving on if a page is blocked or
    returns no recognisable product title:
      1. Standard desktop URL + desktop User-Agent
      2. Same URL with ?th=1&psc=1 (bypasses some "select a variant" pages)
      3. Same URL with a mobile User-Agent (simpler page structure)
    """
    canonical_url = f"https://www.{domain}/dp/{asin}"

    attempts = [
        (canonical_url,                    HEADERS),
        (f"{canonical_url}?th=1&psc=1",    HEADERS),
        (canonical_url,                    MOBILE_HEADERS),
    ]

    last_error = AmazonFetchError("All fetch attempts failed.")

    for url, headers in attempts:
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            last_error = AmazonFetchError(f"Network error: {exc}")
            polite_delay(1)
            continue

        soup = BeautifulSoup(resp.text, "html.parser")

        if _is_blocked_page(soup, resp.status_code):
            last_error = AmazonFetchError(
                "Amazon is blocking requests from this server. "
                "The price will be retried on the next daily check."
            )
            polite_delay(1)
            continue

        title, price_value, currency = _extract_title_and_price(soup)

        if not title:
            last_error = AmazonFetchError(
                "Couldn't read the product page (Amazon may be blocking "
                "this server's IP, or the page layout has changed)."
            )
            polite_delay(1)
            continue

        # Success — return the canonical URL regardless of which attempt worked.
        return {
            "title": title,
            "price": price_value,
            "currency": currency or "$",
            "url": canonical_url,
        }

    raise last_error


def polite_delay(seconds: float = 2.0):
    """Small delay between consecutive Amazon requests in batch jobs."""
    time.sleep(seconds)
