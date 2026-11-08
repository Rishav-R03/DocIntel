variable "environment" {
  type    = string
  default = "dev"
}

variable "region" { type = string }
variable "azs" { type = list(string) }

variable "github_repository" {
  description = "GitHub repo allowed to deploy, as org/repo."
  type        = string
}

variable "certificate_arn" {
  description = "ACM cert for the ALB. Null = HTTP only (acceptable for the very first dev bootstrap only)."
  type        = string
  default     = null
}

variable "image_tag" {
  description = "Tag for the *initial* task definitions. CI deploys replace it with git SHAs."
  type        = string
  default     = "initial"
}

variable "create_github_oidc_provider" {
  type    = bool
  default = true
}
