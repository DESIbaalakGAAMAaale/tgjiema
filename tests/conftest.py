"""pytest 配置与公共 fixture —— 批次三 P0 测试套件（P1-18）。

本文件在 import 任何 tgjiema 业务模块**之前**通过 ``sys.modules`` 注入桩模块，
屏蔽本机未安装/不可用的重型第三方依赖，使纯逻辑回归测试可在沙箱内真实跑通。

桩策略:
  * 重型第三方依赖（loguru / asyncpg / telethon* / telegram* / aiosqlite）——
    用 MagicMock 或最小桩模块替代，避免 import 期触发网络/DB/HTTP 副作用。
  * ``telegram.error`` 必须是**真实的异常类**（继承 Exception），因为 force_join
    等模块在 ``except (BadRequest, Forbidden)`` 中直接使用，MagicMock 不能作为
    异常捕获类型。
  * 项目内部仅作为依赖被引用、且不在单测范围内的模块（storage.r2 /
    storage.delivery_resolver / database.cache_store / config）注入可控的最小桩，
    避免导入真实实现带来的 SQLite/HTTP/Telethon/pydantic 副作用。
  * ``config`` 桩说明：项目 ``config/settings.py`` 使用 class-based ``Config`` 且
    ``extra = "warn"``，而所有 pydantic v2 的 core 仅接受 allow/forbid/ignore，
    导致该模块在任何 pydantic v2 下都无法 import（环境/源码不兼容）。为避免改动
    生产源码，这里注入一个与 ``Settings`` 字段对齐的测试桩 ``config``，保持项目
    源码零修改。pydantic / pydantic-settings 本身仍以真实包安装在 venv 中。
"""

import os
import sys
import types
from unittest.mock import MagicMock, AsyncMock


def _register(name: str, module: types.ModuleType) -> None:
    sys.modules[name] = module


class _AutoModule(types.ModuleType):
    """未知属性自动返回 MagicMock（并缓存），使 `from pkg import AnyName` 对任意名称都成立。"""

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        val = MagicMock()
        setattr(self, name, val)
        return val


# ────────────────────────────────────────────────────────────────────────────
# 1) telegram.error —— 真实异常类（force_join 在 except 子句使用）
# ────────────────────────────────────────────────────────────────────────────
_te = types.ModuleType("telegram.error")


class TelegramError(Exception):
    pass


class Unauthorized(TelegramError):
    pass


class BadRequest(TelegramError):
    pass


class Forbidden(TelegramError):
    pass


class NotFound(TelegramError):
    pass


class NetworkError(TelegramError):
    pass


class TimedOut(TelegramError):
    pass


class ConflictError(TelegramError):
    pass


class RetryAfter(TelegramError):
    def __init__(self, retry_after: float):
        self.retry_after = retry_after
        super().__init__(f"Retry after {retry_after} seconds")


class ChatMigrated(TelegramError):
    def __init__(self, new_chat_id: int):
        self.new_chat_id = new_chat_id
        super().__init__(f"Chat migrated to {new_chat_id}")


class MigrateToChat(TelegramError):
    pass


class InvalidToken(TelegramError):
    pass


_te.TelegramError = TelegramError
_te.Unauthorized = Unauthorized
_te.BadRequest = BadRequest
_te.Forbidden = Forbidden
_te.NotFound = NotFound
_te.NetworkError = NetworkError
_te.TimedOut = TimedOut
_te.ConflictError = ConflictError
_te.RetryAfter = RetryAfter
_te.ChatMigrated = ChatMigrated
_te.MigrateToChat = MigrateToChat
_te.InvalidToken = InvalidToken
_register("telegram.error", _te)

# ────────────────────────────────────────────────────────────────────────────
# 2) telegram / telegram.ext —— 自动模块；Bot 显式设为 AsyncMock 以便方法可 await
# ────────────────────────────────────────────────────────────────────────────
_tg = _AutoModule("telegram")
_tg.Bot = AsyncMock
_register("telegram", _tg)

_tge = _AutoModule("telegram.ext")
_register("telegram.ext", _tge)

# ────────────────────────────────────────────────────────────────────────────
# 3) telethon 系列 —— 自动模块（TelegramClient 等按需返回 MagicMock）
# ────────────────────────────────────────────────────────────────────────────
_th = _AutoModule("telethon")
_register("telethon", _th)

_th_tl_types = _AutoModule("telethon.tl.types")
_register("telethon.tl.types", _th_tl_types)

_th_tl_funcs = _AutoModule("telethon.tl.functions")
_register("telethon.tl.functions", _th_tl_funcs)

