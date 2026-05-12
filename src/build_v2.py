"""Build unified v2 database combining parts catalog + EasyEDA Pro v2 CAD data.

Produces:
- jlcpcb-v2-basic.sqlite3 — basic/preferred parts + symbols/footprints/3D models
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from .config import DATA_DIR, OUTPUT_DB, V2_CACHE_DB

log = logging.getLogger(__name__)

V2_BASIC_DB = DATA_DIR / "jlcpcb-v2-basic.sqlite3"


def build_v2_basic(
    parts_db: Path | None = None,
    cache_db: Path | None = None,
    output_db: Path | None = None,
) -> Path:
    parts_db = parts_db or OUTPUT_DB
    cache_db = cache_db or V2_CACHE_DB
    output_db = output_db or V2_BASIC_DB

    if not parts_db.exists():
        raise FileNotFoundError(f"Parts DB not found: {parts_db}")
    if not cache_db.exists():
        raise FileNotFoundError(f"V2 cache DB not found: {cache_db}")

    output_db.unlink(missing_ok=True)

    dst = sqlite3.connect(str(output_db))
    try:
        dst.execute("PRAGMA journal_mode = WAL")
        dst.execute("PRAGMA page_size = 4096")

        dst.execute("ATTACH DATABASE ? AS parts", (f"file:{parts_db}?mode=ro",))
        dst.execute("ATTACH DATABASE ? AS cache", (f"file:{cache_db}?mode=ro",))

        dst.executescript("""
            CREATE TABLE categories (
                id INTEGER PRIMARY KEY,
                category TEXT NOT NULL,
                subcategory TEXT NOT NULL,
                UNIQUE (id, category, subcategory)
            );

            CREATE TABLE manufacturers (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            );

            CREATE TABLE parts (
                lcsc INTEGER PRIMARY KEY,
                category_id INTEGER REFERENCES categories(id),
                mfr TEXT,
                package TEXT,
                joints INTEGER,
                manufacturer_id INTEGER REFERENCES manufacturers(id),
                basic INTEGER DEFAULT 0,
                preferred INTEGER DEFAULT 0,
                description TEXT,
                datasheet TEXT,
                stock INTEGER,
                price TEXT,
                last_update INTEGER,
                extra TEXT,
                flag INTEGER,
                last_on_stock INTEGER,
                jlc_extra TEXT
            );

            CREATE TABLE components (
                uuid TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                doc_type INTEGER NOT NULL,
                data TEXT NOT NULL,
                fetched_at TEXT NOT NULL
            );
            CREATE INDEX idx_components_type ON components(doc_type);

            CREATE TABLE devices (
                lcsc INTEGER PRIMARY KEY REFERENCES parts(lcsc),
                device_uuid TEXT NOT NULL,
                symbol_uuid TEXT NOT NULL REFERENCES components(uuid),
                footprint_uuid TEXT NOT NULL REFERENCES components(uuid),
                model_uuid TEXT,
                model_title TEXT,
                model_transform TEXT,
                designator TEXT,
                fetched_at TEXT NOT NULL
            );
            CREATE INDEX idx_devices_symbol ON devices(symbol_uuid);
            CREATE INDEX idx_devices_footprint ON devices(footprint_uuid);

            CREATE TABLE models (
                uuid TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                step_data BLOB,
                size_bytes INTEGER,
                fetched_at TEXT
            );
        """)

        dst.execute("""
            INSERT INTO parts
            SELECT lcsc, category_id, mfr, package, joints, manufacturer_id,
                   basic, preferred, description, datasheet, stock, price,
                   last_update, extra, flag, last_on_stock, jlc_extra
            FROM parts.components
            WHERE basic = 1 OR preferred = 1
        """)

        dst.execute("""
            INSERT INTO categories
            SELECT * FROM parts.categories
            WHERE id IN (SELECT DISTINCT category_id FROM main.parts)
        """)
        dst.execute("""
            INSERT INTO manufacturers
            SELECT * FROM parts.manufacturers
            WHERE id IN (SELECT DISTINCT manufacturer_id FROM main.parts)
        """)

        dst.execute("""
            INSERT INTO devices
            SELECT d.* FROM cache.devices d
            WHERE d.lcsc IN (SELECT lcsc FROM main.parts)
        """)

        dst.execute("""
            INSERT OR IGNORE INTO components
            SELECT c.* FROM cache.components c
            WHERE c.uuid IN (
                SELECT symbol_uuid FROM main.devices
                UNION
                SELECT footprint_uuid FROM main.devices
            )
        """)

        dst.execute("""
            INSERT OR IGNORE INTO models
            SELECT m.* FROM cache.models m
            WHERE m.uuid IN (
                SELECT model_uuid FROM main.devices
                WHERE model_uuid IS NOT NULL AND model_uuid != ''
            )
        """)

        dst.execute("""
            CREATE VIRTUAL TABLE parts_fts USING fts5(
                lcsc, mfr, package, description, datasheet,
                content='parts'
            )
        """)
        dst.execute("""
            INSERT INTO parts_fts(lcsc, mfr, package, description, datasheet)
            SELECT lcsc, mfr, package, description, datasheet FROM parts
        """)

        dst.execute("""
            CREATE VIEW v_parts AS
            SELECT p.*, c.category, c.subcategory, m.name AS manufacturer_name
            FROM parts p
            LEFT JOIN categories c ON p.category_id = c.id
            LEFT JOIN manufacturers m ON p.manufacturer_id = m.id
        """)

        dst.commit()
        dst.execute("DETACH DATABASE parts")
        dst.execute("DETACH DATABASE cache")
        dst.execute("VACUUM")
        dst.execute("ANALYZE")
        dst.commit()
    finally:
        dst.close()

    conn = sqlite3.connect(f"file:{output_db}?mode=ro", uri=True)
    part_count = conn.execute("SELECT COUNT(*) FROM parts").fetchone()[0]
    device_count = conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
    comp_count = conn.execute("SELECT COUNT(*) FROM components").fetchone()[0]
    model_count = conn.execute("SELECT COUNT(*) FROM models").fetchone()[0]
    conn.close()

    size_mb = output_db.stat().st_size / (1024**2)
    log.info(
        "V2 basic: %d parts, %d devices, %d components, %d models (%.1f MB)",
        part_count, device_count, comp_count, model_count, size_mb,
    )
    return output_db


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    build_v2_basic()


if __name__ == "__main__":
    main()
