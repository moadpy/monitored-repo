"""
load_generator.py

Sends continuous HTTP traffic to the chaos-app at a configurable rate.
This is what makes the config changes observable — without load,
a pool of 10 connections would not be exhausted.

Usage:
    python3 load_generator.py             # 50 req/s default
    python3 load_generator.py --rps 20   # 20 req/s
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from collections import deque

import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DEFAULT_RPS = 50
PAYLOADS = [
    "order123",
    "payment456",
    "auth789",
    "user_checkout_flow",
    "batch_process_trigger",
]

# ---------------------------------------------------------------------------
# Stats tracking
# ---------------------------------------------------------------------------
_latencies: deque[float] = deque(maxlen=500)
_errors: deque[bool] = deque(maxlen=500)
_total = 0
_start = time.monotonic()


def print_stats() -> None:
    elapsed = time.monotonic() - _start
    rps = _total / elapsed if elapsed > 0 else 0
    lats = list(_latencies)
    errs = list(_errors)
    if lats:
        p50 = round(statistics.median(lats), 1)
        p99 = round(sorted(lats)[int(len(lats) * 0.99)], 1) if len(lats) >= 100 else round(max(lats), 1)
    else:
        p50 = p99 = 0.0
    err_pct = round(sum(errs) / len(errs) * 100, 1) if errs else 0.0
    print(
        f"[load-gen] {_total:>6} reqs | {rps:.1f} rps | "
        f"p50={p50}ms p99={p99}ms | errors={err_pct}%"
    )


# ---------------------------------------------------------------------------
# Request coroutine
# ---------------------------------------------------------------------------
async def send_request(client: httpx.AsyncClient, app_url: str, payload: str) -> None:
    global _total
    start = time.perf_counter()
    is_error = False
    try:
        r = await client.get(f"{app_url}/api/process?payload={payload}", timeout=20.0)
        if r.status_code >= 500:
            is_error = True
    except Exception:
        is_error = True
    elapsed_ms = (time.perf_counter() - start) * 1000
    _latencies.append(elapsed_ms)
    _errors.append(is_error)
    _total += 1


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
async def run(app_url: str, rps: int) -> None:
    interval = 1.0 / rps
    print(f"[load-gen] Sending {rps} req/s to {app_url}  (Ctrl+C to stop)")

    async with httpx.AsyncClient() as client:
        req_idx = 0
        stats_at = time.monotonic() + 30
        while True:
            payload = f"{PAYLOADS[req_idx % len(PAYLOADS)]}_{req_idx}"
            asyncio.create_task(send_request(client, app_url, payload))
            req_idx += 1

            if time.monotonic() >= stats_at:
                print_stats()
                stats_at = time.monotonic() + 30

            await asyncio.sleep(interval)


def main() -> None:
    import os
    parser = argparse.ArgumentParser(description="Chaos-app load generator")
    parser.add_argument("--rps", type=int, default=int(os.getenv("LOAD_RPS", str(DEFAULT_RPS))))
    parser.add_argument("--url", type=str, default=os.getenv("APP_URL", "http://localhost:8080"))
    args = parser.parse_args()
    asyncio.run(run(args.url, args.rps))


if __name__ == "__main__":
    main()
