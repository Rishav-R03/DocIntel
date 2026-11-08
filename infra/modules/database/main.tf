terraform {
  required_providers {
    aws = { source = "hashicorp/aws" }
  }
}

variable "name" { type = string }
variable "vpc_id" { type = string }
variable "subnet_ids" { type = list(string) }

variable "allowed_security_group_ids" {
  description = "Security groups allowed to reach Postgres on 5432 (api, worker)."
  type        = list(string)
}

variable "instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "engine_version" {
  description = "Major version only; AWS applies minor upgrades (auto_minor_version_upgrade)."
  type        = string
  default     = "16"
}

variable "allocated_storage" {
  type    = number
  default = 20
}

variable "max_allocated_storage" {
  type    = number
  default = 100
}

variable "multi_az" {
  type    = bool
  default = false
}

variable "backup_retention_days" {
  type    = number
  default = 7
}

variable "deletion_protection" {
  type    = bool
  default = true
}

variable "skip_final_snapshot" {
  type    = bool
  default = false
}

variable "db_name" {
  type    = string
  default = "docintel"
}

variable "master_username" {
  type    = string
  default = "docintel_admin"
}

resource "aws_kms_key" "this" {
  description             = "RDS storage encryption for ${var.name}"
  enable_key_rotation     = true
  deletion_window_in_days = 30
}

resource "aws_db_subnet_group" "this" {
  name       = var.name
  subnet_ids = var.subnet_ids
}

resource "aws_security_group" "this" {
  name        = "${var.name}-db"
  description = "Postgres access for ${var.name}"
  vpc_id      = var.vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "postgres" {
  count                        = length(var.allowed_security_group_ids)
  security_group_id            = aws_security_group.this.id
  referenced_security_group_id = var.allowed_security_group_ids[count.index]
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_db_parameter_group" "this" {
  name   = var.name
  family = "postgres16"

  parameter {
    name  = "rds.force_ssl" # reject any non-TLS connection
    value = "1"
  }

  parameter {
    name  = "log_min_duration_statement" # log queries slower than 500 ms
    value = "500"
  }
}

resource "aws_db_instance" "this" {
  identifier            = var.name
  engine                = "postgres"
  engine_version        = var.engine_version
  instance_class        = var.instance_class
  allocated_storage     = var.allocated_storage
  max_allocated_storage = var.max_allocated_storage
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = aws_kms_key.this.arn

  db_name                     = var.db_name
  username                    = var.master_username
  manage_master_user_password = true # RDS creates + rotates the secret; it never touches TF state

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.this.id]
  parameter_group_name   = aws_db_parameter_group.this.name
  publicly_accessible    = false
  ca_cert_identifier     = "rds-ca-rsa2048-g1"

  multi_az                   = var.multi_az
  backup_retention_period    = var.backup_retention_days
  auto_minor_version_upgrade = true
  copy_tags_to_snapshot      = true
  deletion_protection        = var.deletion_protection
  skip_final_snapshot        = var.skip_final_snapshot
  final_snapshot_identifier  = "${var.name}-final"
}

output "address" { value = aws_db_instance.this.address }
output "port" { value = aws_db_instance.this.port }
output "db_name" { value = aws_db_instance.this.db_name }
output "security_group_id" { value = aws_security_group.this.id }
output "master_user_secret_arn" { value = aws_db_instance.this.master_user_secret[0].secret_arn }
