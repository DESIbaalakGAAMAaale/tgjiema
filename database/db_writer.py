"""DBWriter 进程(方案B 的核心组件)

设计:
- BRPOP 消费 Redis Queue,串行写入 SQLite(独立连接,无并发冲突)
- 写完后 DEL Redis 缓冲 key(清除 redis_key)
- 信号处理:SIGTERM/SIGINT 触发优雅停止(消费完当前消息后退出)
- 批量消费:单次 BRPOP 取 WRITER_BATCH_SIZE 条消息,减少往返
- 错误处理:单条消息处理失败不影响后续消息,记录 ERROR 日志

架构:
  Redis Queue ─> DBWriter ─> SQLite ─> DEL Redis 缓冲 key
  (BRPOP)       (串行)      (WAL)     (清缓冲)

复用 CacheStore 的写方法,确保 SQL 语句与 bot 进程直写时一致。
Writer 独占 aiosqlite 连接,WAL 模式下无多进程锁冲突,
CacheStore 内置的 "locked" 重试逻辑不会触发(无竞争)。
"""
import asyncio
from dataclasses import dataclass

from loguru import logger

from database import redis_queue
from database.cache_store import CacheStore, DB_PATH


# 允许 DBWriter 调用的 cache_store 方法白名单(防止调用未授权方法)
# 完整覆盖所有可异步落盘的写操作(CAS/事务操作虽在 DIRECT_WRITE 集合中,
# 但此处也一并提供支持,以防未来路由策略调整)
_ALLOWED_METHODS: frozenset[str] = frozenset({
    # 心跳
    "write_heartbeat", "write_bot_heartbeat",
    # 配额
    "upsert_user_quota", "increment_user_quota_used", "refund_quota",
    "mark_quota_synced", "invalidate_user_quota",
    # 热路径全表缓存 CRUD
    "upsert_file_record_local", "upsert_code_local", "upsert_user_local",
    "mark_file_record_synced", "mark_code_synced", "mark_user_synced",
    # cells 本地逐行存储
    "update_cell_fields_local", "increment_cell_file_count_local",
    "mark_cell_synced_local",
    "batch_update_cells_local",  # 需要事务(BEGIN IMMEDIATE)
    "delete_cell_local",         # 需要事务(BEGIN IMMEDIATE)
    "save_cells_snapshot",
    # KV / TTL 缓存
    "set_kv", "cache_set",
    # 跨进程通知
    "notify_new_upload", "notify_dsp_new_job",
    "notify_relay_change", "notify_record_change",
    # 用户 Bot 启动状态
    "mark_user_started", "add_pending_file_code", "delete_pending_file_code",
    # 本地任务队列
    "update_local_job_status", "retry_local_job", "retry_local_dead_job",
    "mark_local_job_synced", "cleanup_local_jobs",
    # 启动统计快照
    "save_counter_snapshot",
    # Manifest 副本同步
    "upsert_manifest", "upsert_manifest_batch",
    # 通用
    "delete", "cleanup", "cleanup_notify_tables",
})


@dataclass
class DBWriterMessage:
    """从 Redis Queue 弹出的消息(对应 cache_store 的写操作)"""
    op_type: str          # upsert/update/delete/insert
    table: str            # SQLite 表名
    method_name: str      # cache_store 方法名(Writer 用于分派)
    data: dict            # 方法参数(解包为关键字参数调用 method_name)
    redis_key: str        # Writer 写完后 DEL 的 key(空串表示无关联缓存)
    created_at: float     # 消息创建时间(用于监控队列延迟)


