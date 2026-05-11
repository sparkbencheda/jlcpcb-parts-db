# jlcpcb-parts.sqlite3 Schema

Full catalog of in-stock JLCPCB parts derived from [yaqwsx/jlcparts](https://github.com/yaqwsx/jlcparts). Filtered to parts with stock >= 5, with basic/preferred assembly flags applied from the JLCPCB API and an FTS5 full-text search index.

Variants:
- **jlcpcb-parts.sqlite3** -- all in-stock parts (~631K rows, ~1.8 GB)
- **jlcpcb-parts-basic.sqlite3** -- only basic and preferred parts (~1.3K rows, ~4 MB)

Both variants share the same schema.

## Tables

### components

One row per in-stock JLCPCB part.

| Column | Type | Description |
|--------|------|-------------|
| `lcsc` | INTEGER PRIMARY KEY | LCSC part number (e.g. 1002 = C1002) |
| `category_id` | INTEGER | FK → `categories.id` |
| `mfr` | TEXT | Manufacturer part number (MPN) |
| `package` | TEXT | Package/footprint name (e.g. "0603", "SOP-8") |
| `joints` | INTEGER | Solder joint / pin count |
| `manufacturer_id` | INTEGER | FK → `manufacturers.id` |
| `basic` | INTEGER | 1 = JLCPCB basic part (no extended assembly fee) |
| `preferred` | INTEGER | 1 = JLCPCB preferred part (reduced fee) |
| `description` | TEXT | Part description with electrical specs |
| `datasheet` | TEXT | Datasheet URL |
| `stock` | INTEGER | Current stock quantity |
| `price` | TEXT | JSON — pricing tiers (see below) |
| `last_update` | INTEGER | Unix timestamp of last upstream update |
| `extra` | TEXT | JSON — enriched part data from yaqwsx (see below) |
| `flag` | INTEGER | Upstream flag field |
| `last_on_stock` | INTEGER | Unix timestamp when last seen in stock |
| `jlc_extra` | TEXT | JSON — JLCPCB-specific extended data (see below) |

### categories

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PRIMARY KEY | Category ID |
| `category` | TEXT | Top-level category (e.g. "Filters/EMI Optimization") |
| `subcategory` | TEXT | Subcategory (e.g. "Ferrite Beads") |

### manufacturers

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PRIMARY KEY | Manufacturer ID |
| `name` | TEXT | Manufacturer name (e.g. "Sunlord", "TI") |

## Views

### v_components

Convenience view joining `components`, `categories`, and `manufacturers` into a single flat row. Includes all component columns plus `category`, `subcategory`, and manufacturer `name`.

## Virtual Tables

### components_fts (FTS5)

Full-text search index over `components`. Indexed columns:
- `lcsc`
- `mfr`
- `package`
- `description`
- `datasheet`

Query example:

```sql
SELECT c.* FROM components_fts fts
JOIN components c ON c.lcsc = fts.lcsc
WHERE components_fts MATCH 'ferrite bead 0603'
ORDER BY rank
LIMIT 20;
```

## JSON Column Schemas

### `price` — Pricing Tiers

Array of price breaks from the yaqwsx upstream. Quantities are expressed as ranges.

```json
[
  {"qFrom": 1, "qTo": 199, "price": 0.015},
  {"qFrom": 200, "qTo": 999, "price": 0.0058},
  {"qFrom": 1000, "qTo": 2999, "price": 0.0047},
  {"qFrom": 3000, "qTo": 4999, "price": 0.0044}
]
```

| Field | Type | Description |
|-------|------|-------------|
| `qFrom` | int | Minimum quantity for this tier |
| `qTo` | int | Maximum quantity for this tier |
| `price` | float | Unit price in USD |

### `extra` — Enriched Part Data (yaqwsx)

Large JSON blob with LCSC catalog data scraped by the upstream project. Contains pricing from LCSC (distinct from JLCPCB assembly pricing in `price`), images, datasheets, and component attributes.

```json
{
  "id": 1354,
  "number": "C1002",
  "category": {
    "id1": 10991,
    "id2": 527,
    "name1": "Filters",
    "name2": "Ferrite Beads"
  },
  "manufacturer": {
    "id": 270,
    "name": "Sunlord"
  },
  "mpn": "GZ1608D601TF",
  "quantity": 389835,
  "warehouse_stock": {
    "central": 389835,
    "oversea": 0,
    "oversea_hk": 0,
    "oversea_us": 0
  },
  "moq": 20,
  "order_multiple": 20,
  "packaging": "Tape & Reel (TR)",
  "prices": [
    {"min_qty": 20, "max_qty": 199, "currency": "USD", "price": 0.0047},
    {"min_qty": 200, "max_qty": 999, "currency": "USD", "price": 0.003}
  ],
  "datasheet": {
    "pdf": "https://wmsc.lcsc.com/wmsc/upload/file/pdf/v2/lcsc/2304140030_Sunlord-GZ1608D601TF_C1002.pdf"
  },
  "images": [
    {
      "96x96": "https://assets.lcsc.com/.../96x96/...",
      "224x224": "https://assets.lcsc.com/.../224x224/...",
      "900x900": "https://assets.lcsc.com/.../900x900/..."
    }
  ],
  "rohs": true,
  "attributes": {
    "DC Resistance": "450mOhm",
    "Tolerance": "+/-25%",
    "Rated Current": "300mA",
    "Impedance @ Frequency": "600Ohms@100MHz"
  },
  "url": "https://lcsc.com/product-detail/..."
}
```

Key fields:

| Field | Type | Description |
|-------|------|-------------|
| `number` | string | Full LCSC part number ("C1002") |
| `mpn` | string | Manufacturer part number |
| `category.name1` / `name2` | string | Human-readable category hierarchy |
| `quantity` | int | Stock count at time of upstream scrape |
| `moq` | int | Minimum order quantity |
| `order_multiple` | int | Must order in multiples of this |
| `packaging` | string | e.g. "Tape & Reel (TR)", "Cut Tape (CT)" |
| `prices` | array | LCSC retail pricing tiers (USD) |
| `datasheet.pdf` | string | Direct PDF download URL |
| `images` | array | Product photos at 3 resolutions |
| `rohs` | bool | RoHS compliance |
| `attributes` | object | Electrical/mechanical specs (keys vary by category) |

### `jlc_extra` — JLCPCB Extended Data

Additional data from the JLCPCB OpenAPI. Contains assembly-relevant attributes, export compliance, and RoHS status from JLCPCB's own classification (may differ from the LCSC `rohs` field in `extra`).

```json
{
  "source": "jlcpcb_openapi",
  "rohs": true,
  "eccn": "EAR99",
  "assembly": false,
  "attributes": {
    "Number of Circuits": "1",
    "Impedance @ Frequency": "600Ohms@100MHz",
    "DC Resistance": "450mOhm",
    "Rated Current": "300mA",
    "Tolerance": "±25%"
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `source` | string | Always `"jlcpcb_openapi"` |
| `rohs` | bool | JLCPCB's RoHS classification |
| `eccn` | string | Export Control Classification Number (e.g. "EAR99") |
| `assembly` | bool | Whether JLCPCB can assemble this part |
| `attributes` | object | Component specs from JLCPCB (keys vary by category) |

## Notes

- The `basic`/`preferred` columns are populated by scraping the JLCPCB API (`scrape_basic_preferred.py`). When the API is unavailable, these flags may be 0 for all parts.
- The `price` column contains yaqwsx-sourced pricing. The `extra.prices` field contains LCSC retail pricing. These are different price lists.
- The `jlc_extra` column is from the upstream yaqwsx DB and may be NULL for some parts.
- The basic variant (`jlcpcb-parts-basic.sqlite3`) contains only rows where `basic = 1 OR preferred = 1`, along with their referenced categories and manufacturers. It includes its own FTS5 index.
