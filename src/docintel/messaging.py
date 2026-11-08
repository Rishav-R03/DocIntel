"""SQS publisher for job messages."""

from typing import Any

from docintel.domain.messages import JobSubmitted


class JobQueue:
    def __init__(self, sqs: Any, queue_url: str) -> None:
        self._sqs = sqs
        self._queue_url = queue_url

    async def publish(self, message: JobSubmitted, *, request_id: str | None) -> str:
        attributes = {"type": {"DataType": "String", "StringValue": message.type}}
        if request_id:
            # Correlates worker logs with the originating HTTP request until OTel trace context
            # propagation lands (Phase 6).
            attributes["request_id"] = {"DataType": "String", "StringValue": request_id}
        response = await self._sqs.send_message(
            QueueUrl=self._queue_url,
            MessageBody=message.model_dump_json(),
            MessageAttributes=attributes,
        )
        message_id: str = response["MessageId"]
        return message_id
