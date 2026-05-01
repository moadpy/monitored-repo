# Chaos Microservice — PR-Triggered Real Incidents

> **Goal**: When you merge a specific PR, it causes a real incident matching one of your 5 signatures. The RCA system then finds that exact PR as the root cause. Fully repeatable for demos.

---

## The Core Idea

You build a **test application** (`chaos-app/`) deployed on the VM. This app reads its config from YAML files in the repo. A GitHub Actions workflow re-deploys the app whenever config changes are merged to `main`. 

**The loop**:
1. Merge a "break" PR (e.g. `max_connections: 100 → 10`)
2. GitHub Actions deploys new config to VM → app restarts with bad config
3. App starts failing → Azure Monitor detects metric breach → fires alert
4. Your RCA pipeline classifies the incident + RAG finds the PR + LLM reads the diff
5. Merge a "fix" PR (reverse the change) → app recovers → ready for next demo

The PR is **both the trigger AND the evidence** the RCA system discovers.

---

## Architecture

```
Developer merges "break" PR
        │
        ├──→ GitHub Webhook → Azure Function (existing)
        │         → Indexes PR + code_diff into AI Search
        │
        └──→ GitHub Actions workflow
                  → SSH into VM → pull new config → restart app
                          │
                          ▼
                  App runs with broken config
                          │
                          ▼
                  Metrics breach threshold (real Azure Monitor)
                          │
                          ▼
                  Alert Rule fires → Action Group webhook
                          │
                          ▼
                  POST /api/incident/new (your RCA backend)
                          │
                          ▼
                  ML classifies → RAG finds the PR → LLM correlates diff
```

---

## The Test Application (`chaos-app/`)

A simple FastAPI microservice with configurable failure modes:

```
chaos-app/
├── app.py                     # Main app: HTTP server + background workers
├── config/
│   ├── db.yml                 # max_pool_size, connection_timeout
│   ├── app.yml                # worker_threads, cache_max_mb, cache_eviction
│   ├── services.yml           # downstream_url, circuit_breaker_enabled
│   └── infra.yml              # nsg_outbound_policy (read by Terraform)
├── load_generator.py          # Steady traffic to the app (runs on VM)
├── metrics_emitter.py         # Pushes custom metrics to Azure Monitor
├── docker-compose.yml         # app + postgres + downstream-nginx
├── Dockerfile
└── requirements.txt
```

**What the app does**:
- Serves HTTP requests on port 8080
- Connects to a local PostgreSQL (Docker) with configurable pool size
- Calls a "downstream service" (nginx container) on each request
- Has a background worker that caches data in memory
- A `load_generator.py` runs continuously, sending ~50 req/s to the app
- A `metrics_emitter.py` reads real app metrics and pushes them to Azure Monitor

---

## 5 Scenarios — PR Diff → Incident

### 1. `db_pool_exhaustion`

| Item | Detail |
|---|---|
| **Config file** | `chaos-app/config/db.yml` |
| **Break PR title** | "Optimize DB connections — reduce pool for cost savings" |
| **Break diff** | `max_pool_size: 100` → `max_pool_size: 10` |
| **Why it breaks** | Load generator sends 50 req/s, each needs a DB connection. Pool of 10 exhausts instantly → wait time spikes to 300ms+ |
| **Metric that breaches** | `db_conn_pool_wait_ms > 200ms` |
| **Fix PR title** | "Revert DB pool — restore original pool size" |
| **Fix diff** | `max_pool_size: 10` → `max_pool_size: 100` |

### 2. `memory_leak_progressive`

| Item | Detail |
|---|---|
| **Config file** | `chaos-app/config/app.yml` |
| **Break PR title** | "Add request caching for performance improvement" |
| **Break diff** | `cache_eviction: true` → `cache_eviction: false` AND `cache_max_mb: 50` → `cache_max_mb: 0` (unlimited) |
| **Why it breaks** | Background worker caches every response without eviction. Memory climbs ~50MB/min until >85% |
| **Metric that breaches** | `memory_percent > 80%` sustained 3 min |
| **Fix PR** | Restore `cache_eviction: true`, `cache_max_mb: 50` |

### 3. `cpu_saturation_burst`

