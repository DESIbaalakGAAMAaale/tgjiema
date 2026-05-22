import json
from typing import Dict, List

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    BOT_TOKENS: Dict[str, str] = {
        "UPLOAD_BOT": "",
        "DECODER_BOT": "",
        "SENDER_BOT": "",
        "BACKUP_BOT_1": "",
        "BACKUP_BOT_2": "",
        "BACKUP_BOT_3": "",
    }

    MAIN_STORAGE_CHANNEL_ID: int = -1000000000000
    DECODER_BOT_CHAT_ID: int = 0

    BACKUP_CHANNELS_GROUP_1: List[int] = []
    BACKUP_CHANNELS_GROUP_2: List[int] = []
    BACKUP_CHANNELS_GROUP_3: List[int] = []

    MONGODB_URI: str = "mongodb://localhost:27017"
    MONGODB_DB_NAME: str = "tgjiema"

    ADMIN_WEB_PORT: int = 8080
    ADMIN_WEB_HOST: str = "127.0.0.1"
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "admin123"

    FREE_DAILY_QUOTA: int = 3
    BASIC_DAILY_QUOTA: int = 20
    PREMIUM_DAILY_QUOTA: int = -1

    FREE_EXTERNAL_DAILY_QUOTA: int = 0
    BASIC_EXTERNAL_DAILY_QUOTA: int = -1
    PREMIUM_EXTERNAL_DAILY_QUOTA: int = -1

    RATE_LIMIT_GLOBAL_PER_SECOND: int = 30
    RATE_LIMIT_PER_USER_PER_MINUTE: int = 10

    LOG_LEVEL: str = "INFO"

    FILE_CODE_PREFIX: str = "tgwenjian"

    @property
    def ALL_BACKUP_CHANNELS(self) -> List[int]:
        return (
            self.BACKUP_CHANNELS_GROUP_1
            + self.BACKUP_CHANNELS_GROUP_2
            + self.BACKUP_CHANNELS_GROUP_3
        )

    @property
    def ALL_STORAGE_CHANNELS(self) -> List[int]:
        return [self.MAIN_STORAGE_CHANNEL_ID] + self.ALL_BACKUP_CHANNELS

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

        @classmethod
        def parse_env_var(cls, field_name: str, raw_val: str):
            if field_name == "BOT_TOKENS":
                return json.loads(raw_val)
            if field_name in (
                "BACKUP_CHANNELS_GROUP_1",
                "BACKUP_CHANNELS_GROUP_2",
                "BACKUP_CHANNELS_GROUP_3",
            ):
                return json.loads(raw_val)
            return raw_val


settings = Settings()