"""EasyEDA API crawler for JLCPCB parts.

Fetches footprint/symbol/3D model data from EasyEDA by LCSC ID.
Stores compressed JSON in a SQLite cache DB.

Ported from sparkbench-parts/src/crawler.py.
"""
from __future__ import annotations

import collections
import gzip
import json
import logging
import os
import signal
import socket
import sqlite3
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import EASYEDA_CACHE_DB, OUTPUT_DB

log = logging.getLogger(__name__)

API_ENDPOINT = "https://easyeda.com/api/products/{lcsc_id}/components"
ENDPOINT_SVG = "https://easyeda.com/api/products/{lcsc_id}/svgs"

HEADERS = {
    "Accept-Encoding": "gzip, deflate",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://easyeda.com/",
}

MIN_PROXY_INTERVAL = 4.0

_socket_lock = threading.Lock()
_orig_socket = socket.socket


def _decode_response(raw: bytes) -> str:
    if raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw).decode("utf-8")
    return raw.decode("utf-8")


def _create_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())
    except (ImportError, Exception):
        pass
    return ctx


def _compress(data: str) -> bytes:
    return gzip.compress(data.encode("utf-8"), compresslevel=6)


class RateLimitError(Exception):
    pass


class SocksProxy:
    def __init__(self, host: str, port: int, user: str, password: str) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.last_used: float = 0.0
        self.cooldown_until: float = 0.0
        self.fail_count: int = 0

    def __repr__(self) -> str:
        return f"{self.host}:{self.port}"


class ProxyPool:
    """Thread-safe proxy pool with per-proxy rate limiting and cooldowns."""

    def __init__(self, proxies: list[SocksProxy], min_interval: float = MIN_PROXY_INTERVAL) -> None:
        self._proxies = proxies
        self._min_interval = min_interval
        self._lock = threading.Lock()

    def acquire(self) -> SocksProxy:
        while True:
            with self._lock:
                now = time.monotonic()
                best: SocksProxy | None = None
                best_wait = float("inf")

                for p in self._proxies:
                    if now < p.cooldown_until:
                        continue
                    ready_at = p.last_used + self._min_interval
                    wait = max(0.0, ready_at - now)
                    if wait < best_wait:
                        best_wait = wait
                        best = p

                if best is not None and best_wait <= 0:
                    best.last_used = now
                    return best

            time.sleep(min(best_wait if best_wait < float("inf") else 0.5, 0.5))

    def report_success(self, proxy: SocksProxy) -> None:
        with self._lock:
            proxy.fail_count = max(0, proxy.fail_count - 1)

    def report_rate_limited(self, proxy: SocksProxy) -> None:
        with self._lock:
            proxy.fail_count += 1
            cooldown = min(120, 15 * proxy.fail_count)
            proxy.cooldown_until = time.monotonic() + cooldown

    def report_error(self, proxy: SocksProxy) -> None:
        with self._lock:
            proxy.fail_count += 1
            proxy.cooldown_until = time.monotonic() + min(60, 10 * proxy.fail_count)

    def stats(self) -> dict[str, int]:
        with self._lock:
            now = time.monotonic()
            active = sum(1 for p in self._proxies if now >= p.cooldown_until)
            cooled = len(self._proxies) - active
            return {"active": active, "cooldown": cooled, "total": len(self._proxies)}


def load_proxies(path: Path) -> list[SocksProxy]:
    proxies: list[SocksProxy] = []
    for line in path.read_text().strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(":")
        if len(parts) == 4:
            proxies.append(SocksProxy(parts[0], int(parts[1]), parts[2], parts[3]))
        elif len(parts) == 2:
            proxies.append(SocksProxy(parts[0], int(parts[1]), "", ""))
    return proxies


def _fetch_with_proxy(
    url: str,
    proxy: SocksProxy,
    ssl_ctx: ssl.SSLContext,
) -> str | None:
    import socks

    with _socket_lock:
        socks.set_default_proxy(
            socks.SOCKS5,
            proxy.host,
            proxy.port,
            username=proxy.user or None,
            password=proxy.password or None,
        )
        socket.socket = socks.socksocket
        try:
            req = urllib.request.Request(url=url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30, context=ssl_ctx) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (403, 429) or e.code >= 500:
                raise RateLimitError(f"HTTP {e.code} via {proxy}")
            return None
        finally:
            socket.socket = _orig_socket

    return _decode_response(raw)


