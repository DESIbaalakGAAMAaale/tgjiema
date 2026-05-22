import datetime

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
        "created_at": datetime.datetime.utcnow().isoformat(),
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
    return SendTaskResult(
        target_user_id=row["target_user_id"],
        channel_id=row["channel_id"],
        message_id=row["message_id"],
        file_code=row.get("file_code", ""),
    )


class SendTaskResult:
    def __init__(self, target_user_id: int, channel_id: int, message_id: int, file_code: str):
        self.target_user_id = target_user_id
        self.channel_id = channel_id
        self.message_id = message_id
        self.file_code = file_code
