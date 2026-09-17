#!/usr/bin/env python3
import os
import sys
import json
import math
import time
import random
import logging
import urllib.request
import schedule
from datetime import date

import numpy as np
import pg8000.dbapi
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("market-fetcher")

DB_HOST = os.environ.get("DB_HOST", "database")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "teslamate")
DB_USER = os.environ.get("DB_USER", "teslamate")
DB_PASS = os.environ.get("DB_PASS", "teslamate")

PAGES_TO_FETCH = int(os.environ.get("PAGES_TO_FETCH", "5"))
FETCH_ON_START = os.environ.get("FETCH_ON_START", "true").lower() == "true"
SCHEDULE_DAY   = os.environ.get("SCHEDULE_DAY", "monday")
SCHEDULE_TIME  = os.environ.get("SCHEDULE_TIME", "09:00")
MIN_SAMPLES    = 5

MODEL_MAP = {
    ("Y", "LR AWD"):      {"slug": "model-y", "version": "long-range-awd"},
    ("Y", "Performance"): {"slug": "model-y", "version": "performance"},
    ("Y", "RWD"):         {"slug": "model-y", "version": "standard-range"},
    ("3", "LR AWD"):      {"slug": "model-3", "version": "long-range"},
    ("3", "Performance"): {"slug": "model-3", "version": "performance"},
    ("3", "RWD"):         {"slug": "model-3", "version": "standard-range"},
    ("S", "LR AWD"):      {"slug": "model-s", "version": "long-range"},
    ("S", "Performance"): {"slug": "model-s", "version": "performance"},
    ("X", "LR AWD"):      {"slug": "model-x", "version": "long-range"},
    ("X", "Performance"): {"slug": "model-x", "version": "performance"},
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:136.0) Gecko/20100101 Firefox/136.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:136.0) Gecko/20100101 Firefox/136.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_7_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15",
]


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


def ensure_tables():
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS market_listings (
                id          SERIAL PRIMARY KEY,
                fetched_at  DATE NOT NULL,
                source      VARCHAR(20) DEFAULT 'autoscout24',
                make        VARCHAR(50),
                model       VARCHAR(50),
                version     VARCHAR(100),
                reg_year    SMALLINT,
                mileage_km  INTEGER,
                price       NUMERIC(10,0),
                CONSTRAINT market_listings_unique
                    UNIQUE (fetched_at, source, reg_year, mileage_km, price)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_market_listings_fetched
            ON market_listings (fetched_at DESC)
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS depreciation_curve (
                id           SERIAL PRIMARY KEY,
                computed_at  TIMESTAMPTZ DEFAULT NOW(),
                make         VARCHAR(50),
                model        VARCHAR(50),
                version      VARCHAR(100),
                base_price   NUMERIC(10,0),
                coeff_age    NUMERIC(10,8),
                coeff_km     NUMERIC(12,10),
                r_squared    NUMERIC(5,4),
                sample_count INTEGER,
                CONSTRAINT depreciation_curve_unique
                    UNIQUE (computed_at, make, model, version)
            )
        """)
        conn.commit()
        log.info("Database schema OK")
    finally:
        cur.close()
        conn.close()


def get_cars():
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT DISTINCT model, marketing_name
            FROM cars
            WHERE model IS NOT NULL AND marketing_name IS NOT NULL
        """)
        return cur.fetchall()
    finally:
        cur.close()
        conn.close()


