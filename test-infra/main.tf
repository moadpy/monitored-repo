terraform {
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    azapi = {
      source  = "Azure/azapi"
      version = "~> 1.13"
    }
  }
}

provider "azurerm" {
  features {}
  use_cli = true
  use_msi = false
}

provider "azapi" {
  use_cli = true
  use_msi = false
}

# ---------------------------------------------------------------------------
# Variables
# ---------------------------------------------------------------------------
variable "nsg_block_outbound" {
  description = "CHAOS TOGGLE: set to true to simulate network_partition incident"
  type        = bool
  default     = false
}

variable "resource_group_name" {
  default = "rg-rca-testing-sandbox2"
}

variable "location" {
  default = "eastus"
}

variable "webhook_url" {
  description = "URL of the RCA backend webhook — POST /api/incident/new"
  type        = string
  default     = ""
}

locals {
  service_name                   = "payment-api"
  app_metrics_table_name         = "AppMetricsRaw_CL"
  app_metrics_stream_name        = "Custom-AppMetricsRaw"
  app_metrics_output_stream_name = "Custom-AppMetricsRaw_CL"
}

# ---------------------------------------------------------------------------
# Resource Group
# ---------------------------------------------------------------------------
resource "azurerm_resource_group" "rg" {
  name     = var.resource_group_name
  location = var.location
}

# ---------------------------------------------------------------------------
# Log Analytics Workspace
# ---------------------------------------------------------------------------
resource "azurerm_log_analytics_workspace" "law" {
  name                = "law-rca-test"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
}

# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------
resource "azurerm_virtual_network" "vnet" {
  name                = "vnet-rca-test"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
  address_space       = ["10.98.0.0/16"]
}

# ---------------------------------------------------------------------------
# NSG with chaos toggle (network_partition scenario)
# ---------------------------------------------------------------------------
resource "azurerm_network_security_group" "nsg" {
  name                = "nsg-rca-test"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name

  # Allow SSH always (port 22) — so we can still reach the VM for cleanup
  security_rule {
    name                       = "allow-ssh-inbound"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "22"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }

  # Allow app port 8080 inbound (load generator traffic)
  security_rule {
    name                       = "allow-app-inbound"
    priority                   = 110
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "8080"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }

  # CHAOS TOGGLE: block outbound HTTPS when nsg_block_outbound = true
  # This simulates a firewall/ACL misconfiguration (network_partition)
  security_rule {
    name                       = "chaos-outbound-https"
    priority                   = 200
    direction                  = "Outbound"
    access                     = var.nsg_block_outbound ? "Deny" : "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "443"
    source_address_prefix      = "*"
    destination_address_prefix = "Internet"
  }
}

resource "azurerm_subnet" "subnet" {
  name                 = "snet-rca-test"
  resource_group_name  = azurerm_resource_group.rg.name
  virtual_network_name = azurerm_virtual_network.vnet.name
  address_prefixes     = ["10.98.1.0/24"]
}

resource "azurerm_subnet_network_security_group_association" "nsg_assoc" {
  subnet_id                 = azurerm_subnet.subnet.id
  network_security_group_id = azurerm_network_security_group.nsg.id
}

resource "azurerm_public_ip" "pip" {
  name                = "pip-rca-victim"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
  allocation_method   = "Static"
}

resource "azurerm_network_interface" "nic" {
  name                = "nic-rca-victim"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name

  ip_configuration {
    name                          = "ipconfig1"
    subnet_id                     = azurerm_subnet.subnet.id
    private_ip_address_allocation = "Dynamic"
    public_ip_address_id          = azurerm_public_ip.pip.id
  }
}

