#!/bin/bash
set -euo pipefail

cd /opt/jlcpcb-parts-db
source .venv/bin/activate

LOG="/opt/jlcpcb-parts-db/data/update.log"
mkdir -p data/upstream

echo "[$(date -Iseconds)] Starting DB update..." | tee -a "$LOG"

python -m src.pull_upstream 2>&1 | tee -a "$LOG"
python -m src.scrape_basic_preferred 2>&1 | tee -a "$LOG"
python -m src.build_db 2>&1 | tee -a "$LOG"
python -m src.build_basic 2>&1 | tee -a "$LOG"

rm -f data/upstream/cache.sqlite3
echo "[$(date -Iseconds)] Upstream cache cleaned" | tee -a "$LOG"

echo "[$(date -Iseconds)] DB update complete" | tee -a "$LOG"
