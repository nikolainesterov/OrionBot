"""
Handles everything related to Amazon: extracting ASINs from URLs and
fetching current title/price for a product.

Fetch strategy (in order):
  1. Apify (https://apify.com) — if APIFY_API_TOKEN is set in the environment,
     this is used first. Apify runs the request through rotating residential
     proxies with headless Chrome and built-in CAPTCHA solving, which is why
     it works reliably from cloud servers where direct scraping is blocked.
     The free plan ($5/month, no card) covers ~hundreds of lookups/month —
     far more than a personal bot needs.

  2. Direct HTML scraping — fallback used when APIFY_API_TOKEN is not set or
     when Apify fails. This is inherently fragile from cloud IPs because
     Amazon actively blocks automated requests from datacenter IP ranges, but
     it's kept as a fallback so the bot degrades gracefully rather than
     failing completely.
"""

import os
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
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-CA,en-US;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    # These Chromium-specific headers are what a real Chrome browser sends.
    # Amazon uses their presence (and consistency with the User-Agent) as
    # part of its bot-detection fingerprinting.
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

# Mobile User-Agent as a fallback — Amazon sometimes returns a simpler page
# structure for mobile clients that is easier to parse.
MOBILE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
        "Mobile/15E148 Safari/604.1"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-CA,en-US;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

REQUEST_TIMEOUT = 15

# Apify actor for Amazon scraping. If this specific actor is ever deprecated,
# find a replacement at https://apify.com/store?search=amazon+product
# The actor ID uses ~ instead of / in API URLs.
APIFY_ACTOR_ID = "dtrungtin~amazon-scraper"
# We tell Apify to cap the actor run at 60s, and wait up to 90s total
# (60s run + network overhead). gunicorn's --timeout in render.yaml must
# be longer than this so the worker isn't killed mid-request.
APIFY_RUN_TIMEOUT_S = 60
APIFY_REQUEST_TIMEOUT_S = 90

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


def _normalize_url(url: str) -> str:
    """Add https:// if the URL has no scheme — handles bare links like a.co/d/xxx."""
    url = url.strip()
    if url and "://" not in url:
        url = "https://" + url
    return url


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
    Accepts bare URLs without https:// (e.g. a.co/d/xxx or amazon.com/dp/xxx).
    """
    if not url:
        return None
    url = _normalize_url(url)
    if not _looks_like_amazon(url):
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


def _fetch_via_apify(asin: str, domain: str, api_token: str) -> dict:
    """
    Fetch product info via the Apify Amazon scraper Actor.
    Returns the same dict shape as fetch_product_info on success.
    Raises AmazonFetchError on any failure so the caller can fall back.
    """
    product_url = f"https://www.{domain}/dp/{asin}"

    try:
        resp = requests.post(
            f"https://api.apify.com/v2/acts/{APIFY_ACTOR_ID}"
            f"/run-sync-get-dataset-items",
            json={
                "startUrls": [{"url": product_url}],
                "maxItems": 1,
                "proxyConfiguration": {"useApifyProxy": True},
            },
            headers={"Authorization": f"Bearer {api_token}"},
            params={"memory": 128, "timeout": APIFY_RUN_TIMEOUT_S},
            timeout=APIFY_REQUEST_TIMEOUT_S,
        )
    except requests.RequestException as exc:
        raise AmazonFetchError(f"Apify network error: {exc}") from exc

    if resp.status_code == 401:
        raise AmazonFetchError(
            "Apify API token is invalid — check APIFY_API_TOKEN in your environment."
        )
    if resp.status_code != 200:
        raise AmazonFetchError(
            f"Apify returned HTTP {resp.status_code}: {resp.text[:200]}"
        )

    try:
        items = resp.json()
    except ValueError as exc:
        raise AmazonFetchError(f"Apify returned invalid JSON: {exc}") from exc

    if not items:
        raise AmazonFetchError(
            "Apify found no data for this product — the URL may be invalid "
            "or the product unavailable."
        )

    item = items[0]

    # Try several field names since different actor versions vary.
    title = (
        item.get("title")
        or item.get("name")
        or item.get("productTitle")
        or item.get("product_title")
    )
    if not title:
        raise AmazonFetchError(
            "Apify returned a result but with no product title. "
            "The actor output format may have changed — check the Apify Store "
            f"for actor '{APIFY_ACTOR_ID}'."
        )

    # Price can be a float/int or a string like "$19.99" / "CA$29.99".
    price_value, currency = None, "$"
    raw_price = (
        item.get("price")
        or item.get("currentPrice")
        or item.get("salePrice")
        or item.get("priceWithCurrency")
    )
    if isinstance(raw_price, (int, float)):
        price_value = round(float(raw_price), 2)
    elif isinstance(raw_price, str) and raw_price:
        price_value, currency = _parse_price_text(raw_price)

    # Currency can be a symbol ("$", "CA$") or an ISO code ("USD", "CAD").
    raw_currency = (
        item.get("currency")
        or item.get("currencyCode")
        or item.get("currencySymbol")
    )
    if raw_currency:
        currency = raw_currency

    return {
        "title": title.strip(),
        "price": price_value,
        "currency": currency or "$",
        "url": product_url,
    }


def fetch_product_info(asin: str, domain: str = "amazon.com") -> dict:
    """
    Fetch the current title and price for a product.
    Returns {"title": str, "price": float|None, "currency": str, "url": str}.
    Raises AmazonFetchError if all methods fail.

    Tries Apify first if APIFY_API_TOKEN is set, then falls back to direct
    HTML scraping. If Apify is configured but fails, that error is surfaced
    rather than the misleading direct-scraping error.
    """
    apify_token = os.environ.get("APIFY_API_TOKEN", "").strip()
    apify_error = None

    if apify_token:
        try:
            return _fetch_via_apify(asin, domain, apify_token)
        except AmazonFetchError as exc:
            # Save the real Apify error — if direct scraping also fails we'll
            # raise this so the user sees the actual problem, not a misleading
            # "set APIFY_API_TOKEN" message when the token IS set.
            apify_error = exc

    # --- Direct scraping fallback ---
    canonical_url = f"https://www.{domain}/dp/{asin}"
    attempts = [
        (canonical_url,                 HEADERS),
        (f"{canonical_url}?th=1&psc=1", HEADERS),
        (canonical_url,                 MOBILE_HEADERS),
    ]

    session = requests.Session()
    for url, headers in attempts:
        try:
            resp = session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        except requests.RequestException:
            polite_delay(1)
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        if _is_blocked_page(soup, resp.status_code):
            polite_delay(1)
            continue

        title, price_value, currency = _extract_title_and_price(soup)
        if not title:
            polite_delay(1)
            continue

        return {
            "title": title,
            "price": price_value,
            "currency": currency or "$",
            "url": canonical_url,
        }

    # Everything failed. If Apify was configured, its error is more useful.
    if apify_error:
        raise AmazonFetchError(f"Apify error: {apify_error}")
    raise AmazonFetchError(
        "Couldn't fetch the product. Set APIFY_API_TOKEN to enable "
        "reliable scraping through Apify — see the README."
    )


def polite_delay(seconds: float = 2.0):
    """Small delay between consecutive Amazon requests in batch jobs."""
    time.sleep(seconds)
