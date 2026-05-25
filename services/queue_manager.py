import datetime
import json

from database import get_send_queue_col


async def enqueue_send_task(
    target_user_id: int, channel_id: int, message_id: int, file_code: str
) -> None:
    col = get_send_queue_col()
    await col.insert_one({
        "target_user_id": target_user_id,
        "channel_id": channel_id,
        "message_id": message_id,
        "file_code": file_code,
        "task_type": "single",
        "channel_msg_ids": "",
        "batch_file_meta": "",
        "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "processed": 0,
    })


async def enqueue_batch_send_task(
    target_user_id: int,
    channel_id: int,
    channel_msg_ids: list,
    batch_file_meta: str,
    file_code: str,
    page: int = 1,
) -> None:
    col = get_send_queue_col()
    await col.insert_one({
        "target_user_id": target_user_id,
        "channel_id": channel_id,
        "message_id": channel_msg_ids[0] if channel_msg_ids else 0,
        "file_code": file_code,
        "task_type": "batch",
        "channel_msg_ids": json.dumps(channel_msg_ids),
        "batch_file_meta": batch_file_meta,
        "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "processed": 0,
    })


async def dequeue_send_task():
    col = get_send_queue_col()
    rows = await col.find({"processed": 0}, limit=1)
    if not rows:
        return None
    row = rows[0]
    pk = row.get("id")
    await col.update_one({"id": pk}, {"$set": {"processed": 1}})

    task_type = row.get("task_type", "single")

    channel_msg_ids = []
    channel_msg_ids_str = row.get("channel_msg_ids", "")
    if channel_msg_ids_str:
        try:
            channel_msg_ids = json.loads(channel_msg_ids_str)
        except (json.JSONDecodeError, TypeError):
            channel_msg_ids = []

    return SendTaskResult(
        target_user_id=row["target_user_id"],
        channel_id=row["channel_id"],
        message_id=row["message_id"],
        file_code=row.get("file_code", ""),
        task_type=task_type,
        channel_msg_ids=channel_msg_ids,
        batch_file_meta=row.get("batch_file_meta", ""),
    )


class SendTaskResult:
    def __init__(
        self,
        target_user_id: int,
        channel_id: int,
        message_id: int,
        file_code: str,
        task_type: str = "single",
        channel_msg_ids: list = None,
        batch_file_meta: str = "",
    ):
        self.target_user_id = target_user_id
        self.channel_id = channel_id
        self.message_id = message_id
        self.file_code = file_code
        self.task_type = task_type
        self.channel_msg_ids = channel_msg_ids or []
        self.batch_file_meta = batch_file_meta
