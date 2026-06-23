"""
Handles Amazon URL parsing and product price/title fetching.

Fetch strategy (in order):
  1. Apify Proxy — if APIFY_API_TOKEN is set, routes our requests through
     Apify's proxy network (country-specific datacenter IPs) so Amazon
     doesn't block us as a cloud server. We use our own BeautifulSoup
     parsing, NOT an Apify actor. This avoids the 30-40s actor startup time
     and the title/price extraction bugs in third-party actors.

  2. Direct scraping — fallback with no proxy. Unreliable from cloud IPs
     (Amazon blocks them) but kept as a last resort.

Why proxy instead of actor?
  The dtrungtin~amazon-scraper actor consistently returns empty title/price
  for amazon.ca because Amazon Canada shows a postal-code prompt that hides
  product content before the actor's selectors run. Using the proxy directly
  lets us control the full request — headers, retries, parsing — and we can
  handle the location prompt ourselves.
"""

import os
import re
import time
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

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
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

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

REQUEST_TIMEOUT = 30

# Apify proxy — datacenter IPs routed by country code.
# Docs: https://docs.apify.com/platform/proxy/connection-settings
APIFY_PROXY_HOST = "proxy.apify.com"
APIFY_PROXY_PORT = 8000

ASIN_PATTERNS = [
    re.compile(r"/dp/([A-Z0-9]{10})"),
    re.compile(r"/gp/product/([A-Z0-9]{10})"),
    re.compile(r"/gp/aw/d/([A-Z0-9]{10})"),
    re.compile(r"/product/([A-Z0-9]{10})"),
    re.compile(r"[?&]asin=([A-Z0-9]{10})", re.IGNORECASE),
]

SHORT_LINK_HOSTS = {"amzn.to", "a.co", "amzn.eu", "amzn.asia"}


class AmazonFetchError(Exception):
    """Raised when we can't get a clean product page from Amazon."""


def _normalize_url(url: str) -> str:
    """Add https:// if the URL has no scheme (handles bare a.co/d/xxx links)."""
    url = url.strip()
    if url and "://" not in url:
        url = "https://" + url
    return url


