"""Build the final jlcpcb-parts.sqlite3 from upstream + flags.

Pipeline:
1. Create fresh output DB
2. Attach upstream cache.sqlite3 and insert only in-stock parts
3. Apply basic/preferred flags from scraped data
4. Build FTS5 index
5. VACUUM + optimize
"""
from __future__ import annotations

import logging
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
    OUTPUT_DB.unlink(missing_ok=True)

    conn = sqlite3.connect(str(OUTPUT_DB))
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA mmap_size = 536870912")

        conn.execute("ATTACH DATABASE ? AS upstream", (f"file:{UPSTREAM_DB}?mode=ro",))

        log.info("Creating schema from upstream...")
        for table in ("categories", "manufacturers"):
            schema = conn.execute(
                "SELECT sql FROM upstream.sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()[0]
            conn.execute(schema)
            conn.execute(f"INSERT INTO {table} SELECT * FROM upstream.{table}")

        comp_schema = conn.execute(
            "SELECT sql FROM upstream.sqlite_master WHERE type='table' AND name='components'"
        ).fetchone()[0]
        conn.execute(comp_schema)

        cur = conn.cursor()
        cur.execute(
            "INSERT INTO components SELECT * FROM upstream.components WHERE stock >= ?",
            (MIN_STOCK,),
        )
        log.info("Inserted %d in-stock parts (stock >= %d)", cur.rowcount, MIN_STOCK)

        view_sql = conn.execute(
            "SELECT sql FROM upstream.sqlite_master WHERE type='view' AND name='v_components'"
        ).fetchone()
        if view_sql:
            conn.execute(view_sql[0])

        for idx in conn.execute(
            "SELECT sql FROM upstream.sqlite_master WHERE type='index' AND sql IS NOT NULL"
        ).fetchall():
            try:
                conn.execute(idx[0])
            except sqlite3.OperationalError:
                pass

        conn.commit()
        conn.execute("DETACH DATABASE upstream")

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

            conn.commit()
            conn.execute("DETACH DATABASE flags")
        else:
            log.warning("No flags DB found at %s — skipping flag application", FLAGS_DB)

        log.info("Building FTS5 index...")
        cur.execute("""
            CREATE VIRTUAL TABLE components_fts USING fts5(
                lcsc, mfr, package, description, datasheet,
                content='components'
            )
        """)
        cur.execute("""
            INSERT INTO components_fts(lcsc, mfr, package, description, datasheet)
            SELECT lcsc, mfr, package, description, datasheet FROM components
        """)
        conn.commit()
        log.info("FTS5 index built")

        log.info("Running VACUUM, ANALYZE...")
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
