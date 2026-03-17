#!/usr/bin/env python3
import os
import sys
import json
import time
import random
import logging
import urllib.request
import schedule
from datetime import date, timedelta

import pg8000.dbapi

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("fuel-fetcher")

DB_HOST = os.environ.get("DB_HOST", "database")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "teslamate")
DB_USER = os.environ.get("DB_USER", "teslamate")
DB_PASS = os.environ.get("DB_PASS", "teslamate")

MASE_URL = os.environ.get("MASE_URL", "https://sisen.mase.gov.it/dgsaie/api/v1/weekly-prices/report/export?format=JSON&lang=it")
FUEL_FIELD = "BENZINA"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:136.0) Gecko/20100101 Firefox/136.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:136.0) Gecko/20100101 Firefox/136.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_7_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15",
]

SYNC_ON_START = os.environ.get("SYNC_ON_START", "true").lower() == "true"
SYNC_SINCE = os.environ.get("SYNC_SINCE", "")
SCHEDULE_DAY = os.environ.get("SCHEDULE_DAY", "tuesday")
SCHEDULE_TIME = os.environ.get("SCHEDULE_TIME", "15:00")


def get_db():
    return pg8000.dbapi.connect(
        host=DB_HOST, port=int(DB_PORT), database=DB_NAME,
        user=DB_USER, password=DB_PASS
    )


def wait_for_db(max_retries=30, delay=5):
    for i in range(max_retries):
        try:
            conn = get_db()
            conn.close()
            log.info("Database connection OK")
            return
        except Exception:
            log.info(f"Waiting for database... ({i+1}/{max_retries})")
            time.sleep(delay)
    log.error("Could not connect to database")
    sys.exit(1)


def ensure_table():
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fuel_prices (
                id SERIAL PRIMARY KEY,
                week_start DATE NOT NULL,
                price_per_liter NUMERIC(5,3) NOT NULL,
                source VARCHAR(20) DEFAULT 'api',
                station_count INTEGER,
                price_min NUMERIC(5,3),
                price_max NUMERIC(5,3),
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                CONSTRAINT fuel_prices_week_start_unique UNIQUE (week_start)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_fuel_prices_week_start
            ON fuel_prices (week_start DESC)
        """)
        cur.execute("""
            CREATE OR REPLACE FUNCTION get_fuel_price(target_date DATE)
            RETURNS NUMERIC AS $$
                SELECT price_per_liter
                FROM fuel_prices
                WHERE week_start <= target_date
                ORDER BY week_start DESC
                LIMIT 1;
            $$ LANGUAGE sql STABLE
        """)
        cur.execute("""
            CREATE OR REPLACE FUNCTION upsert_fuel_price(
                p_week_start DATE,
                p_price NUMERIC,
                p_source VARCHAR DEFAULT 'api',
                p_station_count INTEGER DEFAULT NULL,
                p_price_min NUMERIC DEFAULT NULL,
                p_price_max NUMERIC DEFAULT NULL
            ) RETURNS void AS $$
            BEGIN
                INSERT INTO fuel_prices
                    (week_start, price_per_liter, source, station_count, price_min, price_max)
                VALUES
                    (p_week_start, p_price, p_source, p_station_count, p_price_min, p_price_max)
                ON CONFLICT (week_start) DO UPDATE SET
                    price_per_liter = EXCLUDED.price_per_liter,
                    source = EXCLUDED.source,
                    station_count = EXCLUDED.station_count,
                    price_min = EXCLUDED.price_min,
                    price_max = EXCLUDED.price_max,
                    updated_at = NOW();
            END;
            $$ LANGUAGE plpgsql
        """)
        conn.commit()
        log.info("Database schema OK")
    finally:
        cur.close()
        conn.close()


def fetch_mase_prices():
    req = urllib.request.Request(MASE_URL, headers={
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("iso-8859-1"))

    prices = []
    for row in data:
        raw = row.get(FUEL_FIELD)
        if raw is None:
            continue
        prices.append((
            date.fromisoformat(row["DATA_RILEVAZIONE"]),
            round(float(raw) / 1000.0, 3),
        ))
    return sorted(prices, key=lambda x: x[0])


def sync_prices(since=None):
    try:
        all_prices = fetch_mase_prices()
    except Exception as e:
        log.error(f"Failed to fetch prices: {e}")
        return

    if since:
        all_prices = [(d, p) for d, p in all_prices if d >= since]

    if not all_prices:
        log.info("No prices to sync")
        return

    conn = get_db()
    cur = conn.cursor()
    try:
        for week_date, price in all_prices:
            cur.execute(
                "SELECT upsert_fuel_price(%s, %s, %s, NULL, NULL, NULL)",
                (week_date, price, "mase")
            )
        conn.commit()
        log.info(f"Synced {len(all_prices)} weeks "
                 f"({all_prices[0][0]} to {all_prices[-1][0]})")
        log.info(f"Latest: {all_prices[-1][0]} = {all_prices[-1][1]:.3f} EUR/L")
    except Exception as e:
        log.error(f"Database error during sync: {e}")
        conn.rollback()
    finally:
        cur.close()
        conn.close()


def fetch_latest():
    try:
        all_prices = fetch_mase_prices()
    except Exception as e:
        log.error(f"Failed to fetch prices: {e}")
        return

    if not all_prices:
        log.warning("No prices returned from API")
        return

    latest_date, latest_price = all_prices[-1]
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT upsert_fuel_price(%s, %s, %s, NULL, NULL, NULL)",
            (latest_date, latest_price, "mase")
        )
        conn.commit()
        log.info(f"Latest: {latest_date} = {latest_price:.3f} EUR/L")
    except Exception as e:
        log.error(f"Database error: {e}")
        conn.rollback()
    finally:
        cur.close()
        conn.close()


def scheduled_fetch():
    log.info("Scheduled fetch triggered")
    fetch_latest()


if __name__ == "__main__":
    log.info("Fuel price fetcher starting")

    wait_for_db()
    ensure_table()

    if SYNC_ON_START:
        since = None
        if SYNC_SINCE:
            since = date.fromisoformat(SYNC_SINCE)
        log.info(f"Initial sync (since={since or 'all'})")
        sync_prices(since)

    getattr(schedule.every(), SCHEDULE_DAY).at(SCHEDULE_TIME).do(scheduled_fetch)
    log.info(f"Scheduled: every {SCHEDULE_DAY} at {SCHEDULE_TIME}")

    while True:
        schedule.run_pending()
        time.sleep(60)
