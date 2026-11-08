#!/usr/bin/env bash
# Register new task-definition revisions with IMAGE, run migrations as a one-off task using the
# new revision, then roll both services and wait for them to stabilise.
#
# Migrations run *before* the rollout, so they must stay backward compatible with the currently
# running code for one release (expand/contract, implementation.md §8.2).
set -euo pipefail

: "${IMAGE:?}" "${CLUSTER:?}" "${API_SERVICE:?}" "${WORKER_SERVICE:?}"
: "${API_TASK_FAMILY:?}" "${WORKER_TASK_FAMILY:?}" "${SUBNETS:?}" "${SECURITY_GROUP:?}"

CONTAINER=app

register_revision() {
  local family=$1
  aws ecs describe-task-definition --task-definition "$family" --query taskDefinition --output json \
    | jq --arg image "$IMAGE" --arg name "$CONTAINER" '
        .containerDefinitions |= map(if .name == $name then .image = $image else . end)
        | del(.taskDefinitionArn, .revision, .status, .requiresAttributes, .compatibilities,
              .registeredAt, .registeredBy, .deregisteredAt)' \
    > "/tmp/${family}.json"
  aws ecs register-task-definition --cli-input-json "file:///tmp/${family}.json" \
    --query taskDefinition.taskDefinitionArn --output text
}

echo "::group::Register task definitions"
api_td=$(register_revision "$API_TASK_FAMILY")
worker_td=$(register_revision "$WORKER_TASK_FAMILY")
echo "api=$api_td"
echo "worker=$worker_td"
echo "::endgroup::"

echo "::group::Run database migrations"
task_arn=$(aws ecs run-task \
  --cluster "$CLUSTER" \
  --task-definition "$api_td" \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[${SUBNETS}],securityGroups=[${SECURITY_GROUP}],assignPublicIp=DISABLED}" \
  --overrides "{\"containerOverrides\":[{\"name\":\"${CONTAINER}\",\"command\":[\"alembic\",\"upgrade\",\"head\"]}]}" \
  --started-by "deploy-migrate" \
  --query 'tasks[0].taskArn' --output text)
echo "migration task: $task_arn"
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$task_arn"
exit_code=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$task_arn" \
  --query "tasks[0].containers[?name=='${CONTAINER}'].exitCode | [0]" --output text)
if [[ "$exit_code" != "0" ]]; then
  reason=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$task_arn" \
    --query 'tasks[0].stoppedReason' --output text)
  echo "::error::migration failed (exit=$exit_code, reason=$reason); services NOT updated"
  exit 1
fi
echo "::endgroup::"

echo "::group::Roll out services"
aws ecs update-service --cluster "$CLUSTER" --service "$API_SERVICE" --task-definition "$api_td" >/dev/null
aws ecs update-service --cluster "$CLUSTER" --service "$WORKER_SERVICE" --task-definition "$worker_td" >/dev/null
aws ecs wait services-stable --cluster "$CLUSTER" --services "$API_SERVICE" "$WORKER_SERVICE"
echo "::endgroup::"
echo "deployed $IMAGE"
