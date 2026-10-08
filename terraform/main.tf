locals {
  repo_root       = abspath("${path.root}/..")
  dashboards_dir  = "${local.repo_root}/dashboards"
  dashboard_files = fileset(local.dashboards_dir, "*.json")

  dashboard_hashes = [
    for file_name in local.dashboard_files :
    filesha256("${local.dashboards_dir}/${file_name}")
  ]
}

resource "null_resource" "mock_rmp_deploy" {
  triggers = {
    dashboards_hash = sha256(join(",", local.dashboard_hashes))
    environment     = var.environment
  }

  provisioner "local-exec" {
    working_dir = local.repo_root
    command     = "${var.python_executable} scripts/mock_deploy.py --all --environment ${var.environment}"
  }
}
