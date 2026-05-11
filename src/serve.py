"""HTTP server for downloading JLCPCB parts databases."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

from .config import DATA_DIR

log = logging.getLogger(__name__)

SERVE_FILES = {
    "jlcpcb-parts": {
        "path": DATA_DIR / "jlcpcb-parts.sqlite3",
        "description": "Full JLCPCB parts catalog",
    },
    "jlcpcb-assets": {
        "path": DATA_DIR / "jlcpcb-assets.sqlite3",
        "description": "EasyEDA CAD data (footprints, symbols, 3D models)",
    },
    "jlcpcb-parts-basic": {
        "path": DATA_DIR / "jlcpcb-parts-basic.sqlite3",
        "description": "Basic and preferred JLCPCB parts only",
    },
    "jlcpcb-assets-basic": {
        "path": DATA_DIR / "jlcpcb-assets-basic.sqlite3",
        "description": "EasyEDA CAD data for basic/preferred parts only",
    },
}

DEFAULT_PORT = 8484


def _file_metadata(key: str, entry: dict) -> dict | None:
    path: Path = entry["path"]
    if not path.exists():
        return None

    stat = path.stat()
    meta: dict = {
        "key": key,
        "filename": path.name,
        "description": entry["description"],
        "size_bytes": stat.st_size,
        "size_mb": round(stat.st_size / (1024 ** 2), 1),
        "updated_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        "download_url": f"/{path.name}",
    }

    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        if "assets" in key:
            counts = dict(conn.execute(
                "SELECT status, COUNT(*) FROM easyeda_cache GROUP BY status"
            ).fetchall())
            meta["parts_ok"] = counts.get("ok", 0)
            meta["parts_not_found"] = counts.get("not_found", 0)
            meta["parts_total"] = sum(counts.values())
        else:
            meta["parts_total"] = conn.execute("SELECT COUNT(*) FROM components").fetchone()[0]
            basic = conn.execute("SELECT COUNT(*) FROM components WHERE basic=1").fetchone()[0]
            preferred = conn.execute("SELECT COUNT(*) FROM components WHERE preferred=1").fetchone()[0]
            meta["parts_basic"] = basic
            meta["parts_preferred"] = preferred
        conn.close()
    except Exception:
        pass

    return meta


class DBHandler(SimpleHTTPRequestHandler):
    def do_HEAD(self) -> None:
        file_path = self._resolve_path()
        if file_path is None:
            self.send_error(404)
            return

        size = file_path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/x-sqlite3")
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", f"attachment; filename={file_path.name}")
        self.end_headers()

    def do_GET(self) -> None:
        if self.path == "/":
            self._serve_index()
            return
        if self.path == "/metadata.json":
            self._serve_metadata()
            return

        file_path = self._resolve_path()
        if file_path is None:
            self.send_error(404)
            return

        size = file_path.stat().st_size
        range_header = self.headers.get("Range")

        if range_header:
            self._serve_range(file_path, size, range_header)
        else:
            self._serve_full(file_path, size)

    def _resolve_path(self) -> Path | None:
        for entry in SERVE_FILES.values():
            if self.path == f"/{entry['path'].name}":
                path = entry["path"]
                if path.exists():
                    return path
        return None

    def _serve_full(self, file_path: Path, size: int) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/x-sqlite3")
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", f"attachment; filename={file_path.name}")
        self.end_headers()

        with open(file_path, "rb") as f:
            while chunk := f.read(1024 * 1024):
                self.wfile.write(chunk)

    def _serve_range(self, file_path: Path, size: int, range_header: str) -> None:
        try:
            range_spec = range_header.replace("bytes=", "")
            start_str, end_str = range_spec.split("-", 1)
            start = int(start_str) if start_str else 0
            end = int(end_str) if end_str else size - 1
        except (ValueError, IndexError):
            self.send_error(416, "Invalid range")
            return

        if start >= size or end >= size or start > end:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return

        content_length = end - start + 1
        self.send_response(206)
        self.send_header("Content-Type", "application/x-sqlite3")
        self.send_header("Content-Length", str(content_length))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

        with open(file_path, "rb") as f:
            f.seek(start)
            remaining = content_length
            while remaining > 0:
                chunk_size = min(1024 * 1024, remaining)
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _serve_metadata(self) -> None:
        databases = []
        for key, entry in SERVE_FILES.items():
            meta = _file_metadata(key, entry)
            if meta:
                databases.append(meta)

        body = json.dumps({"databases": databases}, indent=2).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_index(self) -> None:
        rows = ""
        for key, entry in SERVE_FILES.items():
            path = entry["path"]
            if path.exists():
                stat = path.stat()
                size_mb = stat.st_size / (1024 ** 2)
                updated = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                size_str = f"{size_mb:.1f} MB" if size_mb < 1024 else f"{size_mb / 1024:.1f} GB"
                rows += f"""<tr>
                    <td><a href="/{path.name}">{path.name}</a></td>
                    <td>{entry['description']}</td>
                    <td>{size_str}</td>
                    <td>{updated}</td>
                </tr>"""
            else:
                rows += f"""<tr>
                    <td>{path.name}</td>
                    <td>{entry['description']}</td>
                    <td colspan="2">Not built</td>
                </tr>"""

        html = f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>JLCPCB Parts DB</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 900px; margin: 40px auto; padding: 0 20px; color: #333; }}
  h1 {{ font-size: 1.4em; }}
  table {{ width: 100%; border-collapse: collapse; margin: 20px 0; }}
  th, td {{ text-align: left; padding: 10px 12px; border-bottom: 1px solid #e0e0e0; }}
  th {{ font-weight: 600; color: #666; font-size: 0.85em; text-transform: uppercase; }}
  a {{ color: #2563eb; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .meta {{ margin-top: 30px; font-size: 0.9em; color: #888; }}
</style>
</head><body>
<h1>JLCPCB Parts DB</h1>
<p>SQLite databases for <a href="https://sparkbench.ai">Sparkbench Parts</a>. Rebuilt daily from <a href="https://github.com/yaqwsx/jlcparts">yaqwsx/jlcparts</a>.</p>
<table>
  <tr><th>File</th><th>Description</th><th>Size</th><th>Updated</th></tr>
  {rows}
</table>
<p class="meta"><a href="/metadata.json">metadata.json</a> &mdash; machine-readable catalog with part counts and timestamps</p>
</body></html>"""
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        log.info(format, *args)


def run(port: int = DEFAULT_PORT) -> None:
    server = HTTPServer(("0.0.0.0", port), DBHandler)
    log.info("Serving databases on http://0.0.0.0:%d", port)
    for entry in SERVE_FILES.values():
        path = entry["path"]
        status = f"{path.stat().st_size / (1024**2):.1f} MB" if path.exists() else "missing"
        log.info("  /%s → %s", path.name, status)
    server.serve_forever()


def main() -> None:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="Serve JLCPCB parts databases")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    run(port=args.port)


if __name__ == "__main__":
    main()
