from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
UPSTREAM_DIR = DATA_DIR / "upstream"

UPSTREAM_DB = UPSTREAM_DIR / "cache.sqlite3"
OUTPUT_DB = DATA_DIR / "jlcpcb-parts.sqlite3"
EASYEDA_CACHE_DB = DATA_DIR / "jlcpcb-assets.sqlite3"
V2_CACHE_DB = DATA_DIR / "jlcpcb-v2-cache.sqlite3"

JLCPARTS_BASE_URL = "https://yaqwsx.github.io/jlcparts/data"

JLCPCB_API_URL = "https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList/v2"

MIN_STOCK = 5