def _worker_fetch_part(
    lcsc: int,
    pool: ProxyPool,
    ssl_ctx: ssl.SSLContext,
    skip_svg: bool,
) -> tuple[int, str, bytes | None, bytes | None]:
    lcsc_id = f"C{lcsc}"

    proxy = pool.acquire()
    try:
        cad_raw = _fetch_with_proxy(API_ENDPOINT.format(lcsc_id=lcsc_id), proxy, ssl_ctx)
        pool.report_success(proxy)
    except RateLimitError:
        pool.report_rate_limited(proxy)
        return (lcsc, "retry", None, None)
    except Exception:
        pool.report_error(proxy)
        return (lcsc, "retry", None, None)

    if cad_raw is None:
        return (lcsc, "not_found", None, None)

    try:
        parsed = json.loads(cad_raw)
        if not parsed or parsed.get("success") is False:
            return (lcsc, "not_found", None, None)
    except json.JSONDecodeError:
        return (lcsc, "error", None, None)

    cad_blob = _compress(cad_raw)

    svg_blob = None
    if not skip_svg:
        proxy2 = pool.acquire()
        try:
            svg_raw = _fetch_with_proxy(ENDPOINT_SVG.format(lcsc_id=lcsc_id), proxy2, ssl_ctx)
            pool.report_success(proxy2)
            if svg_raw:
                svg_blob = _compress(svg_raw)
        except RateLimitError:
            pool.report_rate_limited(proxy2)
            return (lcsc, "partial", cad_blob, None)
        except Exception:
            pool.report_error(proxy2)
            return (lcsc, "partial", cad_blob, None)

    return (lcsc, "ok", cad_blob, svg_blob)


