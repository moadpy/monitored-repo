# Post-Deployment Guide

This guide covers what to do after the infrastructure is provisioned and the `chaos-app` containers are running on the VM.

## 1. Confirm the app is healthy

On the VM:

```bash
cd ~/monitored-repo/chaos-app
docker compose ps
curl http://localhost:8080/health
curl http://localhost:8080/metrics/snapshot
```

Expected:

- `docker compose ps` shows `app`, `postgres`, `downstream-service`, `load-generator`, and `metrics-emitter` as running.
- `/health` returns:

```json
{"status":"ok","service":"payment-api"}
```

- `/metrics/snapshot` returns fields similar to:

```json
{
  "service_name": "payment-api",
  "window_seconds": 30,
  "request_count": 1500,
  "error_count": 0,
  "http_5xx_rate_pct": 0.0,
  "request_latency_p99_ms": 120.5,
  "db_conn_pool_wait_ms": 0.0,
  "cache_size_mb": 20.3,
  "circuit_open": false,
  "failure_count": 0
}
```

## 2. Confirm the emitter is uploading telemetry

Check emitter logs:

```bash
cd ~/monitored-repo/chaos-app
docker logs metrics-emitter --tail 100
```

Expected:

- No missing-config errors
- No Azure auth errors
- Repeated log lines indicating records were emitted successfully

If it fails:

```bash
cat ~/monitored-repo/chaos-app/.env
docker compose restart metrics-emitter
docker logs metrics-emitter --tail 100
```

Verify these values exist in `.env`:

- `AZURE_DCE_ENDPOINT`
- `AZURE_DCR_IMMUTABLE_ID`
- `AZURE_DCR_STREAM_NAME`

## 3. Confirm platform metrics are reaching Log Analytics

The VM CPU and memory metrics should already flow to the workspace through AMA + DCR.

From `test-infra`, get the workspace details:

```bash
cd ~/monitored-repo/test-infra
terraform output law_workspace_resource_id
terraform output law_workspace_id
terraform output app_metrics_table_name
terraform output rca_metric_snapshot_function_name
```

In the Azure Portal:

1. Open the Log Analytics workspace `law-rca-test`
2. Open `Logs`
3. Run:

```kusto
Perf
| where TimeGenerated > ago(15m)
| where CounterPath in (
    "\\Processor Information(_Total)\\% Processor Time",
    "\\Memory\\% Committed Bytes In Use",
    "\\Memory\\Available MBytes"
)
| order by TimeGenerated desc
```

Expected:

- Recent `Perf` rows from the VM
- CPU and memory counters appearing roughly every minute

## 4. Confirm custom app telemetry is reaching Log Analytics

In the same Log Analytics workspace, run:

```kusto
AppMetricsRaw_CL
| where TimeGenerated > ago(15m)
| order by TimeGenerated desc
```

Expected:

- One row roughly every 30 seconds
- Columns including:
  - `ServiceName`
  - `RequestCount`
  - `ErrorCount`
  - `Http5xxRatePct`
  - `DbConnPoolWaitMs`
  - `RequestLatencyP99Ms`

If there are no rows:

- check `docker logs metrics-emitter`
- check whether the VM identity and DCR permissions are correct
- verify the custom DCR and DCE outputs from Terraform match the `.env` file

## 5. Validate the saved KQL function

The workspace function `rca_metric_snapshot` should be the single enrichment query entrypoint.

Run:

```kusto
rca_metric_snapshot("payment-api", now())
```

Expected:

- exactly one row
- fields:
  - `service_name`
  - `alert_timestamp`
  - `cpu_percent_avg5`
  - `memory_percent_avg5`
  - `http_5xx_rate_avg5`
  - `db_conn_pool_wait_avg5`
  - `request_latency_p99_avg5`

If the function fails:

- verify the function exists in the workspace
- check that `Perf` and `AppMetricsRaw_CL` both contain recent data
- confirm the service name is `payment-api`

## 6. Confirm alerting is still working

CPU and memory alerts remain native Azure Monitor metric alerts.

In Azure Portal:

1. Open `Monitor`
2. Open `Alerts`
3. Confirm these rules exist:
   - `alert-cpu-saturation-burst`
   - `alert-memory-leak-progressive`
4. Confirm the action group `ag-rca-webhook` exists
5. Confirm the webhook target is your RCA backend `/api/incident/new`

If the webhook URL changed, update Terraform and re-apply:

```bash
cd ~/monitored-repo/test-infra
terraform apply -var="webhook_url=http://YOUR_RCA_BACKEND_URL"
```

## 7. Test each incident scenario