# ---------------------------------------------------------------------------
# Virtual Machine
# ---------------------------------------------------------------------------
resource "azurerm_linux_virtual_machine" "vm" {
  name                = "vm-rca-victim"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
  size                = "Standard_D2s_v3"
  admin_username      = "azureuser"

  network_interface_ids = [azurerm_network_interface.nic.id]

  admin_ssh_key {
    username   = "azureuser"
    public_key = file(pathexpand("~/.ssh/id_rsa.pub"))
  }

  os_disk {
    caching              = "ReadWrite"
    storage_account_type = "Standard_LRS"
  }

  source_image_reference {
    publisher = "Canonical"
    offer     = "0001-com-ubuntu-server-jammy"
    sku       = "22_04-lts"
    version   = "latest"
  }

  identity {
    type = "SystemAssigned"
  }

  custom_data = base64encode(templatefile("${path.module}/scripts/bootstrap.sh.tftpl", {
    azure_dce_endpoint     = azurerm_monitor_data_collection_endpoint.app_metrics.logs_ingestion_endpoint
    azure_dcr_immutable_id = azurerm_monitor_data_collection_rule.app_metrics.immutable_id
    azure_dcr_stream_name  = local.app_metrics_stream_name
    service_name           = local.service_name
  }))
}

# ---------------------------------------------------------------------------
# Azure Monitor Agent
# ---------------------------------------------------------------------------
resource "azurerm_virtual_machine_extension" "ama" {
  name                       = "AzureMonitorLinuxAgent"
  virtual_machine_id         = azurerm_linux_virtual_machine.vm.id
  publisher                  = "Microsoft.Azure.Monitor"
  type                       = "AzureMonitorLinuxAgent"
  type_handler_version       = "1.0"
  auto_upgrade_minor_version = true
}

# ---------------------------------------------------------------------------
# Data Collection Rule — system metrics (CPU, Memory)
# ---------------------------------------------------------------------------
resource "azurerm_monitor_data_collection_rule" "dcr" {
  name                = "dcr-rca-test-metrics"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name

  destinations {
    log_analytics {
      workspace_resource_id = azurerm_log_analytics_workspace.law.id
      name                  = "law-destination"
    }
  }

  data_flow {
    streams      = ["Microsoft-Perf"]
    destinations = ["law-destination"]
  }

  data_sources {
    performance_counter {
      name                          = "perf-counters"
      streams                       = ["Microsoft-Perf"]
      sampling_frequency_in_seconds = 60
      counter_specifiers = [
        "\\Processor Information(_Total)\\% Processor Time",
        "\\Memory\\% Committed Bytes In Use",
        "\\Memory\\Available MBytes",
      ]
    }
  }
}

resource "azurerm_monitor_data_collection_rule_association" "dcra" {
  name                    = "dcra-rca-victim"
  target_resource_id      = azurerm_linux_virtual_machine.vm.id
  data_collection_rule_id = azurerm_monitor_data_collection_rule.dcr.id
  depends_on              = [azurerm_virtual_machine_extension.ama]
}

# ---------------------------------------------------------------------------
# Custom log ingestion for application metrics
# ---------------------------------------------------------------------------
resource "azapi_resource" "app_metrics_table" {
  type      = "Microsoft.OperationalInsights/workspaces/tables@2022-10-01"
  name      = local.app_metrics_table_name
  parent_id = azurerm_log_analytics_workspace.law.id

  body = jsonencode({
    properties = {
      plan                 = "Analytics"
      retentionInDays      = azurerm_log_analytics_workspace.law.retention_in_days
      totalRetentionInDays = azurerm_log_analytics_workspace.law.retention_in_days
      schema = {
        name = local.app_metrics_table_name
        columns = [
          { name = "TimeGenerated", type = "datetime" },
          { name = "ServiceName", type = "string" },
          { name = "WindowSeconds", type = "int" },
          { name = "RequestCount", type = "int" },
          { name = "ErrorCount", type = "int" },
          { name = "Http5xxRatePct", type = "real" },
          { name = "RequestLatencyP99Ms", type = "real" },
          { name = "DbConnPoolWaitMs", type = "real" },
          { name = "CacheSizeMb", type = "real" },
          { name = "CircuitOpen", type = "boolean" },
          { name = "FailureCount", type = "int" },
        ]
      }
    }
  })
}