def init_cache_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS easyeda_cache (
            lcsc INTEGER PRIMARY KEY,
            cad_data BLOB,
            svg_data BLOB,
            fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
            status TEXT NOT NULL DEFAULT 'ok'
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_easyeda_status ON easyeda_cache(status)
    """)
    conn.commit()
    return conn


def get_all_lcsc_ids(parts_db_path: Path) -> list[int]:
    conn = sqlite3.connect(f"file:{parts_db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT lcsc FROM components ORDER BY lcsc").fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def get_already_fetched(cache_conn: sqlite3.Connection) -> set[int]:
    rows = cache_conn.execute(
        "SELECT lcsc FROM easyeda_cache WHERE status IN ('ok', 'not_found')"
    ).fetchall()
    return {r[0] for r in rows}


def crawl(
    parts_db_path: Path | None = None,
    cache_db_path: Path | None = None,
    skip_svg: bool = False,
    proxy_file: str | Path | None = None,
    workers: int = 20,
) -> None:
    parts_db_path = parts_db_path or OUTPUT_DB
    cache_db_path = cache_db_path or EASYEDA_CACHE_DB

    if not parts_db_path.exists():
        log.error("Parts DB not found: %s", parts_db_path)
        sys.exit(1)

    log.info("Loading part IDs from %s", parts_db_path)
    all_ids = get_all_lcsc_ids(parts_db_path)
    log.info("Total parts in database: %d", len(all_ids))

    cache_conn = init_cache_db(cache_db_path)
    already = get_already_fetched(cache_conn)
    remaining = [lcsc for lcsc in all_ids if lcsc not in already]
    log.info("Already cached: %d, remaining: %d", len(already), len(remaining))

    if not remaining:
        log.info("Nothing to crawl — all parts already cached")
        return

    proxies: list[SocksProxy] = []
    if proxy_file:
        proxies = load_proxies(Path(proxy_file))

    if not proxies:
        log.error("No proxies loaded — proxy file required for crawl")
        return

    pool = ProxyPool(proxies, min_interval=MIN_PROXY_INTERVAL)
    log.info(
        "Loaded %d proxies, %d workers, %.1fs min interval per proxy",
        len(proxies), workers, MIN_PROXY_INTERVAL,
    )

    ssl_ctx = _create_ssl_context()
    stop_event = threading.Event()

    def handle_signal(sig: int, frame: object) -> None:
        stop_event.set()
        log.info("Stop requested — waiting for workers to finish...")

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    fetched = 0
    errors = 0
    not_found = 0
    retries = 0
    write_lock = threading.Lock()
    start_time = time.monotonic()

    retry_queue: collections.deque[int] = collections.deque()

    def get_next_id() -> int | None:
        with write_lock:
            if retry_queue:
                return retry_queue.popleft()
        return None

    def write_result(lcsc: int, status: str, cad_blob: bytes | None, svg_blob: bytes | None) -> None:
        nonlocal fetched, errors, not_found, retries
        with write_lock:
            if status == "retry":
                retries += 1
                retry_queue.append(lcsc)
                return

            cache_conn.execute(
                "INSERT OR REPLACE INTO easyeda_cache (lcsc, cad_data, svg_data, status) "
                "VALUES (?, ?, ?, ?)",
                (lcsc, cad_blob, svg_blob, status),
            )

            if status == "ok":
                fetched += 1
            elif status in ("not_found", "partial"):
                not_found += 1
            else:
                errors += 1

            total_done = fetched + not_found + errors
            if total_done > 0 and total_done % 200 == 0:
                cache_conn.commit()
                ps = pool.stats()
                elapsed = time.monotonic() - start_time
                done = fetched + not_found + errors
                rate = done / max(elapsed / 60, 0.01)
                remaining_est = (len(remaining) - total_done) / max(rate, 0.01)
                log.info(
                    "[%d/%d] ok=%d skip=%d err=%d retry=%d | %.0f/min | ~%.0fh left | proxies: %d/%d",
                    total_done, len(remaining), fetched, not_found, errors, retries,
                    rate, remaining_est / 60, ps["active"], ps["total"],
                )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {}
        idx = 0

        def submit_work() -> None:
            nonlocal idx
            while len(futures) < workers * 2 and not stop_event.is_set():
                lcsc = get_next_id()
                if lcsc is None:
                    if idx < len(remaining):
                        lcsc = remaining[idx]
                        idx += 1
                    else:
                        break
                fut = executor.submit(_worker_fetch_part, lcsc, pool, ssl_ctx, skip_svg)
                futures[fut] = lcsc

        submit_work()

        while futures and not stop_event.is_set():
            done = [f for f in futures if f.done()]
            for fut in done:
                del futures[fut]
                try:
                    write_result(*fut.result())
                except Exception as e:
                    log.error("Worker exception: %s", e)
                    with write_lock:
                        errors += 1

            submit_work()

            if not done:
                time.sleep(0.05)

        if stop_event.is_set():
            for fut in futures:
                fut.cancel()

    cache_conn.commit()
    cache_conn.close()

    elapsed = time.monotonic() - start_time
    total = fetched + not_found + errors
    log.info(
        "Done: %d ok, %d skip, %d err, %d retries in %.0fs (%.0f parts/min)",
        fetched, not_found, errors, retries, elapsed,
        total / max(elapsed / 60, 0.01),
    )


def show_status(parts_db_path: Path | None = None, cache_db_path: Path | None = None) -> None:
    parts_db_path = parts_db_path or OUTPUT_DB
    cache_db_path = cache_db_path or EASYEDA_CACHE_DB

    if not cache_db_path.exists():
        print("No cache database found. Run crawl first.")
        return

    total_parts = len(get_all_lcsc_ids(parts_db_path))
    conn = sqlite3.connect(f"file:{cache_db_path}?mode=ro", uri=True)
    counts = conn.execute(
        "SELECT status, COUNT(*) FROM easyeda_cache GROUP BY status"
    ).fetchall()
    conn.close()

    status_map = dict(counts)
    cached = sum(v for v in status_map.values())
    ok = status_map.get("ok", 0)
    not_found = status_map.get("not_found", 0)
    partial = status_map.get("partial", 0)
    error = status_map.get("error", 0)
    remaining = total_parts - ok - not_found

    db_size = os.path.getsize(cache_db_path)

    print(f"Parts database:   {total_parts:,} parts")
    print(f"Cache database:   {cache_db_path} ({db_size / (1024**2):.1f} MB)")
    print(f"  OK:             {ok:,}")
    print(f"  Not found:      {not_found:,}")
    print(f"  Partial:        {partial:,}")
    print(f"  Errors:         {error:,}")
    pct = (cached / total_parts * 100) if total_parts else 0
    print(f"  Total cached:   {cached:,} ({pct:.1f}%)")
    print(f"  Remaining:      {remaining:,}")

    if ok > 0:
        avg_size = db_size / ok
        est_total = avg_size * total_parts
        print(f"  Est. final size: {est_total / (1024**3):.1f} GB")


def main() -> None:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="EasyEDA crawler for JLCPCB parts")
    sub = parser.add_subparsers(dest="command")

    crawl_p = sub.add_parser("crawl", help="Run the EasyEDA crawler")
    crawl_p.add_argument("--proxy-file", required=True, help="Path to proxy list (host:port:user:pass)")
    crawl_p.add_argument("--workers", type=int, default=20)
    crawl_p.add_argument("--skip-svg", action="store_true")
    crawl_p.add_argument("--parts-db", type=Path, default=None)
    crawl_p.add_argument("--cache-db", type=Path, default=None)

    status_p = sub.add_parser("status", help="Show crawl status")
    status_p.add_argument("--parts-db", type=Path, default=None)
    status_p.add_argument("--cache-db", type=Path, default=None)

    args = parser.parse_args()

    if args.command == "crawl":
        crawl(
            parts_db_path=args.parts_db,
            cache_db_path=args.cache_db,
            skip_svg=args.skip_svg,
            proxy_file=args.proxy_file,
            workers=args.workers,
        )
    elif args.command == "status":
        show_status(parts_db_path=args.parts_db, cache_db_path=args.cache_db)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