def fetch_page(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_listings(html):
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("script", {"id": "__NEXT_DATA__"})
    if not tag:
        return []

    try:
        content = tag.string or tag.get_text()
        data = json.loads(content)
        items = data["props"]["pageProps"]["listings"]
    except (KeyError, json.JSONDecodeError, TypeError):
        return []

    listings = []
    for item in items:
        try:
            tracking = item["tracking"]
            price    = int(tracking["price"])
            mileage  = int(tracking["mileage"])
            reg_year = int(tracking["firstRegistration"].split("-")[1])
            version  = item["vehicle"].get("modelVersionInput", "")
            listings.append({
                "price":    price,
                "mileage":  mileage,
                "reg_year": reg_year,
                "version":  version,
            })
        except (KeyError, ValueError, IndexError):
            continue

    return listings


def fetch_listings(slug, version, pages):
    base_url = (
        f"https://www.autoscout24.it/lst/tesla/{slug}"
        f"?sort=standard&desc=0&ustate=N%2CU&size=20"
        f"&cy=I&powertrain=E&version={version}"
    )
    all_listings = []
    for page in range(1, pages + 1):
        url = f"{base_url}&page={page}"
        try:
            html = fetch_page(url)
            page_listings = parse_listings(html)
            if not page_listings:
                log.info(f"No listings on page {page}, stopping pagination")
                break
            all_listings.extend(page_listings)
            log.info(f"Page {page}: {len(page_listings)} listings")
        except Exception as e:
            log.warning(f"Failed to fetch page {page}: {e}")
            break
        time.sleep(random.uniform(1.5, 3.5))

    return all_listings


def save_listings(listings, make, model, version):
    today = date.today()
    conn = get_db()
    cur = conn.cursor()
    saved = 0
    try:
        for item in listings:
            cur.execute("""
                INSERT INTO market_listings
                    (fetched_at, source, make, model, version, reg_year, mileage_km, price)
                VALUES (%s, 'autoscout24', %s, %s, %s, %s, %s, %s)
                ON CONFLICT ON CONSTRAINT market_listings_unique DO NOTHING
            """, (today, make, model, version, item["reg_year"], item["mileage"], item["price"]))
            saved += cur.rowcount
        conn.commit()
        log.info(f"Saved {saved}/{len(listings)} new listings for {make} {model} {version}")
    except Exception as e:
        log.error(f"DB error saving listings: {e}")
        conn.rollback()
    finally:
        cur.close()
        conn.close()


def fit_regression(make, model, version):
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT reg_year, mileage_km, price
            FROM market_listings
            WHERE make = %s AND model = %s AND version = %s
              AND price > 0 AND mileage_km > 0
        """, (make, model, version))
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    if len(rows) < MIN_SAMPLES:
        log.warning(f"Not enough data for regression ({len(rows)} rows, need {MIN_SAMPLES})")
        return

    current_year = date.today().year
    prices    = np.array([float(r[2]) for r in rows])
    age_years = np.array([current_year - r[0] for r in rows], dtype=float)
    mileage   = np.array([float(r[1]) for r in rows])

    y = np.log(prices)
    X = np.column_stack([np.ones(len(y)), age_years, mileage / 10000.0])
    coeffs, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
    log_base, coeff_age, coeff_km = coeffs

    y_pred  = X @ coeffs
    ss_res  = float(np.sum((y - y_pred) ** 2))
    ss_tot  = float(np.sum((y - np.mean(y)) ** 2))
    r_sq    = round(1.0 - ss_res / ss_tot, 4) if ss_tot > 0 else 0.0
    base_px = round(math.exp(log_base))

    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO depreciation_curve
                (make, model, version, base_price, coeff_age, coeff_km, r_squared, sample_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (make, model, version, base_px,
              float(coeff_age), float(coeff_km), r_sq, len(rows)))
        conn.commit()
        log.info(
            f"Regression ({len(rows)} samples): R²={r_sq:.4f}, "
            f"base={base_px}€, coeff_age={coeff_age:.6f}, coeff_km={coeff_km:.8f}"
        )
    except Exception as e:
        log.error(f"DB error saving regression: {e}")
        conn.rollback()
    finally:
        cur.close()
        conn.close()


def run_fetch():
    log.info("Market value fetch started")
    cars = get_cars()
    if not cars:
        log.warning("No cars found in database")
        return

    processed = set()
    for car_model, marketing_name in cars:
        params = MODEL_MAP.get((car_model, marketing_name))
        if not params:
            log.warning(f"No mapping for model=({car_model!r}, {marketing_name!r}), skipping")
            continue

        key = (params["slug"], params["version"])
        if key in processed:
            continue
        processed.add(key)

        log.info(f"Fetching: model {car_model} {marketing_name} → {params['slug']}/{params['version']}")
        listings = fetch_listings(params["slug"], params["version"], PAGES_TO_FETCH)
        if not listings:
            log.warning(f"No listings fetched for {params['slug']}/{params['version']}")
            continue

        save_listings(listings, "tesla", car_model, params["version"])
        fit_regression("tesla", car_model, params["version"])

    log.info("Market value fetch complete")


def scheduled_fetch():
    log.info("Scheduled fetch triggered")
    run_fetch()


if __name__ == "__main__":
    log.info("Market value fetcher starting")

    wait_for_db()
    ensure_tables()

    if FETCH_ON_START:
        run_fetch()

    getattr(schedule.every(), SCHEDULE_DAY).at(SCHEDULE_TIME).do(scheduled_fetch)
    log.info(f"Scheduled: every {SCHEDULE_DAY} at {SCHEDULE_TIME}")

    while True:
        schedule.run_pending()
        time.sleep(60)
