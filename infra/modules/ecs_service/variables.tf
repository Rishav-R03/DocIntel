variable "name" { type = string }
variable "region" { type = string }
variable "cluster_arn" { type = string }
variable "cluster_name" { type = string }
variable "vpc_id" { type = string }
variable "subnet_ids" { type = list(string) }
variable "image" { type = string }

variable "command" {
  type    = list(string)
  default = null
}

variable "port" {
  type    = number
  default = null
}

variable "cpu" {
  type    = number
  default = 1024
}

variable "memory" {
  type    = number
  default = 2048
}

variable "cpu_architecture" {
  type    = string
  default = "X86_64"
}

variable "environment" {
  type    = map(string)
  default = {}
}

variable "secrets" {
  description = "Env var name -> Secrets Manager valueFrom (ARN, optionally ARN:json-key::)."
  type        = map(string)
  default     = {}
}

variable "secret_arns" {
  description = "Secret ARNs the execution role may read to inject `secrets`."
  type        = list(string)
  default     = []
}

variable "task_role_policy_json" {
  description = "IAM policy for the application itself (S3, SQS, KMS)."
  type        = string
  default     = null
}

variable "health_check_command" {
  type    = list(string)
  default = null
}

variable "stop_timeout" {
  description = "Seconds between SIGTERM and SIGKILL (Fargate max 120)."
  type        = number
  default     = 30
}

variable "target_group_arn" {
  type    = string
  default = null
}

variable "ingress_security_group_id" {
  type    = string
  default = null
}

variable "capacity_provider" {
  type    = string
  default = "FARGATE"
}

variable "desired_count" {
  type    = number
  default = 1
}

variable "enable_autoscaling" {
  type    = bool
  default = true
}

variable "min_capacity" {
  type    = number
  default = 1
}

variable "max_capacity" {
  type    = number
  default = 4
}

variable "cpu_target_percent" {
  type    = number
  default = 70
}

variable "log_retention_days" {
  type    = number
  default = 30
}
