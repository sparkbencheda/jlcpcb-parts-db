"""Scrape JLCPCB API for basic/preferred part flags.

Produces a set of LCSC IDs that are basic or preferred on JLCPCB's assembly service.
No pandas — uses raw requests + json.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
from pathlib import Path

import requests

from .config import DATA_DIR, JLCPCB_API_URL

log = logging.getLogger(__name__)

HEADERS = {
    "Host": "jlcpcb.com",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Content-Type": "application/json",
    "Origin": "https://jlcpcb.com",
    "Referer": "https://jlcpcb.com/parts/basic_parts",
}

FLAGS_DB = DATA_DIR / "jlcpcb-flags.sqlite3"


def _init_flags_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS part_flags (
            lcsc INTEGER PRIMARY KEY,
            basic INTEGER NOT NULL DEFAULT 0,
            preferred INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    return conn


def _scrape_list(library_type: str, preferred: bool) -> list[int]:
    """Scrape all pages of a JLCPCB component list. Returns LCSC IDs (ints)."""
    all_ids: list[int] = []
    page = 1

    while page < 200:
        payload = {
            "currentPage": page,
            "pageSize": 100,
            "keyword": None,
            "componentLibraryType": library_type,
            "preferredComponentFlag": preferred,
            "stockFlag": None,
            "stockSort": None,
            "firstSortName": None,
            "secondSortName": None,
            "componentBrand": None,
            "componentSpecification": None,
            "componentAttributes": [],
            "searchSource": "search",
        }

        try:
            resp = requests.post(JLCPCB_API_URL, headers=HEADERS, json=payload, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            log.warning("Request failed on page %d: %s", page, e)
            break

        codes = re.findall(r'"componentCode":"C(\d+)"', resp.text)
        if not codes:
            break

        all_ids.extend(int(c) for c in codes)
        log.info("  %s (preferred=%s) page %d: %d parts", library_type, preferred, page, len(codes))

        page += 1
        time.sleep(2)

    return all_ids


def scrape() -> Path:
    """Scrape JLCPCB API for basic/preferred flags. Returns path to flags DB."""
    conn = _init_flags_db(FLAGS_DB)
    try:
        log.info("Scraping basic parts...")
        basic_ids = _scrape_list("base", preferred=False)
        log.info("Found %d basic parts", len(basic_ids))

        log.info("Scraping preferred parts...")
        preferred_ids = _scrape_list("base", preferred=True)
        log.info("Found %d preferred parts", len(preferred_ids))

        basic_set = set(basic_ids)
        preferred_set = set(preferred_ids)

        conn.execute("DELETE FROM part_flags")
        conn.executemany(
            "INSERT INTO part_flags (lcsc, basic, preferred) VALUES (?, 1, 0)",
            [(lcsc,) for lcsc in basic_set - preferred_set],
        )
        conn.executemany(
            "INSERT INTO part_flags (lcsc, basic, preferred) VALUES (?, 0, 1)",
            [(lcsc,) for lcsc in preferred_set - basic_set],
        )
        conn.executemany(
            "INSERT INTO part_flags (lcsc, basic, preferred) VALUES (?, 1, 1)",
            [(lcsc,) for lcsc in basic_set & preferred_set],
        )
        conn.commit()

        total = conn.execute("SELECT COUNT(*) FROM part_flags").fetchone()[0]
        log.info("Saved %d flagged parts to %s", total, FLAGS_DB)
    finally:
        conn.close()

    return FLAGS_DB


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    scrape()


if __name__ == "__main__":
    main()
