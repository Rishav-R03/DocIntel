terraform {
  required_providers {
    aws = { source = "hashicorp/aws" }
  }
}

variable "name" { type = string }
variable "vpc_id" { type = string }
variable "subnet_ids" { type = list(string) }

variable "certificate_arn" {
  description = "ACM certificate. When null (bootstrap dev only) the ALB serves plain HTTP."
  type        = string
  default     = null
}

variable "allowed_ingress_cidrs" {
  description = "Narrow this to the upstream API Gateway / corporate ranges outside dev."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "target_port" {
  type    = number
  default = 8000
}

variable "health_check_path" {
  type    = string
  default = "/health/ready"
}

variable "idle_timeout" {
  description = "Seconds; must exceed the SSE heartbeat interval (15 s) with margin."
  type        = number
  default     = 120
}

variable "deletion_protection" {
  type    = bool
  default = false
}

locals {
  https = var.certificate_arn != null
}

resource "aws_security_group" "this" {
  name        = "${var.name}-alb"
  description = "Ingress to ${var.name} ALB"
  vpc_id      = var.vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "http" {
  count             = length(var.allowed_ingress_cidrs)
  security_group_id = aws_security_group.this.id
  cidr_ipv4         = var.allowed_ingress_cidrs[count.index]
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
}

resource "aws_vpc_security_group_ingress_rule" "https" {
  count             = local.https ? length(var.allowed_ingress_cidrs) : 0
  security_group_id = aws_security_group.this.id
  cidr_ipv4         = var.allowed_ingress_cidrs[count.index]
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.this.id
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_lb" "this" {
  name                       = var.name
  load_balancer_type         = "application"
  internal                   = false
  security_groups            = [aws_security_group.this.id]
  subnets                    = var.subnet_ids
  idle_timeout               = var.idle_timeout
  drop_invalid_header_fields = true
  enable_deletion_protection = var.deletion_protection
}

resource "aws_lb_target_group" "this" {
  name                 = var.name
  port                 = var.target_port
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = var.vpc_id
  deregistration_delay = 30

  health_check {
    path                = var.health_check_path
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener" "https" {
  count             = local.https ? 1 : 0
  load_balancer_arn = aws_lb.this.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-3-2021-06" # TLS 1.3 only (NFR-06)
  certificate_arn   = var.certificate_arn
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.this.arn
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = local.https ? "redirect" : "forward"
    target_group_arn = local.https ? null : aws_lb_target_group.this.arn

    dynamic "redirect" {
      for_each = local.https ? [1] : []
      content {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }
}

output "dns_name" { value = aws_lb.this.dns_name }
output "security_group_id" { value = aws_security_group.this.id }
output "target_group_arn" { value = aws_lb_target_group.this.arn }
