data "aws_caller_identity" "current" {}

locals {
  name       = "docintel-${var.environment}"
  account_id = data.aws_caller_identity.current.account_id
  image      = "${module.ecr.repository_url}:${var.image_tag}"
}

# ------------------------------------------------------------------ foundation
module "network" {
  source             = "../../modules/network"
  name               = local.name
  region             = var.region
  azs                = var.azs
  single_nat_gateway = true # dev: one NAT; prod: one per AZ
}

module "ecr" {
  source = "../../modules/ecr"
  name   = "docintel" # one repository shared by every environment; images are promoted, not rebuilt
}

module "storage" {
  source        = "../../modules/storage"
  bucket_name   = "${local.name}-${local.account_id}" # bucket names are global
  force_destroy = true                                # dev only
}

module "jobs_queue" {
  source = "../../modules/queue"
  name   = "${local.name}-jobs"
}

module "audit_replay_queue" {
  source = "../../modules/queue"
  name   = "${local.name}-audit-replay"
}

module "database" {
  source                     = "../../modules/database"
  name                       = local.name
  vpc_id                     = module.network.vpc_id
  subnet_ids                 = module.network.private_subnet_ids
  allowed_security_group_ids = [module.api.security_group_id, module.worker.security_group_id]
  instance_class             = "db.t4g.micro"
  multi_az                   = false
  deletion_protection        = false # dev only
  skip_final_snapshot        = true  # dev only
}

module "cluster" {
  source = "../../modules/ecs_cluster"
  name   = local.name
}

module "alb" {
  source          = "../../modules/alb"
  name            = "${local.name}-api"
  vpc_id          = module.network.vpc_id
  subnet_ids      = module.network.public_subnet_ids
  certificate_arn = var.certificate_arn
}

# Container only: the value is set out of band (`aws secretsmanager put-secret-value`) so it never
# lands in Terraform state. Injected into tasks from Phase 3, once it holds a value; ECS refuses
# to start a task whose referenced secret has no value.
resource "aws_secretsmanager_secret" "gemini_api_key" {
  name                    = "${local.name}/gemini-api-key"
  recovery_window_in_days = 7
}

# ------------------------------------------------------------------ application
locals {
  db_secret = module.database.master_user_secret_arn

  app_environment = {
    DOCINTEL_ENV              = var.environment
    DOCINTEL_LOG_LEVEL        = "INFO"
    DOCINTEL_DB_HOST          = module.database.address
    DOCINTEL_DB_PORT          = tostring(module.database.port)
    DOCINTEL_DB_NAME          = module.database.db_name
    DOCINTEL_DB_SSL_MODE      = "verify-full"
    DOCINTEL_DB_SSL_ROOT_CERT = "/etc/ssl/certs/rds-global-bundle.pem"
    DOCINTEL_AWS_REGION       = var.region
    DOCINTEL_S3_BUCKET        = module.storage.bucket_name
    DOCINTEL_S3_KMS_KEY_ID    = module.storage.kms_key_arn
    DOCINTEL_SQS_JOBS_URL     = module.jobs_queue.url
  }

  # ECS resolves JSON keys inside the RDS-managed secret: <arn>:<json-key>::
  app_secrets = {
    DOCINTEL_DB_USER     = "${local.db_secret}:username::"
    DOCINTEL_DB_PASSWORD = "${local.db_secret}:password::"
  }
}

data "aws_iam_policy_document" "api" {
  statement {
    sid       = "StageAndReadDocuments"
    actions   = ["s3:PutObject", "s3:PutObjectTagging", "s3:GetObject"] # tagged PUT needs PutObjectTagging
    resources = ["${module.storage.bucket_arn}/staging/*", "${module.storage.bucket_arn}/results/*"]
  }
  statement {
    sid       = "BucketHealthCheck" # HeadBucket requires ListBucket
    actions   = ["s3:ListBucket"]
    resources = [module.storage.bucket_arn]
  }
  statement {
    sid       = "BucketKms"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [module.storage.kms_key_arn]
  }
  statement {
    sid       = "EnqueueJobs"
    actions   = ["sqs:SendMessage"]
    resources = [module.jobs_queue.arn]
  }
}

data "aws_iam_policy_document" "worker" {
  statement {
    sid       = "ProcessDocuments"
    actions   = ["s3:GetObject", "s3:DeleteObject"]
    resources = ["${module.storage.bucket_arn}/staging/*"]
  }
  statement {
    sid       = "WriteResults"
    actions   = ["s3:PutObject", "s3:GetObject"]
    resources = ["${module.storage.bucket_arn}/results/*"]
  }
  statement {
    sid       = "BucketKms"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [module.storage.kms_key_arn]
  }
  statement {
    sid = "ConsumeJobs"
    actions = [
      "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility",
      "sqs:GetQueueAttributes",
    ]
    resources = [module.jobs_queue.arn, module.audit_replay_queue.arn]
  }
  statement {
    sid       = "PublishAuditReplay"
    actions   = ["sqs:SendMessage"]
    resources = [module.audit_replay_queue.arn]
  }
}

module "api" {
  source                    = "../../modules/ecs_service"
  name                      = "${local.name}-api"
  region                    = var.region
  cluster_arn               = module.cluster.arn
  cluster_name              = module.cluster.name
  vpc_id                    = module.network.vpc_id
  subnet_ids                = module.network.private_subnet_ids
  image                     = local.image
  port                      = 8000
  environment               = local.app_environment
  secrets                   = local.app_secrets
  secret_arns               = [local.db_secret]
  task_role_policy_json     = data.aws_iam_policy_document.api.json
  target_group_arn          = module.alb.target_group_arn
  ingress_security_group_id = module.alb.security_group_id
  health_check_command = [
    "CMD", "python", "-c",
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3)",
  ]
  desired_count      = 1
  min_capacity       = 1
  max_capacity       = 4
  cpu_target_percent = 70 # PRD NFR; request-count scaling added in Phase 7
}

module "worker" {
  source                = "../../modules/ecs_service"
  name                  = "${local.name}-worker"
  region                = var.region
  cluster_arn           = module.cluster.arn
  cluster_name          = module.cluster.name
  vpc_id                = module.network.vpc_id
  subnet_ids            = module.network.private_subnet_ids
  image                 = local.image
  command               = ["python", "-m", "docintel.worker"]
  environment           = local.app_environment
  secrets               = local.app_secrets
  secret_arns           = [local.db_secret]
  task_role_policy_json = data.aws_iam_policy_document.worker.json
  capacity_provider     = "FARGATE_SPOT" # dev: interruptions are fine, SQS redelivers
  desired_count         = 1
  min_capacity          = 1
  max_capacity          = 4
}

# ------------------------------------------------------------------ CI/CD
module "ci_deploy_role" {
  source               = "../../modules/ci_oidc"
  name                 = "${local.name}-github-deploy"
  region               = var.region
  github_repository    = var.github_repository
  github_environment   = var.environment
  create_oidc_provider = var.create_github_oidc_provider
  ecr_repository_arn   = module.ecr.repository_arn
  ecs_cluster_name     = module.cluster.name
  task_family_prefix   = local.name
  passable_role_arns = [
    module.api.task_role_arn, module.api.execution_role_arn,
    module.worker.task_role_arn, module.worker.execution_role_arn,
  ]
}