class DBWriter:
    """DBWriter 进程:消费 Redis Queue,串行写入 SQLite。

    独占一个 aiosqlite 连接(复用 CacheStore 的 DB_PATH),
    WAL 模式下串行写入,无多进程锁冲突。
    """

    def __init__(self):
        self._store: CacheStore | None = None
        self._running: bool = False
        self._processed_count: int = 0
        self._error_count: int = 0

    async def init(self):
        """初始化 SQLite 连接(独立连接,WAL 模式)。

        复用 CacheStore 的 DB_PATH,确保表结构与 bot 进程一致。
        PRAGMA 设置:journal_mode=WAL, synchronous=NORMAL, busy_timeout=15000。
        """
        # P1修复: 降级模式下优雅退出(exit 0),不触发 systemd 重启
        from config import settings
        if settings.WRITER_MODE != "redis" or not settings.REDIS_URL:
            logger.info("[DBWriter] WRITER_MODE=sqlite 或 Redis 未配置,db_writer 不需要运行,优雅退出")
            import sys
            sys.exit(0)

        # 健康检查:确保 Redis 可达(否则无消息可消费)
        healthy = await redis_queue.health_check()
        if not healthy:
            logger.error("[DBWriter] Redis 不可达,Writer 无法消费消息,退出")
            raise RuntimeError("Redis 不可达,DBWriter 无法启动")

        # 初始化 CacheStore(复用其 SQLite 连接 + DDL + 写方法)
        self._store = CacheStore()
        await self._store.init()
        logger.info(f"[DBWriter] 初始化完成,SQLite 路径: {DB_PATH}")

    async def start(self):
        """主消费循环:BRPOP 消费 Redis Queue,串行写入 SQLite。

        单次 BRPOP 取 WRITER_BATCH_SIZE 条消息,减少往返。
        不注册自定义 SIGTERM handler,让 asyncio.run 通过 CancelledError 传播信号。
        信号到达时在当前 await 点抛出 CancelledError,finally 块负责清理资源。
        """
        from config import settings
        batch_size = settings.WRITER_BATCH_SIZE
        self._running = True
        logger.info(f"[DBWriter] 消费循环启动,批量大小: {batch_size}")

        while self._running:
            try:
                # BRPOP 阻塞消费,timeout=1 保证能及时响应停止信号
                messages = await redis_queue.pop(timeout=1, count=batch_size)
                if not messages:
                    continue
                for msg in messages:
                    await self._process_message(msg)
            except asyncio.CancelledError:
                logger.info("[DBWriter] 收到 CancelledError,准备停止")
                self._running = False
                raise
            except Exception as e:
                # 非预期异常(如 Redis 连接断开),记录后短暂休眠再重试
                logger.error(f"[DBWriter] 消费循环异常: {e}")
                await asyncio.sleep(1)

        logger.info("[DBWriter] 消费循环已停止")

    async def stop(self):
        """优雅停止:设置 _running=False,等当前消息处理完。

        由 run_db_writer 的 finally 块调用,确保即使 CancelledError 也能执行。
        """
        self._running = False
        logger.info(
            f"[DBWriter] 停止完成,已处理 {self._processed_count} 条消息,"
            f"失败 {self._error_count} 条"
        )

    async def close(self):
        """关闭 SQLite 连接和 Redis 连接。"""
        if self._store:
            await self._store.close()
            self._store = None
        await redis_queue.close_redis()
        logger.info("[DBWriter] 资源已清理")

    async def _process_message(self, msg):
        """处理单条消息:解析 → 执行 SQLite → DEL 缓冲 key。

        单条消息处理失败不影响后续消息,记录 ERROR 后转入死信队列。
        CancelledError 不被捕获(它是 BaseException 的子类),会向上传播触发优雅停止。

        P0修复: 失败消息转入死信队列(tgjiema:writer:dead),避免永久丢失
        P1修复: msg 类型校验(非 dict 直接转死信)
        P1修复: TypeError(方法签名不匹配)单独处理为永久失败,入死信不重试
        """
        # P1修复: msg 类型校验(防止 BRPOP 返回非 dict 导致 msg.get 崩溃)
        if not isinstance(msg, dict):
            self._error_count += 1
            logger.error(f"[DBWriter] 消息非 dict 类型: {type(msg).__name__}, 转入死信队列")
            await redis_queue.push_dead(msg, reason="msg is not a dict")
            return

        writer_msg = DBWriterMessage(
            op_type=msg.get("op_type", ""),
            table=msg.get("table", ""),
            method_name=msg.get("method_name", ""),
            data=msg.get("data", {}) or {},
            redis_key=msg.get("redis_key", ""),
            created_at=msg.get("created_at", 0),
        )
        try:
            # 执行 SQLite 写操作
            await self._execute_sqlite(writer_msg)
            self._processed_count += 1
            # 写完后 DEL Redis 缓冲 key(清除缓冲,以 SQLite 为权威)
            # 仅当 SQLite 写成功后才 DEL,失败时保留旧缓存避免读到半成品
            if writer_msg.redis_key:
                await redis_queue.delete(writer_msg.redis_key)
        except TypeError as e:
            # P1修复: TypeError 通常是方法签名不匹配(参数名错误/缺失),
            # 属于永久性错误,重试也不会成功,直接入死信队列
            self._error_count += 1
            logger.error(
                f"[DBWriter] 方法签名不匹配(永久失败,入死信): "
                f"method={writer_msg.method_name}, table={writer_msg.table}: {e}"
            )
            await redis_queue.push_dead(
                msg, reason=f"TypeError: {e}"
            )
        except Exception as e:
            self._error_count += 1
            logger.error(
                f"[DBWriter] 消息处理失败(入死信): method={writer_msg.method_name}, "
                f"table={writer_msg.table}: {e}"
            )
            # P0修复: 失败消息转入死信队列,避免永久丢失
            await redis_queue.push_dead(
                msg, reason=f"{type(e).__name__}: {e}"
            )
            # 不抛出,继续处理下一条消息

    async def _execute_sqlite(self, msg: DBWriterMessage):
        """根据 method_name 分派到对应的 CacheStore 写方法。

        复用 CacheStore 的写方法,确保 SQL 语句与 bot 进程直写时一致。
        Writer 独占连接,无锁冲突,CacheStore 的 "locked" 重试逻辑不会触发。

        支持的方法包括:
        - write_heartbeat, write_bot_heartbeat
        - upsert_user_quota, upsert_file_record_local, upsert_code_local, upsert_user_local
        - update_cell_fields_local, increment_cell_file_count_local
        - set_kv, cache_set
        - notify_new_upload, notify_dsp_new_job, notify_relay_change, notify_record_change
        - mark_user_started, add_pending_file_code, delete_pending_file_code
        - update_local_job_status, retry_local_job, retry_local_dead_job
        - mark_local_job_synced, mark_quota_synced, invalidate_user_quota
        - increment_user_quota_used, refund_quota
        - mark_cell_synced_local, mark_file_record_synced, mark_code_synced, mark_user_synced
        - save_counter_snapshot, save_cells_snapshot
        - upsert_manifest, upsert_manifest_batch
        - delete, cleanup, cleanup_notify_tables, cleanup_local_jobs
        - batch_update_cells_local(需要事务), delete_cell_local(需要事务)
        """
        if not self._store:
            raise RuntimeError("CacheStore 未初始化")

        if not msg.method_name:
            raise ValueError("消息缺少 method_name")

        # 方法名白名单校验(防止调用 init/close/load 等非写方法)
        if msg.method_name not in _ALLOWED_METHODS:
            raise ValueError(f"未授权的方法名: {msg.method_name}")

        method = getattr(self._store, msg.method_name, None)
        if method is None or not callable(method):
            raise ValueError(f"未知的方法名: {msg.method_name}")

        # 调用对应的 CacheStore 方法,data 字典解包为关键字参数
        # 例如:write_heartbeat(slot_id="slot1", ok=True)
        data = msg.data if isinstance(msg.data, dict) else {}
        await method(**data)
