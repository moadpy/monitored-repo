"""
Chaos App — payment-api simulation

A FastAPI microservice whose behavior is controlled entirely by YAML config files.
Merging a config change PR causes the app to fail in a specific, observable way
that maps 1:1 to an RCA incident signature.

Signatures implemented:
  - db_pool_exhaustion       (config/db.yml  → max_pool_size: 10)
  - memory_leak_progressive  (config/app.yml → cache.eviction_enabled: false)
  - cpu_saturation_burst     (config/app.yml → validation.enable_heavy_validation: true)
  - cascade_failure          (config/services.yml → downstream_url: dead-host)
  - network_partition        (test-infra/terraform.tfvars → nsg_block_outbound: true)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import threading
import httpx
import psycopg2
import psycopg2.pool
import yaml
from collections import deque
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Config loader — re-read on every request so hot-reload works after deploy
# ---------------------------------------------------------------------------
CONFIG_DIR = Path(__file__).parent / "config"
SERVICE_NAME = os.getenv("SERVICE_NAME", "payment-api")
METRICS_WINDOW_SECONDS = 30
WORKER_METRICS_DIR = Path("/tmp/payment-api-metrics")
_config_cache: dict[str, tuple[int, dict[str, Any]]] = {}
_config_lock = threading.Lock()


def load_config(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    stat = path.stat()
    with _config_lock:
        cached = _config_cache.get(name)
        if cached and cached[0] == stat.st_mtime_ns:
            return cached[1]

        with open(path, "r") as f:
            loaded = yaml.safe_load(f)
        _config_cache[name] = (stat.st_mtime_ns, loaded)
        return loaded


def reset_config_cache() -> None:
    with _config_lock:
        _config_cache.clear()


def get_db_cfg() -> dict:
    return load_config("db.yml")["database"]


def get_app_cfg() -> dict:
    return load_config("app.yml")


def get_svc_cfg() -> dict:
    return load_config("services.yml")


# ---------------------------------------------------------------------------
# In-memory cache (for memory leak scenario)
# ---------------------------------------------------------------------------
_cache: dict[str, bytes] = {}
_cache_bytes = 0
_cache_seq = 0
_cache_lock = threading.Lock()
_metrics_lock = threading.Lock()


def cache_get(key: str) -> bytes | None:
    with _cache_lock:
        return _cache.get(key)


def cache_set(key: str, value: bytes) -> None:
    global _cache_bytes
    app_cfg = get_app_cfg()
    cache_cfg = app_cfg["cache"]
    eviction_enabled = cache_cfg.get("eviction_enabled", True)
    max_mb = cache_cfg.get("max_size_mb", 50)

    with _cache_lock:
        if eviction_enabled and max_mb > 0:
            # Simple LRU-like eviction: if over limit, drop oldest half
            current_mb = _cache_bytes / (1024 * 1024)
            if current_mb > max_mb:
                keys = list(_cache.keys())
                for k in keys[: len(keys) // 2]:
                    _cache_bytes -= len(_cache[k])
                    del _cache[k]
        # When eviction_enabled=false or max_mb=0, cache grows unbounded → memory leak
        existing = _cache.get(key)
        if existing is not None:
            _cache_bytes -= len(existing)
        _cache[key] = value
        _cache_bytes += len(value)


def get_cache_size_mb() -> float:
    with _cache_lock:
        return _cache_bytes / (1024 * 1024)


def cache_tick() -> None:
    global _cache_seq
    key = f"bg:{_cache_seq}"
    _cache_seq += 1
    cache_set(key, b"x" * (1024 * 1024 * 1))


# ---------------------------------------------------------------------------
# DB pool (rebuilt each time config changes)
# ---------------------------------------------------------------------------
_db_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_db_pool_lock = threading.Lock()
_downstream_client: httpx.AsyncClient | None = None
_last_db_wait_ms = 0.0
_last_request_duration_ms = 0.0
_request_window: deque[tuple[float, float, bool]] = deque()
_downstream_cache: tuple[str, float, dict[str, Any]] | None = None
_downstream_cache_lock = threading.Lock()


def get_db_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _db_pool
    with _db_pool_lock:
        cfg = get_db_cfg()
        if _db_pool is None:
            try:
                _db_pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn=1,
                    maxconn=cfg["max_pool_size"],
                    host=cfg["host"],
                    port=cfg["port"],
                    dbname=cfg["name"],
                    user=cfg["user"],
                    password=cfg["password"],
                    connect_timeout=cfg["connection_timeout_s"],
                )
            except Exception:
                _db_pool = None
    return _db_pool


def reset_db_pool() -> None:
    """Call after config change to rebuild pool with new max_pool_size."""
    global _db_pool
    with _db_pool_lock:
        if _db_pool:
            try:
                _db_pool.closeall()
            except Exception:
                pass
        _db_pool = None


def get_last_db_wait_ms() -> float:
    with _metrics_lock:
        return _last_db_wait_ms


def set_last_db_wait_ms(value: float) -> None:
    global _last_db_wait_ms
    with _metrics_lock:
        _last_db_wait_ms = value


def set_last_request_duration_ms(value: float) -> None:
    global _last_request_duration_ms
    with _metrics_lock:
        _last_request_duration_ms = value


def _trim_request_window(now_ts: float) -> None:
    cutoff = now_ts - METRICS_WINDOW_SECONDS
    while _request_window and _request_window[0][0] < cutoff:
        _request_window.popleft()


def record_request_sample(duration_ms: float, is_error: bool) -> None:
    now_ts = time.time()
    with _metrics_lock:
        _request_window.append((now_ts, duration_ms, is_error))
        _trim_request_window(now_ts)


def _local_request_window_stats(now_ts: float | None = None) -> dict[str, Any]:
    current_ts = now_ts if now_ts is not None else time.time()
    with _metrics_lock:
        _trim_request_window(current_ts)
        samples = list(_request_window)

    durations = [round(sample[1], 2) for sample in samples]
    error_count = sum(1 for _, _, is_error in samples if is_error)
    return {
        "request_count": len(samples),
        "error_count": error_count,
        "durations_ms": durations,
    }


def _worker_snapshot_path() -> Path:
    return WORKER_METRICS_DIR / f"{os.getpid()}.json"


def _write_worker_snapshot() -> None:
    WORKER_METRICS_DIR.mkdir(parents=True, exist_ok=True)
    stats = _local_request_window_stats()
    payload = {
        "pid": os.getpid(),
        "service_name": SERVICE_NAME,
        "window_seconds": METRICS_WINDOW_SECONDS,
        "captured_at": time.time(),
        "request_count": stats["request_count"],
        "error_count": stats["error_count"],
        "durations_ms": stats["durations_ms"],
        "db_conn_pool_wait_ms": round(get_last_db_wait_ms(), 2),
        "cache_size_mb": round(get_cache_size_mb(), 2),
        "circuit_open": _circuit_open,
        "failure_count": _failure_count,
    }
    tmp_path = _worker_snapshot_path().with_suffix(".tmp")
    tmp_path.write_text(json.dumps(payload), encoding="utf-8")
    tmp_path.replace(_worker_snapshot_path())


def _remove_worker_snapshot() -> None:
    with suppress(FileNotFoundError):
        _worker_snapshot_path().unlink()


def _aggregate_worker_snapshots() -> dict[str, Any]:
    now_ts = time.time()
    durations: list[float] = []
    request_count = 0
    error_count = 0
    db_wait_values: list[float] = []
    cache_size_total = 0.0
    circuit_open = False
    failure_count = 0

    if not WORKER_METRICS_DIR.exists():
        return {
            "service_name": SERVICE_NAME,
            "window_seconds": METRICS_WINDOW_SECONDS,
            "request_count": 0,
            "error_count": 0,
            "http_5xx_rate_pct": 0.0,
            "request_latency_p99_ms": 0.0,
            "db_conn_pool_wait_ms": 0.0,
            "cache_size_mb": 0.0,
            "circuit_open": False,
            "failure_count": 0,
        }

    for snapshot_path in WORKER_METRICS_DIR.glob("*.json"):
        try:
            payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        captured_at = float(payload.get("captured_at", 0.0))
        if captured_at < now_ts - (METRICS_WINDOW_SECONDS * 2):
            with suppress(OSError):
                snapshot_path.unlink()
            continue

        request_count += int(payload.get("request_count", 0))
        error_count += int(payload.get("error_count", 0))
        durations.extend(float(value) for value in payload.get("durations_ms", []))
        db_wait_values.append(float(payload.get("db_conn_pool_wait_ms", 0.0)))
        cache_size_total += float(payload.get("cache_size_mb", 0.0))
        circuit_open = circuit_open or bool(payload.get("circuit_open", False))
        failure_count = max(failure_count, int(payload.get("failure_count", 0)))

    if durations:
        durations.sort()
        p99_index = min(len(durations) - 1, max(0, int(len(durations) * 0.99) - 1))
        request_latency_p99_ms = round(durations[p99_index], 2)
    else:
        request_latency_p99_ms = 0.0

    http_5xx_rate_pct = round((error_count / request_count) * 100, 2) if request_count else 0.0

    return {
        "service_name": SERVICE_NAME,
        "window_seconds": METRICS_WINDOW_SECONDS,
        "request_count": request_count,
        "error_count": error_count,
        "http_5xx_rate_pct": http_5xx_rate_pct,
        "request_latency_p99_ms": request_latency_p99_ms,
        "db_conn_pool_wait_ms": round(max(db_wait_values) if db_wait_values else 0.0, 2),
        "cache_size_mb": round(cache_size_total, 2),
        "circuit_open": circuit_open,
        "failure_count": failure_count,
    }


# ---------------------------------------------------------------------------
# Heavy validation (for CPU saturation scenario)
# ---------------------------------------------------------------------------
SIMPLE_REGEX = re.compile(r"^\w+$")
# Catastrophic backtracking regex — burns CPU on long strings
HEAVY_REGEX = re.compile(r"^(a+)+$")


def validate_payload(data: str) -> bool:
    app_cfg = get_app_cfg()
    if app_cfg["validation"].get("enable_heavy_validation", False):
        complexity = app_cfg["validation"].get("regex_complexity", "low")
        if complexity == "high":
            # This is intentionally slow — catastrophic backtracking simulation
            target = "a" * 30 + "!"
            try:
                HEAVY_REGEX.match(target)
            except Exception:
                pass
            return True
    return bool(SIMPLE_REGEX.match(data[:100]))


# ---------------------------------------------------------------------------
# Downstream call (for cascade_failure scenario)
# ---------------------------------------------------------------------------
async def call_downstream() -> dict:
    global _downstream_cache
    svc_cfg = get_svc_cfg()
    url = svc_cfg["downstream"]["payment_gateway_url"]
    timeout = svc_cfg["downstream"]["timeout_s"]
    cb_enabled = svc_cfg["circuit_breaker"]["enabled"]
    now = time.monotonic()

    if cb_enabled and _circuit_open:
        return {"status": "circuit_open", "skipped": True}

    with _downstream_cache_lock:
        cached = _downstream_cache
        if cb_enabled and cached and cached[0] == url and now - cached[1] < 0.25:
            return cached[2]

    try:
        client = _downstream_client
        if client is None:
            async with httpx.AsyncClient(timeout=timeout) as fallback_client:
                r = await fallback_client.get(url)
        else:
            r = await client.get(url, timeout=timeout)
        _record_downstream_success()
        result = {"status": r.status_code, "ok": r.is_success}
        with _downstream_cache_lock:
            _downstream_cache = (url, now, result)
        return result
    except Exception as e:
        _record_downstream_failure()
        with _downstream_cache_lock:
            _downstream_cache = None
        raise HTTPException(status_code=502, detail=f"Downstream failed: {e}")


# ---------------------------------------------------------------------------
# Circuit breaker state
# ---------------------------------------------------------------------------
_circuit_open = False
_failure_count = 0
_last_failure_time = 0.0


def _record_downstream_failure() -> None:
    global _circuit_open, _failure_count, _last_failure_time
    _failure_count += 1
    _last_failure_time = time.monotonic()
    svc_cfg = get_svc_cfg()
    threshold = svc_cfg["circuit_breaker"]["failure_threshold"]
    if _failure_count >= threshold:
        _circuit_open = True


def _record_downstream_success() -> None:
    global _circuit_open, _failure_count
    _failure_count = 0
    _circuit_open = False


async def _circuit_reset_task() -> None:
    """Periodically try to half-open the circuit."""
    while True:
        await asyncio.sleep(5)
        global _circuit_open, _failure_count
        if _circuit_open:
            svc_cfg = get_svc_cfg()
            recovery = svc_cfg["circuit_breaker"].get("recovery_timeout_s", 30)
            if time.monotonic() - _last_failure_time > recovery:
                _circuit_open = False
                _failure_count = 0


def _db_probe_once() -> float:
    pool = get_db_pool()
    if pool is None:
        return 0.0

    started = time.perf_counter()
    conn = pool.getconn()
    wait_ms = (time.perf_counter() - started) * 1000
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        time.sleep(0.35)
        cur.close()
    finally:
        pool.putconn(conn)
    return wait_ms


async def _db_probe_task() -> None:
    while True:
        cfg = get_db_cfg()
        max_pool_size = int(cfg.get("max_pool_size", 100))
        if max_pool_size >= 100:
            set_last_db_wait_ms(0.0)
            await asyncio.sleep(1)
            continue

        probe_width = min(max(max_pool_size + 6, 12), 20)
        results = await asyncio.gather(
            *(asyncio.to_thread(_db_probe_once) for _ in range(probe_width)),
            return_exceptions=True,
        )
        waits = [float(item) for item in results if not isinstance(item, Exception)]
        if waits:
            set_last_db_wait_ms(max(waits))
        await asyncio.sleep(1)


async def _cache_maintenance_task() -> None:
    while True:
        cache_tick()
        await asyncio.sleep(1)


async def _publish_metrics_snapshot_task() -> None:
    while True:
        _write_worker_snapshot()
        await asyncio.sleep(1)


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _downstream_client
    _downstream_client = httpx.AsyncClient()
    circuit_task = asyncio.create_task(_circuit_reset_task())
    cache_task = asyncio.create_task(_cache_maintenance_task())
    db_task = asyncio.create_task(_db_probe_task())
    snapshot_task = asyncio.create_task(_publish_metrics_snapshot_task())
    try:
        yield
    finally:
        for task in (circuit_task, cache_task, db_task, snapshot_task):
            task.cancel()
        with suppress(Exception):
            await asyncio.gather(circuit_task, cache_task, db_task, snapshot_task, return_exceptions=True)
        if _downstream_client is not None:
            await _downstream_client.aclose()
            _downstream_client = None
        _remove_worker_snapshot()
        reset_db_pool()


app = FastAPI(
    title="payment-api (chaos-app)",
    version="1.0.0",
    description="Monitored microservice whose config changes cause observable incidents.",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    """Liveness probe — always returns 200 so the VM stays reachable."""
    return {"status": "ok", "service": "payment-api"}


@app.get("/api/process")
async def process_request(payload: str = "hello"):
    """
    Main endpoint hit by load_generator.py.
    Keeps the steady-state path cheap and only becomes expensive when
    the incident-specific config enables it.
    """
    start = time.perf_counter()
    errors = []

    # 1. CPU saturation — heavy validation
    try:
        validate_payload(payload)
    except Exception as e:
        errors.append(f"validation: {e}")

    # 2. Cascade failure — call downstream
    downstream_ok = True
    try:
        await call_downstream()
    except HTTPException as e:
        downstream_ok = False
        errors.append(f"downstream: {e.detail}")

    duration_ms = (time.perf_counter() - start) * 1000
    set_last_request_duration_ms(duration_ms)
    db_wait_ms = get_last_db_wait_ms()

    if errors and not downstream_ok:
        record_request_sample(duration_ms, is_error=True)
        raise HTTPException(status_code=503, detail={"errors": errors, "duration_ms": duration_ms})
    if errors:
        record_request_sample(duration_ms, is_error=True)
        return JSONResponse(status_code=500, content={"errors": errors, "duration_ms": duration_ms})

    record_request_sample(duration_ms, is_error=False)

    return {
        "ok": True,
        "duration_ms": round(duration_ms, 1),
        "db_wait_ms": round(db_wait_ms, 1),
        "cache_size_mb": round(get_cache_size_mb(), 2),
    }


@app.get("/metrics/snapshot")
async def metrics_snapshot():
    """
    Returns real-time metric values read by metrics_emitter.py.
    This endpoint is polled every 30s by the emitter.
    """
    _write_worker_snapshot()
    return _aggregate_worker_snapshots()


@app.post("/admin/reload-config")
async def reload_config():
    """
    Called by GitHub Actions after deploying new config files.
    Resets the DB pool so it picks up the new max_pool_size.
    """
    global _downstream_cache
    reset_config_cache()
    reset_db_pool()
    with _downstream_cache_lock:
        _downstream_cache = None
    return {"ok": True, "message": "Config reloaded — DB pool reset"}
