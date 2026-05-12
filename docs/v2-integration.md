# V2 Database Integration Guide

Integration guide for consuming `jlcpcb-v2-basic.sqlite3` in Sparkbench Parts.

## Download

```
GET https://parts.sparkbench.ai/jlcpcb-v2-basic.sqlite3
```

49.9 MB single file. Supports `Range` requests and `HEAD` for size checks.

Metadata (part counts, file size, last updated):
```
GET https://parts.sparkbench.ai/metadata.json
```

## Schema Overview

```
parts (1,346 rows)          -- basic/preferred JLCPCB parts
  |
  +-- categories             -- part categories (resistors, capacitors, etc.)
  +-- manufacturers          -- manufacturer names
  |
devices (1,333 rows)         -- maps each LCSC part to its CAD components
  |
  +-- components (1,521)     -- deduplicated symbols (doc_type=2) + footprints (doc_type=4)
  +-- models (158)           -- deduplicated STEP 3D models (gzip-compressed)
```

## Deduplication

Many parts share the same physical package, so footprints and 3D models are stored once and referenced by UUID:

| Layer | Unique | Shared across | Example |
|-------|--------|---------------|---------|
| Symbols | 1,333 | 1,333 parts | Each part has its own symbol (part-specific pin names, values) |
| Footprints | 188 | 1,333 parts | 116 resistors share the single `R0603` footprint |
| 3D Models | 158 | 1,328 parts | 116 resistors share the single `R0603` STEP model |

5 parts have no 3D model reference in EasyEDA. 11 model UUIDs exist in EasyEDA metadata but have no published STEP data.

## Querying

### Search parts (full-text)

```sql
SELECT p.lcsc, p.mfr, p.package, p.description, p.stock,
       p.basic, p.preferred
FROM parts_fts fts
JOIN parts p ON p.lcsc = fts.lcsc
WHERE parts_fts MATCH '100nF 0402'
ORDER BY rank
LIMIT 20;
```

### Search with category and manufacturer names

```sql
SELECT * FROM v_parts
WHERE description LIKE '%100nF%'
AND basic = 1;
```

`v_parts` is a view that joins parts + categories + manufacturers.

### Get full CAD data for a part

```sql
SELECT p.lcsc, p.mfr, p.description, p.package,
       d.designator,
       d.symbol_uuid, d.footprint_uuid, d.model_uuid,
       sym.data   AS symbol_ndjson,
       fp.data    AS footprint_ndjson,
       d.model_title, d.model_transform
FROM parts p
JOIN devices d ON p.lcsc = d.lcsc
JOIN components sym ON d.symbol_uuid = sym.uuid
JOIN components fp ON d.footprint_uuid = fp.uuid
WHERE p.lcsc = 1002;
```

### Get 3D model STEP data

```sql
SELECT m.uuid, m.title, m.step_data, m.size_bytes
FROM models m
WHERE m.uuid = ?;
```

`step_data` is gzip-compressed STEP. Decompress before use:

```typescript
import { gunzipSync } from 'zlib';

const stepText = gunzipSync(row.step_data).toString('utf-8');
```

### Check if a part has a 3D model

```sql
SELECT d.lcsc, m.title, m.size_bytes
FROM devices d
JOIN models m ON d.model_uuid = m.uuid
WHERE d.lcsc = ? AND m.step_data IS NOT NULL;
```

### Find all parts sharing a footprint

```sql
SELECT p.lcsc, p.mfr, p.description
FROM devices d
JOIN parts p ON d.lcsc = p.lcsc
WHERE d.footprint_uuid = (
    SELECT footprint_uuid FROM devices WHERE lcsc = 1002
);
```

## NDJSON Format (Symbols & Footprints)

The `components.data` column contains newline-delimited JSON arrays. Each line is a drawing command.

### Parsing

```typescript
function parseNDJSON(data: string): any[][] {
  return data.trim().split('\n').map(line => JSON.parse(line));
}
```

### Symbol Commands (doc_type=2)

```
["DOCTYPE", "SYMBOL", "1.1"]
["HEAD", ...]
["PIN", pinNumber, name, x, y, length, rotation, type, ...]
["RECT", x, y, width, height, ...]
["POLY", points[], ...]
["ARC", cx, cy, rx, ry, startAngle, endAngle, ...]
["CIRCLE", cx, cy, radius, ...]
["TEXT", text, x, y, fontSize, ...]
["ATTR", key, value, x, y, ...]
```

Key commands for rendering:
- `PIN` defines electrical pins with position, name, number, direction
- `RECT`, `POLY`, `ARC`, `CIRCLE` define the symbol body outline
- `ATTR` contains "Designator" (e.g. "R?"), "Name", "Value"

### Footprint Commands (doc_type=4)

```
["DOCTYPE", "FOOTPRINT", "1.8"]
["LAYER", id, name, color, ...]
["CANVAS", units, gridSize, originX, originY, ...]
["PAD", padNumber, shape, cx, cy, width, height, rotation, layers[], ...]
["FILL", layer, points[], ...]
["POLY", layer, points[], lineWidth, ...]
["ATTR", key, value, ...]
```

Key commands for rendering:
- `PAD` defines copper pads with shape (rect/oval/polygon), position, size, and layer assignment
- `FILL` defines filled regions on specific layers (copper, solder mask, paste mask, 3D outline)
- `POLY` defines outlines on specific layers (silkscreen, courtyard, assembly)

### Layer IDs

| ID | Name | Rendering use |
|----|------|---------------|
| 1 | TOP | Top copper pads |
| 3 | TOP_SILK | Silkscreen outline |
| 5 | TOP_SOLDER_MASK | Solder mask openings |
| 7 | TOP_PASTE_MASK | Stencil openings |
| 13 | DOCUMENT | Documentation/courtyard |
| 48 | COMPONENT_SHAPE | 3D body outline |
| 49 | COMPONENT_MARKING | Pin 1 / polarity marker |

## 3D Model Transform

`devices.model_transform` contains comma-separated values for positioning the STEP model on the footprint:

```
x, y, z, rx, ry, rz, ...
```

Coordinates are in mm. Rotations are in degrees.

## Price Data

`parts.price` is a JSON string:

```json
[
  {"qFrom": 1, "qTo": 9, "price": 0.0017},
  {"qFrom": 10, "qTo": 29, "price": 0.0012},
  {"qFrom": 30, "qTo": 99, "price": 0.001}
]
```

## Parts Extra Data

`parts.extra` is a JSON string with enriched attributes from yaqwsx/jlcparts (resistance, capacitance, voltage rating, tolerance, etc.). Structure varies by category.

`parts.jlc_extra` is a JSON string with JLCPCB-specific data from their API.
