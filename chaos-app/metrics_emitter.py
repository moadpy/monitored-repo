"""
metrics_emitter.py

Polls the app's /metrics/snapshot endpoint every 30 seconds,
forwards raw application metrics, and pushes them to Azure Monitor
via the Logs Ingestion API.

Custom metrics pushed:
  - RequestCount
  - ErrorCount
  - Http5xxRatePct
  - DbConnPoolWaitMs
  - RequestLatencyP99Ms
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import httpx
from azure.identity import DefaultAzureCredential
from azure.monitor.ingestion import LogsIngestionClient
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
APP_URL = os.getenv("APP_URL", "http://localhost:8080")
POLL_INTERVAL_S = int(os.getenv("POLL_INTERVAL_S", "30"))

DCE_ENDPOINT = os.getenv("AZURE_DCE_ENDPOINT", "")           # Data Collection Endpoint
DCR_IMMUTABLE_ID = os.getenv("AZURE_DCR_IMMUTABLE_ID", "")   # Data Collection Rule ID
STREAM_NAME = os.getenv("AZURE_DCR_STREAM_NAME", "Custom-AppMetricsRaw")


# ---------------------------------------------------------------------------
# Load metrics from app
# ---------------------------------------------------------------------------
def fetch_app_metrics() -> dict | None:
    try:
        r = httpx.get(f"{APP_URL}/metrics/snapshot", timeout=5.0)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"[emitter] Could not reach app: {e}")
    return None


def build_record(metrics: dict) -> dict:
    return {
        "TimeGenerated": datetime.now(timezone.utc).isoformat(),
        "ServiceName": metrics.get("service_name", os.getenv("SERVICE_NAME", "payment-api")),
        "WindowSeconds": int(metrics.get("window_seconds", POLL_INTERVAL_S)),
        "RequestCount": int(metrics.get("request_count", 0)),
        "ErrorCount": int(metrics.get("error_count", 0)),
        "Http5xxRatePct": float(metrics.get("http_5xx_rate_pct", 0.0)),
        "RequestLatencyP99Ms": float(metrics.get("request_latency_p99_ms", 0.0)),
        "DbConnPoolWaitMs": float(metrics.get("db_conn_pool_wait_ms", 0.0)),
        "CacheSizeMb": float(metrics.get("cache_size_mb", 0.0)),
        "CircuitOpen": bool(metrics.get("circuit_open", False)),
        "FailureCount": int(metrics.get("failure_count", 0)),
    }


# ---------------------------------------------------------------------------
# Emit to Azure Monitor
# ---------------------------------------------------------------------------
def create_client() -> LogsIngestionClient:
    if not DCE_ENDPOINT or not DCR_IMMUTABLE_ID or not STREAM_NAME:
        raise RuntimeError(
            "[emitter] Missing Azure Monitor ingestion configuration. "
            "Set AZURE_DCE_ENDPOINT, AZURE_DCR_IMMUTABLE_ID, and AZURE_DCR_STREAM_NAME."
        )
    try:
        credential = DefaultAzureCredential()
        return LogsIngestionClient(endpoint=DCE_ENDPOINT, credential=credential)
    except Exception as e:
        raise RuntimeError(f"[emitter] Failed to initialize Azure Monitor client: {e}") from e


def emit_to_azure_monitor(client: LogsIngestionClient, payload: dict) -> None:
    try:
        body = [payload]
        client.upload(rule_id=DCR_IMMUTABLE_ID, stream_name=STREAM_NAME, logs=body)
        print(f"[emitter] Emitted to Azure Monitor: {payload}")
    except Exception as e:
        print(f"[emitter] Failed to emit: {e}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main() -> None:
    print(f"[emitter] Starting — polling {APP_URL} every {POLL_INTERVAL_S}s")
    client = create_client()

    while True:
        loop_start = time.monotonic()
        app_metrics = fetch_app_metrics()
        if app_metrics is not None:
            emit_to_azure_monitor(client, build_record(app_metrics))

        # Sleep until next poll
        elapsed = time.monotonic() - loop_start
        sleep_for = max(0.0, POLL_INTERVAL_S - elapsed)
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
