"""
Offline unit tests — no network or database required. Run with:
    pip install pytest
    pytest
"""

from bot.amazon import _parse_price_text, extract_asin
from bot.handlers import parse_command


def test_extract_asin_full_url():
    assert extract_asin("https://www.amazon.com/dp/B08N5WRWNW") == (
        "B08N5WRWNW",
        "amazon.com",
    )


def test_extract_asin_no_scheme():
    """Bare URLs without https:// should still be recognised."""
    assert extract_asin("amazon.com/dp/B08N5WRWNW") == ("B08N5WRWNW", "amazon.com")


def test_extract_asin_with_title_slug():
    url = "https://www.amazon.com/Echo-Dot/dp/B08N5WRWNW/ref=sr_1_3?keywords=echo"
    assert extract_asin(url) == ("B08N5WRWNW", "amazon.com")


def test_extract_asin_gp_product():
    url = "https://www.amazon.co.uk/gp/product/B08N5WRWNW"
    assert extract_asin(url) == ("B08N5WRWNW", "amazon.co.uk")


def test_extract_asin_non_amazon_url():
    assert extract_asin("https://www.google.com/dp/B08N5WRWNW") is None


def test_extract_asin_no_asin_in_url():
    assert extract_asin("https://www.amazon.com/s?k=headphones") is None


def test_parse_price_simple_dollar():
    assert _parse_price_text("$19.99") == (19.99, "$")


def test_parse_price_thousands_separator():
    assert _parse_price_text("$1,234.56") == (1234.56, "$")


def test_parse_price_pound():
    assert _parse_price_text("£12.99") == (12.99, "£")


def test_parse_price_empty():
    assert _parse_price_text("") == (None, None)


def test_parse_command_with_args():
    assert parse_command("/add https://amazon.com/dp/B08N5WRWNW") == (
        "add",
        "https://amazon.com/dp/B08N5WRWNW",
    )


def test_parse_command_no_args():
    assert parse_command("/list") == ("list", "")


def test_parse_command_strips_bot_mention():
    assert parse_command("/start@MyBot") == ("start", "")


def test_parse_command_non_command_text():
    assert parse_command("hello there") == (None, None)
