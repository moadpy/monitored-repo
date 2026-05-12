"""
Chaos App — payment-api simulation

This service exists to generate telemetry signatures that match the
incident-classification dataset in ml-repo/data/telemetry_labeled.csv.
The implementation therefore favors stable, repeatable metric envelopes
over "organic" failure behavior.
"""

from __future__ import annotations

import asyncio
import gc
import hashlib
import json
import math
import multiprocessing as mp
import os
import re
import time
import threading
from collections import Counter, deque
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

import httpx
import psycopg2
import psycopg2.pool
import yaml
from fastapi import FastAPI, HTTPException

CONFIG_DIR = Path(__file__).parent / "config"
SERVICE_NAME = os.getenv("SERVICE_NAME", "payment-api")
METRICS_WINDOW_SECONDS = 30
WORKER_METRICS_DIR = Path("/tmp/payment-api-metrics")
HEALTHY_PAYMENT_GATEWAY_URL = "http://downstream-service:8080/health"
DEFAULT_WORKER_COUNT = max(1, int(os.getenv("APP_WORKERS", "2")))
MEMORY_LEAK_TARGET_MB = max(4096, int(os.getenv("MEMORY_LEAK_TARGET_MB", "6144")))
MEMORY_LEAK_GROWTH_MB = max(32, int(os.getenv("MEMORY_LEAK_GROWTH_MB", "64")))

PROFILE_TARGETS: dict[str, dict[str, tuple[float, float]]] = {
    "normal_noisy": {
        "http_5xx_rate_pct": (0.3, 3.6),
        "request_latency_p99_ms": (60.0, 205.0),
        "db_conn_pool_wait_ms": (6.0, 28.0),
    },
    "memory_leak_progressive": {
        "http_5xx_rate_pct": (0.6, 4.4),
        "request_latency_p99_ms": (110.0, 300.0),
        "db_conn_pool_wait_ms": (10.0, 46.0),
    },
    "db_pool_exhaustion": {
        "http_5xx_rate_pct": (5.0, 18.0),
        "request_latency_p99_ms": (260.0, 810.0),
        "db_conn_pool_wait_ms": (220.0, 430.0),
    },
    "network_partition": {
        "http_5xx_rate_pct": (28.0, 47.0),
        "request_latency_p99_ms": (580.0, 1500.0),
        "db_conn_pool_wait_ms": (12.0, 50.0),
    },
    "cpu_saturation_burst": {
        "http_5xx_rate_pct": (10.0, 30.0),
        "request_latency_p99_ms": (760.0, 2200.0),
        "db_conn_pool_wait_ms": (25.0, 80.0),
    },
    "cascade_failure": {
        "http_5xx_rate_pct": (25.0, 48.0),
        "request_latency_p99_ms": (950.0, 2350.0),
        "db_conn_pool_wait_ms": (130.0, 380.0),
    },
}

MEMORY_TARGET_TOTAL_MB = {
    "normal_noisy": 64,
    "db_pool_exhaustion": 128,
    "network_partition": 160,
    "cpu_saturation_burst": 224,
    "memory_leak_progressive": MEMORY_LEAK_TARGET_MB,
    "cascade_failure": 2048,
}

MEMORY_GROWTH_MB = {
    "normal_noisy": 4,
    "db_pool_exhaustion": 8,
    "network_partition": 8,
    "cpu_saturation_burst": 12,
    "memory_leak_progressive": MEMORY_LEAK_GROWTH_MB,
    "cascade_failure": 16,
}

ERROR_SCHEDULES = {
    "normal_noisy": (0, frozenset()),
    "memory_leak_progressive": (40, frozenset({0})),
    "db_pool_exhaustion": (8, frozenset({0})),
    "network_partition": (5, frozenset({0, 2})),
    "cpu_saturation_burst": (6, frozenset({0})),
    "cascade_failure": (5, frozenset({0, 1})),
}

_config_cache: dict[str, tuple[int, dict[str, Any]]] = {}
_config_lock = threading.Lock()
_metrics_lock = threading.Lock()
_memory_lock = threading.Lock()
_sequence_lock = threading.Lock()
_db_pool_lock = threading.Lock()

_request_window: deque[tuple[float, float, bool]] = deque()
_recent_downstream_results: deque[tuple[float, bool]] = deque(maxlen=200)
_memory_blocks: deque[bytearray] = deque()
_memory_bytes = 0
_request_seq = 0
_last_db_wait_ms = 0.0
_last_request_duration_ms = 0.0

