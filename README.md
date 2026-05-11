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

Four databases are produced, two full and two basic/preferred filtered variants:

| Database | Contents | Rows | Size |
|----------|----------|------|------|
| `jlcpcb-parts.sqlite3` | Full in-stock parts catalog with FTS5 | ~631K | ~1.8 GB |
| `jlcpcb-parts-basic.sqlite3` | Basic/preferred parts only with FTS5 | ~1.3K | ~4 MB |
| `jlcpcb-assets.sqlite3` | EasyEDA CAD data for all parts | ~631K | ~3 GB |
| `jlcpcb-assets-basic.sqlite3` | EasyEDA CAD data for basic/preferred | ~1.3K | ~30 MB |

Full schema documentation with JSON column structures and query examples:
- **[docs/schema-jlcpcb-parts.md](docs/schema-jlcpcb-parts.md)** -- components, categories, manufacturers, FTS5, `price`/`extra`/`jlc_extra` JSON schemas
- **[docs/schema-jlcpcb-assets.md](docs/schema-jlcpcb-assets.md)** -- easyeda_cache table, `cad_data`/`svg_data` blob formats, decompression examples

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
