"""Build the final jlcpcb-parts.sqlite3 from upstream + flags.

Pipeline:
1. Copy upstream cache.sqlite3
2. Delete low-stock parts
3. Apply basic/preferred flags from scraped data
4. Build FTS5 index
5. VACUUM + optimize
"""
from __future__ import annotations

import contextlib
import logging
import shutil
import sqlite3
from pathlib import Path

from .config import DATA_DIR, MIN_STOCK, OUTPUT_DB, UPSTREAM_DB

log = logging.getLogger(__name__)

FLAGS_DB = DATA_DIR / "jlcpcb-flags.sqlite3"


def build() -> Path:
    """Build output database. Returns path to final DB."""
    if not UPSTREAM_DB.exists():
        raise FileNotFoundError(f"Upstream DB not found: {UPSTREAM_DB}")

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    log.info("Copying upstream DB to %s ...", OUTPUT_DB)
    shutil.copy2(str(UPSTREAM_DB), str(OUTPUT_DB))

    conn = sqlite3.connect(str(OUTPUT_DB))
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA mmap_size = 536870912")

        cur = conn.cursor()

        cur.execute("DELETE FROM components WHERE stock < ?", (MIN_STOCK,))
        conn.commit()
        log.info("Removed %d low-stock parts (<%d)", cur.rowcount, MIN_STOCK)

        if FLAGS_DB.exists():
            log.info("Applying basic/preferred flags from %s ...", FLAGS_DB)
            conn.execute("ATTACH DATABASE ? AS flags", (str(FLAGS_DB),))

            cur.execute("""
                UPDATE components SET basic = 1
                WHERE lcsc IN (SELECT lcsc FROM flags.part_flags WHERE basic = 1)
            """)
            log.info("Marked %d basic parts", cur.rowcount)

            cur.execute("""
                UPDATE components SET preferred = 1
                WHERE lcsc IN (SELECT lcsc FROM flags.part_flags WHERE preferred = 1)
            """)
            log.info("Marked %d preferred parts", cur.rowcount)

            conn.execute("DETACH DATABASE flags")
            conn.commit()
        else:
            log.warning("No flags DB found at %s — skipping flag application", FLAGS_DB)

        log.info("Building FTS5 index...")
        cur.execute("DROP TABLE IF EXISTS components_fts")
        cur.execute("""
            CREATE VIRTUAL TABLE components_fts USING fts5(
                lcsc,
                mfr,
                package,
                description,
                datasheet,
                content='components'
            )
        """)
        cur.execute("""
            INSERT INTO components_fts(lcsc, mfr, package, description, datasheet)
            SELECT lcsc, mfr, package, description, datasheet FROM components
        """)
        conn.commit()
        log.info("FTS5 index built")

        log.info("Running REINDEX, VACUUM, ANALYZE...")
        cur.execute("REINDEX")
        conn.commit()
        cur.execute("VACUUM")
        conn.commit()
        cur.execute("ANALYZE")
        cur.execute("PRAGMA optimize")
        conn.commit()
    finally:
        conn.close()

    size_mb = OUTPUT_DB.stat().st_size / (1024 ** 2)
    log.info("Final DB: %s (%.1f MB)", OUTPUT_DB, size_mb)
    return OUTPUT_DB


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    build()


if __name__ == "__main__":
    main()