_db_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_db_pool_signature: tuple[Any, ...] | None = None
_downstream_client: httpx.AsyncClient | None = None

_circuit_open = False
_failure_count = 0
_last_failure_time = 0.0
_last_signature = "normal_noisy"

_cpu_burner_process: mp.Process | None = None
_cpu_burner_stop: mp.Event | None = None

SIMPLE_REGEX = re.compile(r"^[\w\-]+$")


def load_config(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    stat = path.stat()
    with _config_lock:
        cached = _config_cache.get(name)
        if cached and cached[0] == stat.st_mtime_ns:
            return cached[1]
        with open(path, "r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f)
        _config_cache[name] = (stat.st_mtime_ns, loaded)
        return loaded


def reset_config_cache() -> None:
    with _config_lock:
        _config_cache.clear()


def get_db_cfg() -> dict[str, Any]:
    return load_config("db.yml")["database"]


def get_app_cfg() -> dict[str, Any]:
    return load_config("app.yml")


def get_svc_cfg() -> dict[str, Any]:
    return load_config("services.yml")


def get_effective_downstream_url() -> str:
    cfg = get_svc_cfg()["downstream"]
    if cfg.get("use_external_dependency", False):
        return cfg["external_dependency_url"]
    return cfg["payment_gateway_url"]


def next_request_id() -> int:
    global _request_seq
    with _sequence_lock:
        _request_seq += 1
        return _request_seq


def get_last_db_wait_ms() -> float:
    with _metrics_lock:
        return _last_db_wait_ms


def set_last_db_wait_ms(value: float) -> None:
    global _last_db_wait_ms
    with _metrics_lock:
        _last_db_wait_ms = max(0.0, value)


def set_last_request_duration_ms(value: float) -> None:
    global _last_request_duration_ms
    with _metrics_lock:
        _last_request_duration_ms = max(0.0, value)


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


def record_downstream_result(ok: bool) -> None:
    now = time.time()
    _recent_downstream_results.append((now, ok))
    cutoff = now - (METRICS_WINDOW_SECONDS * 2)
    while _recent_downstream_results and _recent_downstream_results[0][0] < cutoff:
        _recent_downstream_results.popleft()


def recent_downstream_failure_rate() -> float:
    now = time.time()
    recent = [ok for ts, ok in _recent_downstream_results if ts >= now - METRICS_WINDOW_SECONDS]
    if not recent:
        return 0.0
    failures = sum(1 for ok in recent if not ok)
    return (failures / len(recent)) * 100.0


def get_intended_signature() -> str:
    svc_cfg = get_svc_cfg()
    app_cfg = get_app_cfg()
    db_cfg = get_db_cfg()

    if svc_cfg["downstream"].get("use_external_dependency", False):
        return "network_partition"

    if (
        app_cfg["validation"].get("enable_heavy_validation", False)
        and app_cfg["validation"].get("regex_complexity", "low") == "high"
    ):
        return "cpu_saturation_burst"

    if (
        not app_cfg["cache"].get("eviction_enabled", True)
        and int(app_cfg["cache"].get("max_size_mb", 50)) == 0
    ):
        return "memory_leak_progressive"

    payment_url = svc_cfg["downstream"].get("payment_gateway_url", "")
    circuit_enabled = svc_cfg["circuit_breaker"].get("enabled", True)
    if payment_url != HEALTHY_PAYMENT_GATEWAY_URL or not circuit_enabled:
        return "cascade_failure"

    if int(db_cfg.get("max_pool_size", 100)) <= 20:
        return "db_pool_exhaustion"

    return "normal_noisy"


def get_active_signature() -> str:
    intended = get_intended_signature()
    if intended == "network_partition" and recent_downstream_failure_rate() < 10.0:
        return "normal_noisy"
    return intended


def desired_worker_count() -> int:
    if WORKER_METRICS_DIR.exists():
        now = time.time()
        fresh = 0
        for path in WORKER_METRICS_DIR.glob("*.json"):
            with suppress(OSError, json.JSONDecodeError, ValueError):
                payload = json.loads(path.read_text(encoding="utf-8"))
                if float(payload.get("captured_at", 0.0)) >= now - 10:
                    fresh += 1
        if fresh > 0:
            return fresh
    return DEFAULT_WORKER_COUNT


def get_memory_pressure_mb() -> float:
    with _memory_lock:
        return _memory_bytes / (1024 * 1024)


def _grow_memory_pressure(chunk_mb: int) -> None:
    global _memory_bytes
    block = bytearray(chunk_mb * 1024 * 1024)
    with _memory_lock:
        _memory_blocks.append(block)
        _memory_bytes += len(block)


def _reduce_memory_pressure(target_bytes: int, max_blocks: int = 4) -> None:
    global _memory_bytes
    removed = 0
    with _memory_lock:
        while _memory_bytes > target_bytes and _memory_blocks and removed < max_blocks:
            block = _memory_blocks.pop()
            _memory_bytes -= len(block)
            removed += 1
    if removed:
        gc.collect()


def clear_memory_pressure() -> None:
    global _memory_bytes
    with _memory_lock:
        _memory_blocks.clear()
        _memory_bytes = 0
    gc.collect()


def _memory_target_for_signature(signature: str) -> tuple[int, int]:
    total_target_mb = MEMORY_TARGET_TOTAL_MB.get(signature, 64)
    chunk_mb = MEMORY_GROWTH_MB.get(signature, 8)
    workers = max(1, desired_worker_count())
    local_target_mb = max(32, math.ceil(total_target_mb / workers))
    return local_target_mb * 1024 * 1024, chunk_mb


def _cpu_burn_loop(stop_event: mp.Event) -> None:
    payload = b"payment-api-chaos" * 256
    salt = os.urandom(16)
    while not stop_event.is_set():
        hashlib.pbkdf2_hmac("sha256", payload, salt, 220_000, dklen=64)


def ensure_cpu_burner(active: bool) -> None:
    global _cpu_burner_process, _cpu_burner_stop
    if active:
        if _cpu_burner_process is not None and _cpu_burner_process.is_alive():
            return
        stop_event = mp.Event()
        process = mp.Process(target=_cpu_burn_loop, args=(stop_event,), daemon=True)
        process.start()
        _cpu_burner_stop = stop_event
        _cpu_burner_process = process
        return

    if _cpu_burner_stop is not None:
        _cpu_burner_stop.set()
    if _cpu_burner_process is not None:
        _cpu_burner_process.join(timeout=1.0)
        if _cpu_burner_process.is_alive():
            _cpu_burner_process.kill()
            _cpu_burner_process.join(timeout=1.0)
    _cpu_burner_stop = None
    _cpu_burner_process = None


def _burn_cpu_inline(iterations: int) -> None:
    payload = b"cpu-validation" * 192
    salt = b"rca-demo"
    hashlib.pbkdf2_hmac("sha256", payload, salt, iterations, dklen=32)


def validate_payload(data: str) -> None:
    if not SIMPLE_REGEX.match(data[:128]):
        raise ValueError("payload failed validation")

    cfg = get_app_cfg()["validation"]
    if cfg.get("enable_heavy_validation", False) and cfg.get("regex_complexity", "low") == "high":
        _burn_cpu_inline(120_000)


def get_db_pool() -> psycopg2.pool.ThreadedConnectionPool | None:
    global _db_pool, _db_pool_signature
    cfg = get_db_cfg()
    signature = (
        cfg["host"],
        cfg["port"],
        cfg["name"],
        cfg["user"],
        cfg["max_pool_size"],
        cfg["connection_timeout_s"],
    )
    with _db_pool_lock:
        if _db_pool is not None and _db_pool_signature != signature:
            with suppress(Exception):
                _db_pool.closeall()
            _db_pool = None
            _db_pool_signature = None

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
                _db_pool_signature = signature
            except Exception as exc:
                print(f"[db-pool] init failed: {exc}")
                _db_pool = None
                _db_pool_signature = None
    return _db_pool


def reset_db_pool() -> None:
    global _db_pool, _db_pool_signature
    with _db_pool_lock:
        if _db_pool is not None:
            with suppress(Exception):
                _db_pool.closeall()
        _db_pool = None
        _db_pool_signature = None


def _borrow_db_connection(timeout_s: float) -> tuple[psycopg2.extensions.connection | None, float]:
    pool = get_db_pool()
    if pool is None:
        return None, 0.0

    started = time.perf_counter()
    deadline = started + timeout_s
    while True:
        try:
            conn = pool.getconn()
            waited_ms = (time.perf_counter() - started) * 1000
            return conn, waited_ms
        except psycopg2.pool.PoolError:
            if time.perf_counter() >= deadline:
                raise
            time.sleep(0.01)


def _db_roundtrip(hold_s: float) -> float:
    cfg = get_db_cfg()
    conn, wait_ms = _borrow_db_connection(float(cfg.get("connection_timeout_s", 5)))
    if conn is None:
        return 0.0
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        time.sleep(hold_s)
        cur.close()
        return wait_ms
    finally:
        pool = get_db_pool()
        if pool is not None:
            pool.putconn(conn)


async def maybe_run_db_roundtrip(signature: str) -> float:
    if signature == "db_pool_exhaustion":
        wait_ms = await asyncio.to_thread(_db_roundtrip, 0.18)
    elif signature == "cascade_failure":
        wait_ms = await asyncio.to_thread(_db_roundtrip, 0.10)
    else:
        return 0.0

    set_last_db_wait_ms(max(get_last_db_wait_ms(), wait_ms))
    return wait_ms


async def _db_pressure_task() -> None:
    while True:
        signature = get_active_signature()
        if signature == "db_pool_exhaustion":
            width = min(int(get_db_cfg().get("max_pool_size", 10)), 10)
            hold_s = 0.42
            sleep_s = 0.06
        elif signature == "cascade_failure":
            width = 4
            hold_s = 0.28
            sleep_s = 0.18
        else:
            set_last_db_wait_ms(0.0)
            await asyncio.sleep(0.5)
            continue

        results = await asyncio.gather(
            *(asyncio.to_thread(_db_roundtrip, hold_s) for _ in range(max(1, width))),
            return_exceptions=True,
        )
        waits = [float(item) for item in results if not isinstance(item, Exception)]
        if waits:
            set_last_db_wait_ms(max(waits))
        await asyncio.sleep(sleep_s)


def _record_downstream_failure() -> None:
    global _circuit_open, _failure_count, _last_failure_time
    _failure_count += 1
    _last_failure_time = time.monotonic()
    cfg = get_svc_cfg()["circuit_breaker"]
    threshold = int(cfg.get("failure_threshold", 5))
    if cfg.get("enabled", True) and _failure_count >= threshold:
        _circuit_open = True


def _record_downstream_success() -> None:
    global _circuit_open, _failure_count
    _failure_count = 0
    _circuit_open = False


def clear_recent_downstream_results() -> None:
    _recent_downstream_results.clear()


def clear_runtime_pressures() -> None:
    global _circuit_open, _failure_count, _last_failure_time
    clear_memory_pressure()
    clear_recent_downstream_results()
    set_last_db_wait_ms(0.0)
    set_last_request_duration_ms(0.0)
    _circuit_open = False
    _failure_count = 0
    _last_failure_time = 0.0


async def _circuit_reset_task() -> None:
    global _circuit_open, _failure_count
    while True:
        await asyncio.sleep(5)
        if not _circuit_open:
            continue
        recovery_timeout_s = float(get_svc_cfg()["circuit_breaker"].get("recovery_timeout_s", 30))
        if time.monotonic() - _last_failure_time > recovery_timeout_s:
            _circuit_open = False
            _failure_count = 0


async def call_downstream() -> dict[str, Any]:
    cfg = get_svc_cfg()
    downstream_cfg = cfg["downstream"]
    url = get_effective_downstream_url()
    timeout_s = float(downstream_cfg.get("timeout_s", 1.2))
    retry_attempts = max(1, int(downstream_cfg.get("retry_attempts", 2)))
    retry_backoff_s = float(downstream_cfg.get("retry_backoff_s", 0.2))
    use_external_dependency = downstream_cfg.get("use_external_dependency", False)
    circuit_enabled = cfg["circuit_breaker"].get("enabled", True) and not use_external_dependency

    if circuit_enabled and _circuit_open:
        record_downstream_result(False)
        raise HTTPException(status_code=502, detail="circuit breaker open")

    last_error: Exception | None = None
    for attempt in range(1, retry_attempts + 1):
        try:
            client = _downstream_client
            if client is None:
                async with httpx.AsyncClient(timeout=timeout_s) as fallback_client:
                    response = await fallback_client.get(url, timeout=timeout_s)
            else:
                response = await client.get(url, timeout=timeout_s)

            if not response.is_success:
                raise RuntimeError(f"downstream returned {response.status_code}")

            _record_downstream_success()
            record_downstream_result(True)
            return {"ok": True, "status": response.status_code}
        except Exception as exc:
            last_error = exc
            if attempt < retry_attempts:
                await asyncio.sleep(min(retry_backoff_s * attempt, 0.5))

    _record_downstream_failure()
    record_downstream_result(False)
    detail = str(last_error) if last_error is not None else "unknown downstream error"
    raise HTTPException(status_code=502, detail=f"downstream failed after {retry_attempts} attempts: {detail}")


def should_fail_request(signature: str, request_id: int, downstream_failed: bool) -> bool:
    cycle, failures = ERROR_SCHEDULES.get(signature, (0, frozenset()))
    if signature in {"network_partition", "cascade_failure"} and not downstream_failed:
        return False
    if cycle <= 0:
        return False
    return request_id % cycle in failures


async def apply_signature_latency(signature: str, request_id: int, downstream_failed: bool) -> None:
    if signature == "normal_noisy":
        delay_ms = 15 if request_id % 9 == 0 else 0
    elif signature == "memory_leak_progressive":
        delay_ms = 90 + (request_id % 5) * 28
    elif signature == "db_pool_exhaustion":
        delay_ms = 120 + (request_id % 4) * 55
    elif signature == "network_partition":
        delay_ms = 320 + (request_id % 5) * 170 if downstream_failed else 0
    elif signature == "cpu_saturation_burst":
        delay_ms = 140 + (request_id % 5) * 85
    elif signature == "cascade_failure":
        delay_ms = 420 + (request_id % 5) * 210 if downstream_failed else 160
    else:
        delay_ms = 0

    if delay_ms > 0:
        await asyncio.sleep(delay_ms / 1000.0)


def maybe_add_cpu_work(signature: str, request_id: int) -> None:
    if signature == "cpu_saturation_burst":
        _burn_cpu_inline(70_000 + ((request_id % 3) * 25_000))
    elif signature == "cascade_failure":
        _burn_cpu_inline(45_000 + ((request_id % 2) * 20_000))


async def _scenario_governor_task() -> None:
    global _last_signature
    while True:
        signature = get_active_signature()
        if signature != _last_signature:
            # Recovery from the heavier signatures should be immediate once the
            # config is restored; otherwise the app can look "stuck" for minutes.
            if _last_signature in {"cascade_failure", "memory_leak_progressive"} and signature not in {
                "cascade_failure",
                "memory_leak_progressive",
            }:
                clear_runtime_pressures()
            elif _last_signature in {"db_pool_exhaustion", "network_partition", "cpu_saturation_burst"}:
                clear_recent_downstream_results()
                set_last_db_wait_ms(0.0)
            _last_signature = signature

        ensure_cpu_burner(signature in {"cpu_saturation_burst", "cascade_failure"})

        target_bytes, chunk_mb = _memory_target_for_signature(signature)
        current_bytes = int(get_memory_pressure_mb() * 1024 * 1024)
        if current_bytes < target_bytes:
            _grow_memory_pressure(chunk_mb)
        else:
            _reduce_memory_pressure(target_bytes)

        await asyncio.sleep(1)


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
        "cache_size_mb": round(get_memory_pressure_mb(), 2),
        "circuit_open": _circuit_open,
        "failure_count": _failure_count,
        "active_signature": get_active_signature(),
    }
    tmp_path = _worker_snapshot_path().with_suffix(".tmp")
    tmp_path.write_text(json.dumps(payload), encoding="utf-8")
    tmp_path.replace(_worker_snapshot_path())


def _remove_worker_snapshot() -> None:
    with suppress(FileNotFoundError):
        _worker_snapshot_path().unlink()


def _phase_target(signature: str, metric_name: str) -> float:
    low, high = PROFILE_TARGETS[signature][metric_name]
    seed = (len(metric_name) * 0.19) + ((os.getpid() % 11) * 0.11)
    period_s = 17.0 if signature in {"cascade_failure", "network_partition"} else 23.0
    phase = (time.time() / period_s) + seed
    return low + ((math.sin(phase) + 1.0) / 2.0) * (high - low)


def _shape_metric(signature: str, metric_name: str, raw_value: float) -> float:
    low, high = PROFILE_TARGETS[signature][metric_name]
    target = _phase_target(signature, metric_name)

    if signature == "normal_noisy":
        shaped = raw_value if raw_value > 0 else target
        return round(min(high, max(low, shaped)), 2)

    if raw_value <= 0:
        shaped = target
    else:
        raw_weight = 0.35 if signature in {"db_pool_exhaustion", "cpu_saturation_burst"} else 0.25
        shaped = (raw_value * raw_weight) + (target * (1.0 - raw_weight))

    return round(min(high, max(low, shaped)), 2)


def _aggregate_worker_snapshots() -> dict[str, Any]:
    now_ts = time.time()
    durations: list[float] = []
    request_count = 0
    error_count = 0
    db_wait_values: list[float] = []
    cache_size_total = 0.0
    circuit_open = False
    failure_count = 0
    signature_counts: Counter[str] = Counter()

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
            "active_signature": "normal_noisy",
            "raw_http_5xx_rate_pct": 0.0,
            "raw_request_latency_p99_ms": 0.0,
            "raw_db_conn_pool_wait_ms": 0.0,
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
        signature = str(payload.get("active_signature", "normal_noisy"))
        signature_counts[signature] += 1

    if durations:
        durations.sort()
        index = min(len(durations) - 1, max(0, int(len(durations) * 0.99) - 1))
        raw_latency_p99_ms = round(durations[index], 2)
    else:
        raw_latency_p99_ms = 0.0

    raw_http_5xx_rate_pct = round((error_count / request_count) * 100, 2) if request_count else 0.0
    raw_db_wait_ms = round(max(db_wait_values) if db_wait_values else 0.0, 2)
    active_signature = signature_counts.most_common(1)[0][0] if signature_counts else "normal_noisy"

    return {
        "service_name": SERVICE_NAME,
        "window_seconds": METRICS_WINDOW_SECONDS,
        "request_count": request_count,
        "error_count": error_count,
        "raw_http_5xx_rate_pct": raw_http_5xx_rate_pct,
        "raw_request_latency_p99_ms": raw_latency_p99_ms,
        "raw_db_conn_pool_wait_ms": raw_db_wait_ms,
        "http_5xx_rate_pct": _shape_metric(active_signature, "http_5xx_rate_pct", raw_http_5xx_rate_pct),
        "request_latency_p99_ms": _shape_metric(active_signature, "request_latency_p99_ms", raw_latency_p99_ms),
        "db_conn_pool_wait_ms": _shape_metric(active_signature, "db_conn_pool_wait_ms", raw_db_wait_ms),
        "cache_size_mb": round(cache_size_total, 2),
        "circuit_open": circuit_open,
        "failure_count": failure_count,
        "active_signature": active_signature,
    }


async def _publish_metrics_snapshot_task() -> None:
    while True:
        try:
            conn = pool.getconn()
            return conn, (time.perf_counter() - started) * 1000
        except psycopg2.pool.PoolError:
            if time.perf_counter() >= deadline:
                raise
            time.sleep(0.01)


def _hold_db_connection(hold_s: float) -> float:
    cfg = get_db_cfg()
    pool = get_db_pool()
    if pool is None:
        return 0.0

    conn, wait_ms = _borrow_db_connection(float(cfg.get("connection_timeout_s", 5)))
    if conn is None:
        return 0.0
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        time.sleep(hold_s)
        cur.close()
    finally:
        pool.putconn(conn)
    return wait_ms


def _cascade_cpu_burn(duration_s: float) -> None:
    deadline = time.perf_counter() + duration_s
    acc = 0
    while time.perf_counter() < deadline:
        for i in range(8000):
            acc = ((acc * 33) ^ i) % 1_000_003
    if acc == -1:
        print("unreachable")


def _cascade_pressure_active() -> bool:
    if get_svc_cfg()["downstream"].get("use_external_dependency", False):
        return False
    return _failure_count > 0 and (time.monotonic() - _last_failure_time) < CASCADE_PRESSURE_WINDOW_S


async def amplify_cascade_failure(payload: str) -> None:
    if get_svc_cfg()["downstream"].get("use_external_dependency", False):
        return

    _push_cascade_backlog(payload, chunk_mb=4)
    _cascade_cpu_burn(0.25)
    results = await asyncio.gather(
        *(asyncio.to_thread(_hold_db_connection, 0.5) for _ in range(2)),
        return_exceptions=True,
    )
    db_waits = [float(item) for item in results if not isinstance(item, Exception)]
    if db_waits:
        set_last_db_wait_ms(max(get_last_db_wait_ms(), max(db_waits)))


# ---------------------------------------------------------------------------
# Downstream call (for cascade_failure scenario)
# ---------------------------------------------------------------------------
async def call_downstream() -> dict:
    global _downstream_cache
    svc_cfg = get_svc_cfg()
    downstream_cfg = svc_cfg["downstream"]
    use_external = downstream_cfg.get("use_external_dependency", False)
    url = (
        downstream_cfg["external_dependency_url"]
        if use_external
        else downstream_cfg["payment_gateway_url"]
    )
    timeout = downstream_cfg["timeout_s"]
    retry_attempts = max(1, int(downstream_cfg.get("retry_attempts", 1)))
    retry_backoff_s = float(downstream_cfg.get("retry_backoff_s", 0.2))
    cb_enabled = svc_cfg["circuit_breaker"]["enabled"]
    cb_short_circuit_enabled = cb_enabled and not use_external
    now = time.monotonic()

    if cb_short_circuit_enabled and _circuit_open:
        return {"status": "circuit_open", "skipped": True}

    with _downstream_cache_lock:
        cached = _downstream_cache
        if cb_short_circuit_enabled and cached and cached[0] == url and now - cached[1] < 0.25:
            return cached[2]

    last_error: Exception | None = None
    for attempt in range(1, retry_attempts + 1):
        try:
            client = _downstream_client
            if client is None:
                async with httpx.AsyncClient(timeout=timeout) as fallback_client:
                    r = await fallback_client.get(url)
            else:
                r = await client.get(url, timeout=timeout)

            if not r.is_success:
                raise HTTPException(status_code=502, detail=f"Downstream returned status {r.status_code}")

            _record_downstream_success()
            result = {"status": r.status_code, "ok": True}
            with _downstream_cache_lock:
                _downstream_cache = (url, now, result)
            return result
        except Exception as e:
            last_error = e
            if attempt < retry_attempts:
                await asyncio.sleep(min(retry_backoff_s * attempt, 1.0))

    _record_downstream_failure()
    with _downstream_cache_lock:
        _downstream_cache = None

    if isinstance(last_error, HTTPException):
        detail = last_error.detail
    else:
        detail = str(last_error)
    raise HTTPException(status_code=502, detail=f"Downstream failed after {retry_attempts} attempts: {detail}")


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
    if svc_cfg["circuit_breaker"].get("enabled", True) and _failure_count >= threshold:
        _circuit_open = True


def _record_downstream_success() -> None:
    global _circuit_open, _failure_count
    _failure_count = 0
    _circuit_open = False
    _clear_cascade_backlog()


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
    cfg = get_db_cfg()
    pool = get_db_pool()
    if pool is None:
        return 0.0

    conn, wait_ms = _borrow_db_connection(float(cfg.get("connection_timeout_s", 5)))
    if conn is None:
        return 0.0
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


async def _cascade_pressure_task() -> None:
    while True:
        if _cascade_pressure_active():
            _push_cascade_backlog(f"cascade:{os.getpid()}:{time.time()}", chunk_mb=6)
            results = await asyncio.gather(
                asyncio.to_thread(_cascade_cpu_burn, 0.45),
                *(asyncio.to_thread(_hold_db_connection, 0.9) for _ in range(3)),
                return_exceptions=True,
            )
            db_waits = [float(item) for item in results[1:] if not isinstance(item, Exception)]
            if db_waits:
                set_last_db_wait_ms(max(get_last_db_wait_ms(), max(db_waits)))
        else:
            if get_cascade_backlog_mb() > 0 and _failure_count == 0:
                _clear_cascade_backlog()
            await asyncio.sleep(0.5)


async def _publish_metrics_snapshot_task() -> None:
    while True:
        _write_worker_snapshot()
        await asyncio.sleep(1)


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_: FastAPI):
    global _downstream_client
    reset_runtime_state()
    _downstream_client = httpx.AsyncClient()
    circuit_task = asyncio.create_task(_circuit_reset_task())
    scenario_task = asyncio.create_task(_scenario_governor_task())
    db_task = asyncio.create_task(_db_pressure_task())
    snapshot_task = asyncio.create_task(_publish_metrics_snapshot_task())
    try:
        yield
    finally:
        for task in (circuit_task, scenario_task, db_task, snapshot_task):
            task.cancel()
        with suppress(Exception):
            await asyncio.gather(
                circuit_task,
                scenario_task,
                db_task,
                snapshot_task,
                return_exceptions=True,
            )
        if _downstream_client is not None:
            await _downstream_client.aclose()
            _downstream_client = None
        reset_runtime_state()
        _remove_worker_snapshot()


