# Unified Parts + CAD Database Schema (v2)

Single SQLite database combining the parts catalog (from yaqwsx/jlcparts) with EasyEDA Pro v2 CAD data (symbols, footprints, 3D model references). Replaces the current split of `jlcpcb-parts.sqlite3` + `jlcpcb-assets.sqlite3`.

## Data Sources

- **Parts catalog**: yaqwsx/jlcparts upstream, filtered to stock >= 5, with JLCPCB basic/preferred flags
- **Symbols & footprints**: EasyEDA Pro v2 API, stored as NDJSON
- **3D models**: EasyEDA CDN (STEP format), fetched lazily

## API Endpoints (EasyEDA Pro v2)

```
POST https://pro.easyeda.com/api/devices/searchByCodes
  Body: {"codes": ["C1002", "C14663"]}
  → device UUIDs, symbol/footprint/3D model UUIDs, attributes

GET https://pro.easyeda.com/api/v2/components/{uuid}
  → JSON envelope with dataStrId (encrypted URL), iv, key

GET {dataStrId}
  → AES-256-GCM encrypted blob (key/iv from previous response)
  → decrypt → gzip decompress → NDJSON Pro format

GET https://modules.easyeda.com/3dmodel/{model_uuid}
  → OBJ format 3D model

GET https://modules.easyeda.com/qAxj6KHrDKw4blvCG8QJPs7Y/{model_uuid}
  → STEP format 3D model
```

## Schema

```sql
-- Part catalog (from yaqwsx/jlcparts upstream)
-- ============================================================

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
    mfr TEXT,                     -- manufacturer part number
    package TEXT,                 -- "0603", "SOP-8", "LQFP-64"
    joints INTEGER,               -- solder joint / pin count
    manufacturer_id INTEGER REFERENCES manufacturers(id),
    basic INTEGER DEFAULT 0,      -- 1 = JLCPCB basic part
    preferred INTEGER DEFAULT 0,  -- 1 = JLCPCB preferred part
    description TEXT,
    datasheet TEXT,               -- URL
    stock INTEGER,
    price TEXT,                   -- JSON: [{qFrom, qTo, price}, ...]
    last_update INTEGER,          -- unix timestamp
    extra TEXT,                   -- JSON: yaqwsx enriched data
    flag INTEGER,
    last_on_stock INTEGER,
    jlc_extra TEXT                -- JSON: JLCPCB OpenAPI data
);

CREATE VIRTUAL TABLE parts_fts USING fts5(
    lcsc, mfr, package, description, datasheet,
    content='parts'
);

CREATE VIEW v_parts AS
SELECT p.*, c.category, c.subcategory, m.name AS manufacturer_name
FROM parts p
LEFT JOIN categories c ON p.category_id = c.id
LEFT JOIN manufacturers m ON p.manufacturer_id = m.id;

-- EasyEDA v2 CAD data
-- ============================================================

-- Symbols (doc_type=2) and footprints (doc_type=4)
-- Many parts share the same component (e.g. all 0603 resistors use R0603)
CREATE TABLE components (
    uuid TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    doc_type INTEGER NOT NULL,    -- 2=symbol, 4=footprint
    data TEXT NOT NULL,           -- raw NDJSON (EasyEDA Pro format)
    fetched_at TEXT NOT NULL
);
CREATE INDEX idx_components_type ON components(doc_type);

-- Links each LCSC part to its symbol, footprint, and 3D model
CREATE TABLE devices (
    lcsc INTEGER PRIMARY KEY REFERENCES parts(lcsc),
    device_uuid TEXT NOT NULL,
    symbol_uuid TEXT NOT NULL REFERENCES components(uuid),
    footprint_uuid TEXT NOT NULL REFERENCES components(uuid),
    model_uuid TEXT,
    model_title TEXT,
    model_transform TEXT,         -- "x,y,z,rx,ry,rz,..." offset/rotation for 3D placement
    designator TEXT,              -- "R?", "C?", "U?"
    fetched_at TEXT NOT NULL
);
CREATE INDEX idx_devices_symbol ON devices(symbol_uuid);
CREATE INDEX idx_devices_footprint ON devices(footprint_uuid);

-- 3D models (STEP files, fetched lazily by server or client)
CREATE TABLE models (
    uuid TEXT PRIMARY KEY,
    title TEXT NOT NULL,           -- e.g. "R0603_L1.6-W0.8-H0.6"
    step_data BLOB,               -- gzip-compressed STEP (null until fetched)
    size_bytes INTEGER,
    fetched_at TEXT
);
```

## NDJSON Format (EasyEDA Pro v2)

Symbols and footprints are stored as newline-delimited JSON arrays in the `components.data` column. Each line is a drawing command.

### Symbol Commands (doc_type=2)