| Item | Detail |
|---|---|
| **Config file** | `chaos-app/config/app.yml` |
| **Break PR title** | "Enable deep request validation for security audit" |
| **Break diff** | `enable_heavy_validation: false` → `enable_heavy_validation: true` AND `worker_threads: 4` → `worker_threads: 1` |
| **Why it breaks** | Each request now runs an expensive regex validation on a single thread. Under 50 req/s load, CPU saturates to 95%+ |
| **Metric that breaches** | `cpu_percent > 90%` sustained 3 min |
| **Fix PR** | Restore `enable_heavy_validation: false`, `worker_threads: 4` |

### 4. `cascade_failure`

| Item | Detail |
|---|---|
| **Config file** | `chaos-app/config/services.yml` |
| **Break PR title** | "Migrate payment gateway to new endpoint" |
| **Break diff** | `downstream_url: http://downstream:8080` → `downstream_url: http://dead-host:9999` AND `circuit_breaker_enabled: true` → `circuit_breaker_enabled: false` |
| **Why it breaks** | Every request tries to call a dead downstream, no circuit breaker → timeouts cascade → CPU spikes (threads waiting), memory spikes (queued requests), latency spikes, 5xx spikes — ALL metrics simultaneously |
| **Metric that breaches** | All metrics spike together (cascade pattern) |
| **Fix PR** | Restore URL + re-enable circuit breaker |

### 5. `network_partition`

| Item | Detail |
|---|---|
| **Config file** | `test-infra/terraform.tfvars` |
| **Break PR title** | "Restrict outbound traffic — security hardening" |
| **Break diff** | `nsg_block_outbound = false` → `nsg_block_outbound = true` |
| **Why it breaks** | GitHub Actions runs `terraform apply` → NSG blocks port 443 outbound → app can't reach any external service → 5xx rate spikes, latency = timeout. But CPU stays normal (key differentiator from cascade) |
| **Metric that breaches** | `http_5xx_rate > 50`, `latency > 2000ms`, CPU normal |
| **Fix PR** | Set `nsg_block_outbound = false` |

---

## GitHub Actions Workflow

```yaml
# .github/workflows/deploy_chaos_app.yml
name: Deploy Chaos App Config

on:
  push:
    branches: [main]
    paths:
      - 'chaos-app/config/**'
      - 'test-infra/terraform.tfvars'

jobs:
  deploy-config:
    runs-on: ubuntu-latest
    if: "!contains(github.event.head_commit.message, '[skip-deploy]')"
    steps:
      - uses: actions/checkout@v4

      - name: Deploy config to VM
        if: contains(github.event.head_commit.modified, 'chaos-app/config/')
        env:
          VM_IP: ${{ secrets.VM_PUBLIC_IP }}
          SSH_KEY: ${{ secrets.VM_SSH_PRIVATE_KEY }}
        run: |
          echo "$SSH_KEY" > key.pem && chmod 600 key.pem
          scp -i key.pem -o StrictHostKeyChecking=no \
            -r chaos-app/config/ azureuser@$VM_IP:~/chaos-app/config/
          ssh -i key.pem azureuser@$VM_IP \
            "cd ~/chaos-app && docker-compose restart app"

  deploy-infra:
    runs-on: ubuntu-latest
    if: contains(github.event.head_commit.modified, 'test-infra/')
    steps:
      - uses: actions/checkout@v4
      - uses: hashicorp/setup-terraform@v3
      - name: Terraform Apply (NSG mutation)
        env:
          ARM_CLIENT_ID: ${{ secrets.AZURE_CLIENT_ID }}
          ARM_CLIENT_SECRET: ${{ secrets.AZURE_CLIENT_SECRET }}
          ARM_TENANT_ID: ${{ secrets.AZURE_TENANT_ID }}
          ARM_SUBSCRIPTION_ID: ${{ secrets.AZURE_SUBSCRIPTION_ID }}
        run: |
          cd test-infra
          terraform init
          terraform apply -auto-approve
```

---

## Repeatable Demo Script

A CLI script using `gh` CLI to automate the break/fix cycle:

