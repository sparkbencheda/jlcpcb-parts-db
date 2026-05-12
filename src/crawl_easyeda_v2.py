"""EasyEDA Pro v2 API crawler for JLCPCB parts.

Fetches symbols, footprints (NDJSON Pro format), and 3D models (STEP)
via the EasyEDA Pro v2 API. Stores in unified v2 schema with
deduplicated components.

API chain:
  1. POST searchByCodes → device UUIDs + component refs
  2. GET /api/v2/components/{uuid} → encrypted envelope (symbols/footprints)
     or inline JSON (3D model metadata, docType=16)
  3. Decrypt AES-256-GCM + gzip decompress → NDJSON
  4. 3D model: 3d_model_uuid → STEP from CDN
"""
from __future__ import annotations

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

from .config import DATA_DIR, OUTPUT_DB

log = logging.getLogger(__name__)

SEARCH_URL = "https://pro.easyeda.com/api/devices/searchByCodes"
COMPONENT_URL = "https://pro.easyeda.com/api/v2/components/{uuid}"
STEP_CDN = "https://modules.easyeda.com/qAxj6KHrDKw4blvCG8QJPs7Y/{uuid}"

V2_CACHE_DB = DATA_DIR / "jlcpcb-v2-cache.sqlite3"

HEADERS = {
    "Accept": "application/json",
    "Accept-Encoding": "gzip",
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

BATCH_SIZE = 10
MIN_PROXY_INTERVAL = 4.0

_socket_lock = threading.Lock()
_orig_socket = socket.socket


class RateLimitError(Exception):
    pass


class SocksProxy:
    __slots__ = ("host", "port", "user", "password", "last_used", "cooldown_until", "fail_count")

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
            proxy.cooldown_until = time.monotonic() + min(120, 15 * proxy.fail_count)

    def report_error(self, proxy: SocksProxy) -> None:
        with self._lock:
            proxy.fail_count += 1
            proxy.cooldown_until = time.monotonic() + min(60, 10 * proxy.fail_count)

    def stats(self) -> dict[str, int]:
        with self._lock:
            now = time.monotonic()
            active = sum(1 for p in self._proxies if now >= p.cooldown_until)
            return {"active": active, "cooldown": len(self._proxies) - active, "total": len(self._proxies)}


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


def _create_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())
    except (ImportError, Exception):
        pass
    return ctx


def _decode_response(raw: bytes) -> bytes:
    if raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    return raw


