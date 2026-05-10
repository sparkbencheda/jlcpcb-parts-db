"""Download the latest jlcparts cache.sqlite3 from yaqwsx GitHub Pages.

The upstream publishes as a split zip (cache.zip + cache.z01..zNN).
We download all volumes and extract with 7z.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

from .config import JLCPARTS_BASE_URL, UPSTREAM_DB, UPSTREAM_DIR

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}


def _download(url: str, dest: Path, retries: int = 3) -> None:
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=120) as resp:
                with open(dest, "wb") as f:
                    shutil.copyfileobj(resp, f)
            return
        except Exception as e:
            if attempt == retries:
                raise
            log.warning("Download attempt %d failed for %s: %s", attempt, url, e)
            time.sleep(5 * attempt)


def _get_volume_count(zip_path: Path) -> int:
    """Parse 7z listing to find total volume count."""
    result = subprocess.run(
        ["7z", "l", str(zip_path)],
        capture_output=True, text=True,
    )
    for line in result.stdout.splitlines():
        if "Volume Index" in line:
            parts = line.split("=")
            if len(parts) == 2:
                count = int(parts[1].strip())
                if count > 50:
                    raise RuntimeError(f"Unreasonable volume count: {count}")
                return count
    raise RuntimeError("Could not determine volume count from cache.zip")


def pull() -> Path:
    """Download and extract upstream DB. Returns path to extracted sqlite3."""
    UPSTREAM_DIR.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        log.info("Downloading cache.zip from %s ...", JLCPARTS_BASE_URL)
        zip_path = tmp_path / "cache.zip"
        _download(f"{JLCPARTS_BASE_URL}/cache.zip", zip_path)

        volumes = _get_volume_count(zip_path)
        log.info("Need %d additional volumes", volumes)

        for i in range(1, volumes + 1):
            vol_name = f"cache.z{i:02d}"
            vol_url = f"{JLCPARTS_BASE_URL}/{vol_name}"
            vol_path = tmp_path / vol_name
            log.info("Downloading %s ...", vol_name)
            _download(vol_url, vol_path)

        log.info("Extracting...")
        subprocess.run(
            ["7z", "x", "-y", str(zip_path)],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )

        extracted = tmp_path / "cache.sqlite3"
        if not extracted.exists():
            candidates = list(tmp_path.glob("*.sqlite3"))
            if not candidates:
                raise FileNotFoundError("No .sqlite3 found after extraction")
            extracted = candidates[0]

        shutil.move(str(extracted), str(UPSTREAM_DB))
        log.info("Upstream DB saved to %s", UPSTREAM_DB)

    return UPSTREAM_DB


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    pull()


if __name__ == "__main__":
    main()
