# RCA Engine Metrics Architecture Handoff

This document describes the current telemetry and enrichment architecture after the monitoring redesign.

The main backend impact is:

- The RCA engine should no longer fetch enrichment metrics from Azure Monitor Metrics APIs via the old SDK-based approach.
- The RCA engine should query the Log Analytics workspace as the single enrichment source.
- The preferred query entrypoint is the saved KQL function `rca_metric_snapshot(serviceName:string, alertTime:datetime)`.

## 1. Current Source of Truth

All enrichment telemetry now lives in a single Log Analytics workspace:

- Workspace resource: `azurerm_log_analytics_workspace.law`
- Terraform file: [test-infra/main.tf](/home/azureuser/monitored-repo/test-infra/main.tf:48)

There are now two data sources inside that workspace:

1. `Perf`
- Built-in Log Analytics table
- Populated by Azure Monitor Agent + DCR from the VM
- Used for system metrics

2. `AppMetricsRaw_CL`
- Custom Log Analytics table
- Populated by the app-side emitter through the Logs Ingestion API
- Used for application/custom metrics

The RCA engine should treat the Log Analytics workspace as the only enrichment backend.

## 2. Metrics Storage Model

### 2.1 `Perf` table

`Perf` contains VM system counters collected every 60 seconds through Azure Monitor Agent.

Current Linux counters configured in Terraform:

- `Processor(*)\\% Processor Time`
- `Memory(*)\\% Used Memory`
- `Memory(*)\\Available MBytes Memory`

Terraform definition:
- [test-infra/main.tf](/home/azureuser/monitored-repo/test-infra/main.tf:221)

For enrichment, the important mappings are:

- CPU feature:
  - `ObjectName == "Processor"`
  - `CounterName == "% Processor Time"`
  - `InstanceName == "total"`

- Memory feature:
  - `ObjectName == "Memory"`
  - `CounterName == "% Used Memory"`

### 2.2 `AppMetricsRaw_CL` table

`AppMetricsRaw_CL` contains one raw app telemetry row per emitter poll.

Emitter cadence:
- every 30 seconds

Terraform table schema:
- [test-infra/main.tf](/home/azureuser/monitored-repo/test-infra/main.tf:252)

Columns:

- `TimeGenerated` `datetime`
- `ServiceName` `string`
- `WindowSeconds` `int`
- `RequestCount` `int`
- `ErrorCount` `int`
- `Http5xxRatePct` `real`
- `RequestLatencyP99Ms` `real`
- `DbConnPoolWaitMs` `real`
- `CacheSizeMb` `real`
- `CircuitOpen` `boolean`
- `FailureCount` `int`

Emitter implementation:
- [chaos-app/metrics_emitter.py](/home/azureuser/monitored-repo/chaos-app/metrics_emitter.py:1)

Emitter behavior:

- polls `GET /metrics/snapshot`
- builds one raw record
- uploads to Logs Ingestion API
- does not compute `*_avg5` locally

## 3. Application Metrics Contract

The app exposes interval metrics from:

- [chaos-app/app.py](/home/azureuser/monitored-repo/chaos-app/app.py:682)

Current `/metrics/snapshot` response fields:

- `service_name`
- `window_seconds`
- `request_count`
- `error_count`
- `http_5xx_rate_pct`
- `request_latency_p99_ms`
- `db_conn_pool_wait_ms`
- `cache_size_mb`
- `circuit_open`
- `failure_count`

These are raw recent-window metrics, not pre-aggregated 5-minute enrichment features.

## 4. Saved Workspace Function for Enrichment

The current workspace-level query contract is a saved KQL function:

- function alias: `rca_metric_snapshot`
- Terraform definition:
  - [test-infra/main.tf](/home/azureuser/monitored-repo/test-infra/main.tf:333)

Signature:

```text
rca_metric_snapshot(serviceName:string, alertTime:datetime)
```

Behavior:

- looks back 5 minutes from `alertTime`
- reads CPU and memory from `Perf`
- reads custom app metrics from `AppMetricsRaw_CL`
- returns one enriched row

Current KQL output shape:

- `service_name`
- `alert_timestamp`
- `cpu_percent_avg5`
- `memory_percent_avg5`
- `http_5xx_rate_avg5`
- `db_conn_pool_wait_avg5`
- `request_latency_p99_avg5`

Current aggregation logic:

- `cpu_percent_avg5` = avg of `Perf.CounterValue` for processor total
- `memory_percent_avg5` = avg of `Perf.CounterValue` for `% Used Memory`
- `http_5xx_rate_avg5` = avg of `AppMetricsRaw_CL.Http5xxRatePct`
- `db_conn_pool_wait_avg5` = avg of `AppMetricsRaw_CL.DbConnPoolWaitMs`
- `request_latency_p99_avg5` = avg of `AppMetricsRaw_CL.RequestLatencyP99Ms`

## 5. Alerting Model

Alerts are now metric-based, not signature-based.

This is important for the RCA engine:

