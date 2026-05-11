# jlcpcb-assets.sqlite3 Schema

EasyEDA CAD data (schematic symbols, PCB footprints, 3D model references) crawled from the EasyEDA API for every JLCPCB part. Each row stores gzip-compressed JSON responses keyed by LCSC part number.

Variants:
- **jlcpcb-assets.sqlite3** -- CAD data for all crawled parts (~631K rows, ~3 GB when complete)
- **jlcpcb-assets-basic.sqlite3** -- CAD data for basic/preferred parts only (~1.3K rows, ~30 MB)

Both variants share the same schema.

## Tables

### easyeda_cache

One row per LCSC part. Contains compressed API responses from two EasyEDA endpoints.

| Column | Type | Description |
|--------|------|-------------|
| `lcsc` | INTEGER PRIMARY KEY | LCSC part number (matches `components.lcsc` in jlcpcb-parts) |
| `cad_data` | BLOB | gzip-compressed JSON from EasyEDA components API |
| `svg_data` | BLOB | gzip-compressed JSON from EasyEDA SVGs API (nullable) |
| `fetched_at` | TEXT | ISO 8601 timestamp of when this row was fetched |
| `status` | TEXT | Crawl result status (see below) |

### Indexes

| Index | Column | Description |
|-------|--------|-------------|
| `idx_easyeda_status` | `status` | Fast filtering by crawl status |

## Status Values

| Status | Meaning |
|--------|---------|
| `ok` | Both endpoints returned valid data |
| `partial` | `cad_data` present but `svg_data` is NULL (crawled with `--skip-svg`) |
| `not_found` | Part has no EasyEDA CAD data (404 from API) |
| `error` | Crawl failed for this part (transient error, will be retried) |

## Working with CAD Data

### Decompressing

The `cad_data` and `svg_data` blobs are gzip-compressed JSON strings. Decompress before parsing:

```python
import gzip
import json
import sqlite3

conn = sqlite3.connect("jlcpcb-assets.sqlite3")
row = conn.execute(
    "SELECT cad_data FROM easyeda_cache WHERE lcsc = ? AND status IN ('ok', 'partial')",
    (1002,)
).fetchone()

if row and row[0]:
    cad = json.loads(gzip.decompress(row[0]))
```

### cad_data JSON Structure

The EasyEDA components API returns a response containing schematic symbols, PCB footprints, and 3D model references. Top-level structure:

```json
{
  "success": true,
  "code": 0,
  "result": {
    "uuid": "...",
    "title": "GZ1608D601TF",
    "description": "600Ohm 300mA ...",
    "docType": 4,
    "dataStr": "...",
    "packageDetail": {
      "title": "FB0603",
      "dataStr": "...",
      "docType": 4
    },
    "lcsc": {
      "number": "C1002",
      "package": "0603"
    }
  }
}
```

Key fields:

| Field | Type | Description |
|-------|------|-------------|
| `result.title` | string | Component title (usually the MPN) |
| `result.description` | string | Part description |
| `result.docType` | int | 2 = schematic symbol, 4 = PCB footprint |
| `result.dataStr` | string | EasyEDA format geometry data for the symbol |
| `result.packageDetail` | object | PCB footprint data |
| `result.packageDetail.dataStr` | string | EasyEDA format geometry data for the footprint |
| `result.lcsc.number` | string | LCSC part number with prefix |
| `result.lcsc.package` | string | Package name |

The `dataStr` fields contain EasyEDA's proprietary geometry format — a series of drawing commands that encode pad positions, pin names, silk screen outlines, and courtyard boundaries. These are parsed by sparkbench-parts to generate KiCad-compatible footprints and symbols.

### svg_data JSON Structure

The SVGs API returns pre-rendered SVG previews of the symbol and footprint. This endpoint is optional (crawled without `--skip-svg` flag).

```json
{
  "success": true,
  "result": {
    "svgSymbol": "<svg>...</svg>",
    "svgFootprint": "<svg>...</svg>"
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `result.svgSymbol` | string | SVG markup of the schematic symbol |
| `result.svgFootprint` | string | SVG markup of the PCB footprint |

## Querying with the Parts DB

The `lcsc` column is the join key between jlcpcb-parts and jlcpcb-assets. To look up CAD data for a specific part:

```sql
-- Attach both databases
ATTACH DATABASE 'jlcpcb-parts.sqlite3' AS parts;

SELECT c.lcsc, c.mfr, c.description, e.cad_data, e.status
FROM parts.components c
LEFT JOIN easyeda_cache e ON c.lcsc = e.lcsc
WHERE c.lcsc = 1002;
```

To find parts that have CAD data available:

```sql
ATTACH DATABASE 'jlcpcb-parts.sqlite3' AS parts;

SELECT c.lcsc, c.mfr, c.package, c.description
FROM parts.components c
INNER JOIN easyeda_cache e ON c.lcsc = e.lcsc
WHERE e.status IN ('ok', 'partial')
  AND c.basic = 1;
```

## Notes

- The initial crawl covers all ~631K in-stock parts. After completion, incremental crawls pick up new parts added upstream.
- The crawl uses SOCKS5 proxy rotation to avoid rate limiting from EasyEDA's API.
- `svg_data` is NULL for rows crawled with `--skip-svg`. The initial backfill skips SVGs to complete faster; a follow-up pass fetches them.
- The basic variant (`jlcpcb-assets-basic.sqlite3`) contains only rows whose `lcsc` matches a part with `basic = 1 OR preferred = 1` in jlcpcb-parts.sqlite3.
- Compressed blob sizes vary: simple passives (resistors, capacitors) are ~2-5 KB; complex ICs with many pins can be 50+ KB.
