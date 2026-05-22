from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from config import settings

_client: AsyncIOMotorClient = None
_db: AsyncIOMotorDatabase = None


async def init_db():
    global _client, _db
    _client = AsyncIOMotorClient(settings.MONGODB_URI)
    _db = _client[settings.MONGODB_DB_NAME]
    await _db.users.create_index("user_id", unique=True)
    await _db.file_records.create_index("file_code", unique=True)
    await _db.decode_logs.create_index("file_code")
    await _db.decode_logs.create_index("requester_id")


async def close_db():
    global _client
    if _client:
        _client.close()


def get_db() -> AsyncIOMotorDatabase:
    return _db


def get_users_col():
    return _db.users


def get_file_records_col():
    return _db.file_records


def get_decode_logs_col():
    return _db.decode_logs