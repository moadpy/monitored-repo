# monitored-repo

> A **test application** whose config changes cause real Azure Monitor incidents, enabling end-to-end testing of the RCA Engine pipeline.

## How It Works

Merging a PR to this repo causes:
1. GitHub Actions deploys the new config to a VM running the `chaos-app`
2. The app fails in a way that matches the target incident signature
3. Azure Monitor detects the metric breach and fires an alert webhook
4. The RCA pipeline classifies the incident and **finds this PR as the root cause**

## Quick Start — Demo a Full Incident

```powershell
# Trigger a db_pool_exhaustion incident
.\demo.ps1 break db_pool_exhaustion

# Wait ~7 minutes → check RCA dashboard

# Fix it
.\demo.ps1 fix db_pool_exhaustion
```

## Supported Scenarios

| Signature | What breaks | Config changed |
|---|---|---|
| `db_pool_exhaustion` | DB connection pool: 100 → 10 | `chaos-app/config/db.yml` |
| `memory_leak_progressive` | Cache eviction disabled, unlimited size | `chaos-app/config/app.yml` |
| `cpu_saturation_burst` | Catastrophic regex validation + 1 worker | `chaos-app/config/app.yml` |
| `cascade_failure` | Downstream URL → dead host, circuit breaker off | `chaos-app/config/services.yml` |
| `network_partition` | NSG blocks outbound HTTPS via Terraform | `test-infra/terraform.tfvars` |

## Repository Structure

```
monitored-repo/
├── chaos-app/                  # The monitored microservice
│   ├── app.py                  # FastAPI app (all failure modes)
│   ├── metrics_emitter.py      # Pushes custom metrics to Azure Monitor
│   ├── load_generator.py       # Steady 50 req/s traffic
│   ├── config/
│   │   ├── db.yml              # DB pool config (db_pool_exhaustion)
│   │   ├── app.yml             # Cache + validation (memory, cpu)
│   │   └── services.yml        # Downstream URLs (cascade_failure)
│   ├── docker-compose.yml      # app + postgres + nginx + load-gen + emitter
│   └── requirements.txt
├── test-infra/                 # Terraform — VM + NSG + Alert Rules
│   ├── main.tf                 # NSG chaos toggle + Azure Monitor alert rules
│   ├── terraform.tfvars        # nsg_block_outbound = false (network_partition toggle)
│   └── scripts/bootstrap.sh   # Cloud-init: installs Docker, starts app
├── patches/                    # Pre-written break/fix patches
│   ├── break-*.patch + .msg    # 5 break patches
│   └── fix-*.patch + .msg      # 5 fix patches
├── .github/workflows/
│   └── deploy_chaos_app.yml    # Auto-deploys on config PR merge
└── demo.ps1                    # One-command demo automation
```

## Prerequisites

- `git` installed
- `gh` (GitHub CLI) installed and authenticated: `gh auth login`
- Azure VM running with the chaos-app (`terraform apply` in `test-infra/`)
- GitHub Secrets set (see below)

## GitHub Secrets Required

| Secret | Value |
|---|---|
| `VM_PUBLIC_IP` | Public IP of the test VM (from `terraform output vm_public_ip`) |
| `VM_SSH_PRIVATE_KEY` | Content of `~/.ssh/id_rsa` (private key matching the VM's public key) |
| `AZURE_CLIENT_ID` | Service Principal client ID |
| `AZURE_CLIENT_SECRET` | Service Principal secret |
| `AZURE_TENANT_ID` | Azure tenant ID |
| `AZURE_SUBSCRIPTION_ID` | Azure subscription ID |

## First-Time Setup

```bash
# 1. Deploy the VM and infrastructure
cd test-infra
terraform init
terraform apply -var="webhook_url=http://YOUR_RCA_BACKEND_URL"

# 2. Note the VM IP
terraform output vm_public_ip

# 3. Add GitHub Secrets (in repo Settings → Secrets → Actions)

# 4. SSH into VM to verify app is running (takes ~5 min after VM creation)
ssh azureuser@<vm_ip>
docker-compose -f ~/monitored-repo/chaos-app/docker-compose.yml ps
curl http://localhost:8080/health
```
