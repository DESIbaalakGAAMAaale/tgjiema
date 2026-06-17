import json
from typing import List, Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── 环形冗余 v2 架构：5 个 Bot Token ──
    UPLOAD_BOT_TOKEN: str = ""
    DECODER_BOT_TOKEN: str = ""
    SENDER_BOT_TOKEN: str = ""
    ADMIN_BOT_TOKEN: str = ""
    MON_BOT_TOKEN: str = ""
    ADMIN_TELEGRAM_ID: int = 0

    MAIN_STORAGE_CHANNEL_ID: int = -1000000000000
    DECODER_BOT_CHAT_ID: int = 0
    MON_CHECK_INTERVAL: int = 60

    # ── 轮转参数（可在 .env 或管理员 Bot 运行时覆盖） ──
    ROTATION_ACTIVE_WINDOW_SIZE: int = 3       # 同一时刻活跃频道数
    ROTATION_FILES_PER_SLOT: int = 500         # 每槽位文件数上限
    ROTATION_TIME_PER_SLOT: int = 3600         # 每槽位最多使用时间（秒）

    # ── 账号频道配置（部署时在 .env 中填写，无需编辑 groups.yaml） ──
    # 5 个账号 × 9 频道 = 45 频道 = 15 组
    # 格式: ACCOUNT_N_NAME=账号名, ACCOUNT_N_CHANNELS=频道ID,频道ID,...
    ACCOUNT_1_NAME: str = ""
    ACCOUNT_1_CHANNELS: str = ""
    ACCOUNT_2_NAME: str = ""
    ACCOUNT_2_CHANNELS: str = ""
    ACCOUNT_3_NAME: str = ""
    ACCOUNT_3_CHANNELS: str = ""
    ACCOUNT_4_NAME: str = ""
    ACCOUNT_4_CHANNELS: str = ""
    ACCOUNT_5_NAME: str = ""
    ACCOUNT_5_CHANNELS: str = ""
    R100_CHANNEL: int = 0               # R100 兜底频道（不参与环形调度）

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

    R2_ACCOUNT_ID: str = ""
    R2_ACCESS_KEY_ID: str = ""
    R2_SECRET_ACCESS_KEY: str = ""
    R2_BUCKET_NAME: str = "tgjiema-backup"
    R2_ENDPOINT: Optional[str] = None

    DB_BACKUP_INTERVAL_MINUTES: int = 60
    DB_BACKUP_ENABLED: bool = True

    ADMIN_WEB_PORT: int = 8080
    ADMIN_WEB_HOST: str = "127.0.0.1"
    ADMIN_USERNAME: str = ""
    ADMIN_PASSWORD: str = ""

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

    # ── 上传选项默认值 ──
    DEFAULT_PROTECT_CONTENT: bool = False
    DEFAULT_FILE_TTL_DAYS: int = 0  # 0=永久有效

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    @property
    def STORAGE_CHANNEL_ID(self) -> int:
        return self.MAIN_STORAGE_CHANNEL_ID

    def get_accounts_config(self) -> dict:
        """从 .env 配置中解析账号频道配置。
        返回格式与 groups.yaml 兼容：
        {
            "accounts": [{"name": "账号1", "channels": [-1001, -1002, ...]}, ...],
            "r100": {"channel": -1009999, "fallback": []}
        }
        """
        accounts = []
        for i in range(1, 6):
            name = getattr(self, f"ACCOUNT_{i}_NAME", "")
            channels_str = getattr(self, f"ACCOUNT_{i}_CHANNELS", "")
            if not name or not channels_str:
                continue
            channels = []
            for ch in channels_str.split(","):
                ch = ch.strip()
                if ch:
                    try:
                        channels.append(int(ch))
                    except ValueError:
                        pass
            if channels:
                accounts.append({"name": name, "channels": channels})

        r100_ch = self.R100_CHANNEL if self.R100_CHANNEL != 0 else None

        return {
            "accounts": accounts,
            "r100": {"channel": r100_ch, "fallback": []},
        }

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