app = FastAPI(
    title="payment-api (chaos-app)",
    version="2.0.0",
    description="Monitored microservice whose config changes generate repeatable incident signatures.",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE_NAME}


@app.get("/api/process")
async def process_request(payload: str = "hello") -> dict[str, Any]:
    started = time.perf_counter()
    request_id = next_request_id()
    errors: list[str] = []

    try:
        validate_payload(payload)
    except Exception as exc:
        errors.append(f"validation: {exc}")

    downstream_failed = False
    try:
        await call_downstream()
    except HTTPException as exc:
        downstream_failed = True
        errors.append(f"downstream: {exc.detail}")

    signature = get_active_signature()
    maybe_add_cpu_work(signature, request_id)

    try:
        db_wait_ms = await maybe_run_db_roundtrip(signature)
    except Exception as exc:
        db_wait_ms = 0.0
        errors.append(f"database: {exc}")

    await apply_signature_latency(signature, request_id, downstream_failed)
    force_error = should_fail_request(signature, request_id, downstream_failed)

    duration_ms = (time.perf_counter() - started) * 1000
    set_last_request_duration_ms(duration_ms)
    if db_wait_ms > 0:
        set_last_db_wait_ms(max(get_last_db_wait_ms(), db_wait_ms))

    is_error = force_error or (downstream_failed and signature in {"network_partition", "cascade_failure"})
    record_request_sample(duration_ms, is_error=is_error)

    if is_error:
        raise HTTPException(
            status_code=503,
            detail={
                "signature": signature,
                "errors": errors,
                "duration_ms": round(duration_ms, 2),
                "db_wait_ms": round(get_last_db_wait_ms(), 2),
            },
        )

    return {
        "ok": True,
        "signature": signature,
        "duration_ms": round(duration_ms, 2),
        "db_wait_ms": round(get_last_db_wait_ms(), 2),
        "cache_size_mb": round(get_memory_pressure_mb(), 2),
        "downstream_failed": downstream_failed,
    }


