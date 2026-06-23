"""
Data access layer. Uses Postgres (this project is set up for Neon —
https://neon.tech — via DATABASE_URL, but any Postgres connection string
works) so tracked products survive restarts/redeploys. Render's free web
services have an ephemeral filesystem, so SQLite would lose all data every
time the service restarts — that's why this uses a hosted Postgres database
instead.
"""

import os
from contextlib import contextmanager

import psycopg2
import psycopg2.extras


def _connection_string():
    url = os.environ["DATABASE_URL"]
    # Render (and some other providers) hand out "postgres://" URLs, but
    # psycopg2 wants "postgresql://".
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


@contextmanager
def get_conn():
    # connect_timeout is critical here: without it, a wrong or unreachable
    # DATABASE_URL can hang the connection attempt for minutes instead of
    # failing quickly with a clear error.
    conn = psycopg2.connect(_connection_string(), connect_timeout=10)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS products (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    asin TEXT NOT NULL,
                    domain TEXT NOT NULL DEFAULT 'amazon.com',
                    title TEXT,
                    url TEXT NOT NULL,
                    currency TEXT DEFAULT '$',
                    last_price NUMERIC,
                    lowest_price NUMERIC,
                    last_checked TIMESTAMPTZ,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE (chat_id, asin, domain)
                )
                """
            )


def add_product(chat_id, asin, domain, url, title, price, currency):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO products
                    (chat_id, asin, domain, url, title, currency,
                     last_price, lowest_price, last_checked)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (chat_id, asin, domain) DO NOTHING
                RETURNING id
                """,
                (chat_id, asin, domain, url, title, currency, price, price),
            )
            row = cur.fetchone()
            return row is not None  # False means it was already tracked


def list_products(chat_id):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT * FROM products
                WHERE chat_id = %s
                ORDER BY id ASC
                """,
                (chat_id,),
            )
            return cur.fetchall()


def get_product_by_position(chat_id, position):
    """1-based position within the user's /list output."""
    products = list_products(chat_id)
    if 1 <= position <= len(products):
        return products[position - 1]
    return None


def find_product(chat_id, identifier):
    """identifier can be a list position (e.g. '2') or an ASIN."""
    identifier = identifier.strip()
    if identifier.isdigit():
        return get_product_by_position(chat_id, int(identifier))
    asin = identifier.upper()
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT * FROM products
                WHERE chat_id = %s AND asin = %s
                """,
                (chat_id, asin),
            )
            return cur.fetchone()


def remove_product(chat_id, product_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM products WHERE chat_id = %s AND id = %s",
                (chat_id, product_id),
            )
            return cur.rowcount > 0


def update_price(product_id, new_price):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE products
                SET last_price = %s,
                    lowest_price = LEAST(COALESCE(lowest_price, %s), %s),
                    last_checked = NOW()
                WHERE id = %s
                """,
                (new_price, new_price, new_price, product_id),
            )


def touch_checked(product_id):
    """Update last_checked without changing the price (used when a fetch fails)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE products SET last_checked = NOW() WHERE id = %s",
                (product_id,),
            )


def update_title(product_id, title):
    """Fix a placeholder title once we successfully fetch the real one."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE products SET title = %s WHERE id = %s",
                (title, product_id),
            )


def get_all_products():
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM products ORDER BY id ASC")
            return cur.fetchall()