resource "azurerm_monitor_data_collection_endpoint" "app_metrics" {
  name                = "dce-rca-app-metrics"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
}

resource "azurerm_monitor_data_collection_rule" "app_metrics" {
  name                        = "dcr-rca-app-metrics"
  location                    = azurerm_resource_group.rg.location
  resource_group_name         = azurerm_resource_group.rg.name
  data_collection_endpoint_id = azurerm_monitor_data_collection_endpoint.app_metrics.id

  destinations {
    log_analytics {
      workspace_resource_id = azurerm_log_analytics_workspace.law.id
      name                  = "law-destination"
    }
  }

  stream_declaration {
    stream_name = local.app_metrics_stream_name

    column {
      name = "TimeGenerated"
      type = "datetime"
    }

    column {
      name = "ServiceName"
      type = "string"
    }

    column {
      name = "WindowSeconds"
      type = "int"
    }

    column {
      name = "RequestCount"
      type = "int"
    }

    column {
      name = "ErrorCount"
      type = "int"
    }

    column {
      name = "Http5xxRatePct"
      type = "real"
    }

    column {
      name = "RequestLatencyP99Ms"
      type = "real"
    }

    column {
      name = "DbConnPoolWaitMs"
      type = "real"
    }

    column {
      name = "CacheSizeMb"
      type = "real"
    }

    column {
      name = "CircuitOpen"
      type = "boolean"
    }

    column {
      name = "FailureCount"
      type = "int"
    }
  }

  data_flow {
    streams       = [local.app_metrics_stream_name]
    destinations  = ["law-destination"]
    output_stream = local.app_metrics_output_stream_name
    transform_kql = "source"
  }

  depends_on = [azapi_resource.app_metrics_table]
}

resource "azurerm_role_assignment" "vm_can_publish_app_metrics" {
  scope                = azurerm_monitor_data_collection_rule.app_metrics.id
  role_definition_name = "Monitoring Metrics Publisher"
  principal_id         = azurerm_linux_virtual_machine.vm.identity[0].principal_id
}

resource "azurerm_log_analytics_saved_search" "rca_metric_snapshot" {
  name                       = "rca-metric-snapshot"
  log_analytics_workspace_id = azurerm_log_analytics_workspace.law.id
  category                   = "RCA Functions"
  display_name               = "rca_metric_snapshot"
  function_alias             = "rca_metric_snapshot"
  function_parameters        = ["serviceName:string", "alertTime:datetime"]
  query                      = <<-KQL
    let windowStart = alertTime - 5m;
    let serviceMatches = serviceName == "${local.service_name}";
    let cpuAvg = toscalar(
        Perf
        | where TimeGenerated between (windowStart .. alertTime)
        | where _ResourceId == "${azurerm_linux_virtual_machine.vm.id}"
        | where CounterPath == "\\Processor Information(_Total)\\% Processor Time"
        | summarize avg(CounterValue)
    );
    let memoryAvg = toscalar(
        Perf
        | where TimeGenerated between (windowStart .. alertTime)
        | where _ResourceId == "${azurerm_linux_virtual_machine.vm.id}"
        | where CounterPath == "\\Memory\\% Committed Bytes In Use"
        | summarize avg(CounterValue)
    );
    let customRows = materialize(
        AppMetricsRaw_CL
        | where TimeGenerated between (windowStart .. alertTime)
        | where ServiceName == serviceName
    );
    print
        service_name = serviceName,
        alert_timestamp = alertTime,
        cpu_percent_avg5 = iff(serviceMatches, cpuAvg, real(null)),
        memory_percent_avg5 = iff(serviceMatches, memoryAvg, real(null)),
        http_5xx_rate_avg5 = iff(serviceMatches, toscalar(customRows | summarize avg(Http5xxRatePct)), real(null)),
        db_conn_pool_wait_avg5 = iff(serviceMatches, toscalar(customRows | summarize avg(DbConnPoolWaitMs)), real(null)),
        request_latency_p99_avg5 = iff(serviceMatches, toscalar(customRows | summarize avg(RequestLatencyP99Ms)), real(null))
  KQL

  depends_on = [azurerm_monitor_data_collection_rule.app_metrics]
}

