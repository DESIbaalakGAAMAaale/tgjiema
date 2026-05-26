import json
from typing import List, Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    UPLOAD_BOT_TOKEN: str = ""
    DECODER_BOT_TOKEN: str = ""
    SENDER_BOT_TOKEN: str = ""
    BACKUP_BOT_1_TOKEN: str = ""
    BACKUP_BOT_2_TOKEN: str = ""
    BACKUP_BOT_3_TOKEN: str = ""
    ADMIN_BOT_TOKEN: str = ""
    ADMIN_TELEGRAM_ID: int = 0

    MAIN_STORAGE_CHANNEL_ID: int = -1000000000000
    DECODER_BOT_CHAT_ID: int = 0

    BACKUP_CHANNELS_GROUP_1: List[int] = []
    BACKUP_CHANNELS_GROUP_2: List[int] = []
    BACKUP_CHANNELS_GROUP_3: List[int] = []

    COCKROACHDB_URL: str = ""

    FORCE_JOIN_CHANNEL_ID: int = 0
    FORCE_JOIN_CHANNEL_LINK: str = ""

    UPLOAD_BOT_USERNAME: str = ""
    DECODER_BOT_USERNAME: str = ""
    SENDER_BOT_USERNAME: str = ""

    RELAY_API_ID: int = 0
    RELAY_API_HASH: str = ""
    RELAY_PHONE: str = ""
    RELAY_CODE: str = ""

    AI_API_BASE_URL: str = ""
    AI_API_KEY: str = ""
    AI_MODEL: str = "gpt-4o-mini"

    R2_ACCOUNT_ID: str = ""
    R2_ACCESS_KEY_ID: str = ""
    R2_SECRET_ACCESS_KEY: str = ""
    R2_BUCKET_NAME: str = "tgjiema-backup"
    R2_ENDPOINT: Optional[str] = None

    DB_BACKUP_INTERVAL_MINUTES: int = 60
    DB_BACKUP_ENABLED: bool = True

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

    LOG_LEVEL: str = "DEBUG"

    FILE_CODE_PREFIX: str = "tgwenjian"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

        @classmethod
        def parse_env_var(cls, field_name: str, raw_val: str):
            if field_name in (
                "BACKUP_CHANNELS_GROUP_1",
                "BACKUP_CHANNELS_GROUP_2",
                "BACKUP_CHANNELS_GROUP_3",
            ):
                return json.loads(raw_val)
            return raw_val

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

    @staticmethod
    def get_config_default(key: str) -> str:
        defaults = {
            "storage_channel_id": "",
            "decoder_chat_id": "",
            "file_code_prefix": "tgwenjian",
            "force_join_channel_id": "",
            "force_join_link": "",
            "upload_bot_username": "",
            "decoder_bot_username": "",
            "sender_bot_username": "",
            "quota_default_free": "3",
            "quota_default_basic": "20",
            "quota_default_premium": "-1",
            "quota_external_free": "0",
            "quota_external_basic": "-1",
            "quota_external_premium": "-1",
            "r2_account_id": "",
            "r2_access_key": "",
            "r2_secret_key": "",
            "r2_bucket": "tgjiema-backup",
            "r2_endpoint": "",
            "db_backup_interval": "60",
            "db_backup_enabled": "true",
        }
        return defaults.get(key, "")


settings = Settings()