def _fetch_with_proxy(
    url: str,
    proxy: SocksProxy,
    ssl_ctx: ssl.SSLContext,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> bytes | None:
    import socks

    hdrs = dict(HEADERS)
    if headers:
        hdrs.update(headers)

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
            req = urllib.request.Request(url=url, data=body, headers=hdrs, method=method)
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


def _decrypt_component(encrypted: bytes, key_hex: str, iv_hex: str) -> str:
    from Crypto.Cipher import AES

    key = bytes.fromhex(key_hex)
    iv = bytes.fromhex(iv_hex)
    tag = encrypted[-16:]
    ciphertext = encrypted[:-16]
    cipher = AES.new(key, AES.MODE_GCM, nonce=iv)
    decrypted = cipher.decrypt_and_verify(ciphertext, tag)
    return gzip.decompress(decrypted).decode("utf-8")


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_v2_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS components (
            uuid TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            doc_type INTEGER NOT NULL,
            data TEXT NOT NULL,
            fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_components_type ON components(doc_type);

        CREATE TABLE IF NOT EXISTS devices (
            lcsc INTEGER PRIMARY KEY,
            device_uuid TEXT NOT NULL,
            symbol_uuid TEXT NOT NULL,
            footprint_uuid TEXT NOT NULL,
            model_uuid TEXT,
            model_title TEXT,
            model_transform TEXT,
            designator TEXT,
            fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_devices_symbol ON devices(symbol_uuid);
        CREATE INDEX IF NOT EXISTS idx_devices_footprint ON devices(footprint_uuid);

        CREATE TABLE IF NOT EXISTS models (
            uuid TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            step_data BLOB,
            size_bytes INTEGER,
            fetched_at TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    return conn


def get_basic_lcsc_ids(parts_db_path: Path) -> list[int]:
    conn = sqlite3.connect(f"file:{parts_db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT lcsc FROM components WHERE basic = 1 OR preferred = 1 ORDER BY lcsc"
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def get_all_lcsc_ids(parts_db_path: Path) -> list[int]:
    conn = sqlite3.connect(f"file:{parts_db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT lcsc FROM components ORDER BY lcsc").fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Phase 1: searchByCodes → devices table
# ---------------------------------------------------------------------------

def _parse_device(item: dict) -> dict | None:
    attrs = item.get("attributes", {})
    lcsc_str = attrs.get("Supplier Part", "")
    if not lcsc_str or not lcsc_str.startswith("C"):
        return None

    try:
        lcsc = int(lcsc_str[1:])
    except ValueError:
        return None

    symbol_uuid = attrs.get("Symbol", "")
    footprint_uuid = attrs.get("Footprint", "")
    if not symbol_uuid or not footprint_uuid:
        return None

    return {
        "lcsc": lcsc,
        "device_uuid": item.get("uuid", ""),
        "symbol_uuid": symbol_uuid,
        "footprint_uuid": footprint_uuid,
        "model_uuid": attrs.get("3D Model", ""),
        "model_title": attrs.get("3D Model Title", ""),
        "model_transform": attrs.get("3D Model Transform", ""),
        "designator": attrs.get("Designator", ""),
    }


def crawl_devices(
    lcsc_ids: list[int],
    conn: sqlite3.Connection,
    pool: ProxyPool,
    ssl_ctx: ssl.SSLContext,
    stop_event: threading.Event,
) -> tuple[int, int]:
    already = {r[0] for r in conn.execute("SELECT lcsc FROM devices").fetchall()}
    remaining = [i for i in lcsc_ids if i not in already]
    log.info("Devices: %d already fetched, %d remaining", len(already), len(remaining))

    if not remaining:
        return len(already), 0

    ok = 0
    errors = 0

    for batch_start in range(0, len(remaining), BATCH_SIZE):
        if stop_event.is_set():
            break

        batch = remaining[batch_start : batch_start + BATCH_SIZE]
        codes = [f"C{lcsc}" for lcsc in batch]

        proxy = pool.acquire()
        body = json.dumps({"codes": codes}).encode("utf-8")

        try:
            raw = _fetch_with_proxy(SEARCH_URL, proxy, ssl_ctx, method="POST", body=body)
            pool.report_success(proxy)
        except RateLimitError:
            pool.report_rate_limited(proxy)
            log.warning("Rate limited on searchByCodes batch %d, retrying later", batch_start)
            errors += len(batch)
            continue
        except Exception as e:
            pool.report_error(proxy)
            log.error("Error on searchByCodes batch %d: %s", batch_start, e)
            errors += len(batch)
            continue

        if raw is None:
            errors += len(batch)
            continue

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.error("Invalid JSON from searchByCodes batch %d", batch_start)
            errors += len(batch)
            continue

        results = data.get("result", [])
        if not isinstance(results, list):
            log.error("Unexpected result type from searchByCodes: %s", type(results).__name__)
            errors += len(batch)
            continue

        batch_ok = 0
        for item in results:
            device = _parse_device(item)
            if device is None:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO devices "
                "(lcsc, device_uuid, symbol_uuid, footprint_uuid, model_uuid, model_title, model_transform, designator) "
                "VALUES (:lcsc, :device_uuid, :symbol_uuid, :footprint_uuid, :model_uuid, :model_title, :model_transform, :designator)",
                device,
            )
            batch_ok += 1

        conn.commit()
        ok += batch_ok

        total_done = batch_start + len(batch)
        if total_done % 100 == 0 or total_done == len(remaining):
            ps = pool.stats()
            log.info(
                "Devices [%d/%d] ok=%d err=%d | proxies: %d/%d",
                total_done, len(remaining), ok, errors, ps["active"], ps["total"],
            )

    return ok, errors


# ---------------------------------------------------------------------------
# Phase 2: fetch components (symbols + footprints)
# ---------------------------------------------------------------------------

def _fetch_one_component(
    uuid: str,
    pool: ProxyPool,
    ssl_ctx: ssl.SSLContext,
) -> tuple[str, str | None, int | None, str | None]:
    """Returns (uuid, ndjson_data, doc_type, title) or (uuid, None, None, error_reason)."""
    proxy = pool.acquire()
    url = COMPONENT_URL.format(uuid=uuid)

    try:
        raw = _fetch_with_proxy(url, proxy, ssl_ctx)
        pool.report_success(proxy)
    except RateLimitError:
        pool.report_rate_limited(proxy)
        return (uuid, None, None, "rate_limited")
    except Exception as e:
        pool.report_error(proxy)
        return (uuid, None, None, str(e))

    if raw is None:
        return (uuid, None, None, "not_found")

    try:
        envelope = json.loads(raw).get("result", {})
    except json.JSONDecodeError:
        return (uuid, None, None, "invalid_json")

    doc_type = envelope.get("docType")
    title = envelope.get("display_title") or envelope.get("title", "")

    if "dataStrId" in envelope:
        data_url = envelope["dataStrId"]
        iv_hex = envelope.get("iv", "")
        key_hex = envelope.get("key", "")

        proxy2 = pool.acquire()
        try:
            encrypted = _fetch_with_proxy(
                data_url, proxy2, ssl_ctx,
                headers={"Content-Type": "", "Accept": "*/*"},
            )
            pool.report_success(proxy2)
        except RateLimitError:
            pool.report_rate_limited(proxy2)
            return (uuid, None, None, "rate_limited_data")
        except Exception as e:
            pool.report_error(proxy2)
            return (uuid, None, None, f"data_fetch_error: {e}")

        if encrypted is None:
            return (uuid, None, None, "data_not_found")

        try:
            ndjson = _decrypt_component(encrypted, key_hex, iv_hex)
        except Exception as e:
            return (uuid, None, None, f"decrypt_error: {e}")

        return (uuid, ndjson, doc_type, title)

    elif "dataStr" in envelope:
        data_str = envelope["dataStr"]
        if isinstance(data_str, str):
            return (uuid, data_str, doc_type, title)
        return (uuid, json.dumps(data_str), doc_type, title)

    return (uuid, None, None, "no_data_field")


def crawl_components(
    conn: sqlite3.Connection,
    pool: ProxyPool,
    ssl_ctx: ssl.SSLContext,
    stop_event: threading.Event,
    workers: int = 10,
) -> tuple[int, int]:
    symbol_uuids = {r[0] for r in conn.execute("SELECT DISTINCT symbol_uuid FROM devices").fetchall()}
    footprint_uuids = {r[0] for r in conn.execute("SELECT DISTINCT footprint_uuid FROM devices").fetchall()}
    all_uuids = symbol_uuids | footprint_uuids

    already = {r[0] for r in conn.execute("SELECT uuid FROM components").fetchall()}
    remaining = [u for u in all_uuids if u not in already]
    log.info(
        "Components: %d unique (sym=%d, fp=%d), %d already fetched, %d remaining",
        len(all_uuids), len(symbol_uuids), len(footprint_uuids), len(already), len(remaining),
    )

    if not remaining:
        return len(already), 0

    ok = 0
    errors = 0
    retries: list[str] = []
    write_lock = threading.Lock()

    def process_result(uuid: str, data: str | None, doc_type: int | None, info: str | None) -> None:
        nonlocal ok, errors
        with write_lock:
            if data is None:
                if info and "rate_limited" in info:
                    retries.append(uuid)
                else:
                    log.warning("Component %s failed: %s", uuid, info)
                    errors += 1
                return

            conn.execute(
                "INSERT OR IGNORE INTO components (uuid, title, doc_type, data) VALUES (?, ?, ?, ?)",
                (uuid, info or "", doc_type or 0, data),
            )
            ok += 1

            total = ok + errors
            if total % 50 == 0:
                conn.commit()
                ps = pool.stats()
                log.info(
                    "Components [%d/%d] ok=%d err=%d retry=%d | proxies: %d/%d",
                    total, len(remaining), ok, errors, len(retries), ps["active"], ps["total"],
                )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {}
        idx = 0

        def submit_batch() -> None:
            nonlocal idx
            while len(futures) < workers * 2 and not stop_event.is_set():
                uuid = None
                with write_lock:
                    if retries:
                        uuid = retries.pop(0)
                if uuid is None:
                    if idx < len(remaining):
                        uuid = remaining[idx]
                        idx += 1
                    else:
                        break
                fut = executor.submit(_fetch_one_component, uuid, pool, ssl_ctx)
                futures[fut] = uuid

        submit_batch()

        while futures and not stop_event.is_set():
            done = [f for f in futures if f.done()]
            for fut in done:
                del futures[fut]
                try:
                    process_result(*fut.result())
                except Exception as e:
                    log.error("Worker exception: %s", e)
                    with write_lock:
                        errors += 1
            submit_batch()
            if not done:
                time.sleep(0.05)

    conn.commit()
    return ok, errors


# ---------------------------------------------------------------------------
# Phase 3: fetch 3D models
# ---------------------------------------------------------------------------

def _resolve_model_step_uuid(
    model_component_uuid: str,
    pool: ProxyPool,
    ssl_ctx: ssl.SSLContext,
) -> tuple[str, str | None, str | None]:
    """Fetch model component metadata to get the actual STEP file UUID.
    Returns (model_component_uuid, step_uuid, title)."""
    proxy = pool.acquire()
    url = COMPONENT_URL.format(uuid=model_component_uuid)

    try:
        raw = _fetch_with_proxy(url, proxy, ssl_ctx)
        pool.report_success(proxy)
    except RateLimitError:
        pool.report_rate_limited(proxy)
        return (model_component_uuid, None, None)
    except Exception:
        pool.report_error(proxy)
        return (model_component_uuid, None, None)

    if raw is None:
        return (model_component_uuid, None, None)

    try:
        result = json.loads(raw).get("result", {})
    except json.JSONDecodeError:
        return (model_component_uuid, None, None)

    step_uuid = result.get("3d_model_uuid", "")
    if not step_uuid:
        data_str = result.get("dataStr", "")
        if isinstance(data_str, str):
            try:
                ds = json.loads(data_str)
                step_uuid = ds.get("model", "")
            except json.JSONDecodeError:
                pass

    title = result.get("display_title") or result.get("title", "")
    return (model_component_uuid, step_uuid or None, title)


def _fetch_step(
    step_uuid: str,
    pool: ProxyPool,
    ssl_ctx: ssl.SSLContext,
) -> bytes | None:
    proxy = pool.acquire()
    url = STEP_CDN.format(uuid=step_uuid)
    try:
        raw = _fetch_with_proxy(
            url, proxy, ssl_ctx,
            headers={"Content-Type": "", "Accept": "*/*"},
        )
        pool.report_success(proxy)
        return raw
    except RateLimitError:
        pool.report_rate_limited(proxy)
        return None
    except Exception:
        pool.report_error(proxy)
        return None


def crawl_models(
    conn: sqlite3.Connection,
    pool: ProxyPool,
    ssl_ctx: ssl.SSLContext,
    stop_event: threading.Event,
    workers: int = 10,
) -> tuple[int, int]:
    model_uuids = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT model_uuid FROM devices WHERE model_uuid IS NOT NULL AND model_uuid != ''"
        ).fetchall()
    }
    already = {r[0] for r in conn.execute("SELECT uuid FROM models").fetchall()}
    remaining = [u for u in model_uuids if u not in already]
    log.info("Models: %d unique, %d already fetched, %d remaining", len(model_uuids), len(already), len(remaining))

    if not remaining:
        return len(already), 0

    ok = 0
    errors = 0
    write_lock = threading.Lock()

    def process_one(model_comp_uuid: str) -> None:
        nonlocal ok, errors

        if stop_event.is_set():
            return

        comp_uuid, step_uuid, title = _resolve_model_step_uuid(model_comp_uuid, pool, ssl_ctx)
        if step_uuid is None:
            with write_lock:
                log.warning("Could not resolve STEP UUID for model component %s", comp_uuid)
                errors += 1
            return

        step_data = _fetch_step(step_uuid, pool, ssl_ctx)
        if step_data is None:
            with write_lock:
                log.warning("Could not fetch STEP for %s (step_uuid=%s)", comp_uuid, step_uuid)
                errors += 1
            return

        compressed = gzip.compress(step_data, compresslevel=6)

        with write_lock:
            conn.execute(
                "INSERT OR IGNORE INTO models (uuid, title, step_data, size_bytes) VALUES (?, ?, ?, ?)",
                (comp_uuid, title or "", compressed, len(step_data)),
            )
            ok += 1

            total = ok + errors
            if total % 20 == 0:
                conn.commit()
                ps = pool.stats()
                log.info(
                    "Models [%d/%d] ok=%d err=%d | proxies: %d/%d",
                    total, len(remaining), ok, errors, ps["active"], ps["total"],
                )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(process_one, u): u for u in remaining[:workers * 2]}
        idx = min(workers * 2, len(remaining))

        while futures and not stop_event.is_set():
            done = [f for f in futures if f.done()]
            for fut in done:
                del futures[fut]
                try:
                    fut.result()
                except Exception as e:
                    log.error("Model worker exception: %s", e)
                    with write_lock:
                        errors += 1

                if idx < len(remaining) and not stop_event.is_set():
                    new_fut = executor.submit(process_one, remaining[idx])
                    futures[new_fut] = remaining[idx]
                    idx += 1

            if not done:
                time.sleep(0.05)

    conn.commit()
    return ok, errors


# ---------------------------------------------------------------------------
# Main crawl orchestration
# ---------------------------------------------------------------------------

def crawl(
    parts_db_path: Path | None = None,
    cache_db_path: Path | None = None,
    proxy_file: str | Path | None = None,
    workers: int = 10,
    basic_only: bool = True,
) -> None:
    parts_db_path = parts_db_path or OUTPUT_DB
    cache_db_path = cache_db_path or V2_CACHE_DB

    if not parts_db_path.exists():
        log.error("Parts DB not found: %s", parts_db_path)
        sys.exit(1)

    if basic_only:
        lcsc_ids = get_basic_lcsc_ids(parts_db_path)
        log.info("Basic/preferred parts: %d", len(lcsc_ids))
    else:
        lcsc_ids = get_all_lcsc_ids(parts_db_path)
        log.info("All parts: %d", len(lcsc_ids))

    if not lcsc_ids:
        log.error("No parts found")
        return

    proxies: list[SocksProxy] = []
    if proxy_file:
        proxies = load_proxies(Path(proxy_file))
    if not proxies:
        log.error("No proxies loaded — proxy file required")
        return

    pool = ProxyPool(proxies, min_interval=MIN_PROXY_INTERVAL)
    ssl_ctx = _create_ssl_context()
    conn = init_v2_db(cache_db_path)

    stop_event = threading.Event()

    def handle_signal(sig: int, frame: object) -> None:
        stop_event.set()
        log.info("Stop requested — finishing current work...")

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    start = time.monotonic()
    log.info("Loaded %d proxies, %d workers", len(proxies), workers)

    log.info("=== Phase 1: searchByCodes → devices ===")
    dev_ok, dev_err = crawl_devices(lcsc_ids, conn, pool, ssl_ctx, stop_event)
    log.info("Devices done: %d ok, %d errors", dev_ok, dev_err)

    if stop_event.is_set():
        conn.close()
        return

    log.info("=== Phase 2: fetch components (symbols + footprints) ===")
    comp_ok, comp_err = crawl_components(conn, pool, ssl_ctx, stop_event, workers=workers)
    log.info("Components done: %d ok, %d errors", comp_ok, comp_err)

    if stop_event.is_set():
        conn.close()
        return

    log.info("=== Phase 3: fetch 3D models (STEP) ===")
    model_ok, model_err = crawl_models(conn, pool, ssl_ctx, stop_event, workers=workers)
    log.info("Models done: %d ok, %d errors", model_ok, model_err)

    conn.close()
    elapsed = time.monotonic() - start
    log.info(
        "Crawl complete in %.0fs: devices=%d components=%d models=%d",
        elapsed, dev_ok, comp_ok, model_ok,
    )


def show_status(cache_db_path: Path | None = None) -> None:
    cache_db_path = cache_db_path or V2_CACHE_DB
    if not cache_db_path.exists():
        print("No v2 cache database found. Run crawl first.")
        return

    conn = sqlite3.connect(f"file:{cache_db_path}?mode=ro", uri=True)

    devices = conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
    components = conn.execute("SELECT COUNT(*) FROM components").fetchone()[0]
    symbols = conn.execute("SELECT COUNT(*) FROM components WHERE doc_type = 2").fetchone()[0]
    footprints = conn.execute("SELECT COUNT(*) FROM components WHERE doc_type = 4").fetchone()[0]
    models_total = conn.execute("SELECT COUNT(*) FROM models").fetchone()[0]
    models_with_step = conn.execute("SELECT COUNT(*) FROM models WHERE step_data IS NOT NULL").fetchone()[0]
    step_size = conn.execute("SELECT COALESCE(SUM(size_bytes), 0) FROM models WHERE step_data IS NOT NULL").fetchone()[0]

    conn.close()
    db_size = os.path.getsize(cache_db_path)

    print(f"V2 Cache DB:     {cache_db_path} ({db_size / (1024**2):.1f} MB)")
    print(f"  Devices:       {devices:,}")
    print(f"  Components:    {components:,} (symbols={symbols:,}, footprints={footprints:,})")
    print(f"  3D Models:     {models_with_step:,} / {models_total:,} with STEP data")
    print(f"  STEP size:     {step_size / (1024**2):.1f} MB (uncompressed)")


def main() -> None:
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="EasyEDA Pro v2 crawler")
    sub = parser.add_subparsers(dest="command")

    crawl_p = sub.add_parser("crawl", help="Run the v2 crawler")
    crawl_p.add_argument("--proxy-file", required=True)
    crawl_p.add_argument("--workers", type=int, default=10)
    crawl_p.add_argument("--parts-db", type=Path, default=None)
    crawl_p.add_argument("--cache-db", type=Path, default=None)
    crawl_p.add_argument("--all", action="store_true", help="Crawl all parts (not just basic/preferred)")

    sub.add_parser("status", help="Show crawl status")

    args = parser.parse_args()

    if args.command == "crawl":
        crawl(
            parts_db_path=args.parts_db,
            cache_db_path=args.cache_db,
            proxy_file=args.proxy_file,
            workers=args.workers,
            basic_only=not args.all,
        )
    elif args.command == "status":
        show_status()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
