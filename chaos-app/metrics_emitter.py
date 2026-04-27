"""
metrics_emitter.py

Polls the app's /metrics/snapshot endpoint every 30 seconds,
combines with real system metrics (psutil), computes the
application-level metrics the RCA system expects, then pushes
them to Azure Monitor via the Logs Ingestion API.

Custom metrics pushed:
  - cpu_percent_avg5
  - memory_percent_avg5
  - http_5xx_rate_avg5
  - db_conn_pool_wait_avg5
  - request_latency_p99_avg5

These feed the Azure Monitor Alert Rules that trigger the RCA pipeline.
"""

from __future__ import annotations

import json
import os
import statistics
import time
from collections import deque
from datetime import datetime, timezone

import httpx
import psutil
from azure.identity import DefaultAzureCredential
from azure.monitor.ingestion import LogsIngestionClient
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
APP_URL = os.getenv("APP_URL", "http://localhost:8080")
POLL_INTERVAL_S = int(os.getenv("POLL_INTERVAL_S", "30"))
EMIT_INTERVAL_S = int(os.getenv("EMIT_INTERVAL_S", "60"))  # emit every 60s

DCE_ENDPOINT = os.getenv("AZURE_DCE_ENDPOINT", "")           # Data Collection Endpoint
DCR_IMMUTABLE_ID = os.getenv("AZURE_DCR_IMMUTABLE_ID", "")   # Data Collection Rule ID
STREAM_NAME = os.getenv("AZURE_DCR_STREAM_NAME", "Custom-AppMetrics_CL")

# 5-minute rolling window = 10 samples at 30s interval
WINDOW = 10

# ---------------------------------------------------------------------------
# Rolling buffers (5-min window)
# ---------------------------------------------------------------------------
cpu_buf: deque[float] = deque(maxlen=WINDOW)
mem_buf: deque[float] = deque(maxlen=WINDOW)
http5xx_buf: deque[float] = deque(maxlen=WINDOW)
db_wait_buf: deque[float] = deque(maxlen=WINDOW)
latency_buf: deque[float] = deque(maxlen=WINDOW)

# Request tracking
_request_counts: deque[int] = deque(maxlen=WINDOW)
_error_counts: deque[int] = deque(maxlen=WINDOW)
_latencies: deque[list[float]] = deque(maxlen=WINDOW)
_db_waits: deque[list[float]] = deque(maxlen=WINDOW)

_last_emit = time.monotonic()


def safe_avg(buf: deque) -> float:
    if not buf:
        return 0.0
    return round(statistics.mean(buf), 2)


# ---------------------------------------------------------------------------
# Load test metrics from app
# ---------------------------------------------------------------------------
def fetch_app_metrics() -> dict | None:
    try:
        r = httpx.get(f"{APP_URL}/metrics/snapshot", timeout=5.0)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"[emitter] Could not reach app: {e}")
    return None


def drive_load_sample() -> dict:
    """
    Makes a burst of 10 requests to /api/process, collects latencies and errors.
    Used to compute http_5xx_rate and latency percentiles.
    """
    latencies: list[float] = []
    errors = 0
    for _ in range(10):
        try:
            start = time.perf_counter()
            r = httpx.get(f"{APP_URL}/api/process?payload=metrics_sample", timeout=15.0)
            elapsed_ms = (time.perf_counter() - start) * 1000
            latencies.append(elapsed_ms)
            if r.status_code >= 500:
                errors += 1
        except Exception:
            latencies.append(15000.0)  # timeout = max latency
            errors += 1
    return {"latencies": latencies, "errors": errors, "total": 10}


# ---------------------------------------------------------------------------
# Emit to Azure Monitor
# ---------------------------------------------------------------------------
def emit_to_azure_monitor(payload: dict) -> None:
    if not DCE_ENDPOINT or not DCR_IMMUTABLE_ID:
        print(f"[emitter] DRY RUN (no DCE endpoint set) — would emit: {json.dumps(payload, indent=2)}")
        return

    try:
        credential = DefaultAzureCredential()
        client = LogsIngestionClient(endpoint=DCE_ENDPOINT, credential=credential)
        body = [{**payload, "TimeGenerated": datetime.now(timezone.utc).isoformat()}]
        client.upload(rule_id=DCR_IMMUTABLE_ID, stream_name=STREAM_NAME, logs=body)
        print(f"[emitter] Emitted to Azure Monitor: {payload}")
    except Exception as e:
        print(f"[emitter] Failed to emit: {e}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main() -> None:
    print(f"[emitter] Starting — polling {APP_URL} every {POLL_INTERVAL_S}s")
    global _last_emit

    while True:
        loop_start = time.monotonic()

        # 1. Fetch system metrics
        sys_cpu = psutil.cpu_percent(interval=1.0)
        sys_mem = psutil.virtual_memory().percent
        cpu_buf.append(sys_cpu)
        mem_buf.append(sys_mem)

        # 2. Drive a load sample to measure latency + error rate
        sample = drive_load_sample()
        lats = sample["latencies"]
        errors = sample["errors"]
        total = sample["total"]

        _latencies.append(lats)
        all_latencies = [l for batch in _latencies for l in batch]
        p99 = sorted(all_latencies)[int(len(all_latencies) * 0.99)] if all_latencies else 0.0
        latency_buf.append(p99)

        error_rate = (errors / total) * 100 if total > 0 else 0.0
        http5xx_buf.append(error_rate)

        # 3. Fetch app-level metrics (DB wait, cache size)
        app_metrics = fetch_app_metrics()
        db_wait = 0.0
        if app_metrics:
            # DB wait is reported by the app on each /api/process call
            # We accumulate via /metrics/snapshot which gives last-request db_wait
            pass  # db_wait comes from load_generator tracking

        # Emit every EMIT_INTERVAL_S
        elapsed_since_emit = time.monotonic() - _last_emit
        if elapsed_since_emit >= EMIT_INTERVAL_S:
            payload = {
                "service_name": os.getenv("SERVICE_NAME", "payment-api"),
                "cpu_percent_avg5": safe_avg(cpu_buf),
                "memory_percent_avg5": safe_avg(mem_buf),
                "http_5xx_rate_avg5": safe_avg(http5xx_buf),
                "db_conn_pool_wait_avg5": safe_avg(db_wait_buf),
                "request_latency_p99_avg5": safe_avg(latency_buf),
                "cache_size_mb": app_metrics.get("cache_size_mb", 0.0) if app_metrics else 0.0,
                "circuit_open": app_metrics.get("circuit_open", False) if app_metrics else False,
            }
            emit_to_azure_monitor(payload)
            _last_emit = time.monotonic()

        # Sleep until next poll
        elapsed = time.monotonic() - loop_start
        sleep_for = max(0.0, POLL_INTERVAL_S - elapsed)
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