# ---------------------------------------------------------------------------
# Action Group — fires webhook to RCA backend
# ---------------------------------------------------------------------------
resource "azurerm_monitor_action_group" "rca_webhook" {
  name                = "ag-rca-webhook"
  resource_group_name = azurerm_resource_group.rg.name
  short_name          = "rca-hook"

  webhook_receiver {
    name                    = "rca-backend"
    service_uri             = var.webhook_url != "" ? "${var.webhook_url}/api/incident/new" : "https://example.com/placeholder"
    use_common_alert_schema = true
  }
}

# ---------------------------------------------------------------------------
# Alert Rules — one per signature
# ---------------------------------------------------------------------------

# 1. cpu_saturation_burst — CPU > 90% for 3 min
resource "azurerm_monitor_metric_alert" "cpu_saturation" {
  name                = "alert-cpu-saturation-burst"
  resource_group_name = azurerm_resource_group.rg.name
  scopes              = [azurerm_linux_virtual_machine.vm.id]
  description         = "CPU > 90% — signature: cpu_saturation_burst"
  severity            = 2
  frequency           = "PT1M"
  window_size         = "PT5M"

  criteria {
    metric_namespace = "Microsoft.Compute/virtualMachines"
    metric_name      = "Percentage CPU"
    aggregation      = "Average"
    operator         = "GreaterThan"
    threshold        = 90
  }

  action {
    action_group_id = azurerm_monitor_action_group.rca_webhook.id
    webhook_properties = {
      service_name       = "payment-api"
      incident_signature = "cpu_saturation_burst"
    }
  }
}

# 2. memory_leak_progressive — Available Memory < 1500 MB (on 8 GB VM ≈ 81%)
resource "azurerm_monitor_metric_alert" "memory_leak" {
  name                = "alert-memory-leak-progressive"
  resource_group_name = azurerm_resource_group.rg.name
  scopes              = [azurerm_linux_virtual_machine.vm.id]
  description         = "Available memory < 1500 MB — signature: memory_leak_progressive"
  severity            = 2
  frequency           = "PT1M"
  window_size         = "PT5M"

  criteria {
    metric_namespace = "Microsoft.Compute/virtualMachines"
    metric_name      = "Available Memory Bytes"
    aggregation      = "Average"
    operator         = "LessThan"
    threshold        = 1500 * 1024 * 1024 # 1500 MB in bytes
  }

  action {
    action_group_id = azurerm_monitor_action_group.rca_webhook.id
    webhook_properties = {
      service_name       = "payment-api"
      incident_signature = "memory_leak_progressive"
    }
  }
}

# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------
output "vm_public_ip" {
  value = azurerm_public_ip.pip.ip_address
}

output "law_workspace_id" {
  value = azurerm_log_analytics_workspace.law.workspace_id
}

output "law_workspace_resource_id" {
  value = azurerm_log_analytics_workspace.law.id
}

output "app_metrics_dce_logs_ingestion_endpoint" {
  value = azurerm_monitor_data_collection_endpoint.app_metrics.logs_ingestion_endpoint
}

output "app_metrics_dcr_immutable_id" {
  value = azurerm_monitor_data_collection_rule.app_metrics.immutable_id
}

output "app_metrics_dcr_stream_name" {
  value = local.app_metrics_stream_name
}

output "app_metrics_table_name" {
  value = local.app_metrics_table_name
}

output "rca_metric_snapshot_function_name" {
  value = azurerm_log_analytics_saved_search.rca_metric_snapshot.function_alias
}

output "ssh_command" {
  value = "ssh azureuser@${azurerm_public_ip.pip.ip_address}"
}

output "nsg_block_outbound_status" {
  value = "nsg_block_outbound = ${var.nsg_block_outbound}"
}
