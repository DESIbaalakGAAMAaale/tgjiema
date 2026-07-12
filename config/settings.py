import base64
import re
from typing import Optional

from loguru import logger
from pydantic import model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── R35 P0-1: 服务角色(用于分服务校验 secrets) ──
    # 取值: "up" / "idx" / "dsp" / "mon" / "admin_bot" / "admin" / "db_writer" / "db_backup" / ""
    # 空字符串(默认)→ 校验全部字段(向后兼容老部署模式)
    # 部署脚本通过 systemd Environment=SERVICE_ROLE=up 注入
    SERVICE_ROLE: str = ""

    # ── 环形冗余 v2 架构：5 个 Bot Token（file_bot 已独立部署至 CF Workers）──
    UPLOAD_BOT_TOKEN: str = ""
    DECODER_BOT_TOKEN: str = ""
    SENDER_BOT_TOKEN: str = ""
    ADMIN_BOT_TOKEN: str = ""
    MON_BOT_TOKEN: str = ""
    ADMIN_TELEGRAM_ID: int = 0

    MON_CHECK_INTERVAL: int = 60

    # ── 轮转参数（可在 .env 或管理员 Bot 运行时覆盖） ──
    ROTATION_ACTIVE_WINDOW_SIZE: int = 3       # 同一时刻活跃频道数
    ROTATION_FILES_PER_SLOT: int = 500         # 每槽位文件数上限
    ROTATION_TIME_PER_SLOT: int = 3600         # 每槽位最多使用时间（秒）

    # ── 数据保留（替代废弃的 CRDB TTL job，0 RU 起） ──
    DATA_RETENTION_DAYS: int = 7               # decode_logs / jobs 保留天数
    CRDB_CLEANUP_CRON_HOURS: int = 6           # 清理周期（小时）
    CRDB_CLEANUP_BATCH_SIZE: int = 5000        # 单批删除条数上限

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

    FORCE_JOIN_CHANNEL_ID: int = 0         # 强制关注频道ID（admin_bot 可热更新）
    FORCE_JOIN_CHANNEL_LINK: str = ""      # 强制关注频道链接（兼作官方频道地址）

    UPLOAD_BOT_USERNAME: str = ""
    DECODER_BOT_USERNAME: str = ""
    SENDER_BOT_USERNAME: str = ""

    RELAY_ENCRYPTION_KEY: str = ""

    # ─── 中继账号白名单：逗号分隔的 Telegram 用户 ID，仅这些账号可向 Up Bot 发送 EXTERNAL_RELAY 文件 ───
    # 支持热修改：admin_bot /relay_whitelist 命令可动态增删，DB 配置优先于环境变量
    RELAY_ACCOUNT_IDS: str = ""

    # ─── 采集器账号白名单：逗号分隔的 Telegram 用户 ID，仅这些账号可向主系统推送采集结果 ───
    # 支持热修改：admin_bot /collector_whitelist 命令可动态增删，DB 配置优先于环境变量
    COLLECTOR_ACCOUNT_IDS: str = ""

    # ─── Telegram Relay API 密钥（添加中继账号时从此处读取）───
    RELAY_API_ID: int = 0
    RELAY_API_HASH: str = ""

    # ─── P1-9: 允许的外部解码器 bot 白名单(逗号分隔的用户名,不带@) ───
    # 为空时:
    #   - ALLOWED_DECODER_BOTS_FAIL_CLOSED=False (默认, 兼容现有部署): 允许所有 bot
    #   - ALLOWED_DECODER_BOTS_FAIL_CLOSED=True (商用建议): 拒绝所有外部解码器
    # 配置后仅允许白名单内 bot
    ALLOWED_DECODER_BOTS: str = ""
    # ─── P2-1: 白名单 fail-closed 开关 ───
    # 默认 False 保持向后兼容; 商用部署应设为 True, 空白名单时拒绝所有外部解码器
    ALLOWED_DECODER_BOTS_FAIL_CLOSED: bool = False

    R2_ACCOUNT_ID: str = ""
    R2_ACCESS_KEY_ID: str = ""
    R2_SECRET_ACCESS_KEY: str = ""
    R2_BUCKET_NAME: str = "tgjiema-backup"
    R2_ENDPOINT: Optional[str] = None

    # PRE-07: 默认关闭备份（避免新部署未配置 R2 凭证时静默失败 + 持续消耗 RU）。
    # 启用备份需显式设置 DB_BACKUP_ENABLED=true 并配置 R2 凭证。
    # 间隔从 14400(10天) 改为 360(6小时)，与原 get_config_default 一致，更符合备份预期。
    DB_BACKUP_INTERVAL_MINUTES: int = 360
    DB_BACKUP_ENABLED: bool = False

    ADMIN_WEB_PORT: int = 8080
    ADMIN_WEB_HOST: str = "127.0.0.1"
    ADMIN_USERNAME: str = ""
    ADMIN_PASSWORD: str = ""

    # ─── Dsp Bot 频道降级阈值（Mon 不可用时的兜底机制）────────
    CHANNEL_FAILURE_THRESHOLD: int = 3   # 60 秒内失败 N 次触发降级
    CHANNEL_FAILURE_WINDOW: int = 60     # 统计窗口（秒）

    # ─── Relay Pool 负载均衡权重配置（relay_pool.py 动态读取） ──
    RELAY_WEIGHT_AVG_WAIT: float = 0.4   # avg_wait_ms 权重
    RELAY_WEIGHT_TODAY_REQ: float = 0.4  # today_requests 权重
    RELAY_WEIGHT_GAP: float = 0.2        # last_request_gap 权重
    RELAY_NORM_AVG_WAIT: float = 1000.0  # avg_wait_ms 归一化因子
    RELAY_NORM_TODAY_REQ: float = 50000.0  # today_requests 归一化因子
    RELAY_NORM_GAP: float = 3600.0       # gap 归一化因子

    # C3: 中继账号安全池最小数量,低于此值告警(mon_bot 通过 getattr 读取)
    RELAY_SAFE_POOL_SIZE: int = 2  # C3: 中继账号安全池最小数量,低于此值告警

    # ─── C1: Redis Stream 事件驱动(dsp_bot 替代轮询)───
    # REDIS_URL 为空时禁用 Redis,使用 SQLite 轮询(本地开发零依赖)
    REDIS_URL: str = ""
    REDIS_STREAM_MAXLEN: int = 10000  # Stream 最大长度,防止无限增长

    # ─── 方案B v2: Redis Streams + Writer 进程(消除 SQLite 锁冲突,零数据丢失)───
    # WRITER_MODE=redis: 写操作入 Redis Stream,db_writer 进程串行落盘 SQLite
    # WRITER_MODE=sqlite: 降级模式,直写 SQLite(旧逻辑,兼容本地开发)
    # R33修复: 从 Redis List BRPOP 改为 Streams XREADGROUP,实现可靠消费
    WRITER_MODE: str = "redis"
    # R33: Stream key(替代 List key,支持 Consumer Group)
    WRITER_STREAM_KEY: str = "tgjiema:writer:stream"
    # R33: Consumer Group 名(db_writer 通过此 group 消费 Stream)
    WRITER_CONSUMER_GROUP: str = "tgjiema-writer-group"
    # R33: Consumer 名(区分不同 db_writer 实例)
    WRITER_CONSUMER_NAME: str = "db_writer"
    # R33: pending 消息回收阈值(ms),超过此时间的 pending 消息会被 XAUTOCLAIM 回收
    WRITER_RECLAIM_IDLE_MS: int = 30000
    # Writer 单次 XREADGROUP 批量大小(一次取多条消息减少往返)
    WRITER_BATCH_SIZE: int = 10
    # Writer 队列积压告警阈值(mon_bot 监控 pending 数)
    WRITER_QUEUE_ALERT_THRESHOLD: int = 1000
    # 读缓存 TTL(秒),按数据类型分级
    WRITER_CACHE_TTL_QUOTA: int = 5        # 用户配额(高频变更,短TTL)
    WRITER_CACHE_TTL_FILE_RECORD: int = 30 # 文件记录(中频变更)
    WRITER_CACHE_TTL_CODE: int = 30        # 验证码(中频变更)
    WRITER_CACHE_TTL_USER: int = 30        # 用户记录(中频变更)
    WRITER_CACHE_TTL_CELLS: int = 10       # 全量cells(中频变更)
    WRITER_CACHE_TTL_BOT_HB: int = 5       # Bot心跳(高频变更,短TTL)
    WRITER_CACHE_TTL_KV: int = 60          # KV存储(低频变更,长TTL)
    # R33: 死信队列 Stream key(替代 List,支持重试闭环)
    WRITER_DEAD_STREAM_KEY: str = "tgjiema:writer:dead"
    # R33: 死信最大重试次数(超过后永久死信,需人工排查)
    WRITER_DEAD_MAX_ATTEMPTS: int = 3
    # R33: 死信重试延迟(秒,失败后延迟 XADD 回主队列)
    WRITER_DEAD_RETRY_DELAY: int = 60
    # M0 收尾: writer_inbox 清理保留期(小时)。
    # R35 P1-3 修复: 从 168(7天)调整为 2160(90天),确保覆盖消息最长生命周期。
    # 依据: Stream 安全窗口 24h + DLQ 最大重试 3 次 × 60s 退避 + 停机维护窗口 7 天
    #       + 人工处理窗口 30 天 ≈ 90 天。
    # 必须远大于 WRITER_RECLAIM_IDLE_MS(30秒)的回收阈值,
    # 确保崩溃恢复 + 长停机 + 人工排查窗口内仍有 inbox 记录可查(幂等校验)。
    WRITER_INBOX_RETENTION_HOURS: int = 2160
    # db_writer systemd 服务名(mon_bot 监控用,可配置以支持不同部署前缀)
    DB_WRITER_SERVICE_NAME: str = "tgjiema-db_writer"

    # ─── M1 业务闭环配置 ───────────────────────────────────────
    # upload_sessions 上传会话状态机
    UPLOAD_SESSION_TTL: int = 3600          # 上传会话超时(秒),超时未推进则 EXPIRED
    UPLOAD_SESSION_LEASE_SECONDS: int = 300 # 上传会话租约时长(秒)
    # upload_outbox 事务发件箱
    UPLOAD_OUTBOX_MAX_ATTEMPTS: int = 5     # 发件箱最大重试次数
    UPLOAD_OUTBOX_RETRY_DELAY: int = 60     # 发件箱重试延迟(秒)
    # quota_ledger 配额变更流水
    QUOTA_LEDGER_RETENTION_DAYS: int = 90   # 配额流水保留天数
    # delivery_receipts 投递回执
    DELIVERY_RECEIPT_RETENTION_DAYS: int = 30  # 投递回执保留天数
    # replication_tasks 副本复制任务
    REPLICATION_TASK_MAX_ATTEMPTS: int = 3  # 副本复制最大重试次数
    REPLICATION_TASK_RETRY_DELAY: int = 60 # 副本复制重试延迟(秒)
    REPLICATION_BATCH_SIZE: int = 30        # 副本复制批量大小

    # ─── 配额同步间隔 ──────────────────────────────────────────
    QUOTA_SYNC_INTERVAL: int = 300       # 秒(5分钟),减少 CRDB RU 消耗

    # ─── 进程管理 ──────────────────────────────────────────────
    RESTART_COOLDOWN: int = 600          # 子进程崩溃冷却期（秒）
    TOPOLOGY_SEED_RETRIES: int = 3       # 拓扑初始化重试次数

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

    # ── 上传选项默认值 ──
    DEFAULT_PROTECT_CONTENT: bool = False
    DEFAULT_FILE_TTL_DAYS: int = 0  # 0=永久有效

    # ── 可调参数（原硬编码魔法数字） ──
    MEDIA_GROUP_BUFFER_WAIT: float = 3.0       # 媒体组缓冲等待时间（秒）
    PENDING_TTL: int = 300                      # 外部码等待超时（秒）
    SEND_CONCURRENCY: int = 25                  # Dsp 发送并发上限
    PAGE_SIZE: int = 10                         # 分页大小
    EXTERNAL_MEDIA_GROUP_TTL: int = 300         # 外部媒体组 TTL（秒）
    CACHE_STORE_CLEANUP_DAYS: int = 30          # 本地缓存清理天数
    MAX_RESTART_COUNT: int = 3                  # 5分钟内最大重启次数
    MAX_RESTART_WINDOW: int = 300               # 重启计数窗口（秒）

    # ── 动态限速参数 ──
    RATE_LIMIT_BASE_DELAY: float = 0.2          # 空闲时基础延迟（秒）
    RATE_LIMIT_MAX_DELAY: float = 3.0           # 高峰期最大延迟（秒）
    RATE_LIMIT_THRESHOLD_LOW: int = 10          # 低负载阈值（jobs 数量 < 此值用基础延迟）
    RATE_LIMIT_THRESHOLD_HIGH: int = 30         # 高负载阈值（jobs 数量 > 此值用最大延迟）

    # ── 数据库连接池（自动适配，不超过 CRDB 上限）──
    CRDB_POOL_MIN_SIZE: int = 1                 # 最小连接数
    CRDB_POOL_MAX_SIZE: int = 5                 # 最大连接数（7 进程 × 此值 ≤ CRDB 上限，建议 ≤20）

    # ── 缓存参数 ──
    CACHE_USER_MAX_SIZE: int = 1000             # 用户缓存最大条目
    CACHE_USER_TTL: int = 10800                 # 用户缓存 TTL（秒）
    CACHE_FILE_MAX_SIZE: int = 1000             # 文件缓存最大条目
    CACHE_FILE_TTL: int = 300                   # 文件缓存 TTL（秒）
    CACHE_CONFIG_MAX_SIZE: int = 100            # 配置缓存最大条目
    CACHE_CONFIG_TTL: int = 600                 # 配置缓存 TTL（秒）
    CACHE_NEGATIVE_TTL: int = 60                # 负面缓存 TTL（秒）
    CACHE_REQUEST_COUNT_FLUSH: int = 900        # 请求计数刷新间隔（秒）
    CACHE_DECODE_LOG_FLUSH: int = 3600          # 解码日志刷新间隔（秒）

    # ── Admin Web 参数 ──
    ADMIN_LOGIN_WINDOW: int = 300               # 登录失败计数窗口（秒）
    ADMIN_LOGIN_MAX_FAIL: int = 5               # 窗口内最大失败次数
    ADMIN_COUNT_CACHE_TTL: int = 60             # count 缓存 TTL（秒）
    ADMIN_SEARCH_MAX_LENGTH: int = 50           # 搜索输入最大长度
    ADMIN_PAGE_SIZE: int = 20                   # 默认分页大小
    ADMIN_FILES_PAGE_SIZE: int = 50             # 文件列表分页大小
    CSRF_COOKIE_SECURE: bool = False            # CSRF Cookie Secure 标志；部署 TLS 后设为 1/true

    # ─── 管理员 Bot 配置键名映射 ──────────────────────────
    @property
    def db_backup_interval(self) -> int:
        return self.DB_BACKUP_INTERVAL_MINUTES

    @db_backup_interval.setter
    def db_backup_interval(self, value: int):
        self.DB_BACKUP_INTERVAL_MINUTES = int(value)

    @property
    def db_backup_enabled(self) -> bool:
        return self.DB_BACKUP_ENABLED

    @db_backup_enabled.setter
    def db_backup_enabled(self, value):
        if isinstance(value, str):
            value = value.lower() in ("true", "1", "on", "yes")
        self.DB_BACKUP_ENABLED = bool(value)

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"

    @model_validator(mode='before')
    @classmethod
    def strip_inline_comments(cls, data: dict) -> dict:
        """移除 .env 文件中行内注释（# 及之后的内容），防止 pydantic 解析失败。

        P3: 改进正则,要求 # 前有空格且 # 后有空格或行尾,
        避免截断 URL fragment(# 无空格)和密码中的 # 字符。
        """
        if not isinstance(data, dict):
            return data
        stripped = {}
        for key, value in data.items():
            if isinstance(value, str):
                # 只匹配 " # comment" 格式(# 前有空格, # 后有空格或行尾)
                # 不匹配 "value#fragment"(URL fragment)或 "pass#word"(密码)
                value = re.sub(r'\s+#(?:\s.*$|$)', '', value).rstrip()
            stripped[key] = value
        return stripped

    @model_validator(mode='after')
    def validate_required_fields(self):
        """验证必填字段,根据 SERVICE_ROLE 只校验当前服务所需的字段。

        R35 P0-1 修复: Secrets 真隔离后,每个服务只加载自己的 secrets,
        不再强制所有服务都拥有全部 5 个 Bot Token + CRDB URL + Admin 凭证。

        SERVICE_ROLE 为空时(向后兼容)校验全部字段(老部署模式)。
        """
        role = self.SERVICE_ROLE

        # 向后兼容: SERVICE_ROLE 为空时,校验全部字段(老部署模式)
        if not role:
            return self._validate_all_fields()

        # 按角色校验
        role_validators = {
            "up": self._validate_up_fields,
            "idx": self._validate_idx_fields,
            "dsp": self._validate_dsp_fields,
            "mon": self._validate_mon_fields,
            "admin_bot": self._validate_admin_bot_fields,
            "admin": self._validate_admin_fields,
            "db_writer": self._validate_writer_fields,
            "db_backup": self._validate_backup_fields,
        }
        validator = role_validators.get(role)
        if validator:
            validator()
        # 未知 role 不校验(只记录警告)
        elif role:
            logger.warning(f"[Settings] 未知 SERVICE_ROLE={role},跳过必填字段校验")

        return self

    def _validate_all_fields(self):
        """向后兼容: 校验全部必填字段(老部署模式,SERVICE_ROLE 为空)"""
        missing = []
        if not self.UPLOAD_BOT_TOKEN: missing.append('UPLOAD_BOT_TOKEN')
        if not self.DECODER_BOT_TOKEN: missing.append('DECODER_BOT_TOKEN')
        if not self.SENDER_BOT_TOKEN: missing.append('SENDER_BOT_TOKEN')
        if not self.MON_BOT_TOKEN: missing.append('MON_BOT_TOKEN')
        if not self.ADMIN_BOT_TOKEN: missing.append('ADMIN_BOT_TOKEN')
        if not self.COCKROACHDB_URL: missing.append('COCKROACHDB_URL')
        if missing:
            raise ValueError(f"[Settings] 以下必填环境变量未配置: {', '.join(missing)}。请检查 .env 文件。")
        self._validate_relay_key()
        self._validate_admin_credentials()
        return self

    def _validate_up_fields(self):
        """Up Bot 必填字段"""
        if not self.UPLOAD_BOT_TOKEN:
            raise ValueError("[Settings][up] UPLOAD_BOT_TOKEN 未配置")
        if not self.ACCOUNT_1_CHANNELS:
            raise ValueError("[Settings][up] ACCOUNT_1_CHANNELS 未配置(至少需要 1 个账号频道)")
        # Up Bot 处理中继文件,需要 RELAY_ENCRYPTION_KEY
        self._validate_relay_key()
        # Up 不需要 CRDB URL、其他 Bot Token、Admin 凭证

    def _validate_idx_fields(self):
        """Idx Bot 必填字段"""
        if not self.DECODER_BOT_TOKEN:
            raise ValueError("[Settings][idx] DECODER_BOT_TOKEN 未配置")
        # Idx 需要 CRDB URL(读写 file_records/codes/jobs)
        if not self.COCKROACHDB_URL:
            raise ValueError("[Settings][idx] COCKROACHDB_URL 未配置")
        # Idx Bot 使用 relay_db 加解密 API_HASH,需要 RELAY_ENCRYPTION_KEY
        self._validate_relay_key()

    def _validate_dsp_fields(self):
        """Dsp Bot 必填字段"""
        if not self.SENDER_BOT_TOKEN:
            raise ValueError("[Settings][dsp] SENDER_BOT_TOKEN 未配置")

    def _validate_mon_fields(self):
        """Mon Bot 必填字段"""
        if not self.MON_BOT_TOKEN:
            raise ValueError("[Settings][mon] MON_BOT_TOKEN 未配置")

    def _validate_admin_bot_fields(self):
        """Admin Bot 必填字段"""
        if not self.ADMIN_BOT_TOKEN:
            raise ValueError("[Settings][admin_bot] ADMIN_BOT_TOKEN 未配置")
        if not self.ADMIN_TELEGRAM_ID:
            raise ValueError("[Settings][admin_bot] ADMIN_TELEGRAM_ID 未配置")

    def _validate_admin_fields(self):
        """Admin Web 必填字段"""
        self._validate_admin_credentials()

    def _validate_writer_fields(self):
        """DBWriter 必填字段 — 设计为无 secrets,只需 Redis"""
        if self.WRITER_MODE == "redis" and not self.REDIS_URL:
            raise ValueError("[Settings][db_writer] WRITER_MODE=redis 但 REDIS_URL 未配置")
        # db_writer 不需要任何 Bot Token、CRDB URL、Admin 凭证

    def _validate_backup_fields(self):
        """DB Backup 必填字段"""
        if not self.COCKROACHDB_URL:
            raise ValueError("[Settings][db_backup] COCKROACHDB_URL 未配置")
        if self.DB_BACKUP_ENABLED:
            if not self.R2_ACCESS_KEY_ID:
                raise ValueError("[Settings][db_backup] DB_BACKUP_ENABLED=true 但 R2_ACCESS_KEY_ID 未配置")
            if not self.R2_SECRET_ACCESS_KEY:
                raise ValueError("[Settings][db_backup] DB_BACKUP_ENABLED=true 但 R2_SECRET_ACCESS_KEY 未配置")
        # db_backup 需要 CRDB URL 和 R2 凭证,但不需要 Bot Token

    def _validate_relay_key(self):
        """校验 RELAY_ENCRYPTION_KEY(仅 up/idx/dsp/mon 需要)"""
        if not self.RELAY_ENCRYPTION_KEY:
            raise ValueError(
                "[Settings] RELAY_ENCRYPTION_KEY 未配置。"
                "请运行: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )
        try:
            decoded = base64.urlsafe_b64decode(self.RELAY_ENCRYPTION_KEY.encode())
            if len(decoded) != 32:
                raise ValueError(f"解码后长度 {len(decoded)} != 32")
        except Exception as e:
            raise ValueError(
                f"[Settings] RELAY_ENCRYPTION_KEY 不是合法的 Fernet 密钥({e})。"
                "请运行: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )

    def _validate_admin_credentials(self):
        """校验 Admin Web 凭证(仅 admin/admin_bot 需要)"""
        if not self.ADMIN_USERNAME:
            raise ValueError("[Settings] ADMIN_USERNAME 未配置")
        if not self.ADMIN_PASSWORD or self.ADMIN_PASSWORD == "CHANGE_ME_NOW":
            raise ValueError("[Settings] ADMIN_PASSWORD 未配置或仍为默认值 'CHANGE_ME_NOW'")
        if self.ADMIN_WEB_HOST != "127.0.0.1" and len(self.ADMIN_PASSWORD) < 12:
            raise ValueError("[Settings] ADMIN_WEB_HOST 非本地回环时,ADMIN_PASSWORD 长度必须 >= 12")

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
            name = getattr(self, f"ACCOUNT_{i}_NAME", "") or f"账号{i}"
            channels_str = getattr(self, f"ACCOUNT_{i}_CHANNELS", "")
            if not channels_str:
                continue
            channels = []
            for ch in channels_str.split(","):
                ch = ch.strip()
                if ch:
                    try:
                        channels.append(int(ch))
                    except ValueError:
                        logger.warning(f"[Settings] 无效的频道ID(已跳过): '{ch}'")
            if channels:
                accounts.append({"name": name, "channels": channels})

        r100_ch = self.R100_CHANNEL if self.R100_CHANNEL != 0 else None

        # P3: 前置校验,accounts 为空时记录 warning 提示运维
        if not accounts:
            logger.warning(
                "[Settings] get_accounts_config 返回空账号列表,"
                "请检查 .env 中 ACCOUNT_*_CHANNELS 是否已配置"
            )

        return {
            "accounts": accounts,
            "r100": {"channel": r100_ch, "fallback": []},
        }

    @staticmethod
    def get_config_default(key: str) -> str:
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
            "r2_account_id": "",
            "r2_access_key": "",
            "r2_secret_key": "",
            "r2_bucket": "tgjiema-backup",
            "r2_endpoint": "",
            # PRE-07: 与 DB_BACKUP_* 新默认值对齐
            "db_backup_interval": "360",
            "db_backup_enabled": "false",
        }
        return defaults.get(key, "")


settings = Settings()