_th_err = types.ModuleType("telethon.errors")


class SessionPasswordNeededError(Exception):
    pass


class FloodWaitError(Exception):
    pass


_th_err.SessionPasswordNeededError = SessionPasswordNeededError
_th_err.FloodWaitError = FloodWaitError
_register("telethon.errors", _th_err)

# ────────────────────────────────────────────────────────────────────────────
# 5) 其它重型第三方依赖
# ────────────────────────────────────────────────────────────────────────────
_lg = types.ModuleType("loguru")
_lg.logger = MagicMock()
_register("loguru", _lg)

_register("asyncpg", MagicMock())


# aiosqlite —— 提供可 await 的异步上下文管理器，便于 factory_reset 的本地缓存清理块
class _FakeAiosqliteConn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *args, **kwargs):
        return None

    async def commit(self, *args, **kwargs):
        return None

    async def close(self, *args, **kwargs):
        return None


_aiosqlite = types.ModuleType("aiosqlite")


def _aiosqlite_connect(*args, **kwargs):
    return _FakeAiosqliteConn()


_aiosqlite.connect = _aiosqlite_connect
_register("aiosqlite", _aiosqlite)

# ────────────────────────────────────────────────────────────────────────────
# 6) 项目内部依赖桩（仅作为依赖被引用、不在单测范围内）
# ────────────────────────────────────────────────────────────────────────────
_storage_r2 = _AutoModule("storage.r2")
_storage_r2._r2 = MagicMock()
_register("storage.r2", _storage_r2)

_storage_dr = _AutoModule("storage.delivery_resolver")
_storage_dr.invalidate_cell_cache = MagicMock()
_register("storage.delivery_resolver", _storage_dr)


# database.cache_store —— 项目内部 SQLite 持久化层，单测中不应真实写磁盘。
# 用 MagicMock 兜底：任何 `from database.cache_store import X` 都成立，具体行为
# 在各测试用例内通过 monkeypatch 控制（如 invalidate_code_entry 测试替换 get_cache_store）。
# 注意：MagicMock 不会因缓存缺失而触发 AttributeError，避免 import 期漏配字段导致失败。
_database_cache_store = MagicMock()
_register("database.cache_store", _database_cache_store)


