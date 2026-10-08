variable "python_executable" {
  description = "Interpreter used by the mock RMP deployer. Use 'py' on Windows if 'python' is not on PATH."
  type        = string
  default     = "python"
}

variable "environment" {
  description = "Logical environment recorded in the deployment manifest."
  type        = string
  default     = "local"
}
