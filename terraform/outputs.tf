output "repo_root" {
  description = "Resolved repository root used by the mock deployer."
  value       = local.repo_root
}

output "dashboard_count" {
  description = "Number of dashboard definitions discovered."
  value       = length(local.dashboard_files)
}

output "dashboard_names" {
  description = "Dashboard definition file names discovered."
  value       = sort(tolist(local.dashboard_files))
}

output "deployed_files" {
  description = "Absolute paths of the mock RMP deployment artifacts."
  value = [
    for file_name in local.dashboard_files :
    "${local.repo_root}/deployed/${file_name}"
  ]
}
