terraform {
  required_version = ">= 1.5"
}

module "dashboard" {
  source    = "../modules/rmp-dashboard"
  dashboard = yamldecode(file("${path.module}/../../dashboards/customer-service/dashboard.yaml"))
}

output "dashboard_id" {
  value = module.dashboard.dashboard_id
}
