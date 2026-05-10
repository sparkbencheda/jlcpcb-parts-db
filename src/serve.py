"""HTTP server for downloading JLCPCB parts databases."""
from __future__ import annotations

import logging
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

from .config import DATA_DIR

log = logging.getLogger(__name__)

SERVE_FILES = {
    "/jlcpcb-parts.sqlite3": DATA_DIR / "jlcpcb-parts.sqlite3",
    "/jlcpcb-assets.sqlite3": DATA_DIR / "jlcpcb-assets.sqlite3",
    "/jlcpcb-parts-basic.sqlite3": DATA_DIR / "jlcpcb-parts-basic.sqlite3",
    "/jlcpcb-assets-basic.sqlite3": DATA_DIR / "jlcpcb-assets-basic.sqlite3",
}

DEFAULT_PORT = 8484


class DBHandler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/":
            self._serve_index()
            return

        file_path = SERVE_FILES.get(self.path)
        if file_path is None or not file_path.exists():
            self.send_error(404)
            return

        size = file_path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/x-sqlite3")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f"attachment; filename={file_path.name}")
        self.end_headers()

        with open(file_path, "rb") as f:
            while chunk := f.read(1024 * 1024):
                self.wfile.write(chunk)

    def _serve_index(self) -> None:
        lines = ["JLCPCB Parts DB\n"]
        for url_path, file_path in SERVE_FILES.items():
            if file_path.exists():
                size_mb = file_path.stat().st_size / (1024 ** 2)
                lines.append(f"  {url_path}  ({size_mb:.1f} MB)")
            else:
                lines.append(f"  {url_path}  (not built)")
        body = "\n".join(lines).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        log.info(format, *args)


def run(port: int = DEFAULT_PORT) -> None:
    server = HTTPServer(("0.0.0.0", port), DBHandler)
    log.info("Serving databases on http://0.0.0.0:%d", port)
    for url_path, file_path in SERVE_FILES.items():
        status = f"{file_path.stat().st_size / (1024**2):.1f} MB" if file_path.exists() else "missing"
        log.info("  %s → %s", url_path, status)
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