| Command | Count | Purpose |
|---------|-------|---------|
| `DOCTYPE` | 1 | Format version: `["DOCTYPE","SYMBOL","1.1"]` |
| `HEAD` | 1 | Origin, editor version |
| `LINESTYLE` | 1+ | Line style definitions |
| `FONTSTYLE` | 3+ | Font style definitions |
| `PART` | 1+ | Part boundary with bounding box |
| `PIN` | N | Pin definitions (number, name, position, length, direction, type) |
| `RECT` | 0+ | Body rectangles |
| `POLY` | 0+ | Polylines (body outline, decorative lines) |
| `ARC` | 0+ | Arcs (inductor coils, etc.) |
| `CIRCLE` | 0+ | Circles (pin 1 dot, op-amp symbols) |
| `TEXT` | 0+ | Text labels |
| `ATTR` | N | Attributes (name, value, designator) |

### Footprint Commands (doc_type=4)

| Command | Count | Purpose |
|---------|-------|---------|
| `DOCTYPE` | 1 | Format version: `["DOCTYPE","FOOTPRINT","1.8"]` |
| `LAYER` | ~20 | Layer definitions (TOP, TOP_SILK, TOP_SOLDER_MASK, etc.) |
| `ACTIVE_LAYER` | 1-2 | Default active layers |
| `CANVAS` | 1 | Units, grid, origin |
| `PAD` | N | Copper pads (shape, position, size, pad number, layers) |
| `FILL` | 0+ | Filled regions (copper, solder mask, paste mask, 3D outline) |
| `POLY` | 0+ | Polylines (silk screen, courtyard, assembly outline) |
| `CONNECT` | 0+ | Pad connectivity rules |
| `PRIMITIVE` | 0+ | Embedded primitives |
| `ATTR` | 2+ | Footprint name, designator |

### Layer IDs

| ID | Name | Purpose |
|----|------|---------|
| 1 | TOP | Top copper |
| 2 | BOTTOM | Bottom copper |
| 3 | TOP_SILK | Top silkscreen |
| 5 | TOP_SOLDER_MASK | Top solder mask |
| 7 | TOP_PASTE_MASK | Top paste mask |
| 13 | DOCUMENT | Documentation |
| 48 | COMPONENT_SHAPE | Component body outline |
| 49 | COMPONENT_MARKING | Polarity/pin 1 marker |
| 50 | PIN_SOLDERING | Pad copper fill |
| 52 | COMPONENT_MODEL | 3D model outline |

## Example Queries

### Get full CAD data for a part

```sql
SELECT p.lcsc, p.mfr, p.description,
       sym.data AS symbol_ndjson,
       fp.data AS footprint_ndjson,
       d.model_uuid, d.model_title, d.designator
FROM parts p
JOIN devices d ON p.lcsc = d.lcsc
JOIN components sym ON d.symbol_uuid = sym.uuid
JOIN components fp ON d.footprint_uuid = fp.uuid
WHERE p.lcsc = 1002;
```

### Find all parts using a specific footprint

```sql
SELECT p.lcsc, p.mfr, p.description
FROM devices d
JOIN parts p ON d.lcsc = p.lcsc
WHERE d.footprint_uuid = '50b4943912284dab97752312e589e9e2';  -- R0603
```

### Search parts with CAD data available

```sql
SELECT p.lcsc, p.mfr, p.description, p.package
FROM parts_fts fts
JOIN parts p ON p.lcsc = fts.lcsc
JOIN devices d ON p.lcsc = d.lcsc
WHERE parts_fts MATCH 'STM32 LQFP'
ORDER BY rank
LIMIT 20;
```

### Check 3D model availability

```sql
SELECT m.title, m.size_bytes, COUNT(d.lcsc) AS part_count
FROM models m
JOIN devices d ON m.uuid = d.model_uuid
WHERE m.step_data IS NOT NULL
GROUP BY m.uuid
ORDER BY part_count DESC
LIMIT 20;
```

## Size Estimates

| Table | Rows | Size |
|-------|------|------|
| parts + categories + manufacturers | 631K | ~1.8 GB |
| parts_fts | 631K | ~200 MB |
| components (deduplicated symbols + footprints) | ~76K | ~250 MB |
| devices (LCSC → component mapping) | ~631K | ~30 MB |
| models (metadata only, no STEP) | ~56K | ~5 MB |
| **Total (without 3D)** | | **~2.3 GB** |
| models with STEP data | ~56K | +50-60 GB |

### Variants

| Database | Contents | Size |
|----------|----------|------|
| `jlcpcb-parts.sqlite3` | All in-stock parts + CAD | ~2.3 GB |
| `jlcpcb-parts-basic.sqlite3` | Basic/preferred only + CAD | ~10-15 MB |

## Client Architecture

```
sparkbench-parts (Electron)
├── Downloads jlcpcb-parts.sqlite3 on install (or basic variant)
├── Renders symbols/footprints from local components table
├── Full-text search via parts_fts
└── 3D models fetched on demand:
    GET /3d/{model_uuid}.step → stored in local models table
```
