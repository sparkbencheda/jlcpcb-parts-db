"""Build filtered basic/preferred databases.

Produces:
- jlcpcb-parts-basic.sqlite3  — only basic/preferred parts
- jlcpcb-assets-basic.sqlite3 — only EasyEDA data for basic/preferred parts
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from .config import DATA_DIR, EASYEDA_CACHE_DB, OUTPUT_DB

log = logging.getLogger(__name__)

PARTS_BASIC_DB = DATA_DIR / "jlcpcb-parts-basic.sqlite3"
ASSETS_BASIC_DB = DATA_DIR / "jlcpcb-assets-basic.sqlite3"


def _build_parts_basic() -> Path:
    if not OUTPUT_DB.exists():
        raise FileNotFoundError(f"Parts DB not found: {OUTPUT_DB}")

    PARTS_BASIC_DB.unlink(missing_ok=True)

    src = sqlite3.connect(f"file:{OUTPUT_DB}?mode=ro", uri=True)
    dst = sqlite3.connect(str(PARTS_BASIC_DB))
    try:
        dst.execute("PRAGMA journal_mode = WAL")

        for table in ("categories", "manufacturers", "components"):
            schema = src.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()[0]
            dst.execute(schema)

        dst.execute("ATTACH DATABASE ? AS source", (f"file:{OUTPUT_DB}?mode=ro",))

        dst.execute("""
            INSERT INTO components
            SELECT * FROM source.components WHERE basic = 1 OR preferred = 1
        """)
        dst.execute("""
            INSERT INTO categories SELECT * FROM source.categories
            WHERE id IN (SELECT DISTINCT category_id FROM components)
        """)
        dst.execute("""
            INSERT INTO manufacturers SELECT * FROM source.manufacturers
            WHERE id IN (SELECT DISTINCT manufacturer_id FROM components)
        """)

        view_sql = src.execute(
            "SELECT sql FROM sqlite_master WHERE type='view' AND name='v_components'"
        ).fetchone()
        if view_sql:
            dst.execute(view_sql[0])

        dst.execute("""
            CREATE VIRTUAL TABLE components_fts USING fts5(
                lcsc, mfr, package, description, datasheet,
                content='components'
            )
        """)
        dst.execute("""
            INSERT INTO components_fts(lcsc, mfr, package, description, datasheet)
            SELECT lcsc, mfr, package, description, datasheet FROM components
        """)

        dst.commit()
        dst.execute("DETACH DATABASE source")
        dst.execute("VACUUM")
        dst.execute("ANALYZE")
        dst.commit()
    finally:
        src.close()
        dst.close()

    conn = sqlite3.connect(f"file:{PARTS_BASIC_DB}?mode=ro", uri=True)
    count = conn.execute("SELECT COUNT(*) FROM components").fetchone()[0]
    conn.close()
    size_mb = PARTS_BASIC_DB.stat().st_size / (1024 ** 2)
    log.info("Parts basic: %d parts (%.1f MB)", count, size_mb)
    return PARTS_BASIC_DB


def _build_assets_basic() -> Path:
    if not EASYEDA_CACHE_DB.exists():
        log.warning("Assets DB not found: %s — skipping", EASYEDA_CACHE_DB)
        return ASSETS_BASIC_DB
    if not OUTPUT_DB.exists():
        raise FileNotFoundError(f"Parts DB not found: {OUTPUT_DB}")

    ASSETS_BASIC_DB.unlink(missing_ok=True)

    dst = sqlite3.connect(str(ASSETS_BASIC_DB))
    try:
        dst.execute("PRAGMA journal_mode = WAL")
        dst.execute("ATTACH DATABASE ? AS parts", (f"file:{OUTPUT_DB}?mode=ro",))
        dst.execute("ATTACH DATABASE ? AS assets", (f"file:{EASYEDA_CACHE_DB}?mode=ro",))

        dst.execute("""
            CREATE TABLE easyeda_cache (
                lcsc INTEGER PRIMARY KEY,
                cad_data BLOB,
                svg_data BLOB,
                fetched_at TEXT NOT NULL,
                status TEXT NOT NULL
            )
        """)
        dst.execute("""
            INSERT INTO easyeda_cache
            SELECT ec.* FROM assets.easyeda_cache ec
            INNER JOIN parts.components c ON ec.lcsc = c.lcsc
            WHERE c.basic = 1 OR c.preferred = 1
        """)
        dst.execute("CREATE INDEX idx_easyeda_status ON easyeda_cache(status)")
        dst.commit()

        dst.execute("DETACH DATABASE parts")
        dst.execute("DETACH DATABASE assets")
        dst.execute("VACUUM")
        dst.commit()
    finally:
        dst.close()

    conn = sqlite3.connect(f"file:{ASSETS_BASIC_DB}?mode=ro", uri=True)
    counts = dict(conn.execute("SELECT status, COUNT(*) FROM easyeda_cache GROUP BY status").fetchall())
    conn.close()
    size_mb = ASSETS_BASIC_DB.stat().st_size / (1024 ** 2)
    log.info("Assets basic: %s (%.1f MB)", counts, size_mb)
    return ASSETS_BASIC_DB


def build() -> tuple[Path, Path]:
    parts = _build_parts_basic()
    assets = _build_assets_basic()
    return parts, assets


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    build()


if __name__ == "__main__":
    main()
