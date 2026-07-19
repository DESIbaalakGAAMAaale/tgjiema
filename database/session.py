from __future__ import annotations

try:
    import orjson as json
except ImportError:
    import json
import asyncio
import time as _time
import re
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Optional

import asyncpg
from loguru import logger

# SQL 查询日志开关（默认关闭，开启用 ENABLE_SQL_LOG=1）
import os as _os
_SQL_LOG_ENABLED = _os.getenv("ENABLE_SQL_LOG", "0") == "1"


def _json_dumps(obj, **kwargs):
    """json.dumps compatible wrapper."""
    result = json.dumps(obj, **kwargs)
    if isinstance(result, bytes):
        return result.decode()
    return result

DDL_VERSION = 11  # R45: pending_uploads 部分索引 + decode_logs 7天保留

DDL_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        membership_level TEXT DEFAULT 'free',
        daily_decode_quota INTEGER DEFAULT 3,
        quota_used_today INTEGER DEFAULT 0,
        quota_date TEXT,
        can_upload INTEGER DEFAULT 0,
        external_decode_quota INTEGER DEFAULT 0,
        external_used_today INTEGER DEFAULT 0,
        external_quota_date TEXT,
        is_banned INTEGER DEFAULT 0,
        created_at TEXT,
        updated_at TEXT,
        deleted_at TEXT,
        is_tombstone INTEGER DEFAULT 0  -- R44 7.2: tombstone 标记列,使 _is_crdb_table_supports_soft_delete() 返回 True
    )""",
    """CREATE TABLE IF NOT EXISTS file_records (
        file_code TEXT PRIMARY KEY,
        uploader_id BIGINT,
        primary_channel_id BIGINT,
        primary_channel_msg_id BIGINT,
        file_types TEXT,
        backup_channel_msg_ids TEXT,
        batch_msg_ids TEXT,
        batch_file_meta TEXT,
        file_ids TEXT,
        status TEXT DEFAULT 'active',
        request_count INTEGER DEFAULT 0,
        create_time TEXT,
        expire_time TEXT,
        blocked_users JSONB DEFAULT '[]',
        deleted_at TEXT,
        is_tombstone INTEGER DEFAULT 0  -- R44 7.2: tombstone 标记列,使 _is_crdb_table_supports_soft_delete() 返回 True
    )""",
    """CREATE TABLE IF NOT EXISTS decode_logs (
        id SERIAL PRIMARY KEY,
        file_code TEXT,
        requester_id BIGINT,
        request_time TEXT,
        status TEXT DEFAULT 'queued',
        source_channel_id BIGINT
    )""",
    # idx_users_username/first_name 已删除（仅 LIKE '%...%' 前缀模糊查询，B-tree 索引无效）
    "CREATE INDEX IF NOT EXISTS idx_file_records_status ON file_records(status)",
    "CREATE INDEX IF NOT EXISTS idx_file_records_uploader ON file_records(uploader_id)",
    "CREATE INDEX IF NOT EXISTS idx_decode_logs_file_code ON decode_logs(file_code)",
    # idx_decode_logs_requester 已删除（无 WHERE requester_id 查询）
    "CREATE INDEX IF NOT EXISTS idx_decode_logs_request_time ON decode_logs(request_time)",
    # idx_file_records_msg_id 已删除（primary_channel_msg_id 仅做数据字段读取，无 WHERE 过滤）
    # ─── 增量同步索引：_sync_local_tables_loop 用 updated_at 过滤新记录 ───
    "CREATE INDEX IF NOT EXISTS idx_users_updated_at ON users(updated_at)",
    """CREATE TABLE IF NOT EXISTS pending_uploads (
        id SERIAL PRIMARY KEY,
        uploader_id BIGINT,
        primary_channel_id BIGINT,
        primary_channel_msg_id BIGINT,
        file_types TEXT,
        batch_msg_ids TEXT,
        batch_file_meta TEXT,
        status_msg_id BIGINT,
        created_at TEXT,
        processed INTEGER DEFAULT 0,
        claimed_at REAL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_pending_uploads_pending_created ON pending_uploads (created_at) WHERE processed = 0",
    # 已废弃(v2 环形冗余架构用 jobs 表替代 send_queue)
    """CREATE TABLE IF NOT EXISTS send_queue (
        id SERIAL PRIMARY KEY,
        target_user_id BIGINT,
        channel_id BIGINT,
        message_id BIGINT,
        file_code TEXT,
        task_type TEXT DEFAULT 'single',
        channel_msg_ids TEXT,
        batch_file_meta TEXT,
        created_at TEXT,
        processed INTEGER DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS backup_config (
        config_key TEXT PRIMARY KEY,
        config_value TEXT,
        updated_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS message_backups (
        main_msg_id BIGINT,
        backup_channel_id BIGINT,
        backed_msg_id BIGINT,
        backed_at TEXT,
        PRIMARY KEY (main_msg_id, backup_channel_id)
    )""",
    # ─── 环形冗余架构新表 ──────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS cells (
        slot_id TEXT PRIMARY KEY,
        channel_id BIGINT NOT NULL,
        status TEXT NOT NULL DEFAULT 'shadow1',
        next_active_chat_id BIGINT,
        prev_slot_id TEXT,
        demoted_to_channel_id BIGINT,
        account_name TEXT DEFAULT '',
        is_r100 INTEGER DEFAULT 0,
        last_heartbeat TEXT,
        last_synced_msg_id BIGINT DEFAULT 0,
        degrade_count INTEGER DEFAULT 0,
        file_count INTEGER DEFAULT 0,
        rotation_started_at TEXT,
        created_at TEXT,
        updated_at TEXT,
        deleted_at TEXT,
        is_tombstone INTEGER DEFAULT 0  -- R44 7.2: tombstone 标记列,使 _is_crdb_table_supports_soft_delete() 返回 True
    )""",
    """CREATE TABLE IF NOT EXISTS codes (
        code TEXT PRIMARY KEY,
        file_record_code TEXT,
        uploader_id BIGINT,
        file_types TEXT,
        batch_msg_ids TEXT,
        batch_file_meta TEXT,
        primary_channel_id BIGINT,
        status TEXT DEFAULT 'active',
        created_at TEXT,
        expire_time TEXT,
        deleted_at TEXT,
        is_tombstone INTEGER DEFAULT 0  -- R44 7.2: tombstone 标记列,使 _is_crdb_table_supports_soft_delete() 返回 True
    )""",
    """CREATE TABLE IF NOT EXISTS jobs (
        id SERIAL PRIMARY KEY,
        code TEXT NOT NULL,
        target_user_id BIGINT NOT NULL,
        storage_channel_id BIGINT,
        storage_msg_ids TEXT,
        batch_file_meta TEXT,
        task_type TEXT DEFAULT 'single',
        status TEXT DEFAULT 'pending',
        created_at TEXT,
        dispatched_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS rotate_log (
        id SERIAL PRIMARY KEY,
        timestamp TEXT NOT NULL,
        from_slot_id TEXT,
        to_slot_id TEXT,
        from_status TEXT,
        to_status TEXT,
        reason TEXT,
        triggered_by TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_jobs_pending ON jobs(status, created_at)",
    # ─── 备用池 + 轮转配置 ──────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS spare_pool (
        channel_id BIGINT PRIMARY KEY,
        account_name TEXT,
        is_used INTEGER DEFAULT 0,
        created_at TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_spare_pool_account ON spare_pool(account_name)",
    "CREATE INDEX IF NOT EXISTS idx_spare_pool_used ON spare_pool(is_used)",
    """CREATE TABLE IF NOT EXISTS rotation_config (
        config_key TEXT PRIMARY KEY,
        config_value TEXT,
        updated_at TEXT
    )""",
    # ─── 中继账号池（云端备份，与 SQLite relay_pool.db 双向同步）─────────────
    """CREATE TABLE IF NOT EXISTS relay_accounts (
        id           SERIAL PRIMARY KEY,
        api_id       BIGINT NOT NULL,
        api_hash     TEXT NOT NULL,
        phone        TEXT NOT NULL UNIQUE,
        is_active    INTEGER DEFAULT 1,
        created_at   TEXT DEFAULT (CAST(current_timestamp AS TEXT)),
        last_login_at TEXT
    )""",
    # idx_relay_accounts_phone 已删除（UNIQUE 约束自带索引，冗余）
    # ─── 外部码映射表（采集器写入，idx_bot 查询）─────────────────
    """CREATE TABLE IF NOT EXISTS external_code_mapping (
        external_code TEXT PRIMARY KEY,
        system_code TEXT NOT NULL,
        bot_username TEXT,
        created_at TEXT,
        updated_at TEXT
    )""",
    # idx_external_code_mapping_system / idx_external_code_mapping_bot 已删除（WHERE 从未使用）
    # ─── code_bot_mapping 表(代码前缀 → Bot 路由) ──────────────
    """CREATE TABLE IF NOT EXISTS code_bot_mapping (
        code_prefix TEXT PRIMARY KEY,
        bot_username TEXT NOT NULL,
        created_at TEXT DEFAULT ''
    )""",
]

MIGRATION_STATEMENTS = [
    "ALTER TABLE IF EXISTS file_records ADD COLUMN IF NOT EXISTS batch_msg_ids TEXT",
    "ALTER TABLE IF EXISTS file_records ADD COLUMN IF NOT EXISTS batch_file_meta TEXT",
    "ALTER TABLE IF EXISTS file_records ADD COLUMN IF NOT EXISTS file_ids TEXT",
    "ALTER TABLE IF EXISTS pending_uploads ADD COLUMN IF NOT EXISTS batch_file_meta TEXT",
    "ALTER TABLE IF EXISTS pending_uploads ADD COLUMN IF NOT EXISTS status_msg_id BIGINT",
    "ALTER TABLE IF EXISTS send_queue ADD COLUMN IF NOT EXISTS task_type TEXT DEFAULT 'single'",
    "ALTER TABLE IF EXISTS send_queue ADD COLUMN IF NOT EXISTS channel_msg_ids TEXT",
    "ALTER TABLE IF EXISTS send_queue ADD COLUMN IF NOT EXISTS batch_file_meta TEXT",
    "ALTER TABLE IF EXISTS codes ADD COLUMN IF NOT EXISTS note TEXT DEFAULT ''",
    # ─── jobs 表补充───────────────────────────────────────────────
    "ALTER TABLE IF EXISTS jobs ADD COLUMN IF NOT EXISTS protect_content BOOLEAN DEFAULT FALSE",
    # ─── 死信队列(Dead Letter Queue)──────────────────────────────
    "ALTER TABLE IF EXISTS jobs ADD COLUMN IF NOT EXISTS retry_count INTEGER DEFAULT 0",
    "ALTER TABLE IF EXISTS jobs ADD COLUMN IF NOT EXISTS dead_reason TEXT DEFAULT ''",
    "ALTER TABLE IF EXISTS jobs ADD COLUMN IF NOT EXISTS dead_retry_count INTEGER DEFAULT 0",
    "ALTER TABLE IF EXISTS jobs ADD COLUMN IF NOT EXISTS dead_retry BOOLEAN DEFAULT FALSE",
    "ALTER TABLE IF EXISTS jobs ADD COLUMN IF NOT EXISTS dead_retry_at TEXT DEFAULT ''",
    # R36 B0-1: 为 jobs 表添加结构化副本信息字段,使 ReplicaAwareResolver 成为真实投递主路径
    # group_id=拓扑组号(1-5), file_unique_id=Telegram 文件唯一标识, media_group_id=相册分组键
    "ALTER TABLE IF EXISTS jobs ADD COLUMN IF NOT EXISTS group_id INTEGER DEFAULT 0",
    "ALTER TABLE IF EXISTS jobs ADD COLUMN IF NOT EXISTS file_unique_id TEXT DEFAULT ''",
    "ALTER TABLE IF EXISTS jobs ADD COLUMN IF NOT EXISTS media_group_id TEXT DEFAULT ''",
    "ALTER TABLE IF EXISTS pending_uploads ADD COLUMN IF NOT EXISTS note TEXT DEFAULT ''",
    "ALTER TABLE IF EXISTS pending_uploads ADD COLUMN IF NOT EXISTS protect_content BOOLEAN DEFAULT FALSE",
    "ALTER TABLE IF EXISTS pending_uploads ADD COLUMN IF NOT EXISTS file_ttl_days INTEGER DEFAULT 0",
    # I-1: claimed_at 用于 at-least-once 语义，认领后崩溃的记录可被回收重领
    # CRDB 不支持 ADD COLUMN IF NOT EXISTS，用不带 IF NOT EXISTS 的语法（重复执行由 try/except 兼容）
    "ALTER TABLE IF EXISTS pending_uploads ADD COLUMN claimed_at REAL DEFAULT 0",
    "ALTER TABLE IF EXISTS file_records ADD COLUMN IF NOT EXISTS note TEXT DEFAULT ''",
    "ALTER TABLE IF EXISTS file_records ADD COLUMN IF NOT EXISTS protect_content BOOLEAN DEFAULT FALSE",
    "ALTER TABLE IF EXISTS file_records ADD COLUMN IF NOT EXISTS blocked_users JSONB DEFAULT '[]'",
    "ALTER TABLE IF EXISTS file_records ADD COLUMN updated_at TEXT",
    "ALTER TABLE IF EXISTS file_records ADD COLUMN file_ttl_days INTEGER DEFAULT 0",
    # 取件码访问次数限制(0=不限制)，参考 file_ttl_days 迁移写法(CRDB 不支持 ADD COLUMN IF NOT EXISTS,用 try/except 兼容)
    "ALTER TABLE IF EXISTS file_records ADD COLUMN max_requests INTEGER DEFAULT 0",
    # 合集码字段: is_collection 标记是否为合集(0=普通码,1=合集码),collection_codes 存储合集包含的文件码列表(JSON 字符串)
    "ALTER TABLE IF EXISTS file_records ADD COLUMN is_collection INTEGER DEFAULT 0",
    "ALTER TABLE IF EXISTS file_records ADD COLUMN collection_codes TEXT DEFAULT '[]'",
    "CREATE INDEX IF NOT EXISTS idx_file_records_updated_at ON file_records(updated_at)",
    "ALTER TABLE IF EXISTS codes ADD COLUMN updated_at TEXT",
    "CREATE INDEX IF NOT EXISTS idx_codes_updated_at ON codes(updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_external_code_mapping_updated_at ON external_code_mapping(updated_at)",
    # PRE-01: cells 表补充 demoted_to_channel_id 字段（CRDB 不支持 ADD COLUMN IF NOT EXISTS，用 try/except 兼容已存在）
    "ALTER TABLE IF EXISTS cells ADD COLUMN demoted_to_channel_id BIGINT",
    "ALTER TABLE IF EXISTS cells ADD COLUMN prev_slot_id TEXT",
    # ─── 索引清理：删除冗余/未使用的索引（减少 UPDATE 维护开销）─────────────
    "DROP INDEX IF EXISTS idx_cells_channel",
    "DROP INDEX IF EXISTS idx_cells_status",
    "DROP INDEX IF EXISTS idx_cells_next_active",
    "DROP INDEX IF EXISTS idx_codes_expire_time",
    "DROP INDEX IF EXISTS idx_codes_status",
    "DROP INDEX IF EXISTS idx_codes_file_record_code",
    "DROP INDEX IF EXISTS idx_rotate_log_timestamp",
    "DROP INDEX IF EXISTS idx_relay_accounts_phone",
    "DROP INDEX IF EXISTS idx_external_code_mapping_system",
    "DROP INDEX IF EXISTS idx_external_code_mapping_bot",
    # 第二批索引清理：无用索引，减少写入维护开销
    "DROP INDEX IF EXISTS idx_users_username",
    "DROP INDEX IF EXISTS idx_users_first_name",
    "DROP INDEX IF EXISTS idx_decode_logs_requester",
    "DROP INDEX IF EXISTS idx_file_records_msg_id",
    # R37 P1-5: 权威表增加 deleted_at tombstone 列(增量备份可捕捉软删除)
    # 用于解决仅靠 updated_at 增量 watermark 无法捕捉硬删除/状态过滤的问题。
    # 软删除时同时设置 status='deleted' 和 deleted_at=<timestamp>,
    # 备份时通过 deleted_at > watermark 条件捕捉删除事件。
    "ALTER TABLE IF EXISTS users ADD COLUMN IF NOT EXISTS deleted_at TEXT",
    "ALTER TABLE IF EXISTS file_records ADD COLUMN IF NOT EXISTS deleted_at TEXT",
    "ALTER TABLE IF EXISTS codes ADD COLUMN IF NOT EXISTS deleted_at TEXT",
    "ALTER TABLE IF EXISTS cells ADD COLUMN IF NOT EXISTS deleted_at TEXT",
    # R44 7.2: 添加 is_tombstone 列,使 _is_crdb_table_supports_soft_delete() 返回 True,
    # tombstone 走 soft_delete 路径(UPDATE deleted_at + is_tombstone=1)而非 DELETE fallback
    "ALTER TABLE IF EXISTS users ADD COLUMN IF NOT EXISTS is_tombstone INTEGER DEFAULT 0",
    "ALTER TABLE IF EXISTS file_records ADD COLUMN IF NOT EXISTS is_tombstone INTEGER DEFAULT 0",
    "ALTER TABLE IF EXISTS codes ADD COLUMN IF NOT EXISTS is_tombstone INTEGER DEFAULT 0",
    "ALTER TABLE IF EXISTS cells ADD COLUMN IF NOT EXISTS is_tombstone INTEGER DEFAULT 0",
    # R40 P1-11: users 表增加 ban_expires_at 列(临时封禁到期自动解封依据)
    # ban_user 写入,check_user_banned/cleanup_expired_bans 读取判定。
    # NULL 表示永久封禁;非空 ISO 时间字符串表示临时封禁到期时间。
    "ALTER TABLE IF EXISTS users ADD COLUMN IF NOT EXISTS ban_expires_at TEXT",
    # TTL 设置不在此处执行 — ADD COLUMN 是异步 schema change，
    # 紧跟 TTL 修改会报 "cannot modify TTL settings while another schema change
    # is being processed"。改为在下方单独循环中带等待执行。
    # ─── CRDB RU 优化: 部分索引改造 ──────────────────────────────
    # 评估报告 4.3: pending_uploads(processed) 是低基数二值字段,
    # 普通索引会长期收录已处理记录。改为部分索引,仅收录 processed=0 的待处理记录。
    "DROP INDEX IF EXISTS idx_pending_uploads_unprocessed",
    "CREATE INDEX IF NOT EXISTS idx_pending_uploads_pending_created "
    "ON pending_uploads (created_at) WHERE processed = 0",
    "CREATE INDEX IF NOT EXISTS idx_pending_uploads_reclaim "
    "ON pending_uploads (claimed_at, created_at) WHERE processed = 0",
    # 评估报告 5.1: decode_logs 7 天短期保留,保留 request_time 索引用于过期清理
    # (idx_decode_logs_request_time 已在 DDL_STATEMENTS 中创建,这里无需重复)
]


class UpdateResult:
    def __init__(self, matched_count: int = 0):
        self.matched_count = matched_count


class CockroachDBClient:
    def __init__(self):
        self._pool: Optional[asyncpg.Pool] = None
        self._url: str = ""

    def configure(self, url: str):
        self._url = url

    async def connect_runtime_only(self):
        """R37 P1-8: runtime_client 专用连接 — 只创建连接池 + 初始化 SQLite 缓存。

        职责边界(P1-8 拆分):
          - 不执行 DDL_STATEMENTS / MIGRATION_STATEMENTS(migration_runner 专属)
          - 不执行 TTL 修改(migration_runner 专属)
          - 不写入 ddl_version(migration_runner 专属)
          - 不预填充 cells / 全表 bootstrap(bootstrap_runner 专属)

        适用场景:
          - 所有业务 Bot / crdb_sync / db_writer / admin / db_backup 的运行时连接
          - migration_runner 调用此方法获取连接池后执行 DDL
          - bootstrap_runner 调用此方法获取连接池后全量加载

        若 DDL 未执行(migration 未运行),业务查询会失败 — 由部署流程保证
        migration oneshot 先于业务 Bot 启动(systemd After= 依赖)。
        """
        # R36 §6.4.1: min_size=0 允许空闲时关闭所有连接(不再强制 max(1, ...))
        # R36 §6.4.2: application_name 用于按服务追踪 RU 消耗
        from config import settings as _settings
        # 实际 application_name: tgjiema-<role>(如 tgjiema-up / tgjiema-idx)
        role = _settings.SERVICE_ROLE or "default"
        app_name = f"{_settings.CRDB_APPLICATION_NAME_PREFIX}-{role}"

        # 初始化连接时:关闭 follower_reads + 设置 application_name(按服务追踪 RU)
        async def _init_conn(conn):
            await conn.execute("SET default_transaction_use_follower_reads = off")
            await conn.execute("SET application_name = $1", app_name)

        min_size = _settings.CRDB_POOL_MIN_SIZE  # R36: 不再强制 max(1, ...)
        # R37 P0-3: crdb_sync 角色放宽上限(独占同步需要更多连接);
        # 其他业务 Bot 仍保持 ≤2,降低空载连接
        if role == "crdb_sync":
            max_size = min(_settings.CRDB_POOL_MAX_SIZE, 5)  # crdb_sync ≤5
        else:
            max_size = min(_settings.CRDB_POOL_MAX_SIZE, 2)  # 业务 Bot ≤2
        # R64 P1-10: 强制 min_size=0(空载 0 连接),并配置 pool_recycle 空闲回收
        # 防御性:即使环境变量误设为 >0 也强制归零,确保空载期不保持 CRDB 连接
        min_size = 0
        # asyncpg create_pool 不直接支持 pool_recycle,通过 max_inactive_connection_lifetime
        # 传递(asyncpg 0.27+ 支持),实现空闲连接生命周期管理
        # 注意:getattr(MagicMock, ...) 返回 MagicMock 而非 default,需显式 isinstance 检查
        _recycle_raw = getattr(_settings, "CRDB_POOL_RECYCLE_SECONDS", 60)
        recycle_seconds = int(_recycle_raw) if isinstance(_recycle_raw, (int, float)) else 60
        _null_raw = getattr(_settings, "CRDB_POOL_NULL_MODE", False)
        null_mode = bool(_null_raw) if isinstance(_null_raw, bool) else False
        # NullPool 模式:max_size=1,每次用完即关,适合极端 0 RU 空载场景
        if null_mode:
            max_size = 1
            recycle_seconds = 0  # 立即回收
        pool_kwargs: dict = {
            "min_size": min_size,
            "max_size": max_size,
            "statement_cache_size": 256,
            "init": _init_conn,
            "server_settings": {"application_name": app_name},  # 连接级别也设置
        }
        # asyncpg 0.27+ 支持 max_inactive_connection_lifetime(秒)
        # 旧版本忽略此参数(由 try/except 兜底)
        if recycle_seconds > 0:
            pool_kwargs["max_inactive_connection_lifetime"] = float(recycle_seconds)
        try:
            self._pool = await asyncpg.create_pool(self._url, **pool_kwargs)
        except TypeError:
            # 旧版 asyncpg 不支持 max_inactive_connection_lifetime,降级为标准参数
            pool_kwargs.pop("max_inactive_connection_lifetime", None)
            self._pool = await asyncpg.create_pool(self._url, **pool_kwargs)

        # ─── SQLite 缓存备份：初始化并恢复内存缓存───
        from .cache_store import get_cache_store
        from .cache import load_cache_from_disk

        store = get_cache_store()
        await store.init()
        await load_cache_from_disk()

        logger.info(
            f"[DB] R37 P1-8 / R64 P1-10: runtime_client 已连接(role={role}, "
            f"pool={min_size}-{max_size}, recycle={recycle_seconds}s, null_mode={null_mode}, "
            f"app_name={app_name})。"
            f"不执行 DDL/bootstrap(由 migration_runner / bootstrap_runner 专属负责)。"
        )

    async def connect(self):
        """R37 P1-8: 兼容旧调用方 — 等价于 connect_runtime_only()。

        历史上 connect() 同时执行 DDL + bootstrap + runtime 连接,
        违反 P1-8 职责拆分。现在 connect() 仅做 runtime 连接。

        若需在本地开发时自动执行 DDL + bootstrap(避免手动调用 migration),
        设置环境变量 DB_AUTO_MIGRATE=true(init_db_legacy 会自动触发)。
        """
        await self.connect_runtime_only()

        # R37 P1-8: 本地开发便利模式(DB_AUTO_MIGRATE=true 自动执行 DDL + bootstrap)
        # 生产环境必须由 migration oneshot 服务显式调用,不依赖此分支
        import os as _os_mod
        if _os_mod.environ.get("DB_AUTO_MIGRATE", "").lower() in ("true", "1", "yes"):
            logger.warning(
                "[DB] R37 P1-8: DB_AUTO_MIGRATE=true,本地开发模式自动执行 "
                "DDL + bootstrap(生产环境应通过 migration_runner 显式执行)"
            )
            await _legacy_run_ddl_and_bootstrap(self)

    async def close(self):
        if self._pool:
            try:
                # 加 10 秒超时，避免 CRDB 连接池关闭卡住导致进程无法退出
                await asyncio.wait_for(self._pool.close(), timeout=10)
            except asyncio.TimeoutError:
                logger.warning("[DB] CRDB 连接池关闭超时(10s)，强制放弃")
            except Exception as e:
                logger.warning(f"[DB] CRDB 连接池关闭异常: {e}")
            finally:
                self._pool = None

    async def execute(self, sql: str, params: list = None):
        async with self._pool.acquire() as conn:
            await conn.execute(sql, *(params or []))

    async def fetch(self, sql: str, params: list = None) -> list:
        """公开的原始 SQL 查询方法,返回多行记录(列表)。
        供 db_backup 等模块使用,避免外部直接访问 _pool 私有属性。
        """
        if self._pool is None:
            raise RuntimeError("数据库连接池未初始化")
        async with self._pool.acquire() as conn:
            return await conn.fetch(sql, *(params or []))

    @property
    def is_connected(self) -> bool:
        """连接池是否已初始化。"""
        return self._pool is not None

    @asynccontextmanager
    async def transaction(self):
        """获取一个带事务的连接,用于需要原子性的多步操作
        用法:
            async with db.transaction() as conn:
                await conn.execute(...)
                await conn.execute(...)
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                yield conn


_client: CockroachDBClient = CockroachDBClient()

# 后台异步同步任务的引用集合，防止 GC 回收 fire-and-forget 任务
_pending_sync_tasks: set[asyncio.Task] = set()


async def _legacy_run_ddl_and_bootstrap(client: CockroachDBClient):
    """R37 P1-8: 旧版 DDL + bootstrap 执行逻辑(仅 DB_AUTO_MIGRATE=true 时触发)。

    本函数保留了历史上 connect() 中的 DDL + bootstrap 逻辑,
    仅供本地开发便利模式使用,生产环境应通过 migration_runner + bootstrap_runner 显式调用。

    包含:
      1. DDL 版本检查(优先 SQLite kv_store,CRDB 兜底)
      2. DDL_STATEMENTS 执行(CREATE TABLE / CREATE INDEX)
      3. MIGRATION_STATEMENTS 执行(ALTER TABLE / DROP INDEX)
      4. CRDB TTL 设置(decode_logs / jobs 100 年过期)
      5. ddl_version 写入 CRDB rotation_config + SQLite kv_store
      6. cells 预填充到 SQLite
      7. 全表缓存 bootstrap(users / codes / file_records / external_code_mapping)
    """
    from .cache_store import get_cache_store
    store = get_cache_store()

    # ─── DDL 版本检查：优先用 SQLite 缓存，避免每次 init_db 查 CRDB ───
    need_ddl = True
    try:
        # 优先级 1: SQLite 本地缓存（0 CRDB RU）
        ddl_version = await store.get_kv("ddl_version")
        if ddl_version == str(DDL_VERSION):
            need_ddl = False
        else:
            logger.info(f"DDL 版本变更(SQLite): {ddl_version} → {DDL_VERSION}，执行升级")
    except Exception as e:
        logger.debug(f"[DB] DDL版本SQLite检查跳过: {e}")

    if need_ddl:
        # 优先级 2: CRDB 兜底查询（仅 SQLite 无缓存时）
        try:
            current_version = await client._pool.fetchval(
                "SELECT config_value FROM rotation_config WHERE config_key = 'ddl_version'"
            )
            if current_version == str(DDL_VERSION):
                need_ddl = False
                # 回填 SQLite 缓存
                await store.set_kv("ddl_version", str(DDL_VERSION))
                logger.info(f"DDL 版本 {DDL_VERSION} 已是最新，跳过（CRDB 兜底）")
            else:
                logger.info(f"DDL 版本变更(CRDB): {current_version} → {DDL_VERSION}，执行升级")
        except Exception:
            logger.info("首次运行或 rotation_config 表不存在，执行 DDL 初始化")

    if need_ddl:
        try:
            for sql in DDL_STATEMENTS:
                try:
                    await client.execute(sql)
                except Exception as e:
                    logger.warning(f"[DB] DDL SQL 执行失败（可忽略）：{e}")
            for sql in MIGRATION_STATEMENTS:
                try:
                    await client.execute(sql)
                except Exception as e:
                    err_msg = str(e).lower()
                    # 只忽略"列已存在"或"关系已存在"错误，这些是预期的（因为没有 IF NOT EXISTS）
                    if "already exists" in err_msg or "duplicate" in err_msg:
                        logger.warning(f"[DB] 迁移 SQL 执行失败（可忽略，已存在）：{sql[:60]}... → {e}")
                    else:
                        logger.error(f"[DB] 迁移 SQL 执行失败（严重）：{sql} → {e}")
                        raise
            # ─── CRDB 行级 TTL 已废弃：用 SET @yearly + 100年过期 彻底禁用 ───
            # 等待前面的 ADD COLUMN 等 schema change 完成，否则 CRDB 报
            # "cannot modify TTL settings while another schema change is being processed"
            await asyncio.sleep(3)
            for ttl_sql in (
                "ALTER TABLE decode_logs SET (ttl_expiration_expression = 'CAST(request_time AS TIMESTAMPTZ) + INTERVAL ''100 years''', ttl_job_cron = '@yearly')",
                "ALTER TABLE jobs SET (ttl_expiration_expression = 'CAST(created_at AS TIMESTAMPTZ) + INTERVAL ''100 years''', ttl_job_cron = '@yearly')",
            ):
                for attempt in range(3):
                    try:
                        await client.execute(ttl_sql)
                        break
                    except Exception as e:
                        if "another schema change" in str(e).lower() and attempt < 2:
                            logger.warning(f"[DB] TTL 设置等待 schema change 完成，重试 {attempt + 1}/3: {e}")
                            await asyncio.sleep(5)
                        else:
                            logger.warning(f"[DB] TTL设置失败: {e}")
                            break
            await client.execute(
                "UPSERT INTO rotation_config (config_key, config_value) VALUES ('ddl_version', $1)",
                str(DDL_VERSION),
            )
        except Exception:
            logger.exception("[DB] DDL 迁移整体失败")
            raise
        # 写入 SQLite 缓存版本号（后续启动 0 CRDB RU）
        await store.set_kv("ddl_version", str(DDL_VERSION))
        logger.info(f"DDL 升级完成，版本 {DDL_VERSION}")

    # ─── 启动时预填充 cells 快照到 SQLite ───
    # 避免 Mon Bot 首次运行时回退到 CRDB（SELECT * FROM cells）
    try:
        cells = await store.get_all_cells_local()
        if not cells:
            snap_cells, _ = await store.load_cells_snapshot()
            if snap_cells:
                await store.bulk_upsert_cells_local(snap_cells)
                cells = snap_cells
        if not cells:
            col = D1Collection("cells")
            all_cells = await col.find({}, projection=[
                "slot_id", "channel_id", "status", "next_active_chat_id",
                "prev_slot_id", "account_name", "is_r100",
                "file_count", "rotation_started_at", "last_heartbeat",
            ])
            if all_cells:
                await store.bulk_upsert_cells_local(all_cells)
                logger.info(f"[DB] 预填充 cells 到本地 SQLite: {len(all_cells)} 条")
    except Exception as e:
        logger.debug(f"[DB] cells预填充SQLite失败: {e}")

    # ─── Phase 3: 全表缓存热路径到 SQLite（0 CRDB RU）─────
    # 启动时从 CRDB 全量加载 users / codes / file_records / external_code_mapping
    # 之后所有读操作走 SQLite，写操作双写+异步批量同步到 CRDB
    try:
        # 检查是否已经存在完整缓存（根据表行数判断）
        from database.cache_store import get_cache_store
        store = get_cache_store()

        # 1. file_records: 跳过如果已有（已bootstrap）
        fr_count = await store._db.execute_fetchall("SELECT COUNT(*) FROM file_records_local")
        if fr_count[0][0] == 0:
            fr_col = get_file_records_col()
            all_fr = await fr_col.find({}, projection=[
                "file_code", "uploader_id", "primary_channel_id", "primary_channel_msg_id",
                "file_types", "backup_channel_msg_ids", "batch_msg_ids", "batch_file_meta",
                "file_ids", "status", "request_count", "protect_content", "file_ttl_days",
                "note", "expire_time", "blocked_users", "create_time", "updated_at",
                "max_requests", "is_collection", "collection_codes",
            ])
            if all_fr:
                await store.bootstrap_file_records(all_fr)
                logger.info(f"[DB] 预填充 file_records 到本地 SQLite: {len(all_fr)} 条")

        # 2. codes: 跳过如果已有
        codes_count = await store._db.execute_fetchall("SELECT COUNT(*) FROM codes_local")
        if codes_count[0][0] == 0:
            codes_col = get_codes_col()
            all_codes = await codes_col.find({}, projection=[
                "code", "file_record_code", "uploader_id", "file_types",
                "batch_msg_ids", "batch_file_meta", "primary_channel_id",
                "status", "created_at", "expire_time", "note",
            ])
            if all_codes:
                await store.bootstrap_codes(all_codes)
                logger.info(f"[DB] 预填充 codes 到本地 SQLite: {len(all_codes)} 条")

        # 3. users: 跳过如果已有
        users_count = await store._db.execute_fetchall("SELECT COUNT(*) FROM users_local")
        if users_count[0][0] == 0:
            users_col = get_users_col()
            all_users = await users_col.find({}, projection=[
                "user_id", "username", "first_name", "membership_level",
                "daily_decode_quota", "quota_used_today", "quota_date",
                "can_upload", "external_decode_quota", "external_used_today",
                "external_quota_date", "is_banned", "created_at", "updated_at",
            ])
            if all_users:
                await store.bootstrap_users(all_users)
                logger.info(f"[DB] 预填充 users 到本地 SQLite: {len(all_users)} 条")

        # 4. external_code_mapping: 跳过如果已有
        ec_count = await store._db.execute_fetchall("SELECT COUNT(*) FROM external_code_mapping_local")
        if ec_count[0][0] == 0:
            ec_col = get_external_code_mapping_col()
            all_ec = await ec_col.find({}, projection=[
                "external_code", "system_code", "bot_username", "created_at", "updated_at",
            ])
            if all_ec:
                await store.bootstrap_external_mappings(all_ec)
                logger.info(f"[DB] 预填充 external_code_mapping 到本地 SQLite: {len(all_ec)} 条")

    except Exception as e:
        logger.warning(f"[DB] 预填充热表到本地SQLite失败（可忽略）: {e}")


async def init_db():
    """R37 P1-8: 初始化数据库(runtime_client 模式)。

    本函数只做:
      1. 配置 CRDB URL
      2. 调用 connect_runtime_only() 创建连接池 + 初始化 SQLite 缓存

    本函数不再做(P1-8 拆分):
      - DDL / MIGRATION 执行(由 services.migration_runner 专属)
      - TTL 修改(由 services.migration_runner 专属)
      - ddl_version 写入(由 services.migration_runner 专属)
      - cells / 全表 bootstrap(由 services.bootstrap_runner 专属)

    本地开发模式:
      设置 DB_AUTO_MIGRATE=true 环境变量,connect() 会自动调用
      _legacy_run_ddl_and_bootstrap() 完成迁移和 bootstrap。

    生产部署:
      systemd migration oneshot 服务先执行 services.migration_runner.run_migration(),
      (可选)bootstrap oneshot 服务执行 services.bootstrap_runner.run_bootstrap(),
      然后业务 Bot 调用 init_db()(仅 runtime 连接)。
    """
    from config import settings as _settings
    _client.configure(_settings.COCKROACHDB_URL)
    await _client.connect()


async def init_db_with_migration():
    """R37 P1-8: 迁移 + 初始化(本地开发便利函数)。

    等价于:
      1. await services.migration_runner.run_migration()
      2. await services.bootstrap_runner.run_bootstrap()
      3. await init_db()

    本地开发可用此函数一次性完成所有初始化。
    生产环境应通过独立服务显式调用各阶段。
    """
    from services.migration_runner import run_migration
    from services.bootstrap_runner import run_bootstrap
    await run_migration()
    await run_bootstrap()
    await init_db()


async def close_db():
    # 先关闭 SQLite 缓存（释放 WAL 锁），避免后续进程打开同一文件被阻塞
    try:
        from .cache_store import get_cache_store
        await get_cache_store().close()
    except Exception as e:
        logger.debug(f"[DB] 关闭 CacheStore 失败（可忽略）: {e}")
    await _client.close()


def _row_to_dict(record) -> dict:
    result = {}
    for col in record.keys():
        val = record[col]
        if col in (
            "user_id", "primary_channel_id", "primary_channel_msg_id",
            "requester_id", "source_channel_id", "request_count", "id",
            "daily_decode_quota", "quota_used_today", "uploader_id",
            "external_decode_quota", "external_used_today", "target_user_id",
            "channel_id", "message_id", "cnt", "status_msg_id",
            "max_requests", "is_collection",
        ):
            result[col] = int(val) if val is not None else 0
        elif col in ("can_upload", "is_banned"):
            result[col] = bool(val)
        elif col in ("file_types", "backup_channel_msg_ids", "batch_file_meta"):
            if val is None or val == "":
                # 返回正确的空类型，避免下游代码收到空字符串后 json.loads("") 失败
                if col == "file_types":
                    result[col] = {}
                else:
                    result[col] = []
                logger.debug(f"[_row_to_dict] {col} 为空/None, 返回空{type(result[col]).__name__}")
            else:
                try:
                    parsed = json.loads(val)
                    # 重建 dict/list：强制 CPython 重新计算 hash 并重建内部哈希表
                    # orjson(C 扩展)反序列化的 dict 可能存在 get() 查找异常(items 能遍历但 get 返回默认值)
                    if isinstance(parsed, dict):
                        result[col] = {str(k): v for k, v in parsed.items()}
                    elif isinstance(parsed, list):
                        result[col] = list(parsed)
                    else:
                        result[col] = parsed
                    logger.debug(f"[_row_to_dict] {col} JSON解析成功: type={type(result[col]).__name__}, value={result[col]!r}")
                except (json.JSONDecodeError, TypeError) as e:
                    # 如果已经是预期类型（如 asyncpg 直接返回了 dict/list），同样重建
                    if col == "file_types" and isinstance(val, dict):
                        result[col] = {str(k): v for k, v in val.items()}
                    elif col in ("backup_channel_msg_ids", "batch_file_meta") and isinstance(val, list):
                        result[col] = list(val)
                    else:
                        result[col] = val
                    logger.warning(f"[_row_to_dict] {col} JSON解析失败，使用原始值: type={type(val).__name__}, value={val!r}, error={e}")
        elif col == "blocked_users":
            if val is None or val == "" or val == "null":
                result[col] = []
            else:
                try:
                    result[col] = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    result[col] = []
        elif col == "batch_msg_ids":
            result[col] = str(val) if val else ""
        elif col in (
            "created_at", "updated_at", "create_time", "request_time",
            "expire_time", "quota_date", "external_quota_date",
        ):
            if val:
                try:
                    result[col] = datetime.fromisoformat(val)
                except (ValueError, TypeError):
                    result[col] = val
            else:
                result[col] = None
        elif col == "processed":
            result[col] = int(val) if val is not None else 0
        else:
            result[col] = val
    return result


def _safe_str(val: Any):
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val
    if isinstance(val, (list, dict)):
        return _json_dumps(val, default=str)
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, bytes):
        return val.decode()
    return str(val)


def _escape_like(value: str) -> str:
    r"""转义 LIKE 通配符: \ → \\, % → \%, _ → \_"""
    if not isinstance(value, str):
        value = str(value)
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _validate_identifier(name: str) -> str:
    """验证标识符（列名/表名）仅包含安全字符，防止 SQL 注入。

    虽然当前所有列名均来自代码常量，但作为安全加固措施，
    运行时校验确保不会有意外的动态拼接进入 SQL。
    """
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return name


class D1Collection:
    def __init__(self, table: str):
        self.table = table

    async def _query(self, sql: str, params: list = None) -> list[dict]:
        async with _client._pool.acquire() as conn:
            records = await conn.fetch(sql, *(params or []))
            if _SQL_LOG_ENABLED:
                logger.info(f"[SQL_QUERY] {self.table}: {sql[:200]}")
            return [_row_to_dict(r) for r in records]

    async def _execute(self, sql: str, params: list = None) -> int:
        async with _client._pool.acquire() as conn:
            await conn.execute(sql, *(params or []))
            if _SQL_LOG_ENABLED:
                logger.info(f"[SQL_EXEC] {self.table}: {sql[:200]}")
            return 1

    async def execute_raw(self, sql: str, params: list = None) -> int:
        """公开的原始 SQL 执行方法，用于批量操作（如 CASE WHEN UPDATE）"""
        return await self._execute(sql, params)

    async def find_one(self, query: dict, projection: list[str] | None = None) -> Optional[dict]:
        params = []
        where_parts = []
        for k, v in query.items():
            _validate_identifier(k)
            if isinstance(v, dict):
                for op, val in v.items():
                    if op == "$gte":
                        where_parts.append(f"{k} >= ${len(params) + 1}")
                        params.append(val)
                    elif op == "$lte":
                        where_parts.append(f"{k} <= ${len(params) + 1}")
                        params.append(val)
                    elif op == "$gt":
                        where_parts.append(f"{k} > ${len(params) + 1}")
                        params.append(val)
                    elif op == "$lt":
                        where_parts.append(f"{k} < ${len(params) + 1}")
                        params.append(val)
                    elif op == "$ne":
                        where_parts.append(f"{k} != ${len(params) + 1}")
                        params.append(val)
                    elif op == "$in":
                        # 空列表 $in: 匹配 nothing（与 MongoDB 语义一致），避免生成 `IN ()` 语法错误
                        if not val:
                            where_parts.append("FALSE")
                        else:
                            placeholders = [f"${len(params) + j + 1}" for j in range(len(val))]
                            params.extend(val)
                            where_parts.append(f"{k} IN ({', '.join(placeholders)})")
                continue
            where_parts.append(f"{k} = ${len(params) + 1}")
            params.append(v)
        # projection: 指定只查询需要的列，减少 IO 和 RU 消耗
        if projection:
            for col in projection:
                _validate_identifier(col)
            cols = ", ".join(projection)
        else:
            cols = "*"
        sql = f"SELECT {cols} FROM {_validate_identifier(self.table)}"
        if where_parts:
            sql += " WHERE " + " AND ".join(where_parts)
        sql += " LIMIT 1"
        rows = await self._query(sql, params)
        return rows[0] if rows else None

    async def insert_one(self, doc: dict) -> dict:
        # P3: 校验表名防注入(与 find_one 一致)
        _validate_identifier(self.table)
        keys = list(doc.keys())
        # P3: 校验列名防注入
        for k in keys:
            _validate_identifier(k)
        params = [_safe_str(v) for v in doc.values()]
        placeholders = [f"${i + 1}" for i in range(len(params))]
        sql = f"INSERT INTO {self.table} ({', '.join(keys)}) VALUES ({', '.join(placeholders)})"
        await self._execute(sql, params)
        return doc

    async def insert_many(self, docs: list[dict]) -> list[dict]:
        """批量插入文档"""
        if not docs:
            return []
        # P3: 校验表名防注入
        _validate_identifier(self.table)
        # P3: 取所有 doc 键的并集,避免不同键集的 doc 导致值/列错位
        all_keys: list[str] = []
        seen_keys: set[str] = set()
        for doc in docs:
            for k in doc.keys():
                if k not in seen_keys:
                    _validate_identifier(k)
                    all_keys.append(k)
                    seen_keys.add(k)
        placeholders_list = []
        all_params = []
        for doc in docs:
            # 按 all_keys 顺序取值,缺失的键用 NULL
            row_params = [_safe_str(doc.get(k)) for k in all_keys]
            all_params.extend(row_params)
            start = len(all_params) - len(row_params) + 1
            placeholders_list.append(
                "(" + ", ".join([f"${start + i}" for i in range(len(row_params))]) + ")"
            )
        sql = f"INSERT INTO {self.table} ({', '.join(all_keys)}) VALUES {', '.join(placeholders_list)}"
        await self._execute(sql, all_params)
        return docs

    async def update_one(self, query: dict, update: dict, **kwargs) -> UpdateResult:
        all_params = []

        set_parts = []
        if "$set" in update:
            for k, v in update["$set"].items():
                _validate_identifier(k)
                set_parts.append(f"{k} = ${len(all_params) + 1}")
                all_params.append(_safe_str(v))
        if "$inc" in update:
            for k, v in update["$inc"].items():
                _validate_identifier(k)
                set_parts.append(f"{k} = {k} + ${len(all_params) + 1}")
                all_params.append(int(v))
        if "$push" in update:
            for k, v in update["$push"].items():
                _validate_identifier(k)
                val_json = _json_dumps(v, default=str)
                set_parts.append(
                    f"{k} = CASE WHEN {k} IS NULL OR {k} = '' "
                    f"THEN jsonb_build_array(${len(all_params) + 1}::jsonb) "
                    f"ELSE {k}::jsonb || jsonb_build_array(${len(all_params) + 2}::jsonb) END"
                )
                all_params.append(val_json)
                all_params.append(val_json)
        if "$addToSet" in update:
            # P2-5/F-L4: 去重追加,用 jsonb @> 包含检查避免重复入列
            for k, v in update["$addToSet"].items():
                _validate_identifier(k)
                val_json = _json_dumps(v, default=str)
                set_parts.append(
                    f"{k} = CASE WHEN {k} IS NULL OR {k} = '' "
                    f"THEN jsonb_build_array(${len(all_params) + 1}::jsonb) "
                    f"WHEN NOT ({k}::jsonb @> jsonb_build_array(${len(all_params) + 2}::jsonb)) "
                    f"THEN {k}::jsonb || jsonb_build_array(${len(all_params) + 3}::jsonb) "
                    f"ELSE {k}::jsonb END"
                )
                all_params.append(val_json)
                all_params.append(val_json)
                all_params.append(val_json)

        if not set_parts:
            return UpdateResult(0)

        where_parts = []
        for k, v in query.items():
            _validate_identifier(k)
            if isinstance(v, dict):
                continue
            where_parts.append(f"{k} = ${len(all_params) + 1}")
            all_params.append(v)

        sql = f"UPDATE {self.table} SET {', '.join(set_parts)}"
        if where_parts:
            sql += " WHERE " + " AND ".join(where_parts)
        async with _client._pool.acquire() as conn:
            status = await conn.execute(sql, *all_params)
            # asyncpg/CRDB 返回 "UPDATE N" 格式
            try:
                return UpdateResult(int(str(status).split()[-1]))
            except Exception:
                return UpdateResult(0)

    async def delete_one(self, query: dict) -> bool:
        params = []
        where_parts = []
        for k, v in query.items():
            _validate_identifier(k)
            if isinstance(v, dict):
                continue
            where_parts.append(f"{k} = ${len(params) + 1}")
            params.append(v)
        sql = f"DELETE FROM {self.table}"
        if where_parts:
            sql += " WHERE " + " AND ".join(where_parts)
        sql += " LIMIT 1"
        async with _client._pool.acquire() as conn:
            status = await conn.execute(sql, *params)
            # asyncpg/CRDB 返回 "DELETE N" 格式
            try:
                return int(str(status).split()[-1]) > 0
            except Exception:
                return False

    async def delete_many(self, query: dict, limit: int | None = None) -> int:
        """删除匹配 query 的所有记录,返回实际删除条数。
        用于清理任务(如过期 decode_logs / jobs),不依赖 CRDB TTL job。
        limit: 单次删除最多删除多少条,用于分批删除避免大事务。
        """
        params = []
        where_parts = []
        for k, v in query.items():
            _validate_identifier(k)
            if isinstance(v, dict):
                for op, val in v.items():
                    if op == "$gte":
                        where_parts.append(f"{k} >= ${len(params) + 1}")
                        params.append(val)
                    elif op == "$lte":
                        where_parts.append(f"{k} <= ${len(params) + 1}")
                        params.append(val)
                    elif op == "$gt":
                        where_parts.append(f"{k} > ${len(params) + 1}")
                        params.append(val)
                    elif op == "$lt":
                        where_parts.append(f"{k} < ${len(params) + 1}")
                        params.append(val)
                    elif op == "$in":
                        # 空列表 $in: 匹配 nothing（与 MongoDB 语义一致），避免生成 `IN ()` 语法错误
                        if not val:
                            where_parts.append("FALSE")
                        else:
                            placeholders = [f"${len(params) + j + 1}" for j in range(len(val))]
                            params.extend(val)
                            where_parts.append(f"{k} IN ({', '.join(placeholders)})")
                continue
            where_parts.append(f"{k} = ${len(params) + 1}")
            params.append(v)
        sql = f"DELETE FROM {self.table}"
        if where_parts:
            sql += " WHERE " + " AND ".join(where_parts)
        if limit is not None and limit > 0:
            sql += f" LIMIT {limit}"
        async with _client._pool.acquire() as conn:
            status = await conn.execute(sql, *params)
            # asyncpg/CRDB 返回 "DELETE N" 格式
            try:
                return int(str(status).split()[-1])
            except Exception:
                return 0

    async def count_documents(self, query: dict) -> int:
        params = []
        where_parts = []
        for k, v in query.items():
            if k == "$or":
                continue
            _validate_identifier(k)
            if isinstance(v, dict):
                if "$gte" in v:
                    where_parts.append(f"{k} >= ${len(params) + 1}")
                    params.append(_safe_str(v["$gte"]))
                elif "$lte" in v:
                    where_parts.append(f"{k} <= ${len(params) + 1}")
                    params.append(_safe_str(v["$lte"]))
                elif "$gt" in v:
                    where_parts.append(f"{k} > ${len(params) + 1}")
                    params.append(_safe_str(v["$gt"]))
                elif "$lt" in v:
                    where_parts.append(f"{k} < ${len(params) + 1}")
                    params.append(_safe_str(v["$lt"]))
                elif "$ne" in v:
                    where_parts.append(f"{k} != ${len(params) + 1}")
                    params.append(_safe_str(v["$ne"]))
                elif "$in" in v:
                    in_list = v["$in"]
                    # 空列表 $in: 匹配 nothing（与 MongoDB 语义一致），避免生成 `IN ()` 语法错误
                    if not in_list:
                        where_parts.append("FALSE")
                    else:
                        placeholders = [f"${len(params) + j + 1}" for j in range(len(in_list))]
                        params.extend([_safe_str(x) for x in in_list])
                        where_parts.append(f"{k} IN ({', '.join(placeholders)})")
                elif "$regex" in v:
                    where_parts.append(f"{k} LIKE ${len(params) + 1} ESCAPE '\\'")
                    params.append(f"%{_escape_like(_safe_str(v['$regex']))}%")
                continue
            where_parts.append(f"{k} = ${len(params) + 1}")
            params.append(_safe_str(v))

        if "$or" in query:
            or_parts = []
            for sub_q in query["$or"]:
                # 完整翻译每个 $or 子条件(复用单条件操作符逻辑,保持与顶层一致)
                sub_clauses = []
                for sk, sv in sub_q.items():
                    _validate_identifier(sk)
                    if isinstance(sv, dict):
                        if "$gte" in sv:
                            sub_clauses.append(f"{sk} >= ${len(params) + 1}")
                            params.append(_safe_str(sv["$gte"]))
                        elif "$lte" in sv:
                            sub_clauses.append(f"{sk} <= ${len(params) + 1}")
                            params.append(_safe_str(sv["$lte"]))
                        elif "$gt" in sv:
                            sub_clauses.append(f"{sk} > ${len(params) + 1}")
                            params.append(_safe_str(sv["$gt"]))
                        elif "$lt" in sv:
                            sub_clauses.append(f"{sk} < ${len(params) + 1}")
                            params.append(_safe_str(sv["$lt"]))
                        elif "$ne" in sv:
                            sub_clauses.append(f"{sk} != ${len(params) + 1}")
                            params.append(_safe_str(sv["$ne"]))
                        elif "$in" in sv:
                            in_list = sv["$in"]
                            if not in_list:
                                sub_clauses.append("FALSE")
                            else:
                                placeholders = [f"${len(params) + j + 1}" for j in range(len(in_list))]
                                params.extend([_safe_str(x) for x in in_list])
                                sub_clauses.append(f"{sk} IN ({', '.join(placeholders)})")
                        elif "$regex" in sv:
                            sub_clauses.append(f"{sk} LIKE ${len(params) + 1} ESCAPE '\\'")
                            params.append(f"%{_escape_like(_safe_str(sv['$regex']))}%")
                        else:
                            logger.warning(f"[db] $or 子条件含未支持操作符(字段={sk}),已跳过: {sv}")
                    else:
                        sub_clauses.append(f"{sk} = ${len(params) + 1}")
                        params.append(_safe_str(sv))
                if sub_clauses:
                    or_parts.append("(" + " AND ".join(sub_clauses) + ")")
            if or_parts:
                where_parts.append("(" + " OR ".join(or_parts) + ")")

        sql = f"SELECT COUNT(*) as cnt FROM {self.table}"
        if where_parts:
            sql += " WHERE " + " AND ".join(where_parts)
        rows = await self._query(sql, params)
        return rows[0]["cnt"] if rows else 0

    async def find(
        self, query: dict = None, sort: tuple = None,
        skip: int = 0, limit: int = None,
        projection: list[str] | None = None,
    ) -> list[dict]:
        query = query or {}
        params = []
        where_parts = []
        for k, v in query.items():
            if k == "$or":
                continue
            _validate_identifier(k)
            if isinstance(v, dict):
                if "$gte" in v:
                    where_parts.append(f"{k} >= ${len(params) + 1}")
                    params.append(_safe_str(v["$gte"]))
                elif "$lte" in v:
                    where_parts.append(f"{k} <= ${len(params) + 1}")
                    params.append(_safe_str(v["$lte"]))
                elif "$gt" in v:
                    where_parts.append(f"{k} > ${len(params) + 1}")
                    params.append(_safe_str(v["$gt"]))
                elif "$lt" in v:
                    where_parts.append(f"{k} < ${len(params) + 1}")
                    params.append(_safe_str(v["$lt"]))
                elif "$ne" in v:
                    where_parts.append(f"{k} != ${len(params) + 1}")
                    params.append(_safe_str(v["$ne"]))
                elif "$in" in v:
                    in_list = v["$in"]
                    # 空列表 $in: 匹配 nothing（与 MongoDB 语义一致），避免生成 `IN ()` 语法错误
                    if not in_list:
                        where_parts.append("FALSE")
                    else:
                        placeholders = [f"${len(params) + j + 1}" for j in range(len(in_list))]
                        params.extend([_safe_str(x) for x in in_list])
                        where_parts.append(f"{k} IN ({', '.join(placeholders)})")
                elif "$regex" in v:
                    where_parts.append(f"{k} LIKE ${len(params) + 1} ESCAPE '\\'")
                    params.append(f"%{_escape_like(_safe_str(v['$regex']))}%")
                continue
            where_parts.append(f"{k} = ${len(params) + 1}")
            params.append(_safe_str(v))

        if "$or" in query:
            or_parts = []
            for sub_q in query["$or"]:
                # 完整翻译每个 $or 子条件(复用单条件操作符逻辑,保持与顶层一致)
                sub_clauses = []
                for sk, sv in sub_q.items():
                    _validate_identifier(sk)
                    if isinstance(sv, dict):
                        if "$gte" in sv:
                            sub_clauses.append(f"{sk} >= ${len(params) + 1}")
                            params.append(_safe_str(sv["$gte"]))
                        elif "$lte" in sv:
                            sub_clauses.append(f"{sk} <= ${len(params) + 1}")
                            params.append(_safe_str(sv["$lte"]))
                        elif "$gt" in sv:
                            sub_clauses.append(f"{sk} > ${len(params) + 1}")
                            params.append(_safe_str(sv["$gt"]))
                        elif "$lt" in sv:
                            sub_clauses.append(f"{sk} < ${len(params) + 1}")
                            params.append(_safe_str(sv["$lt"]))
                        elif "$ne" in sv:
                            sub_clauses.append(f"{sk} != ${len(params) + 1}")
                            params.append(_safe_str(sv["$ne"]))
                        elif "$in" in sv:
                            in_list = sv["$in"]
                            if not in_list:
                                sub_clauses.append("FALSE")
                            else:
                                placeholders = [f"${len(params) + j + 1}" for j in range(len(in_list))]
                                params.extend([_safe_str(x) for x in in_list])
                                sub_clauses.append(f"{sk} IN ({', '.join(placeholders)})")
                        elif "$regex" in sv:
                            sub_clauses.append(f"{sk} LIKE ${len(params) + 1} ESCAPE '\\'")
                            params.append(f"%{_escape_like(_safe_str(sv['$regex']))}%")
                        else:
                            logger.warning(f"[db] $or 子条件含未支持操作符(字段={sk}),已跳过: {sv}")
                    else:
                        sub_clauses.append(f"{sk} = ${len(params) + 1}")
                        params.append(_safe_str(sv))
                if sub_clauses:
                    or_parts.append("(" + " AND ".join(sub_clauses) + ")")
            if or_parts:
                where_parts.append("(" + " OR ".join(or_parts) + ")")

        if projection:
            for col in projection:
                _validate_identifier(col)
        sql = "SELECT " + (", ".join(projection) if projection else "*") + f" FROM {_validate_identifier(self.table)}"
        if where_parts:
            sql += " WHERE " + " AND ".join(where_parts)
        if sort and len(sort) >= 2:
            if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', sort[0]):
                raise ValueError(f"Invalid sort column: {sort[0]}")
            direction = "DESC" if sort[1] < 0 else "ASC"
            sql += f" ORDER BY {sort[0]} {direction}"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        if skip:
            sql += f" OFFSET {int(skip)}"
        return await self._query(sql, params)


_users_col = D1Collection("users")
_file_records_col = D1Collection("file_records")
_decode_logs_col = D1Collection("decode_logs")
_pending_uploads_col = D1Collection("pending_uploads")
_send_queue_col = D1Collection("send_queue")
_backup_config_col = D1Collection("backup_config")
_code_bot_mapping_col = D1Collection("code_bot_mapping")
_message_backups_col = D1Collection("message_backups")
_cells_col = D1Collection("cells")
_codes_col = D1Collection("codes")
_jobs_col = D1Collection("jobs")
_rotate_log_col = D1Collection("rotate_log")
_spare_pool_col = D1Collection("spare_pool")
_rotation_config_col = D1Collection("rotation_config")
_external_code_mapping_col = D1Collection("external_code_mapping")
_relay_accounts_col = D1Collection("relay_accounts")


def get_users_col() -> D1Collection:
    return _users_col


def get_file_records_col() -> D1Collection:
    return _file_records_col


def get_decode_logs_col() -> D1Collection:
    return _decode_logs_col


def get_pending_uploads_col() -> D1Collection:
    return _pending_uploads_col


def get_send_queue_col() -> D1Collection:
    return _send_queue_col


def get_backup_config_col() -> D1Collection:
    return _backup_config_col


def get_code_bot_mapping_col() -> D1Collection:
    return _code_bot_mapping_col


async def save_code_bot_mapping(code: str, bot_username: str):
    col = get_code_bot_mapping_col()
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    existing = await col.find_one({"code_prefix": code})
    if existing:
        await col.update_one(
            {"code_prefix": code},
            {"$set": {"bot_username": bot_username, "created_at": now}},
        )
    else:
        await col.insert_one({"code_prefix": code, "bot_username": bot_username, "created_at": now})


async def get_bot_for_code(code: str) -> str:
    col = get_code_bot_mapping_col()
    row = await col.find_one({"code_prefix": code})
    if row:
        return row.get("bot_username", "")
    return ""


async def _get_config(key: str) -> Optional[str]:
    row = await _backup_config_col.find_one({"config_key": key})
    return row.get("config_value") if row else None


async def _set_config(key: str, value: str):
    import datetime as _dt
    existing = await _backup_config_col.find_one({"config_key": key})
    if existing:
        await _backup_config_col.update_one(
            {"config_key": key},
            {"$set": {"config_value": value, "updated_at": _dt.datetime.now(_dt.timezone.utc).isoformat()}},
        )
    else:
        await _backup_config_col.insert_one({
            "config_key": key,
            "config_value": value,
            "updated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        })


async def get_backup_channels(group: int) -> list[int]:
    val = await _get_config(f"backup_channels_{group}")
    if val is None:
        return []
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return []


async def set_backup_channels(group: int, channels: list[int]):
    await _set_config(f"backup_channels_{group}", _json_dumps(channels))


async def get_all_backup_channels() -> list[int]:
    result = []
    for g in (1, 2, 3):
        result.extend(await get_backup_channels(g))
    return result


async def get_backup_bot_tokens() -> dict[str, str]:
    tokens = {}
    for i in (1, 2, 3):
        val = await _get_config(f"backup_bot_{i}_token")
        if val:
            tokens[str(i)] = val
    return tokens


async def set_backup_bot_token(bot_num: int, token: str):
    await _set_config(f"backup_bot_{bot_num}_token", token)


async def delete_backup_bot_token(bot_num: int):
    existing = await _backup_config_col.find_one({"config_key": f"backup_bot_{bot_num}_token"})
    if existing:
        await _backup_config_col.update_one(
            {"config_key": f"backup_bot_{bot_num}_token"},
            {"$set": {"config_value": "", "updated_at": ""}},
        )


async def get_config(key: str) -> str | None:
    """获取配置值,走内存缓存(10分钟 TTL"""
    return await get_config_cached(key)


async def set_config(key: str, value: str):
    await _set_config(key, value)
    await _invalidate_config_caches(key)


async def delete_config(key: str):
    existing = await _backup_config_col.find_one({"config_key": key})
    if existing:
        await _backup_config_col.update_one(
            {"config_key": key},
            {"$set": {"config_value": "", "updated_at": ""}},
        )
    await _invalidate_config_caches(key)


async def _invalidate_config_caches(key: str):
    """同时失效 L1 内存缓存和 L2 SQLite 缓存，防止旧值被命中。"""
    get_config_cache().invalidate(f"config:{key}")
    try:
        from .cache_store import get_cache_store
        store = get_cache_store()
        await store.delete(f"config:{key}")
    except Exception as e:
        logger.warning(f"[config] 失效 L2 缓存失败 key={key}: {e}")


# ─── 中继账号白名单 & 采集器白名单：热修改支持 ───
# DB key: relay_account_ids / collector_account_ids
# 优先从 DB 读取（支持热修改），DB 未配置时回退到 settings 环境变量

# 进程内互斥锁：防止并发 add/remove 导致读-改-写竞态（覆盖丢失）
# R45: 懒加载 Lock,避免模块导入时 Python 3.9 要求事件循环存在
_whitelist_modify_lock: asyncio.Lock | None = None


def _get_whitelist_modify_lock() -> asyncio.Lock:
    """懒加载 whitelist modify lock。"""
    global _whitelist_modify_lock
    if _whitelist_modify_lock is None:
        _whitelist_modify_lock = asyncio.Lock()
    return _whitelist_modify_lock


async def get_relay_whitelist() -> set[int]:
    """获取中继账号白名单（动态加载，支持热修改）。
    优先从 DB 读取，DB 未配置时回退到 settings.RELAY_ACCOUNT_IDS。
    使用 get_config_fresh 绕过 L1 缓存,确保 admin_bot 热修改后立即生效。
    """
    from config import settings
    db_val = await get_config_fresh("relay_account_ids")
    raw = db_val if db_val else getattr(settings, "RELAY_ACCOUNT_IDS", "")
    if not raw:
        return set()
    try:
        return {int(x.strip()) for x in raw.split(",") if x.strip()}
    except (ValueError, TypeError):
        return set()


async def add_relay_whitelist(user_id: int) -> bool:
    """添加中继账号到白名单。返回 True 表示新增，False 表示已存在。"""
    async with _get_whitelist_modify_lock():
        current = await get_relay_whitelist()
        if user_id in current:
            return False
        current.add(user_id)
        await set_config("relay_account_ids", ",".join(str(x) for x in sorted(current)))
        return True


async def remove_relay_whitelist(user_id: int) -> bool:
    """从中继账号白名单移除。返回 True 表示已移除，False 表示不存在。"""
    async with _get_whitelist_modify_lock():
        current = await get_relay_whitelist()
        if user_id not in current:
            return False
        current.discard(user_id)
        await set_config("relay_account_ids", ",".join(str(x) for x in sorted(current)))
        return True


async def get_collector_whitelist() -> set[int]:
    """获取采集器账号白名单（动态加载，支持热修改）。
    优先从 DB 读取，DB 未配置时回退到 settings.COLLECTOR_ACCOUNT_IDS。
    使用 get_config_fresh 绕过 L1 缓存,确保 admin_bot 热修改后立即生效。
    """
    from config import settings
    db_val = await get_config_fresh("collector_account_ids")
    raw = db_val if db_val else getattr(settings, "COLLECTOR_ACCOUNT_IDS", "")
    if not raw:
        return set()
    try:
        return {int(x.strip()) for x in raw.split(",") if x.strip()}
    except (ValueError, TypeError):
        return set()


async def add_collector_whitelist(user_id: int) -> bool:
    """添加采集器账号到白名单。返回 True 表示新增，False 表示已存在。"""
    async with _get_whitelist_modify_lock():
        current = await get_collector_whitelist()
        if user_id in current:
            return False
        current.add(user_id)
        await set_config("collector_account_ids", ",".join(str(x) for x in sorted(current)))
        return True


async def remove_collector_whitelist(user_id: int) -> bool:
    """从采集器账号白名单移除。返回 True 表示已移除，False 表示不存在。"""
    async with _get_whitelist_modify_lock():
        current = await get_collector_whitelist()
        if user_id not in current:
            return False
        current.discard(user_id)
        await set_config("collector_account_ids", ",".join(str(x) for x in sorted(current)))
        return True


async def get_relay_config() -> dict:
    api_id = await _get_config("relay_api_id")
    api_hash = await _get_config("relay_api_hash")
    phone = await _get_config("relay_phone")
    # N19-2: config 表中 api_hash 已加密存储，读取时解密
    if api_hash:
        try:
            from .relay_db import decrypt
            api_hash = decrypt(api_hash)
        except (RuntimeError, ImportError):
            pass  # 兼容旧明文数据或解密失败
    return {
        "api_id": int(api_id) if api_id else 0,
        "api_hash": api_hash or "",
        "phone": phone or "",
    }


async def set_relay_config(api_id: int, api_hash: str, phone: str):
    # N19-2: api_hash 入云前加密，与 S-1 relay_accounts 对齐
    if api_hash:
        try:
            from .relay_db import encrypt
            api_hash = encrypt(api_hash)
        except (RuntimeError, ImportError, AttributeError):
            pass  # 加密不可用时保持明文（极端情况）
    if api_id:
        await _set_config("relay_api_id", str(api_id))
    if api_hash:
        await _set_config("relay_api_hash", api_hash)
    if phone:
        await _set_config("relay_phone", phone)


async def get_r2_config() -> dict:
    """读取 R2 配置（config 表优先，r2_secret_key 解密）。

    R26-M1: 与 relay api_hash 对称，写入时 Fernet 加密、读取时解密。
    返回 {"account_id", "access_key", "secret_key", "bucket", "endpoint"}，
    任一字段在 config 表缺失时返回空字符串，由调用方决定是否 fallback 到 .env。
    """
    account_id = await _get_config("r2_account_id") or ""
    access_key = await _get_config("r2_access_key") or ""
    secret_key_cipher = await _get_config("r2_secret_key") or ""
    # R27-M1: 键名与写入侧对齐（admin /set_r2 写入的是 r2_bucket，不是 r2_bucket_name）
    bucket = await _get_config("r2_bucket") or ""
    endpoint = await _get_config("r2_endpoint") or ""

    secret_key = ""
    if secret_key_cipher:
        try:
            from .relay_db import decrypt
            secret_key = decrypt(secret_key_cipher)
        except (RuntimeError, ImportError) as e:
            # R27-L1: 兼容旧明文数据（P2-4 加密前写入的），但打 warning 以便定位
            # 若是真加密数据 + RELAY_ENCRYPTION_KEY 缺失/轮换，会退化为密文当密钥 → SigV4 403
            logger.warning(
                f"[DB] r2_secret_key 解密失败，按旧明文兼容返回。"
                f"若 RELAY_ENCRYPTION_KEY 已轮换，需用正确密钥重新加密或重新录入 R2 凭证: {e}"
            )
            secret_key = secret_key_cipher

    return {
        "account_id": account_id,
        "access_key": access_key,
        "secret_key": secret_key,
        "bucket": bucket,
        "endpoint": endpoint,
    }


async def get_relay_status() -> str:
    return await _get_config("relay_status") or "offline"


# ─── 文件码前缀 → Bot 路由 ──────────────────────────────────────

# 内存缓存(10 分钟 TTL)
_code_bot_routes_cache: dict[str, str] = {}
_code_bot_routes_cache_ts: float = 0.0
_bot_decode_interval_cache: dict[str, int] = {}
_bot_decode_interval_cache_ts: float = 0.0
_BOT_CONFIG_TTL: int = 600  # 10 分钟


async def _refresh_bot_config_cache():
    """DB 刷新 code_bot_route bot_decode_interval 缓存"""
    global _code_bot_routes_cache, _code_bot_routes_cache_ts
    global _bot_decode_interval_cache, _bot_decode_interval_cache_ts
    from loguru import logger

    try:
        # 刷新 code_bot_route 缓存
        routes = {}
        all_routes = await _backup_config_col.find({"config_key": {"$regex": "code_bot_route:"}})
        for row in all_routes:
            key = row.get("config_key", "")
            val = row.get("config_value", "")
            prefix = key.replace("code_bot_route:", "")
            if prefix and val:
                routes[prefix] = val
        _code_bot_routes_cache = routes
        _code_bot_routes_cache_ts = _time.time()

        # 刷新 bot_decode_interval 缓存
        intervals = {}
        all_intervals = await _backup_config_col.find({"config_key": {"$regex": "bot_decode_interval:"}})
        for row in all_intervals:
            key = row.get("config_key", "")
            val = row.get("config_value", "")
            bot = key.replace("bot_decode_interval:", "")
            if bot and val:
                try:
                    intervals[bot] = int(val)
                except ValueError:
                    pass
        _bot_decode_interval_cache = intervals
        _bot_decode_interval_cache_ts = _time.time()

        # 刷新正则路由缓存
        await _refresh_code_bot_routes_regex_cache()

        logger.debug(f"[ConfigCache] 已刷新 {len(routes)} routes, {len(intervals)} intervals, {len(_code_bot_routes_regex_cache)} regex routes")
    except Exception as e:
        logger.warning(f"[ConfigCache] 刷新失败: {e}")


async def set_code_bot_route(prefix: str, bot_username: str):
    await _set_config(f"code_bot_route:{prefix}", bot_username)
    _code_bot_routes_cache[prefix] = bot_username


async def get_code_bot_route(prefix: str) -> str | None:
    return await _get_config(f"code_bot_route:{prefix}")


async def delete_code_bot_route(prefix: str):
    existing = await _backup_config_col.find_one({"config_key": f"code_bot_route:{prefix}"})
    if existing:
        await _backup_config_col.update_one(
            {"config_key": f"code_bot_route:{prefix}"},
            {"$set": {"config_value": "", "updated_at": ""}},
        )
    _code_bot_routes_cache.pop(prefix, None)


async def get_all_code_bot_routes() -> dict[str, str]:
    """获取所code_bot_route,走内存缓存0 分钟 TTL)"""
    # R43: read-only global,无需 global 声明(F824)
    if _time.time() - _code_bot_routes_cache_ts > _BOT_CONFIG_TTL:
        await _refresh_bot_config_cache()
    return _code_bot_routes_cache


async def resolve_bot_for_code(code: str, default_bot: str) -> str:
    """根据文件码前缀匹配配置的路由，无匹配则返回 default_bot。"""
    # R43: read-only global,无需 global 声明(F824)
    if _time.time() - _code_bot_routes_cache_ts > _BOT_CONFIG_TTL:
        await _refresh_bot_config_cache()
    best_prefix = ""
    best_bot = ""
    for prefix, bot_username in _code_bot_routes_cache.items():
        if code.startswith(prefix) and len(prefix) > len(best_prefix):
            best_prefix = prefix
            best_bot = bot_username
    return best_bot or default_bot


# ─── 文件码正则 → Bot 路由 ──────────────────────────────────────
# 用于第三方码格式不规则（40位hash / emoji / 其他非前缀式）时按正则匹配路由。
# config_key 命名: code_bot_route_regex:<id>  其中 <id> 为自增整数序号
# config_value 格式: "<bot_username>\t<regex_pattern>"  (tab 分隔)
# 内存缓存: _code_bot_routes_regex_cache: list[tuple[str, str]]  (bot, pattern)
# _code_bot_routes_regex_cache_ts 与 _code_bot_routes_cache_ts 共享 TTL

_code_bot_routes_regex_cache: list[tuple[str, str]] = []


async def _refresh_code_bot_routes_regex_cache():
    """从 DB 加载正则路由到内存缓存。在 _refresh_bot_config_cache 中调用。"""
    global _code_bot_routes_regex_cache
    try:
        items: list[tuple[str, str]] = []
        rows = await _backup_config_col.find({"config_key": {"$regex": "code_bot_route_regex:"}})
        for row in rows:
            val = row.get("config_value", "")
            if not val or "\t" not in val:
                continue
            bot_username, pattern = val.split("\t", 1)
            if bot_username and pattern:
                items.append((bot_username, pattern))
        _code_bot_routes_regex_cache = items
    except Exception as e:
        from loguru import logger
        logger.warning(f"[ConfigCache] 正则路由刷新失败: {e}")


def _unescape_unicode_in_pattern(pattern: str) -> str:
    r"""将正则字符串中的 \Uxxxxxxxx 和 \uxxxx 转义序列转换为实际 Unicode 字符。

    Python re 模块不原生支持 \U/\u 转义（这是 Python 字符串字面量语法）。
    用户在 Telegram 中只能输入纯 ASCII 文本，因此需要在此解析转义。
    """
    import re as _re
    def _replace(m):
        return chr(int(m.group(1), 16))
    # 先处理 \Uxxxxxxxx (8位)，再处理 \uxxxx (4位)
    pattern = _re.sub(r'\\U([0-9a-fA-F]{8})', _replace, pattern)
    pattern = _re.sub(r'\\u([0-9a-fA-F]{4})', _replace, pattern)
    return pattern


async def set_code_bot_route_regex(pattern: str, bot_username: str) -> int:
    r"""新增一条正则路由，返回分配的 id。pattern 可带锚点也可不带。

    支持 \Uxxxxxxxx 和 \uxxxx 转义序列（用于在 Telegram 中输入 emoji/特殊 Unicode 范围）。
    """
    import re as _re
    # 解析 \U/\u 转义序列，转换为实际 Unicode 字符
    pattern = _unescape_unicode_in_pattern(pattern)
    # 校验正则可编译
    try:
        _re.compile(pattern)
    except _re.error as e:
        raise ValueError(f"正则表达式无效: {e}")
    # 自增 id: 扫描现有最大序号
    existing = await _backup_config_col.find({"config_key": {"$regex": "code_bot_route_regex:"}})
    max_id = 0
    for row in existing:
        key = row.get("config_key", "")
        try:
            n = int(key.replace("code_bot_route_regex:", ""))
            if n > max_id:
                max_id = n
        except ValueError:
            pass
    new_id = max_id + 1
    val = f"{bot_username}\t{pattern}"
    await _set_config(f"code_bot_route_regex:{new_id}", val)
    _code_bot_routes_regex_cache.append((bot_username, pattern))
    return new_id


async def delete_code_bot_route_regex(route_id: int) -> bool:
    """删除指定 id 的正则路由。返回是否删除成功。"""
    key = f"code_bot_route_regex:{route_id}"
    existing = await _backup_config_col.find_one({"config_key": key})
    if not existing:
        return False
    await _backup_config_col.update_one(
        {"config_key": key},
        {"$set": {"config_value": "", "updated_at": ""}},
    )
    # 重新加载缓存（移除被删除项）
    await _refresh_code_bot_routes_regex_cache()
    return True


async def get_all_code_bot_routes_regex() -> list[tuple[int, str, str]]:
    """获取所有正则路由: [(id, bot_username, pattern), ...]"""
    # R43: read-only global,无需 global 声明(F824)
    if _time.time() - _code_bot_routes_cache_ts > _BOT_CONFIG_TTL:
        await _refresh_bot_config_cache()
    result: list[tuple[int, str, str]] = []
    rows = await _backup_config_col.find({"config_key": {"$regex": "code_bot_route_regex:"}})
    for row in rows:
        key = row.get("config_key", "")
        val = row.get("config_value", "")
        if not val or "\t" not in val:
            continue
        try:
            n = int(key.replace("code_bot_route_regex:", ""))
        except ValueError:
            continue
        bot_username, pattern = val.split("\t", 1)
        if bot_username and pattern:
            result.append((n, bot_username, pattern))
    return result


async def resolve_bot_for_code_regex(code: str) -> str | None:
    """按正则路由匹配文件码，返回第一个命中的 bot_username，未命中返回 None。

    使用 re.search 匹配，若 pattern 带 ^ $ 锚点则按全文匹配。
    遍历顺序按 id 升序（先添加的优先）。
    """
    import re as _re
    # R43: read-only global,无需 global 声明(F824)
    if _time.time() - _code_bot_routes_cache_ts > _BOT_CONFIG_TTL:
        await _refresh_bot_config_cache()
    for bot_username, pattern in _code_bot_routes_regex_cache:
        try:
            if _re.search(pattern, code):
                return bot_username
        except Exception:
            continue
    return None


# ─── Bot 解码间隔限流 ────────────────────────────────────────────


async def set_bot_decode_interval(bot_username: str, interval_seconds: int):
    await _set_config(f"bot_decode_interval:{bot_username}", str(interval_seconds))
    # 更新内存缓存
    _bot_decode_interval_cache[bot_username] = interval_seconds


async def get_bot_decode_interval(bot_username: str) -> int:
    """获取 bot 解码间隔，走内存缓存(10 分钟 TTL)"""
    # R43: read-only global,无需 global 声明(F824)
    if _time.time() - _bot_decode_interval_cache_ts > _BOT_CONFIG_TTL:
        await _refresh_bot_config_cache()
    return _bot_decode_interval_cache.get(bot_username, 0)


async def delete_bot_decode_interval(bot_username: str):
    existing = await _backup_config_col.find_one({"config_key": f"bot_decode_interval:{bot_username}"})
    if existing:
        await _backup_config_col.update_one(
            {"config_key": f"bot_decode_interval:{bot_username}"},
            {"$set": {"config_value": "", "updated_at": ""}},
        )
    _bot_decode_interval_cache.pop(bot_username, None)


async def get_all_bot_decode_intervals() -> dict[str, int]:
    col = _backup_config_col
    rows = await col.find({})
    result = {}
    for row in rows:
        key = row.get("config_key", "")
        val = row.get("config_value", "")
        if key.startswith("bot_decode_interval:") and val and val.isdigit():
            bot = key[len("bot_decode_interval:"):]
            result[bot] = int(val)
    return result


def get_message_backups_col() -> D1Collection:
    return _message_backups_col


async def save_message_backup(main_msg_id: int, backup_channel_id: int, backed_msg_id: int):
    import datetime as _dt
    col = get_message_backups_col()
    existing = await col.find_one({
        "main_msg_id": main_msg_id,
        "backup_channel_id": backup_channel_id,
    })
    if existing:
        await col.update_one(
            {"main_msg_id": main_msg_id, "backup_channel_id": backup_channel_id},
            {"$set": {
                "backed_msg_id": backed_msg_id,
                "backed_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            }},
        )
    else:
        await col.insert_one({
            "main_msg_id": main_msg_id,
            "backup_channel_id": backup_channel_id,
            "backed_msg_id": backed_msg_id,
            "backed_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        })


async def get_message_backups(main_msg_id: int) -> list[dict]:
    col = get_message_backups_col()
    return await col.find({"main_msg_id": main_msg_id})


async def get_all_message_backups() -> list[dict]:
    col = get_message_backups_col()
    return await col.find({})


# ─── 缓存查询──────────────────────────────────────────────────
from .cache import get_user_cache, get_file_record_cache, get_config_cache


# ─── 外部码映射查询────────────────────────────────────────────

def get_external_code_mapping_col() -> D1Collection:
    return _external_code_mapping_col


# ─── 外部码映射内存缓存(启动加载,60 秒刷新)─────────────────────
_external_code_mapping_cache: dict[str, str] = {}
_external_code_mapping_cache_ts = 0
_EXTERNAL_CODE_MAPPING_TTL = 60  # 60 秒(缩短以快速响应管理员修改)


async def _refresh_external_code_mapping_cache():
    """刷新外部码映射内存缓存（从 SQLite）"""
    global _external_code_mapping_cache, _external_code_mapping_cache_ts
    try:
        from .cache_store import get_cache_store
        store = get_cache_store()
        rows = await store.get_all_external_mappings_local()
        _external_code_mapping_cache = {
            row["external_code"]: row.get("system_code", "")
            for row in rows
            if row.get("system_code")
        }
        _external_code_mapping_cache_ts = _time.time()
    except Exception as e:
        logger.warning(f"[DB] refresh external_code_mapping cache failed: {e}")


async def get_system_code_for_external(external_code: str) -> str | None:
    """查询外部码对应的系统码,走 SQLite 全表缓存（0 CRDB RU）"""
    # R43: read-only global,无需 global 声明(F824)
    # 检查缓存是否过期
    if _time.time() - _external_code_mapping_cache_ts > _EXTERNAL_CODE_MAPPING_TTL:
        await _refresh_external_code_mapping_cache()
    # 先查内存缓存
    system_code = _external_code_mapping_cache.get(external_code)
    if system_code:
        return system_code
    # 从 SQLite 查询
    try:
        from .cache_store import get_cache_store
        store = get_cache_store()
        row = await store.get_external_mapping_local(external_code)
        if row:
            sc = row.get("system_code")
            if sc:
                _external_code_mapping_cache[external_code] = sc
                return sc
    except Exception as e:
        logger.warning(f"[DB] get_system_code_for_external failed ({external_code}): {e}")
    return None


async def set_external_code_mapping(
    external_code: str,
    system_code: str,
    bot_username: str = "",
) -> bool:
    """写入外部码→系统码映射(由采集器调用)"""
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    try:
        existing = await _external_code_mapping_col.find_one({"external_code": external_code})
        if existing:
            await _external_code_mapping_col.update_one(
                {"external_code": external_code},
                {"$set": {"system_code": system_code, "bot_username": bot_username, "updated_at": now}},
            )
        else:
            await _external_code_mapping_col.insert_one({
                "external_code": external_code,
                "system_code": system_code,
                "bot_username": bot_username,
                "created_at": now,
                "updated_at": now,
            })
        return True
    except Exception as e:
        logger.error(f"[DB] set_external_code_mapping failed ({external_code}): {e}")
        return False


async def get_user_cached(user_id: int) -> Optional[dict]:
    """查询用户，二级缓存：内存 → SQLite 全表（0 CRDB RU）"""
    cache = get_user_cache()
    cache_key = f"user:{user_id}"

    # L1: 内存缓存
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    # L2: SQLite 全表缓存（0 CRDB RU，替代原 KV 缓存 + CRDB 兜底）
    from .cache_store import get_cache_store
    store = get_cache_store()
    user = await store.get_user_local(user_id)
    if user is not None:
        cache.set(cache_key, user)
    return user


async def update_user_and_invalidate(user_id: int, update: dict = None):
    """双写：先写 SQLite(标记 dirty)，再异步写 CRDB"""
    from .cache_store import get_cache_store
    store = get_cache_store()
    
    if update is not None:
        # 1. 先写 CRDB（保证数据安全）
        col = get_users_col()
        await col.update_one({"user_id": user_id}, update)
        # 2. 同步写 SQLite 全表缓存（标记已同步）
        existing = await store.get_user_local(user_id)
        if existing:
            existing.update(update)
            await store.upsert_user_local(existing, mark_dirty=False)
        else:
            # 新用户：从 CRDB 重新读取
            user = await col.find_one({"user_id": user_id})
            if user:
                await store.upsert_user_local(user, mark_dirty=False)
    
    cache = get_user_cache()
    cache.invalidate(f"user:{user_id}")
    from .cache import clear_negative_user
    clear_negative_user(user_id)


async def get_file_record_cached(file_code: str) -> Optional[dict]:
    """查询文件记录，三级缓存：内存 → SQLite 全表 → CRDB

    防止 L1 内存缓存里残留损坏记录(如启动时从 cache_backup 恢复了旧版 status=None 的记录):
    若 L1 命中但 status 字段无效(None/空),视为缓存失效,降级重新查 SQLite/CRDB。
    """
    cache = get_file_record_cache()
    cache_key = f"file:{file_code}"

    # L1: 内存缓存
    cached = cache.get(cache_key)
    if cached is not None:
        # 完整性校验:status 必须是有效字符串,否则视为损坏记录降级重查
        if cached.get("status"):
            return cached
        # 损坏记录,清缓存降级
        cache.invalidate(cache_key)
        logger.warning(f"[get_file_record_cached] L1 缓存记录 status 无效(file_code={file_code}),降级重查")

    # L2: SQLite 全表缓存
    from .cache_store import get_cache_store
    store = get_cache_store()
    record = await store.get_file_record_local(file_code)
    if record is not None:
        cache.set(cache_key, record)
        return record

    # L3: CRDB 回退（SQLite 缓存写入失败或未同步时）
    col = get_file_records_col()
    record = await col.find_one({"file_code": file_code})
    if record is not None:
        cache.set(cache_key, record)
        try:
            await store.upsert_file_record_local(record, mark_dirty=False)
        except Exception:
            pass
    return record


async def update_file_record_and_invalidate(file_code: str, update: dict):
    """双写：先写 CRDB，再同步 SQLite 全表缓存

    PRE-06: 扩展支持 $push 操作符（admin_bot 的 report:block 使用 $push 更新 blocked_users）。
    其他操作符（$set/$inc）保持原有行为。
    """
    from .cache_store import get_cache_store
    store = get_cache_store()

    # 1. 写 CRDB
    col = get_file_records_col()
    await col.update_one({"file_code": file_code}, update)

    # 2. 同步 SQLite（标记 dirty，由 sync 循环确认）
    existing = await store.get_file_record_local(file_code)
    if existing:
        if "$inc" in update:
            for k, v in update["$inc"].items():
                existing[k] = existing.get(k, 0) + v
        if "$set" in update:
            existing.update(update["$set"])
        if "$push" in update:
            # SQLite 中 list 字段以 JSON 字符串存储，需先反序列化再追加
            for k, v in update["$push"].items():
                cur = existing.get(k)
                if isinstance(cur, str):
                    try:
                        cur = json.loads(cur)
                    except (json.JSONDecodeError, TypeError):
                        cur = []
                if isinstance(cur, list):
                    cur.append(v)
                else:
                    cur = [v]
                existing[k] = cur  # upsert_file_record_local 会通过 _serialize 转回 JSON 字符串
        if "$addToSet" in update:
            # P2-5/F-L4: 去重追加,同一值不重复入列
            for k, v in update["$addToSet"].items():
                cur = existing.get(k)
                if isinstance(cur, str):
                    try:
                        cur = json.loads(cur)
                    except (json.JSONDecodeError, TypeError):
                        cur = []
                if isinstance(cur, list):
                    if v not in cur:
                        cur.append(v)
                else:
                    cur = [v]
                existing[k] = cur
        if "$inc" not in update and "$set" not in update and "$push" not in update and "$addToSet" not in update:
            # 兼容：直接传入 {field: value} 形式（无操作符）
            existing.update(update)
        await store.upsert_file_record_local(existing, mark_dirty=False)
    else:
        record = await col.find_one({"file_code": file_code})
        if record:
            await store.upsert_file_record_local(record, mark_dirty=False)

    cache = get_file_record_cache()
    cache.invalidate(f"file:{file_code}")


# ─── I: codes 表缓存查询(纯 SQLite，0 CRDB RU) ──────────────────

async def get_code_entry_cached(code: str) -> Optional[dict]:
    """查 codes 表，二级缓存：内存 → SQLite 全表（0 CRDB RU）"""
    from .cache import get_code_cache
    cache = get_code_cache()
    cache_key = f"code:{code}"
    
    # L1: 内存缓存
    entry = cache.get(cache_key)
    if entry is not None:
        return entry
    
    # L2: SQLite 全表缓存
    from .cache_store import get_cache_store
    store = get_cache_store()
    entry = await store.get_code_local(code)
    if entry is not None:
        cache.set(cache_key, entry)
    return entry


async def get_config_cached(key: str) -> Optional[str]:
    cache = get_config_cache()
    cache_key = f"config:{key}"

    # L1: 内存缓存
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    # L2: SQLite 兜底
    from .cache_store import get_cache_store
    store = get_cache_store()
    cached = await store.get(cache_key)
    if cached is not None:
        cache.set(cache_key, cached)
        return cached

    # CRDB
    val = await _get_config(key)
    cache.set(cache_key, val)
    await store.set(cache_key, val)
    return val


async def get_config_fresh(key: str) -> Optional[str]:
    """读取配置值,绕过 L1 内存缓存,确保跨进程一致性。

    用于白名单等需要跨进程即时一致性的场景。
    读取路径: L2 SQLite (0 RU) → CRDB (L2 miss 时)。
    不会写入 L1 或 L2,避免与 set_config 的失效操作产生竞态导致旧值回填。
    代价: L2 miss 后每次读都走 CRDB (1 RU),但白名单是低频读取,可接受。
    """
    cache_key = f"config:{key}"
    # L2: SQLite (跨进程共享,set_config 时会被失效)
    from .cache_store import get_cache_store
    store = get_cache_store()
    cached = await store.get(cache_key)
    if cached is not None:
        return cached
    # CRDB 兜底,不回填 L2,避免跨进程竞态
    return await _get_config(key)


async def set_config_and_invalidate(key: str, value: str):
    await _set_config(key, value)
    cache = get_config_cache()
    cache.invalidate(f"config:{key}")


# ─── 环形冗余架构 新表操作 ──────────────────────────────────────


def get_cells_col() -> D1Collection:
    return _cells_col


def get_codes_col() -> D1Collection:
    return _codes_col


def get_jobs_col() -> D1Collection:
    return _jobs_col


def get_rotate_log_col() -> D1Collection:
    return _rotate_log_col


def get_spare_pool_col() -> D1Collection:
    return _spare_pool_col


def get_rotation_config_col() -> D1Collection:
    return _rotation_config_col


# N21-1: 通用按表名获取 D1Collection，替代已删除的 get_collection
# 仅供 relay_pending 和 _ensure_telethon_client 等少数场景使用
_COLLECTION_REGISTRY = {
    "users": _users_col,
    "file_records": _file_records_col,
    "decode_logs": _decode_logs_col,
    "pending_uploads": _pending_uploads_col,
    "send_queue": _send_queue_col,
    "backup_config": _backup_config_col,
    "code_bot_mapping": _code_bot_mapping_col,
    "message_backups": _message_backups_col,
    "cells": _cells_col,
    "codes": _codes_col,
    "jobs": _jobs_col,
    "rotate_log": _rotate_log_col,
    "spare_pool": _spare_pool_col,
    "rotation_config": _rotation_config_col,
    "external_code_mapping": _external_code_mapping_col,
    "relay_accounts": _relay_accounts_col,
}


def get_collection(name: str) -> D1Collection:
    """按表名获取对应的 D1Collection 实例。"""
    col = _COLLECTION_REGISTRY.get(name)
    if col is None:
        raise ValueError(f"未知的表名: {name}，可用: {list(_COLLECTION_REGISTRY.keys())}")
    return col


# ─── 备用池操作──────────────────────────────────────────────────

async def add_spare_channel(channel_id: int, account_name: str = None) -> bool:
    """添加备用频道到备用池"""
    import datetime as _dt
    col = get_spare_pool_col()
    existing = await col.find_one({"channel_id": channel_id})
    if existing:
        await col.update_one(
            {"channel_id": channel_id},
            {"$set": {"account_name": account_name, "is_used": 0}},
        )
    else:
        await col.insert_one({
            "channel_id": channel_id,
            "account_name": account_name,
            "is_used": 0,
            "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        })
    return True


async def get_spare_for_account(account_name: str) -> dict | None:
    """获取指定账号的未使用备用频道(优先同账号)"""
    col = get_spare_pool_col()
    # 优先匹配同账号
    spare = await col.find_one({"account_name": account_name, "is_used": 0})
    if spare:
        return spare
    return None


async def get_any_spare() -> dict | None:
    """获取任意未使用的备用频道(无账号归属)"""
    col = get_spare_pool_col()
    # 优先无归属的备用池频道
    spare = await col.find_one({"account_name": None, "is_used": 0})
    if not spare:
        spare = await col.find_one({"is_used": 0})
    return spare


async def consume_spare(channel_id: int) -> bool:
    """标记备用频道为已使用"""
    col = get_spare_pool_col()
    await col.update_one({"channel_id": channel_id}, {"$set": {"is_used": 1}})
    return True


async def release_spare(channel_id: int) -> bool:
    """释放备用频道回池(标记为未使用)"""
    col = get_spare_pool_col()
    await col.update_one({"channel_id": channel_id}, {"$set": {"is_used": 0}})
    return True


async def remove_spare(channel_id: int) -> bool:
    """从备用池中删除频道"""
    col = get_spare_pool_col()
    await col.delete_one({"channel_id": channel_id})
    return True


async def list_spare_pool() -> list[dict]:
    """列出所有备用池频道"""
    col = get_spare_pool_col()
    return await col.find({}, sort=("account_name", 1))


# ─── 轮转配置操作 ──────────────────────────────────────────────

async def get_rotation_config(key: str) -> str | None:
    """读取轮转配置"""
    col = get_rotation_config_col()
    row = await col.find_one({"config_key": key}, projection=["config_value"])
    return row.get("config_value") if row else None


async def set_rotation_config(key: str, value: str):
    """写入轮转配置（值无变化时跳过UPDATE，省RU），同时更新本地 KV 缓存。"""
    import datetime as _dt
    from .cache_store import get_cache_store
    col = get_rotation_config_col()
    existing = await col.find_one({"config_key": key}, projection=["config_key", "config_value"])
    if existing:
        if existing.get("config_value") != value:
            await col.update_one(
                {"config_key": key},
                {"$set": {"config_value": value, "updated_at": _dt.datetime.now(_dt.timezone.utc).isoformat()}},
            )
    else:
        await col.insert_one({
            "config_key": key,
            "config_value": value,
            "updated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        })
    try:
        store = get_cache_store()
        await store.set_kv(f"rotconf:{key}", value)
    except Exception:
        pass


# ─── H方案: 后台异步同步新 job 到 CRDB ─────────────────────────

async def _sync_new_job_to_crdb(
    local_id: int,
    code: str,
    target_user_id: int,
    storage_channel_id: int,
    storage_msg_ids_str: str,
    batch_file_meta: str,
    task_type: str,
    protect_content: bool,
    created_at: str,
    group_id: int = 0,
    file_unique_id: str = "",
    media_group_id: str = "",
):
    """后台异步: 将 SQLite 中的新 job 写入 CRDB(仅审计),更新 crdb_id"""
    from .cache_store import get_cache_store
    try:
        col = get_jobs_col()
        rows = await col._query(
            """INSERT INTO jobs (code, target_user_id, storage_channel_id,
               storage_msg_ids, batch_file_meta, task_type, status,
               protect_content, created_at,
               group_id, file_unique_id, media_group_id)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
               RETURNING id""",
            [code, target_user_id, storage_channel_id, storage_msg_ids_str,
             batch_file_meta, task_type, "pending", protect_content, created_at,
             group_id, file_unique_id, media_group_id],
        )
        crdb_id = rows[0]["id"] if rows else None
        if crdb_id is not None:
            store = get_cache_store()
            await store.update_local_job_crdb_id(local_id, crdb_id)
            # C1: CRDB 写入成功后,用真实 crdb_id 投递到 Redis Stream。
            # 不在 enqueue_job 时 xadd local_id(负数临时 ID),避免与 update_local_job_crdb_id 竞态导致 job 丢失。
            # CRDB 写入失败的 job(crdb_id 保持负数)由 dsp_bot 降级轮询(get_local_pending_jobs)兜底消费。
            try:
                from utils.redis_client import xadd_job
                await xadd_job(crdb_id)
            except Exception:
                pass  # 降级:notify_dsp_new_job 已在 enqueue_job 中写入 dsp_notify
    except Exception as e:
        logger.debug(f"[H] 后台 CRDB 同步失败 local_id={local_id}: {e}")


async def enqueue_job(
    code: str,
    target_user_id: int,
    storage_channel_id: int,
    storage_msg_ids: list[int],
    batch_file_meta: str = "",
    task_type: str = "single",
    protect_content: bool = False,
    group_id: int = 0,
    file_unique_id: str = "",
    media_group_id: str = "",
):
    """jobs 表写入派工任务 + H方案: SQLite 优先，CRDB 异步审计

    SQLite 写入失败 → 抛异常，调用方给用户重试反馈。
    CRDB 写入走后台异步，不阻塞用户响应。

    R36 B0-1: group_id/file_unique_id/media_group_id 为结构化副本信息,
    供 ReplicaAwareResolver 直接消费(无需解析 batch_file_meta JSON)。
    """
    import datetime as _dt
    created_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
    storage_msg_ids_str = _json_dumps(storage_msg_ids)

    # H方案: 先写 SQLite 本地队列(主路径)，失败则抛异常让用户重试
    from .cache_store import get_cache_store
    store = get_cache_store()
    local_id = await store.insert_local_job({
        "code": code,
        "target_user_id": target_user_id,
        "storage_channel_id": storage_channel_id,
        "storage_msg_ids": storage_msg_ids_str,
        "batch_file_meta": batch_file_meta,
        "task_type": task_type,
        "status": "pending",
        "protect_content": protect_content,
        "created_at": created_at,
        "group_id": group_id,
        "file_unique_id": file_unique_id,
        "media_group_id": media_group_id,
    })
    await store.notify_dsp_new_job()
    # C1: Redis Stream 事件通知移至 _sync_new_job_to_crdb 成功后触发(用真实 crdb_id)。
    # 此处仅写 dsp_notify(SQLite),降级轮询路径由此驱动;
    # 若 Redis 可用,_sync_new_job_to_crdb 完成后会 xadd 真实 crdb_id 触发即时派发。

    # 递增本地计数器(用于 admin /status)
    # R25-L2: active_files 由 incr_user_code_count 统一维护(在线码数),此处不再重复递增
    try:
        from utils.shared_counters import status_counters
        status_counters["total_files"] = status_counters.get("total_files", 0) + 1
    except Exception:
        pass

    # H方案: CRDB 写入走后台异步(fire-and-forget)，仅用于审计，不阻塞
    task = asyncio.ensure_future(_sync_new_job_to_crdb(
        local_id=local_id,
        code=code,
        target_user_id=target_user_id,
        storage_channel_id=storage_channel_id,
        storage_msg_ids_str=storage_msg_ids_str,
        batch_file_meta=batch_file_meta,
        task_type=task_type,
        protect_content=protect_content,
        created_at=created_at,
        group_id=group_id,
        file_unique_id=file_unique_id,
        media_group_id=media_group_id,
    ))
    _pending_sync_tasks.add(task)
    task.add_done_callback(_pending_sync_tasks.discard)


async def dequeue_jobs(batch_size: int = 10) -> list:
    """从 jobs 表批量取出待派工任务（原子操作：CTE + FOR UPDATE SKIP LOCKED + UPDATE RETURNING）。

    S-3: FOR UPDATE SKIP LOCKED 防止并发 worker 认领同一行，
    与 CTE + UPDATE + RETURNING 组合保证单次 DB 往返的原子出队。

    Args:
        batch_size: 一次取出的最大任务数，默认 10
    Returns:
        JobResult 列表，可能为空列表
    """
    col = get_jobs_col()
    try:
        rows = await asyncio.wait_for(col._query("""
            WITH next AS (
                SELECT id FROM jobs
                WHERE status = 'pending'
                ORDER BY created_at
                LIMIT $1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE jobs SET status = 'dispatched'
            WHERE id IN (SELECT id FROM next)
            RETURNING id, code, target_user_id, storage_channel_id,
                      storage_msg_ids, batch_file_meta, task_type,
                      protect_content, retry_count,
                      group_id, file_unique_id, media_group_id
        """, [batch_size]), timeout=5.0)
    except asyncio.TimeoutError:
        logger.error("[DB] dequeue_jobs 查询超时(>5s),跳过本次")
        return []
    if not rows:
        return []
    try:
        import orjson as _json
    except ImportError:
        import json as _json
    results = []
    for row in rows:
        storage_msg_ids = []
        raw = row.get("storage_msg_ids", "")
        if raw:
            try:
                storage_msg_ids = _json.loads(raw) if isinstance(raw, str) else raw
            except (_json.JSONDecodeError, TypeError):
                storage_msg_ids = []
        results.append(JobResult(
            job_id=row["id"],
            code=row["code"],
            target_user_id=row["target_user_id"],
            storage_channel_id=row.get("storage_channel_id", 0),
            storage_msg_ids=storage_msg_ids,
            batch_file_meta=row.get("batch_file_meta", ""),
            task_type=row.get("task_type", "single"),
            protect_content=row.get("protect_content", False),
            retry_count=row.get("retry_count", 0),
            group_id=row.get("group_id", 0),
            file_unique_id=row.get("file_unique_id", ""),
            media_group_id=row.get("media_group_id", ""),
        ))
    return results


async def dequeue_job():
    """从 jobs 表取出一个待派工任务(保持向后兼容,内部调用 dequeue_jobs(1))"""
    results = await dequeue_jobs(1)
    return results[0] if results else None


async def get_pending_jobs_count() -> int:
    """获取 pending 状态的 jobs 数量（用于动态限速）。"""
    col = get_jobs_col()
    rows = await col._query(
        "SELECT COUNT(*) as cnt FROM jobs WHERE status = 'pending'",
    )
    if rows:
        return int(rows[0].get("cnt", 0))
    return 0


async def reenqueue_job(job_id: int) -> bool:
    """将一条 dispatched 的 job 重新标记为 pending 并递增 retry_count。
    不再手动重试 — CRDB 已内置自动重试机制，手动重试会导致同一请求计两次 RU。
    """
    col = get_jobs_col()
    try:
        result = await col._query("""
            UPDATE jobs SET status = 'pending', retry_count = COALESCE(retry_count, 0) + 1
            WHERE id = $1 AND status = 'dispatched'
            RETURNING id
        """, [job_id])
        return len(result) > 0
    except Exception as e:
        logger.error(f"[DB] reenqueue_job 失败 job_id={job_id}: {e}")
        return False


async def reenqueue_job_no_retry(job_id: int) -> bool:
    """将一条 dispatched 的 job 重新标记为 pending，不递增 retry_count。
    用于 semaphore 等待超时等尚未实际尝试发送的场景，避免白白消耗重试次数。
    不再手动重试 — CRDB 已内置自动重试机制。
    """
    col = get_jobs_col()
    try:
        result = await col._query("""
            UPDATE jobs SET status = 'pending'
            WHERE id = $1 AND status = 'dispatched'
            RETURNING id
        """, [job_id])
        return len(result) > 0
    except Exception as e:
        logger.error(f"[DB] reenqueue_job_no_retry 失败 job_id={job_id}: {e}")
        return False


class JobResult:
    def __init__(
        self,
        job_id: int,
        code: str,
        target_user_id: int,
        storage_channel_id: int,
        storage_msg_ids: list[int],
        batch_file_meta: str,
        task_type: str = "single",
        protect_content: bool = False,
        retry_count: int = 0,
        group_id: int = 0,
        file_unique_id: str = "",
        media_group_id: str = "",
    ):
        self.job_id = job_id
        self.code = code
        self.target_user_id = target_user_id
        self.storage_channel_id = storage_channel_id
        self.storage_msg_ids = storage_msg_ids or []
        self.batch_file_meta = batch_file_meta
        self.task_type = task_type
        self.protect_content = protect_content
        self.retry_count = retry_count
        # R36 B0-1: 结构化副本信息字段,供 ReplicaAwareResolver 直接消费
        # group_id=拓扑组号(1-5);file_unique_id=Telegram 文件唯一标识;media_group_id=相册分组键
        self.group_id = group_id
        self.file_unique_id = file_unique_id
        self.media_group_id = media_group_id


async def mark_job_dead(job_id: int, reason: str):
    """将反复失败的 job 标记为死信(status='dead'),不再重试。
    不再手动重试 — CRDB 已内置自动重试机制。
    """
    import datetime as _dt
    col = get_jobs_col()
    try:
        await col.update_one(
            {"id": job_id},
            {"$set": {
                "status": "dead",
                "dead_reason": reason,
                "dispatched_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            }},
        )
    except Exception as e:
        logger.error(f"[DB] mark_job_dead 失败 job_id={job_id}: {e}")


# ─── D: SQLite 本地队列同步函数 ─────────────────────────────────

async def sync_jobs_from_crdb_to_sqlite(limit: int = 100):
    """Queue Syncer: 从 CRDB 拉取 pending jobs 到本地 SQLite
    
    启动时一次性 + 30 分钟兜底同步。
    仅同步真正未处理的 pending 任务，避免空闲时反复扫描历史 dispatched 记录。
    """
    from loguru import logger
    from .cache_store import get_cache_store

    col = get_jobs_col()
    try:
        rows = await col.find(
            {"status": "pending"},
            sort=("created_at", 1),
            limit=limit,
            projection=["id", "code", "target_user_id", "storage_channel_id",
                         "storage_msg_ids", "batch_file_meta", "task_type",
                         "status", "retry_count", "protect_content", "created_at",
                         "group_id", "file_unique_id", "media_group_id"],
        )
    except Exception as e:
        logger.debug(f"[QueueSyncer] 拉取 CRDB jobs 失败: {e}")
        return

    if not rows:
        return

    store = get_cache_store()
    synced = 0
    for row in rows:
        job_id = row.get("id")
        if not job_id:
            continue
        await store.upsert_local_job(dict(row))
        synced += 1

    if synced > 0:
        logger.debug(f"[QueueSyncer] 同步 {synced} 条 job 到本地 SQLite")


async def sync_local_jobs_to_crdb():
    """Sync Back: 将本地 SQLite 中状态变更的 job 批量同步回 CRDB
    
    每 120 秒调用一次。处理 retried/dead/done 状态的 job。
    按 status 分组，每组各执行一条 SQL，替代 N 次 update_one。
    """
    from loguru import logger
    from .cache_store import get_cache_store

    store = get_cache_store()
    unsynced = await store.get_local_unsynced_jobs()
    if not unsynced:
        return

    # 按 status 分组
    groups: dict[str, list[dict]] = {"retried": [], "dead": [], "done": []}
    for job in unsynced:
        status = job.get("status", "")
        if status in groups:
            groups[status].append(job)

    try:
        affected = await batch_update_jobs_status(groups)
        for job in unsynced:
            await store.mark_local_job_synced(job["crdb_id"])
        if affected > 0:
            logger.debug(f"[SyncBack] 批量同步 {len(unsynced)} 条 job 状态到 CRDB")
    except Exception as e:
        logger.debug(f"[SyncBack] 批量同步 job 失败: {e}")


async def sync_dirty_cells_to_crdb():
    """Sync Back: 将本地 SQLite 中脏 cells（状态/路由变更）批量同步回 CRDB
    
    由 mon_bot 主循环每 ~5 分钟调用一次。仅同步异常事件和路由变更（ban/lost/rotation/degrade）。
    心跳、file_count、cursor 等高频数据不回写 CRDB。
    
    使用单条 SQL 批量 UPDATE（CASE WHEN 技巧），替代 N 次 update_one。
    """
    from loguru import logger
    from .cache_store import get_cache_store

    store = get_cache_store()
    dirty = await store.get_dirty_cells_local(50)
    if not dirty:
        return

    try:
        await batch_update_cells_dirty(dirty)
        for cell in dirty:
            await store.mark_cell_synced_local(cell["slot_id"])
        logger.debug(f"[SyncBack] 批量同步 {len(dirty)} 条 cell 到 CRDB")
    except Exception as e:
        logger.debug(f"[SyncBack] 批量同步 cell 失败: {e}")


# ─── 批量 UPDATE 优化（替代 N+1 循环）───

async def bulk_update_request_counts(counts: dict[str, int]) -> int:
    """单条 SQL 批量累加 file_records.request_count，替代 N 次 UPDATE。
    
    使用 CASE WHEN 技巧：UPDATE ... SET request_count = request_count + CASE code WHEN ...
    50 个码从 50 次 UPDATE(~500 RU) 压到 1 次(~10 RU)，节省 ~95% RU。
    """
    if not counts:
        return 0
    params = []
    cases = []
    for code, count in counts.items():
        params.append(code)
        params.append(count)
        cases.append(f"WHEN ${len(params) - 1} THEN ${len(params)}")
    
    placeholders = ", ".join([f"${i * 2 + 1}" for i in range(len(counts))])
    sql = (
        f"UPDATE file_records SET request_count = COALESCE(request_count, 0) + "
        f"CASE file_code {' '.join(cases)} END "
        f"WHERE file_code IN ({placeholders})"
    )
    col = get_file_records_col()
    return await col.execute_raw(sql, params)


async def batch_update_cells_dirty(cells: list[dict]) -> int:
    """单条 SQL 批量同步 dirty cells 到 CRDB，替代 N 次 update_one。
    
    对每个 cell 的不同字段用 CASE WHEN 聚合到一条 UPDATE 中。
    """
    if not cells:
        return 0
    import datetime as _dt
    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
    slot_ids = [c["slot_id"] for c in cells]
    
    # 收集所有字段值，按 slot_id 索引
    params = [now_iso]
    field_cases = {}
    for key in ("channel_id", "status", "next_active_chat_id", "demoted_to_channel_id",
                "account_name", "is_r100", "degrade_count", "file_count", "rotation_started_at"):
        parts = []
        seen_slots = set()  # 防止同一 slot_id 出现多个 WHEN（CASE WHEN 冲突）
        for i, c in enumerate(cells):
            val = c.get(key)
            if val is not None:
                sid = c["slot_id"]
                if sid in seen_slots:
                    continue
                seen_slots.add(sid)
                params.append(sid)
                params.append(val)
                parts.append(f"WHEN ${len(params) - 1} THEN ${len(params)}")
        if parts:
            # Handle lost cells: set next_active_chat_id to NULL
            if key == "next_active_chat_id":
                for i, c in enumerate(cells):
                    sid = c["slot_id"]
                    if c.get("status") == "lost" and sid not in seen_slots:
                        seen_slots.add(sid)
                        params.append(sid)
                        params.append(None)
                        parts.append(f"WHEN ${len(params) - 1} THEN ${len(params)}")
            field_cases[key] = " ".join(parts)
    
    set_clauses = ["updated_at = $1"]
    for key, cases in field_cases.items():
        if key == "next_active_chat_id":
            set_clauses.append(f"{key} = CASE slot_id {cases} END")
        else:
            set_clauses.append(f"{key} = CASE slot_id {cases} ELSE {key} END")
    
    placeholders = ", ".join([f"${len(params) + i + 1}" for i in range(len(slot_ids))])
    params.extend(slot_ids)
    
    sql = f"UPDATE cells SET {', '.join(set_clauses)} WHERE slot_id IN ({placeholders})"
    col = get_cells_col()
    return await col.execute_raw(sql, params)


async def batch_update_jobs_status(jobs_by_status: dict[str, list[dict]]) -> int:
    """批量同步 job 状态到 CRDB，按 status 分组各执行一条 SQL。
    
    jobs_by_status: {"retried": [...], "dead": [...], "done": [...]}
    """
    total = 0
    col = get_jobs_col()
    
    for status, jobs in jobs_by_status.items():
        if not jobs:
            continue
        ids = [j["crdb_id"] for j in jobs]
        placeholders = ", ".join([f"${i + 1}" for i in range(len(ids))])
        
        if status == "retried":
            sql = f"UPDATE jobs SET status = 'pending', retry_count = COALESCE(retry_count, 0) + 1 WHERE id IN ({placeholders})"
            total += await col.execute_raw(sql, ids)
        elif status == "done":
            sql = f"UPDATE jobs SET status = 'done' WHERE id IN ({placeholders})"
            total += await col.execute_raw(sql, ids)
        elif status == "dead":
            # dead_reason 各不同，需要 CASE WHEN
            params = []
            cases = []
            for j in jobs:
                params.append(j["crdb_id"])
                params.append(j.get("dead_reason", "unknown"))
                cases.append(f"WHEN ${len(params) - 1} THEN ${len(params)}")
            sql = (
                f"UPDATE jobs SET status = 'dead', "
                f"dead_reason = CASE id {' '.join(cases)} END "
                f"WHERE id IN ({placeholders})"
            )
            total += await col.execute_raw(sql, params)
    
    return total


async def get_pending_jobs_count_local() -> int:
    """D: 从本地 SQLite 获取 pending 任务数(0 RU)"""
    from .cache_store import get_cache_store
    store = get_cache_store()
    return await store.count_local_pending()


# ─── E1: cells 本地缓存 ─────────────────────────────────────────

_cells_local_version: int = 0
_cells_local_cache: list[dict] | None = None


async def get_active_cells_local() -> list[dict]:
    """从本地 SQLite 加载 active cells (0 RU)，优先 cells_local 表，兼容旧 snapshot。"""
    global _cells_local_cache, _cells_local_version
    from .cache_store import get_cache_store
    store = get_cache_store()

    if _cells_local_cache is None:
        all_cells = await store.get_all_cells_local()
        if all_cells:
            _cells_local_cache = all_cells
            _cells_local_version = int(_time.time() * 1000)
        else:
            snap_cells, version = await store.load_cells_snapshot()
            if snap_cells:
                _cells_local_cache = snap_cells
                _cells_local_version = version
                await store.bulk_upsert_cells_local(snap_cells)
        return [c for c in (_cells_local_cache or []) if c.get("status") == "active"]

    has_change, new_version = await store.has_cells_change(_cells_local_version)
    if has_change:
        all_cells = await store.get_all_cells_local()
        if all_cells:
            _cells_local_cache = all_cells
            _cells_local_version = new_version
        else:
            snap_cells, version = await store.load_cells_snapshot()
            if snap_cells:
                _cells_local_cache = snap_cells
                _cells_local_version = version

    return [c for c in (_cells_local_cache or []) if c.get("status") == "active"]


async def get_all_cells_local() -> list[dict]:
    """从本地 SQLite 加载全部 cells (0 RU)。"""
    from .cache_store import get_cache_store
    store = get_cache_store()
    cells = await store.get_all_cells_local()
    if cells:
        return cells
    snap_cells, _ = await store.load_cells_snapshot()
    if snap_cells:
        await store.bulk_upsert_cells_local(snap_cells)
    return snap_cells or []


_cell_by_ch_cache: dict[int, dict] = {}
_cell_by_ch_ts: float = 0
_cell_by_ch_ttl: float = 5.0


async def get_cell_by_channel_local(channel_id: int) -> dict | None:
    """按 channel_id 从本地 SQLite 查询 cell (0 RU)，带 5 秒进程内缓存。"""
    global _cell_by_ch_cache, _cell_by_ch_ts
    now = _time.time()
    if _cell_by_ch_cache and (now - _cell_by_ch_ts) < _cell_by_ch_ttl:
        return _cell_by_ch_cache.get(channel_id)
    all_cells = await get_all_cells_local()
    _cell_by_ch_cache = {c["channel_id"]: c for c in all_cells if "channel_id" in c}
    _cell_by_ch_ts = now
    return _cell_by_ch_cache.get(channel_id)


def invalidate_cell_by_channel_cache():
    """失效按 channel_id 查询的 cell 进程内缓存(P1-14 factory_reset / 拓扑变更调用)。"""
    global _cell_by_ch_cache, _cell_by_ch_ts
    _cell_by_ch_cache = {}
    _cell_by_ch_ts = 0


async def set_code_expiry(code: str, expires_at: str):
    """设置取件码的过期时间"""
    col = get_codes_col()
    await col.update_one(
        {"code": code},
        {"$set": {"expire_time": expires_at}},
    )


async def log_rotate(
    from_slot_id: str,
    to_slot_id: str,
    from_status: str,
    to_status: str,
    reason: str,
    triggered_by: str = "mon",
):
    """写降级审计日志"""
    import datetime as _dt
    col = get_rotate_log_col()
    await col.insert_one({
        "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "from_slot_id": from_slot_id,
        "to_slot_id": to_slot_id,
        "from_status": from_status,
        "to_status": to_status,
        "reason": reason,
        "triggered_by": triggered_by,
    })


# ─── 中继账号池 CRDB 备份 ──────────────────────────────────────

async def get_relay_accounts_from_crdb() -> list[dict]:
    """从 CRDB 拉取中继账号列表（用于 VPS 重启恢复）"""
    try:
        rows = await _query_raw(
            "SELECT id, api_id, api_hash, phone, is_active, created_at, last_login_at "
            "FROM relay_accounts WHERE is_active = 1 ORDER BY id"
        )
        return [
            {
                "id": r["id"],
                "api_id": r["api_id"],
                "api_hash": r["api_hash"],
                "phone": r["phone"],
                "is_active": bool(r["is_active"]),
                "created_at": r.get("created_at"),
                "last_login_at": r.get("last_login_at"),
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning(f"[RelayDB/CRDB] 拉取中继账号失败: {e}")
        return []


async def sync_relay_to_crdb(api_id: int, api_hash: str, phone: str):
    """同步中继账号到 CRDB（幂等操作，phone 有 UNIQUE 约束）"""
    try:
        await _client.execute(
            """INSERT INTO relay_accounts (api_id, api_hash, phone, is_active)
               VALUES ($1, $2, $3, 1)
               ON CONFLICT(phone) DO UPDATE SET
                   api_id = EXCLUDED.api_id,
                   api_hash = EXCLUDED.api_hash,
                   is_active = 1
            """,
            [api_id, api_hash, phone],
        )
    except Exception as e:
        logger.warning(f"[RelayDB/CRDB] 同步中继账号失败 (phone={phone}): {e}")


async def delete_relay_from_crdb(phone: str):
    """从 CRDB 删除中继账号(同步删除,避免重启后从 CRDB 拉回已删除的账号)"""
    try:
        await _client.execute(
            "DELETE FROM relay_accounts WHERE phone = $1",
            [phone],
        )
        logger.info(f"[RelayDB/CRDB] 已从 CRDB 删除中继账号 (phone={phone})")
    except Exception as e:
        logger.warning(f"[RelayDB/CRDB] 删除中继账号失败 (phone={phone}): {e}")


async def _query_raw(sql: str, params: list = None) -> list[dict]:
    """执行原始 SQL 查询，返回 dict 列表"""
    if _client._pool is None:
        logger.error("[DB] _query_raw: 连接池未初始化")
        return []
    async with _client._pool.acquire() as conn:
        records = await conn.fetch(sql, *(params or []))
        return [_row_to_dict(r) for r in records]


# ─── 清理函数已移除──────────────────────────────────────────────
# CRDB 行级 TTL 已启用(decode_logs + jobs 7 天自动清理)# RU 自动清理,Python 兜底清理不再需要
