variable "dashboard" { type = any }

resource "null_resource" "deploy" {
  triggers = {
    content_hash = sha256(jsonencode(var.dashboard))
  }

  provisioner "local-exec" {
    command = "python ${path.module}/mock_deploy.py '${jsonencode(var.dashboard)}'"
  }
}

output "dashboard_id" {
  value = "mock-${sha1(jsonencode(var.dashboard))}"
}
