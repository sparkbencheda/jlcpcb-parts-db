# JLCPCB Parts Database

VPS-deployed pipeline that produces two SQLite databases for [sparkbench-parts](../sparkbench-parts):

1. **jlcpcb-parts.sqlite3** -- full catalog of in-stock JLCPCB parts with FTS5 search
2. **jlcpcb-assets.sqlite3** -- compressed CAD data (footprints, symbols, 3D models) crawled from EasyEDA

## Data Sources

### Parts Catalog

Derived from [yaqwsx/jlcparts](https://github.com/yaqwsx/jlcparts), which scrapes the full JLCPCB catalog ~3x/day and publishes a SQLite database to GitHub Pages as a split zip archive.

Our pipeline downloads this upstream DB, removes parts with stock < 5, applies basic/preferred assembly flags from the JLCPCB API, builds an FTS5 index, and outputs the final `jlcpcb-parts.sqlite3`.

### EasyEDA CAD Data

[EasyEDA](https://easyeda.com) is JLCPCB's PCB design tool. Each JLCPCB part (identified by LCSC ID) has associated CAD data: schematic symbols, PCB footprints, and 3D models. We crawl the EasyEDA API for every part in the catalog and store the compressed JSON responses.

## Database Schemas

### jlcpcb-parts.sqlite3

~616K parts, ~1.6 GB.

**`components`** -- one row per in-stock JLCPCB part:

| Column | Type | Description |
|--------|------|-------------|
| `lcsc` | INTEGER PK | LCSC part number (e.g. 1002 = C1002) |
| `category_id` | INTEGER FK | References `categories.id` |
| `mfr` | TEXT | Manufacturer part number (MPN) |
| `package` | TEXT | Package/footprint name (e.g. "0603", "SOP-8") |
| `joints` | INTEGER | Solder joint count (pin count) |
| `manufacturer_id` | INTEGER FK | References `manufacturers.id` |
| `basic` | INTEGER | 1 = JLCPCB basic part (no extended fee) |
| `preferred` | INTEGER | 1 = JLCPCB preferred part |
| `description` | TEXT | Part description with specs |
| `datasheet` | TEXT | Datasheet URL |
| `stock` | INTEGER | Current stock quantity |
| `price` | TEXT | JSON array of price tiers from yaqwsx (qFrom/qTo/price) |
| `last_update` | INTEGER | Unix timestamp of last upstream update |
| `extra` | TEXT | JSON blob with enriched data (see below) |
| `flag` | INTEGER | Upstream flag field |
| `last_on_stock` | INTEGER | Timestamp when last seen in stock |

**`extra` JSON structure** (from yaqwsx enrichment):

```json
{
  "id": 1354,
  "number": "C1002",
  "category": {"id1": 10991, "id2": 527, "name1": "Filters", "name2": "Ferrite Beads"},
  "manufacturer": {"id": 270, "name": "Sunlord"},
  "mpn": "GZ1608D601TF",
  "quantity": 389835,
  "moq": 20,
  "order_multiple": 20,
  "packaging": "Tape & Reel (TR)",
  "prices": [{"min_qty": 20, "max_qty": 199, "currency": "USD", "price": 0.0047}, ...],
  "datasheet": {"pdf": "https://wmsc.lcsc.com/..."},
  "images": [{"96x96": "...", "224x224": "...", "900x900": "..."}, ...],
  "rohs": true,
  "attributes": {"DC Resistance": "450mOhm", "Tolerance": "+/-25%", ...},
  "url": "https://lcsc.com/product-detail/..."
}
```

**`categories`**:

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | Category ID |
| `category` | TEXT | Top-level category (e.g. "Filters/EMI Optimization") |
| `subcategory` | TEXT | Subcategory (e.g. "Ferrite Beads") |

**`manufacturers`**:

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | Manufacturer ID |
| `name` | TEXT | Manufacturer name |

**`v_components`** -- convenience view joining all three tables.

**`components_fts`** -- FTS5 virtual table indexing `lcsc`, `mfr`, `package`, `description`, `datasheet`.

### jlcpcb-assets.sqlite3

~378K rows, ~1.5 GB (61% crawled, est. 3.1 GB complete).

| Column | Type | Description |
|--------|------|-------------|
| `lcsc` | INTEGER PK | LCSC part number |
| `cad_data` | BLOB | gzip-compressed JSON from EasyEDA components API |
| `svg_data` | BLOB | gzip-compressed JSON from EasyEDA SVGs API (nullable) |
| `fetched_at` | TEXT | ISO timestamp of when this row was fetched |
| `status` | TEXT | `ok`, `not_found`, `partial`, or `error` |

The `cad_data` blob contains the full EasyEDA component response (schematic symbol, footprint geometry, 3D model references). Decompress with `gzip.decompress()` to get the JSON string.

## Pipeline

```
yaqwsx/jlcparts (GitHub Pages, updated 3x/day)
    |
    v
pull_upstream.py         Download split zip, extract cache.sqlite3
    |
    v
scrape_basic_preferred.py   Query JLCPCB API for basic/preferred flags
    |
    v
build_db.py              Filter stock < 5, apply flags, build FTS5, VACUUM
    |
    v
jlcpcb-parts.sqlite3   Final catalog DB

easyeda.com API
    |
    v
crawl_easyeda.py         Crawl all LCSC IDs via SOCKS5 proxies
    |
    v
jlcpcb-assets.sqlite3    Compressed CAD data cache
```

The catalog pipeline runs daily via cron at 06:00 UTC. The EasyEDA crawl runs continuously until the initial backfill is complete, then incrementally for new parts.

## Usage

```bash
# Full catalog pipeline
python -m src.pull_upstream
python -m src.scrape_basic_preferred
python -m src.build_db

# EasyEDA crawl (requires SOCKS5 proxies)
python -m src.crawl_easyeda crawl --proxy-file proxies.txt --workers 40 --skip-svg
python -m src.crawl_easyeda status
```

## Deployment

Deployed to `/opt/jlcpcb-parts-db/` on VPS.

```bash
bash deploy/setup.sh          # install deps, create venv, set up daily cron
bash deploy/cron-update.sh    # manual run of the catalog pipeline
```

The EasyEDA crawl runs in a tmux session:

```bash
tmux new-session -d -s easyeda-crawl \
  "cd /opt/jlcpcb-parts-db && source .venv/bin/activate && \
   python3 -m src.crawl_easyeda crawl --proxy-file proxies.txt --workers 40 --skip-svg 2>&1 | tee data/crawl.log"
```

### Proxy file format

One proxy per line: `host:port:user:pass` (SOCKS5).

## License

MIT -- see [LICENSE](LICENSE).