# config —— 测试桩（见文件头说明）。字段与 config/settings.py 的 Settings 对齐。
class _FakeSettings:
    # 5 个 Bot Token
    UPLOAD_BOT_TOKEN = "test-upload-token"
    DECODER_BOT_TOKEN = "test-decoder-token"
    SENDER_BOT_TOKEN = "test-sender-token"
    MON_BOT_TOKEN = "test-mon-token"
    ADMIN_BOT_TOKEN = "test-admin-token"
    ADMIN_TELEGRAM_ID = 0

    MON_CHECK_INTERVAL = 60

    # 轮转参数
    ROTATION_ACTIVE_WINDOW_SIZE = 3
    ROTATION_FILES_PER_SLOT = 500
    ROTATION_TIME_PER_SLOT = 3600

    # 数据保留
    DATA_RETENTION_DAYS = 7
    CRDB_CLEANUP_CRON_HOURS = 6
    CRDB_CLEANUP_BATCH_SIZE = 5000

    # 账号频道
    ACCOUNT_1_NAME = ""
    ACCOUNT_1_CHANNELS = ""
    ACCOUNT_2_NAME = ""
    ACCOUNT_2_CHANNELS = ""
    ACCOUNT_3_NAME = ""
    ACCOUNT_3_CHANNELS = ""
    ACCOUNT_4_NAME = ""
    ACCOUNT_4_CHANNELS = ""
    ACCOUNT_5_NAME = ""
    ACCOUNT_5_CHANNELS = ""
    R100_CHANNEL = 0

    COCKROACHDB_URL = "postgresql://test@localhost:26257/test"

    FORCE_JOIN_CHANNEL_ID = 0
    FORCE_JOIN_CHANNEL_LINK = ""

    UPLOAD_BOT_USERNAME = ""
    DECODER_BOT_USERNAME = ""
    SENDER_BOT_USERNAME = ""

    RELAY_ENCRYPTION_KEY = "0" * 43 + "="
    RELAY_ACCOUNT_IDS = ""
    COLLECTOR_ACCOUNT_IDS = ""
    RELAY_API_ID = 0
    RELAY_API_HASH = ""

    # R2
    R2_ACCOUNT_ID = "test-r2-account"
    R2_ACCESS_KEY_ID = "test-r2-access-key"
    R2_SECRET_ACCESS_KEY = "test-r2-secret"
    R2_BUCKET_NAME = "tgjiema-backup"
    R2_ENDPOINT = ""

    # 缓存
    CACHE_USER_MAX_SIZE = 1000
    CACHE_USER_TTL = 10800
    CACHE_FILE_MAX_SIZE = 1000
    CACHE_FILE_TTL = 300
    CACHE_CONFIG_MAX_SIZE = 100
    CACHE_CONFIG_TTL = 600
    CACHE_NEGATIVE_TTL = 60
    CACHE_REQUEST_COUNT_FLUSH = 900
    CACHE_DECODE_LOG_FLUSH = 3600
    CACHE_STORE_CLEANUP_DAYS = 30

    # 速率限制
    RATE_LIMIT_BASE_DELAY = 1.0
    RATE_LIMIT_GLOBAL_PER_SECOND = 10
    RATE_LIMIT_MAX_DELAY = 30.0
    RATE_LIMIT_PER_USER_PER_MINUTE = 20
    RATE_LIMIT_THRESHOLD_HIGH = 0.8
    RATE_LIMIT_THRESHOLD_LOW = 0.5

    # 频道健康
    CHANNEL_FAILURE_THRESHOLD = 3
    CHANNEL_FAILURE_WINDOW = 300

    # 配额
    FREE_DAILY_QUOTA = 3
    FREE_EXTERNAL_DAILY_QUOTA = 0
    BASIC_DAILY_QUOTA = 20
    BASIC_EXTERNAL_DAILY_QUOTA = -1
    PREMIUM_DAILY_QUOTA = -1
    PREMIUM_EXTERNAL_DAILY_QUOTA = -1

    # 中继权重
    RELAY_WEIGHT_AVG_WAIT = 0.4
    RELAY_WEIGHT_TODAY_REQ = 0.4
    RELAY_WEIGHT_GAP = 0.2
    RELAY_NORM_AVG_WAIT = 1000.0
    RELAY_NORM_TODAY_REQ = 50000.0
    RELAY_NORM_GAP = 3600.0

    # DB 连接池
    CRDB_POOL_MAX_SIZE = 10
    CRDB_POOL_MIN_SIZE = 1

    # 备份
    DB_BACKUP_ENABLED = False
    DB_BACKUP_INTERVAL_MINUTES = 360

    # Admin Web
    ADMIN_WEB_PORT = 8080
    ADMIN_WEB_HOST = "127.0.0.1"
    ADMIN_USERNAME = "admin"
    ADMIN_PASSWORD = "test-password-123"
    ADMIN_LOGIN_WINDOW = 300
    ADMIN_LOGIN_MAX_FAIL = 5
    ADMIN_COUNT_CACHE_TTL = 60
    ADMIN_SEARCH_MAX_LENGTH = 50
    ADMIN_PAGE_SIZE = 20
    ADMIN_FILES_PAGE_SIZE = 50

    # 杂项
    FILE_CODE_PREFIX = "tgwenjian"
    DEFAULT_FILE_TTL_DAYS = 7
    DEFAULT_PROTECT_CONTENT = False
    EXTERNAL_MEDIA_GROUP_TTL = 600
    MEDIA_GROUP_BUFFER_WAIT = 2.0
    PAGE_SIZE = 20
    PENDING_TTL = 3600
    RESTART_COOLDOWN = 30
    SEND_CONCURRENCY = 5
    LOG_LEVEL = "INFO"

    def get_config_default(self, key: str, default=None):
        defaults = {
            "file_code_prefix": "tgwenjian",
            "upload_bot_username": "",
            "decoder_bot_username": "",
            "sender_bot_username": "",
            "quota_default_free": "3",
            "quota_default_basic": "20",
            "quota_default_premium": "-1",
            "quota_external_free": "0",
            "quota_external_basic": "-1",
            "quota_external_premium": "-1",
        }
        return defaults.get(key, default if default is not None else "")


_fake_settings = _FakeSettings()

_config_settings = types.ModuleType("config.settings")
_config_settings.settings = _fake_settings
_config_settings.Settings = _FakeSettings  # 部分代码可能 from config.settings import Settings
_register("config.settings", _config_settings)

_config = types.ModuleType("config")
_config.settings = _fake_settings
_config.generate_topology = types.ModuleType("config.generate_topology")
_config.generate_topology.generate = MagicMock()
_register("config", _config)
_register("config.generate_topology", _config.generate_topology)

# ────────────────────────────────────────────────────────────────────────────
# 7) 将项目根目录加入 sys.path，保证 `import config` / `import database` 可用
# ────────────────────────────────────────────────────────────────────────────
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
