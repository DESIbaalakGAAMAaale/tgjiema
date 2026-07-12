"""M2 控制面收敛 · 动态配置注册表

为 config/settings.py 中的每个配置项登记元数据,建立 .env 与 DB 动态配置之间的
优先级矩阵。每个键标记:
  - 分类 (ConfigCategory): 所属子系统
  - 重载策略 (ReloadPolicy): hot_reload / restart / deploy / immutable
  - 敏感级别 (SensitivityLevel): public / internal / secret / critical
  - 影响服务 (services): 修改后哪些进程会受影响
  - 验证规则 (validation_regex / min_value / max_value): 用于运行时校验变更

设计要点:
  - 仅新增模块,不修改 config/settings.py
  - 使用 @dataclass + str Enum,便于 JSON 序列化
  - 默认实例 `config_registry` 在导入时即完成所有注册
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ConfigCategory(str, Enum):
    """配置分类"""
    DATABASE = "database"
    REDIS = "redis"
    TELEGRAM = "telegram"
    WRITER = "writer"
    QUOTA = "quota"
    RELAY = "relay"
    ROTATION = "rotation"
    BACKUP = "backup"
    MONITOR = "monitor"
    M1_BUSINESS = "m1_business"
    M2_CONTROL = "m2_control"
    SECURITY = "security"


class ReloadPolicy(str, Enum):
    """重载策略"""
    HOT_RELOAD = "hot_reload"        # 可通过 admin_bot 命令热更新,无需重启
    RESTART_REQUIRED = "restart"      # 修改后需重启服务生效
    DEPLOY_REQUIRED = "deploy"        # 需要重新部署(如 systemd unit 变更)
    IMMUTABLE = "immutable"          # 不可修改(如已存储的数据结构)


class SensitivityLevel(str, Enum):
    """敏感级别"""
    PUBLIC = "public"                # 可公开(如 Bot Username)
    INTERNAL = "internal"            # 内部配置(如限流参数)
    SECRET = "secret"                # 敏感信息(如 Token, Password)
    CRITICAL = "critical"            # 关键凭证(如 API Hash, Encryption Key)


@dataclass
class ConfigMetadata:
    """配置项元数据"""
    key: str
    category: ConfigCategory
    reload_policy: ReloadPolicy
    sensitivity: SensitivityLevel
    description: str
    default_value: str = ""
    env_var: str = ""                # 对应的 .env 变量名
    services: Optional[list[str]] = None  # 影响的服务列表(如 ['up_bot', 'idx_bot'])
    validation_regex: str = ""       # 值验证正则(可选)
    min_value: int | float | None = None  # 数值最小值(可选)
    max_value: int | float | None = None  # 数值最大值(可选)


class ConfigRegistry:
    """动态配置注册表

    记录所有配置项的元数据,支持:
    - 查询某配置项的重载策略
    - 查询某服务的所有配置项
    - 查询所有 secret 配置项
    - 验证配置变更是否合法
    """

    def __init__(self):
        self._registry: dict[str, ConfigMetadata] = {}
        self._init_defaults()

    def register(self, metadata: ConfigMetadata):
        """注册一个配置项"""
        self._registry[metadata.key] = metadata

    def get(self, key: str) -> ConfigMetadata | None:
        """查询配置项元数据"""
        return self._registry.get(key)

    def get_by_category(self, category: ConfigCategory) -> list[ConfigMetadata]:
        """按分类查询"""
        return [m for m in self._registry.values() if m.category == category]

    def get_by_service(self, service: str) -> list[ConfigMetadata]:
        """按影响服务查询"""
        return [m for m in self._registry.values() if m.services and service in m.services]

    def get_secrets(self) -> list[ConfigMetadata]:
        """查询所有敏感配置项"""
        return [m for m in self._registry.values()
                if m.sensitivity in (SensitivityLevel.SECRET, SensitivityLevel.CRITICAL)]

    def get_hot_reloadable(self) -> list[ConfigMetadata]:
        """查询所有可热更新的配置项"""
        return [m for m in self._registry.values()
                if m.reload_policy == ReloadPolicy.HOT_RELOAD]

    def get_restart_required(self) -> list[ConfigMetadata]:
        """查询所有需要重启的配置项"""
        return [m for m in self._registry.values()
                if m.reload_policy == ReloadPolicy.RESTART_REQUIRED]

    def get_deploy_required(self) -> list[ConfigMetadata]:
        """查询所有需要重新部署的配置项"""
        return [m for m in self._registry.values()
                if m.reload_policy == ReloadPolicy.DEPLOY_REQUIRED]

    def validate_change(self, key: str, new_value: str) -> tuple[bool, str]:
        """验证配置变更是否合法

        返回 (is_valid, error_message)
        """
        meta = self._registry.get(key)
        if meta is None:
            return False, f"未知配置项: {key}"
        if meta.reload_policy == ReloadPolicy.IMMUTABLE:
            return False, f"配置项 {key} 不可修改"
        # 正则验证
        if meta.validation_regex:
            import re
            if not re.match(meta.validation_regex, new_value):
                return False, f"值不匹配格式要求: {meta.validation_regex}"
        # 数值范围验证
        if meta.min_value is not None:
            try:
                val = float(new_value)
                if val < meta.min_value:
                    return False, f"值 {val} 小于最小值 {meta.min_value}"
                if meta.max_value is not None and val > meta.max_value:
                    return False, f"值 {val} 大于最大值 {meta.max_value}"
            except ValueError:
                return False, f"值 {new_value} 不是有效数字"
        return True, ""

    def get_all(self) -> list[ConfigMetadata]:
        """返回所有注册的配置项"""
        return list(self._registry.values())

    def __len__(self) -> int:
        return len(self._registry)

    def __contains__(self, key: str) -> bool:
        return key in self._registry

    # ──────────────────────────────────────────────────────────────
    # 默认配置项注册 · 与 config/settings.py 字段一一对应
    # ──────────────────────────────────────────────────────────────
    def _init_defaults(self):
        """注册所有默认配置项元数据

        顺序与 config/settings.py 中的字段声明顺序保持一致,
        便于对照与维护。每个注册项均标注中文描述。
        """
        ALL_BOTS = ["up_bot", "idx_bot", "dsp_bot", "mon_bot", "admin_bot", "db_writer"]
        ALL_READERS = ["up_bot", "idx_bot", "dsp_bot", "mon_bot", "admin_bot"]

        # ── 环形冗余 v2 架构:5 个 Bot Token ──
        self.register(ConfigMetadata(
            key="UPLOAD_BOT_TOKEN",
            category=ConfigCategory.TELEGRAM,
            reload_policy=ReloadPolicy.RESTART_REQUIRED,
            sensitivity=SensitivityLevel.SECRET,
            description="Up Bot(上传入口)的 Telegram Bot Token",
            default_value="",
            env_var="UPLOAD_BOT_TOKEN",
            services=["up_bot"],
            validation_regex=r"^\d+:[A-Za-z0-9_-]{30,}$",
        ))
        self.register(ConfigMetadata(
            key="DECODER_BOT_TOKEN",
            category=ConfigCategory.TELEGRAM,
            reload_policy=ReloadPolicy.RESTART_REQUIRED,
            sensitivity=SensitivityLevel.SECRET,
            description="Idx Bot(解码入口)的 Telegram Bot Token",
            default_value="",
            env_var="DECODER_BOT_TOKEN",
            services=["idx_bot"],
            validation_regex=r"^\d+:[A-Za-z0-9_-]{30,}$",
        ))
        self.register(ConfigMetadata(
            key="SENDER_BOT_TOKEN",
            category=ConfigCategory.TELEGRAM,
            reload_policy=ReloadPolicy.RESTART_REQUIRED,
            sensitivity=SensitivityLevel.SECRET,
            description="Dsp Bot(发送分发)的 Telegram Bot Token",
            default_value="",
            env_var="SENDER_BOT_TOKEN",
            services=["dsp_bot"],
            validation_regex=r"^\d+:[A-Za-z0-9_-]{30,}$",
        ))
        self.register(ConfigMetadata(
            key="ADMIN_BOT_TOKEN",
            category=ConfigCategory.TELEGRAM,
            reload_policy=ReloadPolicy.RESTART_REQUIRED,
            sensitivity=SensitivityLevel.SECRET,
            description="Admin Bot(管理控制台)的 Telegram Bot Token",
            default_value="",
            env_var="ADMIN_BOT_TOKEN",
            services=["admin_bot"],
            validation_regex=r"^\d+:[A-Za-z0-9_-]{30,}$",
        ))
        self.register(ConfigMetadata(
            key="MON_BOT_TOKEN",
            category=ConfigCategory.TELEGRAM,
            reload_policy=ReloadPolicy.RESTART_REQUIRED,
            sensitivity=SensitivityLevel.SECRET,
            description="Mon Bot(监控告警)的 Telegram Bot Token",
            default_value="",
            env_var="MON_BOT_TOKEN",
            services=["mon_bot"],
            validation_regex=r"^\d+:[A-Za-z0-9_-]{30,}$",
        ))
        self.register(ConfigMetadata(
            key="ADMIN_TELEGRAM_ID",
            category=ConfigCategory.TELEGRAM,
            reload_policy=ReloadPolicy.RESTART_REQUIRED,
            sensitivity=SensitivityLevel.INTERNAL,
            description="管理员的 Telegram 用户 ID,admin_bot 仅响应此用户",
            default_value="0",
            env_var="ADMIN_TELEGRAM_ID",
            services=["admin_bot"],
            validation_regex=r"^\d+$",
            min_value=1,
        ))

        # ── 监控参数 ──
        self.register(ConfigMetadata(
            key="MON_CHECK_INTERVAL",
            category=ConfigCategory.MONITOR,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="Mon Bot 巡检间隔(秒)",
            default_value="60",
            env_var="MON_CHECK_INTERVAL",
            services=["mon_bot"],
            validation_regex=r"^\d+$",
            min_value=10,
            max_value=3600,
        ))

        # ── 轮转参数 ──
        self.register(ConfigMetadata(
            key="ROTATION_ACTIVE_WINDOW_SIZE",
            category=ConfigCategory.ROTATION,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="环形冗余:同一时刻活跃频道数",
            default_value="3",
            env_var="ROTATION_ACTIVE_WINDOW_SIZE",
            services=["dsp_bot"],
            validation_regex=r"^\d+$",
            min_value=1,
            max_value=10,
        ))
        self.register(ConfigMetadata(
            key="ROTATION_FILES_PER_SLOT",
            category=ConfigCategory.ROTATION,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="每槽位文件数上限",
            default_value="500",
            env_var="ROTATION_FILES_PER_SLOT",
            services=["dsp_bot"],
            validation_regex=r"^\d+$",
            min_value=1,
        ))
        self.register(ConfigMetadata(
            key="ROTATION_TIME_PER_SLOT",
            category=ConfigCategory.ROTATION,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="每槽位最多使用时间(秒)",
            default_value="3600",
            env_var="ROTATION_TIME_PER_SLOT",
            services=["dsp_bot"],
            validation_regex=r"^\d+$",
            min_value=60,
        ))

        # ── 数据保留(替代废弃的 CRDB TTL job) ──
        self.register(ConfigMetadata(
            key="DATA_RETENTION_DAYS",
            category=ConfigCategory.DATABASE,
            reload_policy=ReloadPolicy.RESTART_REQUIRED,
            sensitivity=SensitivityLevel.INTERNAL,
            description="decode_logs / jobs 保留天数",
            default_value="7",
            env_var="DATA_RETENTION_DAYS",
            services=["db_writer"],
            validation_regex=r"^\d+$",
            min_value=1,
        ))
        self.register(ConfigMetadata(
            key="CRDB_CLEANUP_CRON_HOURS",
            category=ConfigCategory.DATABASE,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="CRDB 清理周期(小时)",
            default_value="6",
            env_var="CRDB_CLEANUP_CRON_HOURS",
            services=["db_writer"],
            validation_regex=r"^\d+$",
            min_value=1,
            max_value=168,
        ))
        self.register(ConfigMetadata(
            key="CRDB_CLEANUP_BATCH_SIZE",
            category=ConfigCategory.DATABASE,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="CRDB 单批删除条数上限",
            default_value="5000",
            env_var="CRDB_CLEANUP_BATCH_SIZE",
            services=["db_writer"],
            validation_regex=r"^\d+$",
            min_value=1,
        ))

        # ── 账号频道配置(部署时在 .env 中填写) ──
        # 5 个账号 × 9 频道 = 45 频道
        for i in range(1, 6):
            self.register(ConfigMetadata(
                key=f"ACCOUNT_{i}_NAME",
                category=ConfigCategory.TELEGRAM,
                reload_policy=ReloadPolicy.RESTART_REQUIRED,
                sensitivity=SensitivityLevel.PUBLIC,
                description=f"环形冗余第 {i} 个账号的名称(可公开)",
                default_value="",
                env_var=f"ACCOUNT_{i}_NAME",
                services=["dsp_bot"],
            ))
            self.register(ConfigMetadata(
                key=f"ACCOUNT_{i}_CHANNELS",
                category=ConfigCategory.TELEGRAM,
                reload_policy=ReloadPolicy.RESTART_REQUIRED,
                sensitivity=SensitivityLevel.INTERNAL,
                description=f"环形冗余第 {i} 个账号的频道 ID 列表(逗号分隔)",
                default_value="",
                env_var=f"ACCOUNT_{i}_CHANNELS",
                services=["dsp_bot"],
                validation_regex=r"^-?\d+(?:\s*,\s*-?\d+)*$|^$",
            ))

        self.register(ConfigMetadata(
            key="R100_CHANNEL",
            category=ConfigCategory.TELEGRAM,
            reload_policy=ReloadPolicy.RESTART_REQUIRED,
            sensitivity=SensitivityLevel.INTERNAL,
            description="R100 兜底频道 ID(不参与环形调度)",
            default_value="0",
            env_var="R100_CHANNEL",
            services=["dsp_bot"],
            validation_regex=r"^-?\d+$",
        ))

        # ── CockroachDB 连接 ──
        self.register(ConfigMetadata(
            key="COCKROACHDB_URL",
            category=ConfigCategory.DATABASE,
            reload_policy=ReloadPolicy.RESTART_REQUIRED,
            sensitivity=SensitivityLevel.SECRET,
            description="CockroachDB 连接字符串(可能含密码,属敏感信息)",
            default_value="",
            env_var="COCKROACHDB_URL",
            services=ALL_READERS + ["db_writer"],
            validation_regex=r"^(postgres|postgresql)://.+",
        ))

        # ── 强制关注频道 ──
        self.register(ConfigMetadata(
            key="FORCE_JOIN_CHANNEL_ID",
            category=ConfigCategory.TELEGRAM,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="强制关注频道 ID,admin_bot 可热更新",
            default_value="0",
            env_var="FORCE_JOIN_CHANNEL_ID",
            services=["up_bot", "dsp_bot"],
            validation_regex=r"^-?\d+$",
        ))
        self.register(ConfigMetadata(
            key="FORCE_JOIN_CHANNEL_LINK",
            category=ConfigCategory.TELEGRAM,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.PUBLIC,
            description="强制关注频道链接(兼作官方频道地址)",
            default_value="",
            env_var="FORCE_JOIN_CHANNEL_LINK",
            services=["up_bot", "dsp_bot"],
            validation_regex=r"^https://t\.me/.+$|^$",
        ))

        # ── Bot Username(公开) ──
        self.register(ConfigMetadata(
            key="UPLOAD_BOT_USERNAME",
            category=ConfigCategory.TELEGRAM,
            reload_policy=ReloadPolicy.RESTART_REQUIRED,
            sensitivity=SensitivityLevel.PUBLIC,
            description="Up Bot 的 username(不含 @)",
            default_value="",
            env_var="UPLOAD_BOT_USERNAME",
            services=["up_bot"],
            validation_regex=r"^[A-Za-z0-9_]+$|^$",
        ))
        self.register(ConfigMetadata(
            key="DECODER_BOT_USERNAME",
            category=ConfigCategory.TELEGRAM,
            reload_policy=ReloadPolicy.RESTART_REQUIRED,
            sensitivity=SensitivityLevel.PUBLIC,
            description="Idx Bot 的 username(不含 @)",
            default_value="",
            env_var="DECODER_BOT_USERNAME",
            services=["idx_bot"],
            validation_regex=r"^[A-Za-z0-9_]+$|^$",
        ))
        self.register(ConfigMetadata(
            key="SENDER_BOT_USERNAME",
            category=ConfigCategory.TELEGRAM,
            reload_policy=ReloadPolicy.RESTART_REQUIRED,
            sensitivity=SensitivityLevel.PUBLIC,
            description="Dsp Bot 的 username(不含 @)",
            default_value="",
            env_var="SENDER_BOT_USERNAME",
            services=["dsp_bot"],
            validation_regex=r"^[A-Za-z0-9_]+$|^$",
        ))

        # ── 中继加密密钥 ──
        self.register(ConfigMetadata(
            key="RELAY_ENCRYPTION_KEY",
            category=ConfigCategory.SECURITY,
            reload_policy=ReloadPolicy.RESTART_REQUIRED,
            sensitivity=SensitivityLevel.CRITICAL,
            description="中继 API Hash 的 Fernet 加密密钥(44 字符 urlsafe-base64)",
            default_value="",
            env_var="RELAY_ENCRYPTION_KEY",
            services=["db_writer", "up_bot", "idx_bot", "dsp_bot", "admin_bot"],
            validation_regex=r"^[A-Za-z0-9_-]{42,44}$",
        ))

        # ── 中继账号白名单 ──
        self.register(ConfigMetadata(
            key="RELAY_ACCOUNT_IDS",
            category=ConfigCategory.RELAY,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="中继账号白名单(逗号分隔的 Telegram 用户 ID),admin_bot /relay_whitelist 可热增删",
            default_value="",
            env_var="RELAY_ACCOUNT_IDS",
            services=["up_bot"],
            validation_regex=r"^\d+(?:\s*,\s*\d+)*$|^$",
        ))
        self.register(ConfigMetadata(
            key="COLLECTOR_ACCOUNT_IDS",
            category=ConfigCategory.RELAY,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="采集器账号白名单(逗号分隔的 Telegram 用户 ID),admin_bot /collector_whitelist 可热增删",
            default_value="",
            env_var="COLLECTOR_ACCOUNT_IDS",
            services=["up_bot"],
            validation_regex=r"^\d+(?:\s*,\s*\d+)*$|^$",
        ))

        # ── Telegram Relay API 凭证 ──
        self.register(ConfigMetadata(
            key="RELAY_API_ID",
            category=ConfigCategory.RELAY,
            reload_policy=ReloadPolicy.RESTART_REQUIRED,
            sensitivity=SensitivityLevel.SECRET,
            description="Telegram Relay API ID(添加中继账号时读取)",
            default_value="0",
            env_var="RELAY_API_ID",
            services=["up_bot"],
            validation_regex=r"^\d+$",
            min_value=1,
        ))
        self.register(ConfigMetadata(
            key="RELAY_API_HASH",
            category=ConfigCategory.SECURITY,
            reload_policy=ReloadPolicy.RESTART_REQUIRED,
            sensitivity=SensitivityLevel.CRITICAL,
            description="Telegram Relay API Hash(关键凭证,加密存储)",
            default_value="",
            env_var="RELAY_API_HASH",
            services=["up_bot"],
            validation_regex=r"^[0-9a-fA-F]{16,64}$",
        ))

        # ── P1-9 外部解码器白名单 ──
        self.register(ConfigMetadata(
            key="ALLOWED_DECODER_BOTS",
            category=ConfigCategory.SECURITY,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="允许的外部解码器 bot 白名单(逗号分隔的 username,不含 @);空表示 fail-open(或根据 ALLOWED_DECODER_BOTS_FAIL_CLOSED 决定)",
            default_value="",
            env_var="ALLOWED_DECODER_BOTS",
            services=["idx_bot"],
            validation_regex=r"^[A-Za-z0-9_]+(?:\s*,\s*[A-Za-z0-9_]+)*$|^$",
        ))

        # ── P2-1 外部解码器白名单 fail-closed 开关 ──
        self.register(ConfigMetadata(
            key="ALLOWED_DECODER_BOTS_FAIL_CLOSED",
            category=ConfigCategory.SECURITY,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="白名单为空时是否拒绝所有外部解码器(True=fail-closed,商用建议;False=fail-open,兼容现有部署)",
            default_value=False,
            env_var="ALLOWED_DECODER_BOTS_FAIL_CLOSED",
            services=["idx_bot"],
            validation_regex=r"^(true|false|True|False|1|0)$",
        ))

        # ── Cloudflare R2 备份凭证 ──
        self.register(ConfigMetadata(
            key="R2_ACCOUNT_ID",
            category=ConfigCategory.BACKUP,
            reload_policy=ReloadPolicy.RESTART_REQUIRED,
            sensitivity=SensitivityLevel.INTERNAL,
            description="Cloudflare R2 账户 ID",
            default_value="",
            env_var="R2_ACCOUNT_ID",
            services=["db_backup"],
            validation_regex=r"^[a-f0-9]{32}$|^$",
        ))
        self.register(ConfigMetadata(
            key="R2_ACCESS_KEY_ID",
            category=ConfigCategory.SECURITY,
            reload_policy=ReloadPolicy.RESTART_REQUIRED,
            sensitivity=SensitivityLevel.CRITICAL,
            description="R2 Access Key ID(关键凭证)",
            default_value="",
            env_var="R2_ACCESS_KEY_ID",
            services=["db_backup"],
        ))
        self.register(ConfigMetadata(
            key="R2_SECRET_ACCESS_KEY",
            category=ConfigCategory.SECURITY,
            reload_policy=ReloadPolicy.RESTART_REQUIRED,
            sensitivity=SensitivityLevel.CRITICAL,
            description="R2 Secret Access Key(关键凭证,泄露将导致备份桶被任意读写)",
            default_value="",
            env_var="R2_SECRET_ACCESS_KEY",
            services=["db_backup"],
        ))
        self.register(ConfigMetadata(
            key="R2_BUCKET_NAME",
            category=ConfigCategory.BACKUP,
            reload_policy=ReloadPolicy.RESTART_REQUIRED,
            sensitivity=SensitivityLevel.INTERNAL,
            description="R2 备份桶名称",
            default_value="tgjiema-backup",
            env_var="R2_BUCKET_NAME",
            services=["db_backup"],
            validation_regex=r"^[a-z0-9][a-z0-9\-]{1,61}[a-z0-9]$",
        ))
        self.register(ConfigMetadata(
            key="R2_ENDPOINT",
            category=ConfigCategory.BACKUP,
            reload_policy=ReloadPolicy.RESTART_REQUIRED,
            sensitivity=SensitivityLevel.INTERNAL,
            description="R2 自定义 endpoint(S3 兼容地址)",
            default_value="",
            env_var="R2_ENDPOINT",
            services=["db_backup"],
            validation_regex=r"^https?://.+$|^$",
        ))

        # ── 数据库备份开关与周期 ──
        self.register(ConfigMetadata(
            key="DB_BACKUP_INTERVAL_MINUTES",
            category=ConfigCategory.BACKUP,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="数据库备份间隔(分钟),admin_bot 可热更新",
            default_value="360",
            env_var="DB_BACKUP_INTERVAL_MINUTES",
            services=["db_backup"],
            validation_regex=r"^\d+$",
            min_value=60,
            max_value=14400,
        ))
        self.register(ConfigMetadata(
            key="DB_BACKUP_ENABLED",
            category=ConfigCategory.BACKUP,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="是否启用数据库备份(true/false)",
            default_value="false",
            env_var="DB_BACKUP_ENABLED",
            services=["db_backup"],
            validation_regex=r"^(true|false|1|0|on|off|yes|no)$",
        ))
        self.register(ConfigMetadata(
            key="BACKUP_KEK",
            category=ConfigCategory.BACKUP,
            reload_policy=ReloadPolicy.COLD_RELOAD,
            sensitivity=SensitivityLevel.SECRET,
            description="R36 H7: 备份 AES-256-GCM 信封加密 KEK(base64, 32 字节);空则降级为明文",
            default_value="",
            env_var="BACKUP_KEK",
            services=["db_backup"],
            validation_regex=r"^[A-Za-z0-9+/=]*$",
        ))

        # ── Admin Web 服务参数 ──
        self.register(ConfigMetadata(
            key="ADMIN_WEB_PORT",
            category=ConfigCategory.M2_CONTROL,
            reload_policy=ReloadPolicy.RESTART_REQUIRED,
            sensitivity=SensitivityLevel.INTERNAL,
            description="Admin Web 监听端口",
            default_value="8080",
            env_var="ADMIN_WEB_PORT",
            services=["admin_bot"],
            validation_regex=r"^\d+$",
            min_value=1,
            max_value=65535,
        ))
        self.register(ConfigMetadata(
            key="ADMIN_WEB_HOST",
            category=ConfigCategory.M2_CONTROL,
            reload_policy=ReloadPolicy.RESTART_REQUIRED,
            sensitivity=SensitivityLevel.INTERNAL,
            description="Admin Web 绑定地址(非回环地址时密码必须 ≥12 位)",
            default_value="127.0.0.1",
            env_var="ADMIN_WEB_HOST",
            services=["admin_bot"],
        ))
        self.register(ConfigMetadata(
            key="ADMIN_USERNAME",
            category=ConfigCategory.SECURITY,
            reload_policy=ReloadPolicy.RESTART_REQUIRED,
            sensitivity=SensitivityLevel.SECRET,
            description="Admin Web 管理员用户名",
            default_value="",
            env_var="ADMIN_USERNAME",
            services=["admin_bot"],
        ))
        self.register(ConfigMetadata(
            key="ADMIN_PASSWORD",
            category=ConfigCategory.SECURITY,
            reload_policy=ReloadPolicy.RESTART_REQUIRED,
            sensitivity=SensitivityLevel.CRITICAL,
            description="Admin Web 管理员密码(非回环地址时必须 ≥12 位)",
            default_value="",
            env_var="ADMIN_PASSWORD",
            services=["admin_bot"],
        ))

        # ── Dsp Bot 频道降级阈值 ──
        self.register(ConfigMetadata(
            key="CHANNEL_FAILURE_THRESHOLD",
            category=ConfigCategory.MONITOR,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="Dsp Bot 频道降级阈值:窗口内失败 N 次触发降级",
            default_value="3",
            env_var="CHANNEL_FAILURE_THRESHOLD",
            services=["dsp_bot"],
            validation_regex=r"^\d+$",
            min_value=1,
        ))
        self.register(ConfigMetadata(
            key="CHANNEL_FAILURE_WINDOW",
            category=ConfigCategory.MONITOR,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="频道失败统计窗口(秒)",
            default_value="60",
            env_var="CHANNEL_FAILURE_WINDOW",
            services=["dsp_bot"],
            validation_regex=r"^\d+$",
            min_value=10,
        ))

        # ── Relay Pool 负载均衡权重 ──
        self.register(ConfigMetadata(
            key="RELAY_WEIGHT_AVG_WAIT",
            category=ConfigCategory.RELAY,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="Relay Pool:avg_wait_ms 权重",
            default_value="0.4",
            env_var="RELAY_WEIGHT_AVG_WAIT",
            services=["up_bot"],
            validation_regex=r"^\d+(\.\d+)?$",
            min_value=0,
            max_value=1,
        ))
        self.register(ConfigMetadata(
            key="RELAY_WEIGHT_TODAY_REQ",
            category=ConfigCategory.RELAY,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="Relay Pool:today_requests 权重",
            default_value="0.4",
            env_var="RELAY_WEIGHT_TODAY_REQ",
            services=["up_bot"],
            validation_regex=r"^\d+(\.\d+)?$",
            min_value=0,
            max_value=1,
        ))
        self.register(ConfigMetadata(
            key="RELAY_WEIGHT_GAP",
            category=ConfigCategory.RELAY,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="Relay Pool:last_request_gap 权重",
            default_value="0.2",
            env_var="RELAY_WEIGHT_GAP",
            services=["up_bot"],
            validation_regex=r"^\d+(\.\d+)?$",
            min_value=0,
            max_value=1,
        ))
        self.register(ConfigMetadata(
            key="RELAY_NORM_AVG_WAIT",
            category=ConfigCategory.RELAY,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="Relay Pool:avg_wait_ms 归一化因子",
            default_value="1000.0",
            env_var="RELAY_NORM_AVG_WAIT",
            services=["up_bot"],
            validation_regex=r"^\d+(\.\d+)?$",
            min_value=1,
        ))
        self.register(ConfigMetadata(
            key="RELAY_NORM_TODAY_REQ",
            category=ConfigCategory.RELAY,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="Relay Pool:today_requests 归一化因子",
            default_value="50000.0",
            env_var="RELAY_NORM_TODAY_REQ",
            services=["up_bot"],
            validation_regex=r"^\d+(\.\d+)?$",
            min_value=1,
        ))
        self.register(ConfigMetadata(
            key="RELAY_NORM_GAP",
            category=ConfigCategory.RELAY,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="Relay Pool:gap 归一化因子",
            default_value="3600.0",
            env_var="RELAY_NORM_GAP",
            services=["up_bot"],
            validation_regex=r"^\d+(\.\d+)?$",
            min_value=1,
        ))
        self.register(ConfigMetadata(
            key="RELAY_SAFE_POOL_SIZE",
            category=ConfigCategory.RELAY,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="中继账号安全池最小数量,低于此值告警(mon_bot 读取)",
            default_value="2",
            env_var="RELAY_SAFE_POOL_SIZE",
            services=["mon_bot"],
            validation_regex=r"^\d+$",
            min_value=1,
        ))

        # ── Redis 配置 ──
        self.register(ConfigMetadata(
            key="REDIS_URL",
            category=ConfigCategory.REDIS,
            reload_policy=ReloadPolicy.RESTART_REQUIRED,
            sensitivity=SensitivityLevel.SECRET,
            description="Redis 连接字符串(可能含密码);为空时降级到 SQLite 轮询",
            default_value="",
            env_var="REDIS_URL",
            services=ALL_BOTS,
            validation_regex=r"^(redis|rediss|redis\+socket)://.+|^$",
        ))
        self.register(ConfigMetadata(
            key="REDIS_STREAM_MAXLEN",
            category=ConfigCategory.REDIS,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="Redis Stream 最大长度,防止无限增长",
            default_value="10000",
            env_var="REDIS_STREAM_MAXLEN",
            services=["db_writer"],
            validation_regex=r"^\d+$",
            min_value=100,
        ))

        # ── Writer 进程配置(方案B v2) ──
        self.register(ConfigMetadata(
            key="WRITER_MODE",
            category=ConfigCategory.WRITER,
            reload_policy=ReloadPolicy.RESTART_REQUIRED,
            sensitivity=SensitivityLevel.INTERNAL,
            description="写入模式:redis(Streams+Writer)/ sqlite(降级直写)",
            default_value="redis",
            env_var="WRITER_MODE",
            services=["db_writer"],
            validation_regex=r"^(redis|sqlite)$",
        ))
        self.register(ConfigMetadata(
            key="WRITER_STREAM_KEY",
            category=ConfigCategory.REDIS,
            reload_policy=ReloadPolicy.RESTART_REQUIRED,
            sensitivity=SensitivityLevel.INTERNAL,
            description="Writer Stream key(替代 List,支持 Consumer Group)",
            default_value="tgjiema:writer:stream",
            env_var="WRITER_STREAM_KEY",
            services=["db_writer"],
        ))
        self.register(ConfigMetadata(
            key="WRITER_CONSUMER_GROUP",
            category=ConfigCategory.REDIS,
            reload_policy=ReloadPolicy.RESTART_REQUIRED,
            sensitivity=SensitivityLevel.INTERNAL,
            description="Writer Consumer Group 名(db_writer 通过此 group 消费 Stream)",
            default_value="tgjiema-writer-group",
            env_var="WRITER_CONSUMER_GROUP",
            services=["db_writer"],
        ))
        self.register(ConfigMetadata(
            key="WRITER_CONSUMER_NAME",
            category=ConfigCategory.REDIS,
            reload_policy=ReloadPolicy.RESTART_REQUIRED,
            sensitivity=SensitivityLevel.INTERNAL,
            description="Writer Consumer 名(区分不同 db_writer 实例)",
            default_value="db_writer",
            env_var="WRITER_CONSUMER_NAME",
            services=["db_writer"],
        ))
        self.register(ConfigMetadata(
            key="WRITER_RECLAIM_IDLE_MS",
            category=ConfigCategory.WRITER,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="pending 消息回收阈值(ms),超过此时间会被 XAUTOCLAIM 回收",
            default_value="30000",
            env_var="WRITER_RECLAIM_IDLE_MS",
            services=["db_writer"],
            validation_regex=r"^\d+$",
            min_value=1000,
        ))
        self.register(ConfigMetadata(
            key="WRITER_BATCH_SIZE",
            category=ConfigCategory.WRITER,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="Writer 单次 XREADGROUP 批量大小",
            default_value="10",
            env_var="WRITER_BATCH_SIZE",
            services=["db_writer"],
            validation_regex=r"^\d+$",
            min_value=1,
            max_value=1000,
        ))
        self.register(ConfigMetadata(
            key="WRITER_QUEUE_ALERT_THRESHOLD",
            category=ConfigCategory.WRITER,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="Writer 队列积压告警阈值(mon_bot 监控 pending 数)",
            default_value="1000",
            env_var="WRITER_QUEUE_ALERT_THRESHOLD",
            services=["mon_bot"],
            validation_regex=r"^\d+$",
            min_value=10,
        ))
        self.register(ConfigMetadata(
            key="WRITER_CACHE_TTL_QUOTA",
            category=ConfigCategory.WRITER,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="读缓存 TTL(秒):用户配额(高频变更,短 TTL)",
            default_value="5",
            env_var="WRITER_CACHE_TTL_QUOTA",
            services=["db_writer"],
            validation_regex=r"^\d+$",
            min_value=1,
        ))
        self.register(ConfigMetadata(
            key="WRITER_CACHE_TTL_FILE_RECORD",
            category=ConfigCategory.WRITER,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="读缓存 TTL(秒):文件记录(中频变更)",
            default_value="30",
            env_var="WRITER_CACHE_TTL_FILE_RECORD",
            services=["db_writer"],
            validation_regex=r"^\d+$",
            min_value=1,
        ))
        self.register(ConfigMetadata(
            key="WRITER_CACHE_TTL_CODE",
            category=ConfigCategory.WRITER,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="读缓存 TTL(秒):验证码(中频变更)",
            default_value="30",
            env_var="WRITER_CACHE_TTL_CODE",
            services=["db_writer"],
            validation_regex=r"^\d+$",
            min_value=1,
        ))
        self.register(ConfigMetadata(
            key="WRITER_CACHE_TTL_USER",
            category=ConfigCategory.WRITER,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="读缓存 TTL(秒):用户记录(中频变更)",
            default_value="30",
            env_var="WRITER_CACHE_TTL_USER",
            services=["db_writer"],
            validation_regex=r"^\d+$",
            min_value=1,
        ))
        self.register(ConfigMetadata(
            key="WRITER_CACHE_TTL_CELLS",
            category=ConfigCategory.WRITER,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="读缓存 TTL(秒):全量 cells(中频变更)",
            default_value="10",
            env_var="WRITER_CACHE_TTL_CELLS",
            services=["db_writer"],
            validation_regex=r"^\d+$",
            min_value=1,
        ))
        self.register(ConfigMetadata(
            key="WRITER_CACHE_TTL_BOT_HB",
            category=ConfigCategory.WRITER,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="读缓存 TTL(秒):Bot 心跳(高频变更,短 TTL)",
            default_value="5",
            env_var="WRITER_CACHE_TTL_BOT_HB",
            services=["db_writer"],
            validation_regex=r"^\d+$",
            min_value=1,
        ))
        self.register(ConfigMetadata(
            key="WRITER_CACHE_TTL_KV",
            category=ConfigCategory.WRITER,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="读缓存 TTL(秒):KV 存储(低频变更,长 TTL)",
            default_value="60",
            env_var="WRITER_CACHE_TTL_KV",
            services=["db_writer"],
            validation_regex=r"^\d+$",
            min_value=1,
        ))
        self.register(ConfigMetadata(
            key="WRITER_DEAD_STREAM_KEY",
            category=ConfigCategory.REDIS,
            reload_policy=ReloadPolicy.RESTART_REQUIRED,
            sensitivity=SensitivityLevel.INTERNAL,
            description="死信队列 Stream key(替代 List,支持重试闭环)",
            default_value="tgjiema:writer:dead",
            env_var="WRITER_DEAD_STREAM_KEY",
            services=["db_writer", "dlq_worker"],
        ))
        self.register(ConfigMetadata(
            key="WRITER_DEAD_MAX_ATTEMPTS",
            category=ConfigCategory.WRITER,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="死信最大重试次数(超过后永久死信,需人工排查)",
            default_value="3",
            env_var="WRITER_DEAD_MAX_ATTEMPTS",
            services=["db_writer", "dlq_worker"],
            validation_regex=r"^\d+$",
            min_value=1,
            max_value=20,
        ))
        self.register(ConfigMetadata(
            key="WRITER_DEAD_RETRY_DELAY",
            category=ConfigCategory.WRITER,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="死信重试延迟(秒,失败后延迟 XADD 回主队列)",
            default_value="60",
            env_var="WRITER_DEAD_RETRY_DELAY",
            services=["db_writer", "dlq_worker"],
            validation_regex=r"^\d+$",
            min_value=1,
        ))
        self.register(ConfigMetadata(
            key="WRITER_INBOX_RETENTION_HOURS",
            category=ConfigCategory.WRITER,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="writer_inbox 清理保留期(小时),必须远大于 WRITER_RECLAIM_IDLE_MS",
            default_value="168",
            env_var="WRITER_INBOX_RETENTION_HOURS",
            services=["db_writer"],
            validation_regex=r"^\d+$",
            min_value=1,
        ))
        self.register(ConfigMetadata(
            key="DB_WRITER_SERVICE_NAME",
            category=ConfigCategory.WRITER,
            reload_policy=ReloadPolicy.DEPLOY_REQUIRED,
            sensitivity=SensitivityLevel.INTERNAL,
            description="db_writer systemd 服务名(mon_bot 监控用,修改需重部署 unit)",
            default_value="tgjiema-db_writer",
            env_var="DB_WRITER_SERVICE_NAME",
            services=["mon_bot"],
        ))

        # ── M1 业务闭环配置 ──
        self.register(ConfigMetadata(
            key="UPLOAD_SESSION_TTL",
            category=ConfigCategory.M1_BUSINESS,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="upload_sessions 上传会话超时(秒),超时未推进则 EXPIRED",
            default_value="3600",
            env_var="UPLOAD_SESSION_TTL",
            services=["up_bot"],
            validation_regex=r"^\d+$",
            min_value=60,
        ))
        self.register(ConfigMetadata(
            key="UPLOAD_SESSION_LEASE_SECONDS",
            category=ConfigCategory.M1_BUSINESS,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="上传会话租约时长(秒)",
            default_value="300",
            env_var="UPLOAD_SESSION_LEASE_SECONDS",
            services=["up_bot"],
            validation_regex=r"^\d+$",
            min_value=10,
        ))
        self.register(ConfigMetadata(
            key="UPLOAD_OUTBOX_MAX_ATTEMPTS",
            category=ConfigCategory.M1_BUSINESS,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="upload_outbox 事务发件箱最大重试次数",
            default_value="5",
            env_var="UPLOAD_OUTBOX_MAX_ATTEMPTS",
            services=["up_bot"],
            validation_regex=r"^\d+$",
            min_value=1,
            max_value=20,
        ))
        self.register(ConfigMetadata(
            key="UPLOAD_OUTBOX_RETRY_DELAY",
            category=ConfigCategory.M1_BUSINESS,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="upload_outbox 发件箱重试延迟(秒)",
            default_value="60",
            env_var="UPLOAD_OUTBOX_RETRY_DELAY",
            services=["up_bot"],
            validation_regex=r"^\d+$",
            min_value=1,
        ))
        self.register(ConfigMetadata(
            key="QUOTA_LEDGER_RETENTION_DAYS",
            category=ConfigCategory.M1_BUSINESS,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="quota_ledger 配额变更流水保留天数",
            default_value="90",
            env_var="QUOTA_LEDGER_RETENTION_DAYS",
            services=["db_writer"],
            validation_regex=r"^\d+$",
            min_value=1,
        ))
        self.register(ConfigMetadata(
            key="DELIVERY_RECEIPT_RETENTION_DAYS",
            category=ConfigCategory.M1_BUSINESS,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="delivery_receipts 投递回执保留天数",
            default_value="30",
            env_var="DELIVERY_RECEIPT_RETENTION_DAYS",
            services=["db_writer"],
            validation_regex=r"^\d+$",
            min_value=1,
        ))
        self.register(ConfigMetadata(
            key="REPLICATION_TASK_MAX_ATTEMPTS",
            category=ConfigCategory.M1_BUSINESS,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="replication_tasks 副本复制最大重试次数",
            default_value="3",
            env_var="REPLICATION_TASK_MAX_ATTEMPTS",
            services=["up_bot"],
            validation_regex=r"^\d+$",
            min_value=1,
            max_value=20,
        ))
        self.register(ConfigMetadata(
            key="REPLICATION_TASK_RETRY_DELAY",
            category=ConfigCategory.M1_BUSINESS,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="replication_tasks 副本复制重试延迟(秒)",
            default_value="60",
            env_var="REPLICATION_TASK_RETRY_DELAY",
            services=["up_bot"],
            validation_regex=r"^\d+$",
            min_value=1,
        ))
        self.register(ConfigMetadata(
            key="REPLICATION_BATCH_SIZE",
            category=ConfigCategory.M1_BUSINESS,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="replication_tasks 副本复制批量大小",
            default_value="30",
            env_var="REPLICATION_BATCH_SIZE",
            services=["up_bot"],
            validation_regex=r"^\d+$",
            min_value=1,
            max_value=500,
        ))

        # ── 配额同步间隔 ──
        self.register(ConfigMetadata(
            key="QUOTA_SYNC_INTERVAL",
            category=ConfigCategory.QUOTA,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="配额同步间隔(秒,5 分钟以减少 CRDB RU 消耗)",
            default_value="300",
            env_var="QUOTA_SYNC_INTERVAL",
            services=["up_bot"],
            validation_regex=r"^\d+$",
            min_value=30,
        ))

        # ── 进程管理 ──
        self.register(ConfigMetadata(
            key="RESTART_COOLDOWN",
            category=ConfigCategory.M2_CONTROL,
            reload_policy=ReloadPolicy.RESTART_REQUIRED,
            sensitivity=SensitivityLevel.INTERNAL,
            description="子进程崩溃冷却期(秒)",
            default_value="600",
            env_var="RESTART_COOLDOWN",
            services=["run_all"],
            validation_regex=r"^\d+$",
            min_value=10,
        ))
        self.register(ConfigMetadata(
            key="TOPOLOGY_SEED_RETRIES",
            category=ConfigCategory.M2_CONTROL,
            reload_policy=ReloadPolicy.RESTART_REQUIRED,
            sensitivity=SensitivityLevel.INTERNAL,
            description="拓扑初始化重试次数",
            default_value="3",
            env_var="TOPOLOGY_SEED_RETRIES",
            services=["run_all"],
            validation_regex=r"^\d+$",
            min_value=0,
            max_value=20,
        ))

        # ── 内部配额(每日限额) ──
        self.register(ConfigMetadata(
            key="FREE_DAILY_QUOTA",
            category=ConfigCategory.QUOTA,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="Free 用户每日配额(-1 表示无限)",
            default_value="3",
            env_var="FREE_DAILY_QUOTA",
            services=["up_bot", "dsp_bot"],
            validation_regex=r"^-?\d+$",
            min_value=-1,
        ))
        self.register(ConfigMetadata(
            key="BASIC_DAILY_QUOTA",
            category=ConfigCategory.QUOTA,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="Basic 用户每日配额(-1 表示无限)",
            default_value="20",
            env_var="BASIC_DAILY_QUOTA",
            services=["up_bot", "dsp_bot"],
            validation_regex=r"^-?\d+$",
            min_value=-1,
        ))
        self.register(ConfigMetadata(
            key="PREMIUM_DAILY_QUOTA",
            category=ConfigCategory.QUOTA,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="Premium 用户每日配额(-1 表示无限)",
            default_value="-1",
            env_var="PREMIUM_DAILY_QUOTA",
            services=["up_bot", "dsp_bot"],
            validation_regex=r"^-?\d+$",
            min_value=-1,
        ))
        self.register(ConfigMetadata(
            key="FREE_EXTERNAL_DAILY_QUOTA",
            category=ConfigCategory.QUOTA,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="Free 用户外部码每日配额(-1 表示无限)",
            default_value="0",
            env_var="FREE_EXTERNAL_DAILY_QUOTA",
            services=["up_bot"],
            validation_regex=r"^-?\d+$",
            min_value=-1,
        ))
        self.register(ConfigMetadata(
            key="BASIC_EXTERNAL_DAILY_QUOTA",
            category=ConfigCategory.QUOTA,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="Basic 用户外部码每日配额(-1 表示无限)",
            default_value="-1",
            env_var="BASIC_EXTERNAL_DAILY_QUOTA",
            services=["up_bot"],
            validation_regex=r"^-?\d+$",
            min_value=-1,
        ))
        self.register(ConfigMetadata(
            key="PREMIUM_EXTERNAL_DAILY_QUOTA",
            category=ConfigCategory.QUOTA,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="Premium 用户外部码每日配额(-1 表示无限)",
            default_value="-1",
            env_var="PREMIUM_EXTERNAL_DAILY_QUOTA",
            services=["up_bot"],
            validation_regex=r"^-?\d+$",
            min_value=-1,
        ))

        # ── 全局速率限制 ──
        self.register(ConfigMetadata(
            key="RATE_LIMIT_GLOBAL_PER_SECOND",
            category=ConfigCategory.QUOTA,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="全局每秒请求上限",
            default_value="30",
            env_var="RATE_LIMIT_GLOBAL_PER_SECOND",
            services=["up_bot", "dsp_bot"],
            validation_regex=r"^\d+$",
            min_value=1,
        ))
        self.register(ConfigMetadata(
            key="RATE_LIMIT_PER_USER_PER_MINUTE",
            category=ConfigCategory.QUOTA,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="单用户每分钟请求上限",
            default_value="10",
            env_var="RATE_LIMIT_PER_USER_PER_MINUTE",
            services=["up_bot", "dsp_bot"],
            validation_regex=r"^\d+$",
            min_value=1,
        ))

        # ── 日志级别 ──
        self.register(ConfigMetadata(
            key="LOG_LEVEL",
            category=ConfigCategory.M2_CONTROL,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="日志级别(DEBUG/INFO/WARNING/ERROR/CRITICAL)",
            default_value="INFO",
            env_var="LOG_LEVEL",
            services=ALL_BOTS,
            validation_regex=r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$",
        ))

        # ── 文件码前缀 ──
        self.register(ConfigMetadata(
            key="FILE_CODE_PREFIX",
            category=ConfigCategory.M1_BUSINESS,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.PUBLIC,
            description="文件码前缀(用户可见)",
            default_value="tgwenjian",
            env_var="FILE_CODE_PREFIX",
            services=["up_bot", "dsp_bot"],
            validation_regex=r"^[A-Za-z0-9_-]{1,32}$",
        ))

        # ── 上传选项默认值 ──
        self.register(ConfigMetadata(
            key="DEFAULT_PROTECT_CONTENT",
            category=ConfigCategory.M1_BUSINESS,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="上传默认是否启用 protect_content(防转发)",
            default_value="false",
            env_var="DEFAULT_PROTECT_CONTENT",
            services=["up_bot", "dsp_bot"],
            validation_regex=r"^(true|false|1|0)$",
        ))
        self.register(ConfigMetadata(
            key="DEFAULT_FILE_TTL_DAYS",
            category=ConfigCategory.M1_BUSINESS,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="文件默认 TTL 天数(0=永久有效)",
            default_value="0",
            env_var="DEFAULT_FILE_TTL_DAYS",
            services=["up_bot", "dsp_bot"],
            validation_regex=r"^\d+$",
            min_value=0,
        ))

        # ── 可调参数(原硬编码魔法数字) ──
        self.register(ConfigMetadata(
            key="MEDIA_GROUP_BUFFER_WAIT",
            category=ConfigCategory.M1_BUSINESS,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="媒体组缓冲等待时间(秒)",
            default_value="3.0",
            env_var="MEDIA_GROUP_BUFFER_WAIT",
            services=["up_bot"],
            validation_regex=r"^\d+(\.\d+)?$",
            min_value=0.1,
            max_value=60,
        ))
        self.register(ConfigMetadata(
            key="PENDING_TTL",
            category=ConfigCategory.M1_BUSINESS,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="外部码等待超时(秒)",
            default_value="300",
            env_var="PENDING_TTL",
            services=["up_bot", "idx_bot"],
            validation_regex=r"^\d+$",
            min_value=10,
        ))
        self.register(ConfigMetadata(
            key="SEND_CONCURRENCY",
            category=ConfigCategory.M1_BUSINESS,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="Dsp Bot 发送并发上限",
            default_value="25",
            env_var="SEND_CONCURRENCY",
            services=["dsp_bot"],
            validation_regex=r"^\d+$",
            min_value=1,
            max_value=200,
        ))
        self.register(ConfigMetadata(
            key="PAGE_SIZE",
            category=ConfigCategory.M1_BUSINESS,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="通用分页大小",
            default_value="10",
            env_var="PAGE_SIZE",
            services=["up_bot", "dsp_bot", "idx_bot"],
            validation_regex=r"^\d+$",
            min_value=1,
            max_value=200,
        ))
        self.register(ConfigMetadata(
            key="EXTERNAL_MEDIA_GROUP_TTL",
            category=ConfigCategory.M1_BUSINESS,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="外部媒体组 TTL(秒)",
            default_value="300",
            env_var="EXTERNAL_MEDIA_GROUP_TTL",
            services=["up_bot"],
            validation_regex=r"^\d+$",
            min_value=10,
        ))
        self.register(ConfigMetadata(
            key="CACHE_STORE_CLEANUP_DAYS",
            category=ConfigCategory.M1_BUSINESS,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="本地缓存清理天数",
            default_value="30",
            env_var="CACHE_STORE_CLEANUP_DAYS",
            services=["up_bot"],
            validation_regex=r"^\d+$",
            min_value=1,
        ))
        self.register(ConfigMetadata(
            key="MAX_RESTART_COUNT",
            category=ConfigCategory.M2_CONTROL,
            reload_policy=ReloadPolicy.RESTART_REQUIRED,
            sensitivity=SensitivityLevel.INTERNAL,
            description="5 分钟内最大重启次数",
            default_value="3",
            env_var="MAX_RESTART_COUNT",
            services=["run_all"],
            validation_regex=r"^\d+$",
            min_value=1,
            max_value=50,
        ))
        self.register(ConfigMetadata(
            key="MAX_RESTART_WINDOW",
            category=ConfigCategory.M2_CONTROL,
            reload_policy=ReloadPolicy.RESTART_REQUIRED,
            sensitivity=SensitivityLevel.INTERNAL,
            description="重启计数窗口(秒)",
            default_value="300",
            env_var="MAX_RESTART_WINDOW",
            services=["run_all"],
            validation_regex=r"^\d+$",
            min_value=60,
        ))

        # ── 动态限速参数 ──
        self.register(ConfigMetadata(
            key="RATE_LIMIT_BASE_DELAY",
            category=ConfigCategory.QUOTA,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="空闲时基础延迟(秒)",
            default_value="0.2",
            env_var="RATE_LIMIT_BASE_DELAY",
            services=["up_bot"],
            validation_regex=r"^\d+(\.\d+)?$",
            min_value=0,
            max_value=10,
        ))
        self.register(ConfigMetadata(
            key="RATE_LIMIT_MAX_DELAY",
            category=ConfigCategory.QUOTA,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="高峰期最大延迟(秒)",
            default_value="3.0",
            env_var="RATE_LIMIT_MAX_DELAY",
            services=["up_bot"],
            validation_regex=r"^\d+(\.\d+)?$",
            min_value=0.1,
            max_value=60,
        ))
        self.register(ConfigMetadata(
            key="RATE_LIMIT_THRESHOLD_LOW",
            category=ConfigCategory.QUOTA,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="低负载阈值(jobs 数量 < 此值用基础延迟)",
            default_value="10",
            env_var="RATE_LIMIT_THRESHOLD_LOW",
            services=["up_bot"],
            validation_regex=r"^\d+$",
            min_value=0,
        ))
        self.register(ConfigMetadata(
            key="RATE_LIMIT_THRESHOLD_HIGH",
            category=ConfigCategory.QUOTA,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="高负载阈值(jobs 数量 > 此值用最大延迟)",
            default_value="30",
            env_var="RATE_LIMIT_THRESHOLD_HIGH",
            services=["up_bot"],
            validation_regex=r"^\d+$",
            min_value=1,
        ))

        # ── 数据库连接池 ──
        self.register(ConfigMetadata(
            key="CRDB_POOL_MIN_SIZE",
            category=ConfigCategory.DATABASE,
            reload_policy=ReloadPolicy.RESTART_REQUIRED,
            sensitivity=SensitivityLevel.INTERNAL,
            description="CRDB 连接池最小连接数(R36: 0=空闲时关闭所有连接,降低空载 RU)",
            default_value="0",
            env_var="CRDB_POOL_MIN_SIZE",
            services=ALL_READERS + ["db_writer"],
            validation_regex=r"^\d+$",
            min_value=0,
        ))
        self.register(ConfigMetadata(
            key="CRDB_POOL_MAX_SIZE",
            category=ConfigCategory.DATABASE,
            reload_policy=ReloadPolicy.RESTART_REQUIRED,
            sensitivity=SensitivityLevel.INTERNAL,
            description="CRDB 连接池最大连接数(R36: 业务 Bot ≤2,crdb_sync 可更高)",
            default_value="2",
            env_var="CRDB_POOL_MAX_SIZE",
            services=ALL_READERS + ["db_writer"],
            validation_regex=r"^\d+$",
            min_value=1,
            max_value=20,
        ))
        self.register(ConfigMetadata(
            key="CRDB_APPLICATION_NAME_PREFIX",
            category=ConfigCategory.DATABASE,
            reload_policy=ReloadPolicy.RESTART_REQUIRED,
            sensitivity=SensitivityLevel.INTERNAL,
            description="application_name 前缀,实际值为 f'{前缀}-{SERVICE_ROLE}'(如 tgjiema-up),按服务追踪 RU",
            default_value="tgjiema",
            env_var="CRDB_APPLICATION_NAME_PREFIX",
            services=ALL_READERS + ["db_writer"],
            validation_regex=r"^[a-zA-Z][a-zA-Z0-9_-]*$",
        ))

        # ── 缓存参数 ──
        self.register(ConfigMetadata(
            key="CACHE_USER_MAX_SIZE",
            category=ConfigCategory.M2_CONTROL,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="用户缓存最大条目",
            default_value="1000",
            env_var="CACHE_USER_MAX_SIZE",
            services=["up_bot", "dsp_bot"],
            validation_regex=r"^\d+$",
            min_value=10,
        ))
        self.register(ConfigMetadata(
            key="CACHE_USER_TTL",
            category=ConfigCategory.M2_CONTROL,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="用户缓存 TTL(秒)",
            default_value="10800",
            env_var="CACHE_USER_TTL",
            services=["up_bot", "dsp_bot"],
            validation_regex=r"^\d+$",
            min_value=60,
        ))
        self.register(ConfigMetadata(
            key="CACHE_FILE_MAX_SIZE",
            category=ConfigCategory.M2_CONTROL,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="文件缓存最大条目",
            default_value="1000",
            env_var="CACHE_FILE_MAX_SIZE",
            services=["up_bot", "dsp_bot"],
            validation_regex=r"^\d+$",
            min_value=10,
        ))
        self.register(ConfigMetadata(
            key="CACHE_FILE_TTL",
            category=ConfigCategory.M2_CONTROL,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="文件缓存 TTL(秒)",
            default_value="300",
            env_var="CACHE_FILE_TTL",
            services=["up_bot", "dsp_bot"],
            validation_regex=r"^\d+$",
            min_value=10,
        ))
        self.register(ConfigMetadata(
            key="CACHE_CONFIG_MAX_SIZE",
            category=ConfigCategory.M2_CONTROL,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="配置缓存最大条目",
            default_value="100",
            env_var="CACHE_CONFIG_MAX_SIZE",
            services=ALL_BOTS,
            validation_regex=r"^\d+$",
            min_value=10,
        ))
        self.register(ConfigMetadata(
            key="CACHE_CONFIG_TTL",
            category=ConfigCategory.M2_CONTROL,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="配置缓存 TTL(秒)",
            default_value="600",
            env_var="CACHE_CONFIG_TTL",
            services=ALL_BOTS,
            validation_regex=r"^\d+$",
            min_value=10,
        ))
        self.register(ConfigMetadata(
            key="CACHE_NEGATIVE_TTL",
            category=ConfigCategory.M2_CONTROL,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="负面缓存 TTL(秒)",
            default_value="60",
            env_var="CACHE_NEGATIVE_TTL",
            services=ALL_BOTS,
            validation_regex=r"^\d+$",
            min_value=1,
        ))
        self.register(ConfigMetadata(
            key="CACHE_REQUEST_COUNT_FLUSH",
            category=ConfigCategory.M2_CONTROL,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="请求计数刷新间隔(秒)",
            default_value="900",
            env_var="CACHE_REQUEST_COUNT_FLUSH",
            services=ALL_BOTS,
            validation_regex=r"^\d+$",
            min_value=10,
        ))
        self.register(ConfigMetadata(
            key="CACHE_DECODE_LOG_FLUSH",
            category=ConfigCategory.M2_CONTROL,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="解码日志刷新间隔(秒)",
            default_value="3600",
            env_var="CACHE_DECODE_LOG_FLUSH",
            services=["idx_bot"],
            validation_regex=r"^\d+$",
            min_value=60,
        ))

        # ── Admin Web 安全参数 ──
        self.register(ConfigMetadata(
            key="ADMIN_LOGIN_WINDOW",
            category=ConfigCategory.SECURITY,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="登录失败计数窗口(秒)",
            default_value="300",
            env_var="ADMIN_LOGIN_WINDOW",
            services=["admin_bot"],
            validation_regex=r"^\d+$",
            min_value=60,
        ))
        self.register(ConfigMetadata(
            key="ADMIN_LOGIN_MAX_FAIL",
            category=ConfigCategory.SECURITY,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="窗口内最大登录失败次数",
            default_value="5",
            env_var="ADMIN_LOGIN_MAX_FAIL",
            services=["admin_bot"],
            validation_regex=r"^\d+$",
            min_value=1,
            max_value=50,
        ))
        self.register(ConfigMetadata(
            key="ADMIN_COUNT_CACHE_TTL",
            category=ConfigCategory.M2_CONTROL,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="Admin Web count 缓存 TTL(秒)",
            default_value="60",
            env_var="ADMIN_COUNT_CACHE_TTL",
            services=["admin_bot"],
            validation_regex=r"^\d+$",
            min_value=1,
        ))
        self.register(ConfigMetadata(
            key="ADMIN_SEARCH_MAX_LENGTH",
            category=ConfigCategory.SECURITY,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="Admin Web 搜索输入最大长度",
            default_value="50",
            env_var="ADMIN_SEARCH_MAX_LENGTH",
            services=["admin_bot"],
            validation_regex=r"^\d+$",
            min_value=1,
            max_value=500,
        ))
        self.register(ConfigMetadata(
            key="ADMIN_PAGE_SIZE",
            category=ConfigCategory.M2_CONTROL,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="Admin Web 默认分页大小",
            default_value="20",
            env_var="ADMIN_PAGE_SIZE",
            services=["admin_bot"],
            validation_regex=r"^\d+$",
            min_value=1,
            max_value=200,
        ))
        self.register(ConfigMetadata(
            key="ADMIN_FILES_PAGE_SIZE",
            category=ConfigCategory.M2_CONTROL,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="Admin Web 文件列表分页大小",
            default_value="50",
            env_var="ADMIN_FILES_PAGE_SIZE",
            services=["admin_bot"],
            validation_regex=r"^\d+$",
            min_value=1,
            max_value=500,
        ))
        self.register(ConfigMetadata(
            key="CSRF_COOKIE_SECURE",
            category=ConfigCategory.SECURITY,
            reload_policy=ReloadPolicy.HOT_RELOAD,
            sensitivity=SensitivityLevel.INTERNAL,
            description="CSRF Cookie Secure 标志;部署 TLS 后设为 true",
            default_value="false",
            env_var="CSRF_COOKIE_SECURE",
            services=["admin_bot"],
            validation_regex=r"^(true|false|1|0)$",
        ))


# 模块级单例:导入即完成所有注册
config_registry = ConfigRegistry()


__all__ = [
    "ConfigCategory",
    "ReloadPolicy",
    "SensitivityLevel",
    "ConfigMetadata",
    "ConfigRegistry",
    "config_registry",
]
