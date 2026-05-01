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
import re
import time
import threading
import httpx
import psutil
import psycopg2
import psycopg2.pool
import yaml
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Config loader — re-read on every request so hot-reload works after deploy
# ---------------------------------------------------------------------------
CONFIG_DIR = Path(__file__).parent / "config"


def load_config(name: str) -> dict[str, Any]:
    with open(CONFIG_DIR / name, "r") as f:
        return yaml.safe_load(f)


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
_cache_lock = threading.Lock()


def cache_get(key: str) -> bytes | None:
    with _cache_lock:
        return _cache.get(key)


def cache_set(key: str, value: bytes) -> None:
    app_cfg = get_app_cfg()
    cache_cfg = app_cfg["cache"]
    eviction_enabled = cache_cfg.get("eviction_enabled", True)
    max_mb = cache_cfg.get("max_size_mb", 50)

    with _cache_lock:
        if eviction_enabled and max_mb > 0:
            # Simple LRU-like eviction: if over limit, drop oldest half
            current_mb = sum(len(v) for v in _cache.values()) / (1024 * 1024)
            if current_mb > max_mb:
                keys = list(_cache.keys())
                for k in keys[: len(keys) // 2]:
                    del _cache[k]
        # When eviction_enabled=false or max_mb=0, cache grows unbounded → memory leak
        _cache[key] = value


# ---------------------------------------------------------------------------
# DB pool (rebuilt each time config changes)
# ---------------------------------------------------------------------------
_db_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_db_pool_lock = threading.Lock()


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
    svc_cfg = get_svc_cfg()
    url = svc_cfg["downstream"]["payment_gateway_url"]
    timeout = svc_cfg["downstream"]["timeout_s"]
    cb_enabled = svc_cfg["circuit_breaker"]["enabled"]

    if cb_enabled and _circuit_open:
        return {"status": "circuit_open", "skipped": True}

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url)
            _record_downstream_success()
            return {"status": r.status_code, "ok": r.is_success}
    except Exception as e:
        _record_downstream_failure()
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


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(_circuit_reset_task())
    yield
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
    Exercises all configured failure modes on each call:
      1. Validates payload (CPU scenario)
      2. Reads/writes cache (memory scenario)
      3. Queries DB (db_pool scenario)
      4. Calls downstream (cascade scenario)
    """
    start = time.perf_counter()
    errors = []

    # 1. CPU saturation — heavy validation
    try:
        validate_payload(payload)
    except Exception as e:
        errors.append(f"validation: {e}")

    # 2. Memory leak — cache with optional eviction
    cache_key = f"resp:{payload[:32]}"
    cached = cache_get(cache_key)
    if not cached:
        data = b"x" * (1024 * 4)  # 4 KB per cache entry
        cache_set(cache_key, data)

    # 3. DB pool exhaustion — acquire + release a connection
    db_wait_start = time.perf_counter()
    db_wait_ms = 0.0
    pool = get_db_pool()
    if pool:
        try:
            conn = pool.getconn()  # blocks when pool exhausted
            db_wait_ms = (time.perf_counter() - db_wait_start) * 1000
            try:
                cur = conn.cursor()
                cur.execute("SELECT 1")
                cur.fetchone()
                cur.close()
            finally:
                pool.putconn(conn)
        except Exception as e:
            db_wait_ms = (time.perf_counter() - db_wait_start) * 1000
            errors.append(f"db: {e}")

    # 4. Cascade failure — call downstream
    downstream_ok = True
    try:
        await call_downstream()
    except HTTPException as e:
        downstream_ok = False
        errors.append(f"downstream: {e.detail}")

    duration_ms = (time.perf_counter() - start) * 1000

    if errors and not downstream_ok:
        raise HTTPException(status_code=503, detail={"errors": errors, "duration_ms": duration_ms})
    if errors:
        return JSONResponse(status_code=500, content={"errors": errors, "duration_ms": duration_ms})

    return {
        "ok": True,
        "duration_ms": round(duration_ms, 1),
        "db_wait_ms": round(db_wait_ms, 1),
        "cache_size_mb": round(sum(len(v) for v in _cache.values()) / (1024 * 1024), 2),
    }


@app.get("/metrics/snapshot")
async def metrics_snapshot():
    """
    Returns real-time metric values read by metrics_emitter.py.
    This endpoint is polled every 30s by the emitter.
    """
    mem = psutil.virtual_memory()
    cpu = psutil.cpu_percent(interval=0.1)
    return {
        "cpu_percent": round(cpu, 1),
        "memory_percent": round(mem.percent, 1),
        "memory_used_mb": round(mem.used / (1024**2), 1),
        "cache_size_mb": round(sum(len(v) for v in _cache.values()) / (1024 * 1024), 2),
        "circuit_open": _circuit_open,
        "failure_count": _failure_count,
    }


@app.post("/admin/reload-config")
async def reload_config():
    """
    Called by GitHub Actions after deploying new config files.
    Resets the DB pool so it picks up the new max_pool_size.
    """
    reset_db_pool()
    return {"ok": True, "message": "Config reloaded — DB pool reset"}
