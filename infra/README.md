# Infrastructure (Terraform)

```text
modules/   network · storage (S3+KMS) · queue (SQS+DLQ) · database (RDS) · ecr · ecs_cluster
           ecs_service (reusable Fargate service) · alb · ci_oidc (GitHub → AWS deploy role)
envs/dev/  wires the modules for the dev environment
```

Requires Terraform ≥ 1.11 and AWS provider ~> 6.0. There's no local install needed: `docker run hashicorp/terraform:1.11.4 ...` works.

## First-time bootstrap of `dev`

1. **State bucket**: create a versioned and encrypted S3 bucket for Terraform state, once per account. Copy `envs/dev/backend.hcl.example` to `backend.hcl` and fill in the bucket name.
2. **Variables**: in `envs/dev/terraform.tfvars`, set `github_repository` and optionally `certificate_arn`. Without a certificate the ALB serves plain HTTP, which is acceptable only for the very first dev bootstrap. The Phase 1 exit criterion requires TLS 1.3.
3. **Apply**:
   ```bash
   cd envs/dev
   terraform init -backend-config=backend.hcl
   terraform apply
   ```
   The services are created pointing at image tag `initial`, which doesn't exist yet, so their tasks fail to start until the first CI deploy. This is expected.
4. **GitHub**: create an Environment named `dev`, and add these **environment variables** from `terraform output`:

   | GitHub variable | Terraform output |
   |---|---|
   | `AWS_REGION` | (your region) |
   | `AWS_DEPLOY_ROLE_ARN` | `deploy_role_arn` |
   | `ECR_REPOSITORY_URL` | `ecr_repository_url` |
   | `ECS_CLUSTER` | `ecs_cluster` |
   | `API_SERVICE` / `WORKER_SERVICE` | `api_service` / `worker_service` |
   | `API_TASK_FAMILY` / `WORKER_TASK_FAMILY` | `api_task_family` / `worker_task_family` |
   | `PRIVATE_SUBNET_IDS` | `private_subnet_ids` |
   | `API_SECURITY_GROUP_ID` | `api_security_group_id` |
   | `API_BASE_URL` | `https://<alb_dns_name or your DNS record>` |

5. **Gemini key** (needed from Phase 3), set out of band so it never enters Terraform state:
   ```bash
   aws secretsmanager put-secret-value --secret-id "$(terraform output -raw gemini_secret_arn)" --secret-string '<key>'
   ```
6. Push to `main`. CI runs, then `deploy.yml` builds the image, runs migrations as a one-off task, and rolls out both services.

## Ownership split

Terraform owns the infrastructure and the **initial** task definitions. CI owns **task-definition revisions** (the image tag). `aws_ecs_service` ignores `task_definition` and `desired_count` drift, so a later `terraform apply` never rolls back a deploy or fights autoscaling.