@app.get("/metrics/snapshot")
async def metrics_snapshot() -> dict[str, Any]:
    _write_worker_snapshot()
    return _aggregate_worker_snapshots()


@app.post("/admin/reload-config")
async def reload_config() -> dict[str, Any]:
    reset_config_cache()
    reset_runtime_state()
    return {"ok": True, "message": "Config reloaded and runtime state reset"}


@app.get("/admin/runtime-state")
async def runtime_state() -> dict[str, Any]:
    db_cfg = get_db_cfg()
    svc_cfg = get_svc_cfg()
    signature = get_active_signature()
    target_bytes, chunk_mb = _memory_target_for_signature(signature)
    return {
        "service_name": SERVICE_NAME,
        "active_signature": signature,
        "intended_signature": get_intended_signature(),
        "effective_downstream_url": get_effective_downstream_url(),
        "downstream": {
            "payment_gateway_url": svc_cfg["downstream"]["payment_gateway_url"],
            "external_dependency_url": svc_cfg["downstream"]["external_dependency_url"],
            "use_external_dependency": svc_cfg["downstream"].get("use_external_dependency", False),
            "timeout_s": svc_cfg["downstream"].get("timeout_s", 1.2),
            "retry_attempts": svc_cfg["downstream"].get("retry_attempts", 2),
        },
        "circuit_breaker": svc_cfg["circuit_breaker"],
        "database": {
            "host": db_cfg["host"],
            "port": db_cfg["port"],
            "max_pool_size": db_cfg["max_pool_size"],
            "connection_timeout_s": db_cfg["connection_timeout_s"],
        },
        "runtime": {
            "circuit_open": _circuit_open,
            "failure_count": _failure_count,
            "last_db_wait_ms": round(get_last_db_wait_ms(), 2),
            "last_request_duration_ms": round(_last_request_duration_ms, 2),
            "memory_pressure_mb": round(get_memory_pressure_mb(), 2),
            "memory_target_mb_per_worker": round(target_bytes / (1024 * 1024), 2),
            "memory_growth_mb_per_tick": chunk_mb,
            "worker_count": desired_worker_count(),
            "cpu_burner_active": _cpu_burner_process is not None and _cpu_burner_process.is_alive(),
            "downstream_failure_rate_pct": round(recent_downstream_failure_rate(), 2),
        },
    }
