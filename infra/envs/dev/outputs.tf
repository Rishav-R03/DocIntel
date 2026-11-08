# These values become GitHub Environment variables for the `dev` environment (see infra/README.md).

output "alb_dns_name" { value = module.alb.dns_name }
output "ecr_repository_url" { value = module.ecr.repository_url }
output "ecs_cluster" { value = module.cluster.name }
output "api_service" { value = module.api.service_name }
output "worker_service" { value = module.worker.service_name }
output "api_task_family" { value = module.api.task_definition_family }
output "worker_task_family" { value = module.worker.task_definition_family }
output "private_subnet_ids" { value = join(",", module.network.private_subnet_ids) }
output "api_security_group_id" { value = module.api.security_group_id }
output "deploy_role_arn" { value = module.ci_deploy_role.role_arn }
output "gemini_secret_arn" { value = aws_secretsmanager_secret.gemini_api_key.arn }
output "jobs_dlq_url" { value = module.jobs_queue.dlq_url }
