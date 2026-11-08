"""Queue message contract between the api (producer) and the worker (consumer).

Versioned explicitly: producers and consumers deploy independently, so a consumer must be able to
recognise a message shape it does not understand instead of mis-parsing it.
"""

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict


class JobSubmitted(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["job.submitted"] = "job.submitted"
    v: Literal[1] = 1
    job_id: uuid.UUID
    schema_id: str