- Azure Monitor detects a threshold breach
- the webhook identifies the breaching metric
- the RCA engine enriches the incident from the workspace
- the ML model determines the incident signature afterward

Current alert rules:

1. Native metric alerts on VM metrics
- `alert-percentage-cpu-high`
- `alert-available-memory-bytes-low`

2. Scheduled query alerts on workspace app metrics
- `alert-db-conn-pool-wait-high`
- `alert-http-5xx-rate-high`
- `alert-request-latency-p99-high`

Terraform definitions:
- [test-infra/main.tf](/home/azureuser/monitored-repo/test-infra/main.tf:436)

Current webhook properties sent by alerts:

- `service_name`
- `breaching_metric`

Examples:

- `percentage_cpu`
- `available_memory_bytes`
- `db_conn_pool_wait_ms`
- `http_5xx_rate_pct`
- `request_latency_p99_ms`

There is no `incident_signature` in the alert payload anymore.

## 6. RCA Engine Change Required

### Old behavior

The previous RCA engine behavior, as described in the architecture doc, was:

- receive Azure Monitor alert
- call Azure Monitor Metrics / SDK APIs directly
- fetch last-5-minute values from multiple sources
- build the enrichment payload in application code

That description is now outdated for this repo.

### New behavior

The RCA engine should now:

1. Receive the Azure Monitor alert webhook.
2. Extract:
- `service_name`
- `breaching_metric`
- `alert_timestamp`
3. Query the Log Analytics workspace.
4. Use `rca_metric_snapshot(serviceName, alertTime)` as the enrichment query.
5. Use the returned row as the ML enrichment payload.

This means the backend should stop treating Azure Monitor Metrics as the primary enrichment API.

## 7. Recommended Query Strategy for the Backend

Preferred approach:

- Query the Log Analytics workspace through the Logs Query API.
- Execute:

```kusto
rca_metric_snapshot("payment-api", datetime(2026-05-02T17:20:00Z))
```

Inputs expected by the backend:

- `service_name`
- `alert_timestamp`

Expected single-row output:

```json
{
  "service_name": "payment-api",
  "alert_timestamp": "2026-05-02T17:20:00Z",
  "cpu_percent_avg5": 81.7,
  "memory_percent_avg5": 74.3,
  "http_5xx_rate_avg5": 100.0,
  "db_conn_pool_wait_avg5": 245.8,
  "request_latency_p99_avg5": 13230.2
}
```

If the backend cannot call saved functions directly, it can inline the KQL from Terraform, but the saved function is the preferred contract.

## 8. Backend Payload Contract for ML

The backend should continue sending the same enrichment fields to the ML model:

- `breaching_metric`
- `service_name`
- `alert_timestamp`
- `cpu_percent_avg5`
- `memory_percent_avg5`
- `http_5xx_rate_avg5`
- `db_conn_pool_wait_avg5`
- `request_latency_p99_avg5`

The payload shape for downstream ML does not need to change.

Only the enrichment source and query mechanism have changed.

## 9. Important Implementation Notes for the Backend Agent

1. Do not fetch CPU and memory from Azure Monitor Metrics separately anymore for enrichment.
2. Do not fetch custom metrics from a second API path.
3. Use the Log Analytics workspace as the only enrichment system.
4. Use `alert_timestamp` as the upper bound for the 5-minute query window.
5. Keep `breaching_metric` as the alert-driven signal name; do not infer the incident signature before ML classification.

## 10. Current Architecture vs. `rca_system_architecture.md`

Some parts of [rca_system_architecture.md](/home/azureuser/monitored-repo/rca_system_architecture.md:194) still describe the older enrichment approach:

- “Fetch context metrics from Monitoring API”
- direct Azure Monitor API lookups for `*_avg5`

For this repository, that is no longer the live implementation.

The effective implementation now is:

- ingestion path:
  - AMA → `Perf`
  - emitter → `AppMetricsRaw_CL`

- enrichment path:
  - backend query → Log Analytics workspace
  - preferred contract → `rca_metric_snapshot(serviceName, alertTime)`

## 11. Minimal Task List for the Backend Agent

The backend agent should:

1. Locate the old Azure Monitor SDK-based enrichment code.
2. Remove direct metric-fetch logic for CPU, memory, 5xx, DB wait, and latency.
3. Add a Log Analytics query client.
4. Query `rca_metric_snapshot(serviceName, alertTime)`.
5. Map the single returned row into the existing ML payload contract.
6. Preserve `breaching_metric` exactly as received from the alert.

## 12. Useful File References

- Workspace + DCR + alerts:
  - [test-infra/main.tf](/home/azureuser/monitored-repo/test-infra/main.tf)

- App metrics snapshot source:
  - [chaos-app/app.py](/home/azureuser/monitored-repo/chaos-app/app.py)

- App metrics emitter:
  - [chaos-app/metrics_emitter.py](/home/azureuser/monitored-repo/chaos-app/metrics_emitter.py)

- Overall RCA architecture:
  - [rca_system_architecture.md](/home/azureuser/monitored-repo/rca_system_architecture.md)