```powershell
# demo.ps1 — Usage: .\demo.ps1 break db_pool_exhaustion
param(
    [ValidateSet("break","fix")]
    [string]$Action,
    [ValidateSet("db_pool_exhaustion","memory_leak_progressive",
                 "cpu_saturation_burst","cascade_failure","network_partition")]
    [string]$Signature
)

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$branch = "chaos/$Action-$Signature-$timestamp"

git checkout main
git pull
git checkout -b $branch

# Apply the pre-written patch
git apply "patches/$Action-$Signature.patch"
git add -A
git commit -m (Get-Content "patches/$Action-$Signature.msg" -Raw)
git push -u origin $branch

# Create and auto-merge the PR
gh pr create --title (Get-Content "patches/$Action-$Signature.msg" -Raw) `
             --body "Automated chaos test: $Action $Signature" `
             --base main
gh pr merge --auto --squash

git checkout main
```

**Pre-written patches** (stored in `patches/` directory):

```
patches/
├── break-db_pool_exhaustion.patch       # db.yml: 100 → 10
├── break-db_pool_exhaustion.msg         # "Optimize DB connections..."
├── fix-db_pool_exhaustion.patch         # db.yml: 10 → 100
├── fix-db_pool_exhaustion.msg           # "Revert DB pool size..."
├── break-memory_leak_progressive.patch
├── fix-memory_leak_progressive.patch
├── break-cpu_saturation_burst.patch
├── fix-cpu_saturation_burst.patch
├── break-cascade_failure.patch
├── fix-cascade_failure.patch
├── break-network_partition.patch
└── fix-network_partition.patch
```

---

## Demo Flow (End-to-End)

```
1. .\demo.ps1 break db_pool_exhaustion
   │
   ├─ Creates PR: "Optimize DB connections — reduce pool for cost savings"
   │  Diff: max_pool_size: 100 → 10
   │
   ├─ PR merges → GitHub webhook indexes PR into AI Search
   │              GitHub Actions deploys new config to VM
   │
   ├─ ~2 min: App restarts with pool=10, load generator causes exhaustion
   │          db_conn_pool_wait_ms spikes to 350ms
   │
   ├─ ~5 min: Azure Monitor alert fires (>200ms for 3 min)
   │          Action Group POSTs to /api/incident/new
   │
   ├─ ~5 min: RCA pipeline:
   │          ML classifier → "db_pool_exhaustion (91%)"
   │          RAG finds PR → "PR: Optimize DB connections"
   │          LLM reads diff → "max_connections reduced 100→10"
   │
   └─ Frontend shows incident with root cause = the PR you just merged ✅

2. .\demo.ps1 fix db_pool_exhaustion
   │
   └─ Restores config → app recovers → ready for next demo
```

**Total time per demo cycle**: ~7 minutes (2 min deploy + 5 min alert threshold)

---

## What To Build (Ordered)

| # | Task | Effort |
|---|---|---|
| 1 | Build `chaos-app/` (FastAPI + docker-compose + Postgres) | 3 hr |
| 2 | Write config files (`db.yml`, `app.yml`, `services.yml`) | 30 min |
| 3 | Build `metrics_emitter.py` (pushes custom metrics to Azure Monitor) | 1.5 hr |
| 4 | Build `load_generator.py` (steady 50 req/s traffic) | 30 min |
| 5 | Add to `test-infra/main.tf`: NSG toggle + 5 Alert Rules + Action Group | 2 hr |
| 6 | Add VM bootstrap script (install stress-ng, docker, python deps) | 30 min |
| 7 | Create GitHub Actions workflow `deploy_chaos_app.yml` | 30 min |
| 8 | Write 10 patch files (5 break + 5 fix) | 1 hr |
| 9 | Write `demo.ps1` automation script | 30 min |
| 10 | Integration testing (trigger each scenario end-to-end) | 2 hr |
| **Total** | | **~12 hr** |

---

## Open Questions

> [!IMPORTANT]
> 1. **Which repo hosts `chaos-app/`?** Should it live in `app-repo` (current repo) or a separate `chaos-repo`? I recommend `app-repo` so the PRs appear in the same repo the RAG ingestion pipeline watches.

> [!IMPORTANT]
> 2. **Do you already have the GitHub webhook → Azure Function ingestion pipeline deployed?** The PR indexing into AI Search is critical for the RAG system to find the "break" PRs. If not deployed yet, we can temporarily use the `index_knowledge_base.py` script to manually index after each PR merge.

> [!IMPORTANT]
> 3. **VM always-on vs. on-demand?** The `Standard_D2s_v3` costs ~$2.30/day. Options: (A) keep it running during dev weeks, (B) start/stop via `az vm start/deallocate` before/after demos.
