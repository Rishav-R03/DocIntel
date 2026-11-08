"""Create the local (moto) S3 bucket and SQS queues that Terraform creates in AWS. Idempotent."""

import asyncio
import json
import os

import aioboto3

ENDPOINT = os.environ["DOCINTEL_AWS_ENDPOINT_URL"]
REGION = os.environ.get("DOCINTEL_AWS_REGION", "us-east-1")
BUCKET = os.environ["DOCINTEL_S3_BUCKET"]
JOBS_QUEUE = os.environ["DOCINTEL_SQS_JOBS_URL"].rsplit("/", 1)[-1]


async def main() -> None:
    session = aioboto3.Session(region_name=REGION)
    async with (
        session.client("s3", endpoint_url=ENDPOINT) as s3,
        session.client("sqs", endpoint_url=ENDPOINT) as sqs,
    ):
        await s3.create_bucket(Bucket=BUCKET)

        dlq = await sqs.create_queue(QueueName=f"{JOBS_QUEUE}-dlq")
        dlq_attrs = await sqs.get_queue_attributes(
            QueueUrl=dlq["QueueUrl"], AttributeNames=["QueueArn"]
        )
        redrive = {"deadLetterTargetArn": dlq_attrs["Attributes"]["QueueArn"], "maxReceiveCount": 3}
        await sqs.create_queue(
            QueueName=JOBS_QUEUE,
            Attributes={"VisibilityTimeout": "180", "RedrivePolicy": json.dumps(redrive)},
        )
    print(f"bootstrapped bucket={BUCKET} queue={JOBS_QUEUE}")


if __name__ == "__main__":
    asyncio.run(main())