def _looks_like_amazon(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return "amazon." in host or host in SHORT_LINK_HOSTS


def _domain_to_country_code(domain: str) -> str:
    mapping = {
        "amazon.ca":     "CA",
        "amazon.co.uk":  "GB",
        "amazon.com.au": "AU",
        "amazon.de":     "DE",
        "amazon.fr":     "FR",
        "amazon.it":     "IT",
        "amazon.es":     "ES",
        "amazon.co.jp":  "JP",
        "amazon.com.mx": "MX",
        "amazon.in":     "IN",
        "amazon.nl":     "NL",
        "amazon.se":     "SE",
        "amazon.pl":     "PL",
    }
    return mapping.get(domain, "US")


def resolve_short_link(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if host not in SHORT_LINK_HOSTS:
        return url
    resp = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
    return resp.url


def extract_asin(url: str):
    """
    Return (asin, domain) from any Amazon link, or None.
    Accepts bare URLs (a.co/d/xxx), short links, and full URLs.
    """
    if not url:
        return None
    url = _normalize_url(url)
    if not _looks_like_amazon(url):
        return None
    try:
        url = resolve_short_link(url)
    except requests.RequestException:
        return None
    parsed = urlparse(url)
    domain = parsed.netloc.lower().replace("www.", "")
    for pattern in ASIN_PATTERNS:
        match = pattern.search(parsed.path) or pattern.search(url)
        if match:
            return match.group(1).upper(), domain
    return None


def _parse_price_text(text: str):
    """Turn '$19.99', 'CA$29.99', '£12.99' etc. into (float, symbol)."""
    if not text:
        return None, None
    text = text.strip()
    m = re.match(r"^([^\d]*)([\d.,]+)", text)
    if not m:
        return None, None
    symbol = m.group(1).strip() or "$"
    number = m.group(2)
    if "," in number and "." in number:
        if number.rfind(",") > number.rfind("."):
            number = number.replace(".", "").replace(",", ".")
        else:
            number = number.replace(",", "")
    elif "," in number:
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
    return any(marker in text for marker in [
        "type the characters you see",
        "to discuss automated access to amazon data",
        "enter the characters you see below",
        "sorry, we just need to make sure you're not a robot",
        "robot check",
        "verify your identity",
        "sign in for the best experience",
    ])


def _extract_title_and_price(soup: BeautifulSoup):
    """Return (title, price, currency) from a parsed Amazon page."""
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


def _attempt_scrape(session: requests.Session, url: str, headers: dict,
                    proxies: dict = None) -> dict:
    """
    Make one GET attempt and return parsed data, or raise AmazonFetchError.
    Used by both the proxy and direct scraping paths.
    """
    kwargs = {"headers": headers, "timeout": REQUEST_TIMEOUT}
    if proxies:
        kwargs["proxies"] = proxies

    try:
        resp = session.get(url, **kwargs)
    except requests.RequestException as exc:
        raise AmazonFetchError(f"Network error: {exc}") from exc

    if resp.status_code == 407:
        raise AmazonFetchError(
            "Apify proxy authentication failed — check APIFY_API_TOKEN."
        )

    soup = BeautifulSoup(resp.text, "html.parser")

    if _is_blocked_page(soup, resp.status_code):
        raise AmazonFetchError("Amazon blocked this request (CAPTCHA/rate limit).")

    title, price_value, currency = _extract_title_and_price(soup)
    if not title:
        raise AmazonFetchError(
            "Page loaded but no product title found — Amazon may be showing "
            "a location prompt or this product isn't available in this region."
        )

    return {"title": title, "price": price_value, "currency": currency or "$"}


def fetch_product_info(asin: str, domain: str = "amazon.com") -> dict:
    """
    Fetch current title and price for a product.
    Returns {"title": str, "price": float|None, "currency": str, "url": str}.
    Raises AmazonFetchError if all methods fail.
    """
    canonical_url = f"https://www.{domain}/dp/{asin}"
    country_code = _domain_to_country_code(domain)

    urls_to_try = [
        canonical_url,
        f"{canonical_url}?th=1&psc=1",
    ]

    # ── 1. Apify proxy (fast, ~5 seconds, bypasses IP blocking) ──────────────
    apify_token = os.environ.get("APIFY_API_TOKEN", "").strip()
    if apify_token:
        # Route through a datacenter IP in the product's country so Amazon
        # serves the correct regional page (important for amazon.ca etc.)
        proxy_url = (
            f"http://country-{country_code}:{apify_token}"
            f"@{APIFY_PROXY_HOST}:{APIFY_PROXY_PORT}"
        )
        proxies = {"http": proxy_url, "https": proxy_url}
        session = requests.Session()

        for url in urls_to_try:
            for headers in [HEADERS, MOBILE_HEADERS]:
                try:
                    data = _attempt_scrape(session, url, headers, proxies=proxies)
                    return {**data, "url": canonical_url}
                except AmazonFetchError:
                    polite_delay(1)

    # ── 2. Direct scraping (no proxy, last resort) ────────────────────────────
    session = requests.Session()
    for url in urls_to_try:
        for headers in [HEADERS, MOBILE_HEADERS]:
            try:
                data = _attempt_scrape(session, url, headers)
                return {**data, "url": canonical_url}
            except AmazonFetchError:
                polite_delay(1)

    raise AmazonFetchError(
        "Couldn't fetch this product after all attempts. "
        "Amazon may be blocking requests from this server's IP. "
        "Make sure APIFY_API_TOKEN is set in your Render environment."
    )


def polite_delay(seconds: float = 2.0):
    """Small delay between consecutive Amazon requests in batch jobs."""
    time.sleep(seconds)
