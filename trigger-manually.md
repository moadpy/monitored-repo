Use the VM directly and edit the deployed files in place, then reload the app or re-apply Terraform. No Git patches required.

## General workflow

App config changes:
1. edit file in `~/monitored-repo/chaos-app/config/`
2. reload app config:
```bash
curl -X POST http://localhost:8080/admin/reload-config
```


3. if needed, rebuild/restart:
```bash
cd ~/monitored-repo/chaos-app
docker compose up -d --build
```

Infra change for network partition:
1. edit `~/monitored-repo/test-infra/terraform.tfvars`
2. apply Terraform:
```bash
cd ~/monitored-repo/test-infra
terraform apply
```

## Baseline check before every test

```bash
cd ~/monitored-repo/chaos-app
curl http://localhost:8080/health
curl http://localhost:8080/metrics/snapshot
docker compose ps
```

You want:
- low `http_5xx_rate_pct`
- low `request_latency_p99_ms`
- `db_conn_pool_wait_ms` near `0`
- no active outage state

## 1. CPU saturation burst

Edit:

```bash
nano ~/monitored-repo/chaos-app/config/app.yml
```

Change:

```yaml
validation:
  enable_heavy_validation: true
  regex_complexity: high
```

Reload:

```bash
curl -X POST http://localhost:8080/admin/reload-config
```

```
use docker compose restart app for immediate restart
```

Wait:
- about 3 to 8 minutes

Check locally:

```bash
curl http://localhost:8080/metrics/snapshot
```

Watch in Azure:
- `alert-percentage-cpu-high`
- `Perf` CPU rises

Restore baseline:

```yaml
validation:
  enable_heavy_validation: false
  regex_complexity: low
```

Then:

```bash
curl -X POST http://localhost:8080/admin/reload-config
```

## 2. Memory leak progressive

Edit:

```bash
nano ~/monitored-repo/chaos-app/config/app.yml
```

Change:

```yaml
cache:
  eviction_enabled: false
  max_size_mb: 0
```

Reload:

```bash
curl -X POST http://localhost:8080/admin/reload-config
```

Wait:
- about 10 to 25 minutes

Check locally:

```bash
curl http://localhost:8080/metrics/snapshot
```

Watch:
- `cache_size_mb` rising
- Azure `Perf` memory rising
- `alert-available-memory-bytes-low`

Restore baseline:

```yaml
cache:
  eviction_enabled: true
  max_size_mb: 50
```

Then reload and restart app to clear memory:

```bash
curl -X POST http://localhost:8080/admin/reload-config
cd ~/monitored-repo/chaos-app
docker compose restart app
```

## 3. DB pool exhaustion

Edit:

```bash
nano ~/monitored-repo/chaos-app/config/db.yml
```

Change:

```yaml
max_pool_size: 10
```

Reload:

```bash
curl -X POST http://localhost:8080/admin/reload-config
```

Wait:
- about 3 to 8 minutes

Check locally:

```bash
curl http://localhost:8080/metrics/snapshot
```

Watch:
- `db_conn_pool_wait_ms` rising
- `alert-db-conn-pool-wait-high`

Restore baseline:

```yaml
max_pool_size: 100
```

Then:

```bash
curl -X POST http://localhost:8080/admin/reload-config
```

## 4. Cascade failure

Edit:

```bash
nano ~/monitored-repo/chaos-app/config/services.yml
```

Change:

```yaml
downstream:
  payment_gateway_url: "http://dead-host:9999/health"

circuit_breaker:
  enabled: false
```

Reload:

```bash
curl -X POST http://localhost:8080/admin/reload-config
```

Wait:
- about 3 to 8 minutes

Check locally:

```bash
curl http://localhost:8080/metrics/snapshot
```

Watch:
- `http_5xx_rate_pct` rising
- `request_latency_p99_ms` rising
- maybe `failure_count` rising
- alerts:
  - `alert-http-5xx-rate-high`
  - `alert-request-latency-p99-high`

Restore baseline:

```yaml
downstream:
  payment_gateway_url: "http://downstream-service:8080/health"

circuit_breaker:
  enabled: true
```

Then:

```bash
curl -X POST http://localhost:8080/admin/reload-config
```

## 5. Network partition

This one needs both app config and Terraform.

### Step A: switch the app to use the external HTTPS dependency

Edit:

```bash
nano ~/monitored-repo/chaos-app/config/services.yml
```

Change:

```yaml
downstream:
  use_external_dependency: true
```

Keep:

```yaml
external_dependency_url: "https://worldtimeapi.org/api/timezone/UTC"
```

Reload:

```bash
curl -X POST http://localhost:8080/admin/reload-config
```

Check healthy external mode first:

```bash
curl http://localhost:8080/metrics/snapshot
```

You want:
- low errors
- acceptable latency

### Step B: block outbound 443

Edit:

```bash
nano ~/monitored-repo/test-infra/terraform.tfvars
```

Change:

```hcl
nsg_block_outbound = true
```

Apply:

```bash
cd ~/monitored-repo/test-infra
terraform apply
```

Wait:
- about 3 to 8 minutes

Check locally:

```bash
curl http://localhost:8080/metrics/snapshot
```

Watch:
- `http_5xx_rate_pct` rising
- `request_latency_p99_ms` rising
- alerts:
  - `alert-http-5xx-rate-high`
  - `alert-request-latency-p99-high`

Restore baseline:
- set `use_external_dependency: false` in `services.yml`
- set `nsg_block_outbound = false` in `terraform.tfvars`

Then run:

```bash
curl -X POST http://localhost:8080/admin/reload-config
cd ~/monitored-repo/test-infra
terraform apply
```

## Recommended order

Test them in this order:
1. DB pool exhaustion
2. CPU saturation
3. Cascade failure
4. Network partition
5. Memory leak progressive

That order keeps the slower memory scenario last and reduces cleanup complexity.

## Minimal local commands you will reuse

Snapshot:
```bash
curl http://localhost:8080/metrics/snapshot
```

Reload config:
```bash
curl -X POST http://localhost:8080/admin/reload-config
```

Restart app only:
```bash
cd ~/monitored-repo/chaos-app
docker compose restart app
```

Rebuild all:
```bash
cd ~/monitored-repo/chaos-app
docker compose up -d --build
```

Infra apply:
```bash
cd ~/monitored-repo/test-infra
terraform apply
```

If you want, I can save this VM-manual procedure as a second guide file in the repo root, separate from the post-deployment guide.