From the repo root, trigger scenarios with the existing demo flow or patch flow.

### DB pool exhaustion

Expected behavior:

- `db_conn_pool_wait_ms` rises
- `request_latency_p99_ms` rises
- custom workspace rows show the spike

### Memory leak progressive

Expected behavior:

- `memory_percent_avg5` rises in `Perf`
- `cache_size_mb` grows over time in `AppMetricsRaw_CL`

### CPU saturation burst

Expected behavior:

- CPU rises in `Perf`
- latency rises in `AppMetricsRaw_CL`

### Cascade failure

Expected behavior:

- `http_5xx_rate_pct` rises
- `request_latency_p99_ms` rises
- `circuit_open` may become `true`

### Network partition

Expected behavior:

- app-level failures rise
- custom metrics show latency and error impact
- CPU should stay relatively lower than cascade failure

## 8. Verify enrichment semantics for ML

Your ML pipeline expects `*_avg5` fields, but they are now computed at query time.

That means the enrichment backend should:

1. Receive alert payload
2. Extract:
   - `service_name`
   - `alert_timestamp`
3. Query:

```kusto
rca_metric_snapshot("payment-api", datetime(2026-01-01T00:05:00Z))
```

4. Use the returned row directly as the metric context block for ML scoring

Do not recompute 5-minute averages in the emitter anymore.

## 9. Operational commands you will use often

### Restart containers

```bash
cd ~/monitored-repo/chaos-app
docker compose restart
```

### Rebuild after code changes

```bash
cd ~/monitored-repo/chaos-app
docker compose up -d --build
```

### Reload app config only

```bash
curl -X POST http://localhost:8080/admin/reload-config
```

### Follow app logs

```bash
docker logs payment-api -f
```

### Follow emitter logs

```bash
docker logs metrics-emitter -f
```

## 10. What to check when something is wrong

### App is down

Check:

```bash
docker compose ps
docker logs payment-api --tail 100
```

### Emitter is not sending rows

Check:

```bash
docker logs metrics-emitter --tail 100
cat ~/monitored-repo/chaos-app/.env
```

### Custom table has no rows

Check:

- DCE endpoint value
- DCR immutable ID
- DCR stream name
- VM identity permissions for ingestion
- workspace table exists

### Perf has no CPU or memory rows

Check:

- Azure Monitor Linux Agent extension on the VM
- DCR association
- workspace destination in the system metrics DCR

### Alerts do not reach the RCA backend

Check:

- alert rule exists
- action group exists
- webhook URL is reachable
- backend `/api/incident/new` is accepting requests

## 11. Recommended next steps

After the stack is stable, do these in order:

1. Verify `Perf` and `AppMetricsRaw_CL` both have live data.
2. Verify `rca_metric_snapshot("payment-api", now())` returns one row.
3. Trigger one low-risk scenario such as `db_pool_exhaustion`.
4. Confirm Azure Monitor fires the alert.
5. Confirm the RCA backend receives the webhook.
6. Confirm enrichment uses only the workspace query path.
7. Confirm the ML payload still matches the existing schema.
8. Document one known-good demo run with timestamps and screenshots.

## 12. Useful KQL snippets

### Latest custom telemetry

```kusto
AppMetricsRaw_CL
| where TimeGenerated > ago(30m)
| project TimeGenerated, ServiceName, RequestCount, ErrorCount, Http5xxRatePct, DbConnPoolWaitMs, RequestLatencyP99Ms, CacheSizeMb, CircuitOpen
| order by TimeGenerated desc
```

### Compare CPU and custom latency

```kusto
let cpu =
    Perf
    | where TimeGenerated > ago(30m)
    | where CounterPath == "\\Processor Information(_Total)\\% Processor Time"
    | summarize cpu_percent=avg(CounterValue) by bin(TimeGenerated, 1m);
let latency =
    AppMetricsRaw_CL
    | where TimeGenerated > ago(30m)
    | summarize latency_p99=avg(RequestLatencyP99Ms) by bin(TimeGenerated, 1m);
cpu
| join kind=fullouter latency on TimeGenerated
| order by TimeGenerated desc
```

### Current enrichment snapshot

```kusto
rca_metric_snapshot("payment-api", now())
```

## 13. Notes about authentication and deployment

- Terraform on the VM may prefer managed identity unless provider auth is pinned.
- Pushing repo changes from the VM should be done only after confirming rebase state is clean.
- The VM bootstrap template is now `test-infra/scripts/bootstrap.sh.tftpl`, not the old static `bootstrap.sh`.
- For ongoing deployments, prefer pushing to a branch and opening a PR rather than pushing directly to `main`.
