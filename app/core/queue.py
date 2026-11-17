"""Redis-backed workflow job queue.

Reliable-queue pattern on Redis lists plus a delayed sorted set:

- ``main``      — jobs waiting to be claimed (list)
- ``processing``— jobs currently claimed by a worker (list; moved atomically)
- ``delayed``   — jobs waiting for their eta (zset, score = eta in ms)

Redis is a *queue only*; the PostgreSQL execution row is the source of truth
for state. A worker crash loses nothing: unacknowledged ``processing`` jobs are
pushed back to ``main`` on the next worker start, and executions whose worker
died mid-run are recovered by the heartbeat poller.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field

from redis.asyncio.client import Redis

logger = logging.getLogger("app.queue")

QUEUE_PREFIX = "vandad:wf"
MAIN_KEY = f"{QUEUE_PREFIX}:queue:main"
PROCESSING_KEY = f"{QUEUE_PREFIX}:queue:processing"
DELAYED_KEY = f"{QUEUE_PREFIX}:queue:delayed"


@dataclass(frozen=True)
class WorkflowJob:
    execution_id: uuid.UUID
    company_id: uuid.UUID
    attempt: int = 1
    enqueued_at: float = field(default_factory=time.time)

    def to_json(self) -> str:
        payload = asdict(self)
        payload["execution_id"] = str(self.execution_id)
        payload["company_id"] = str(self.company_id)
        return json.dumps(payload, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> WorkflowJob:
        payload = json.loads(raw)
        return cls(
            execution_id=uuid.UUID(payload["execution_id"]),
            company_id=uuid.UUID(payload["company_id"]),
            attempt=int(payload.get("attempt", 1)),
            enqueued_at=float(payload.get("enqueued_at", time.time())),
        )


class WorkflowQueue:
    def __init__(self, client: Redis) -> None:
        self._client = client

    async def enqueue(self, job: WorkflowJob) -> None:
        await self._client.lpush(MAIN_KEY, job.to_json())

    async def enqueue_delayed(self, job: WorkflowJob, delay_seconds: float) -> None:
        eta_ms = int((time.time() + max(0.0, delay_seconds)) * 1000)
        await self._client.zadd(DELAYED_KEY, {job.to_json(): eta_ms})

    async def claim(self) -> WorkflowJob | None:
        """Move due delayed jobs into main, then claim one job (main -> processing)."""
        await self._promote_due_delayed()
        raw = await self._client.rpoplpush(MAIN_KEY, PROCESSING_KEY)
        if raw is None:
            return None
        try:
            return WorkflowJob.from_json(raw)
        except (ValueError, KeyError, TypeError):
            # Malformed payload: drop it from processing so it can't wedge the queue.
            await self._client.lrem(PROCESSING_KEY, 1, raw)
            logger.error("discarded malformed queue payload")
            return None

    async def complete(self, job: WorkflowJob) -> None:
        await self._client.lrem(PROCESSING_KEY, 1, job.to_json())

    async def requeue_processing(self) -> int:
        """Return all in-flight jobs to main (worker startup / crash recovery)."""
        moved = 0
        while True:
            raw = await self._client.rpoplpush(PROCESSING_KEY, MAIN_KEY)
            if raw is None:
                break
            moved += 1
        if moved:
            logger.warning("requeued %d in-flight job(s) left by a previous worker", moved)
        return moved

    async def _promote_due_delayed(self) -> int:
        now_ms = int(time.time() * 1000)
        due = await self._client.zrangebyscore(DELAYED_KEY, "-inf", now_ms, start=0, num=100)
        if not due:
            return 0
        moved = 0
        for raw in due:
            removed = await self._client.zrem(DELAYED_KEY, raw)
            if removed:
                await self._client.lpush(MAIN_KEY, raw)
                moved += 1
        return moved

    async def depths(self) -> dict[str, int]:
        """Operational health snapshot (safe to expose)."""
        main = int(await self._client.llen(MAIN_KEY))
        processing = int(await self._client.llen(PROCESSING_KEY))
        delayed = int(await self._client.zcard(DELAYED_KEY))
        return {"main": main, "processing": processing, "delayed": delayed}
