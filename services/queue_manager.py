import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Optional


@dataclass
class SendTask:
    target_user_id: int
    channel_id: int
    message_id: int
    file_code: str


class TaskQueue:
    def __init__(self):
        self._queue: deque[SendTask] = deque()
        self._lock = asyncio.Lock()

    async def push(self, task: SendTask) -> None:
        async with self._lock:
            self._queue.append(task)

    async def pop(self) -> Optional[SendTask]:
        async with self._lock:
            if self._queue:
                return self._queue.popleft()
            return None

    async def size(self) -> int:
        async with self._lock:
            return len(self._queue)


task_queue = TaskQueue()


async def enqueue_send_task(
    target_user_id: int, channel_id: int, message_id: int, file_code: str
) -> None:
    task = SendTask(
        target_user_id=target_user_id,
        channel_id=channel_id,
        message_id=message_id,
        file_code=file_code,
    )
    await task_queue.push(task)


async def dequeue_send_task() -> Optional[SendTask]:
    return await task_queue.pop()