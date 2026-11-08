terraform {
  required_version = ">= 1.11" # S3 backend native locking (use_lockfile) replaces DynamoDB

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Partial config: `terraform init -backend-config=backend.hcl` (see backend.hcl.example).
  backend "s3" {}
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project     = "docintel"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
