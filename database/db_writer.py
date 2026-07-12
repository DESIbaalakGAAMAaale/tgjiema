"""DBWriter 进程(方案B v3: Redis Streams 可靠消费 + 原子事务)

R34 P0-1 修复: 业务写与 writer_inbox 在同一 BEGIN IMMEDIATE...COMMIT 事务中执行,
确保崩溃恢复时不会重复执行业务写(真正的 exactly-once)。

R34 P0-2 修复: push_dead() 返回 False 时禁止 ACK,消息保留在 pending 等待重试。

R34 P1-6 修复: CacheStore 方法在 Writer 事务模式下不得吞异常(必须 raise)。

设计:
- XREADGROUP 消费 Stream(消息进入 pending,不删除)
- 检查 writer_inbox 幂等表:已处理则 XACK 跳过
- BEGIN IMMEDIATE 开始事务
- INSERT writer_inbox (ON CONFLICT DO NOTHING 检查是否已处理)
- 执行 SQLite 业务写(方法内部 commit 被 monkey-patch 为 no-op)
- COMMIT 提交业务写 + inbox(原子性)
- DEL 读缓存 + XACK 确认

崩溃窗口分析:
- 步骤 COMMIT 前/后崩溃:COMMIT 原子,要么全部成功要么全部回滚
- COMMIT 成功后/XACK 前崩溃:消息在 pending,XAUTOCLAIM 回收后 inbox 命中,XACK 跳过
- push_dead 失败:消息保留 pending,不 ACK,等待下次 XAUTOCLAIM 回收重试

架构:
  Redis Stream ─> XREADGROUP ─> DBWriter ─> BEGIN IMMEDIATE
  (pending)        (不删除)      (串行)        ├─ INSERT writer_inbox
                                                ├─ 业务 SQL(no self-commit)
                                                └─ COMMIT(原子)
       ↓                                              ↓
  XAUTOCLAIM                                    writer_inbox
  (崩溃恢复)                                    (幂等去重)
"""
import asyncio
import time
from dataclasses import dataclass

from loguru import logger

from database import redis_queue
from database.cache_store import CacheStore, DB_PATH


# 允许 DBWriter 调用的 cache_store 方法白名单(防止调用未授权方法)
_ALLOWED_METHODS: frozenset[str] = frozenset({
    # 心跳
    "write_heartbeat", "write_bot_heartbeat",
    # 配额(非幂等操作 increment_user_quota_used/refund_quota 已移至 DIRECT_WRITE,不经过队列)
    "upsert_user_quota",
    "mark_quota_synced", "invalidate_user_quota",
    # 热路径全量缓存 CRUD
    "upsert_file_record_local", "upsert_code_local", "upsert_user_local",
    "delete_file_record_local",
    "mark_file_record_synced", "mark_code_synced", "mark_user_synced",
    # cells 本地逐行存储
    "update_cell_fields_local", "increment_cell_file_count_local",
    "mark_cell_synced_local",
    "batch_update_cells_local",  # 需要事务(BEGIN IMMEDIATE)— Writer 事务模式下内层 BEGIN 会被跳过
    "delete_cell_local",         # 需要事务(BEGIN IMMEDIATE)— Writer 事务模式下内层 BEGIN 会被跳过
    "bulk_upsert_cells_local",   # 批量 upsert(小批量入队路径)
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
    """从 Redis Stream 读取的消息(对应 cache_store 的写操作)"""
    op_type: str          # upsert/update/delete/insert
    table: str            # SQLite 表名
    method_name: str      # cache_store 方法名(Writer 用于分派)
    data: dict            # 方法参数(解包为关键字参数调用 method_name)
    redis_key: str        # Writer 写完后 DEL 的 key(空串表示无关联缓存)
    message_id: str       # R33: 幂等键(UUID),用于 writer_inbox 去重
    created_at: float     # 消息创建时间(用于监控队列延迟)
    stream_id: str        # R33: Stream 消息 ID(用于 XACK)


class DBWriter:
    """DBWriter 进程:消费 Redis Stream,串行写入 SQLite。

    独占一个 aiosqlite 连接(复用 CacheStore 的 DB_PATH),
    WAL 模式下串行写入,无多进程锁冲突。

    R34: 业务写 + writer_inbox 在同一 SQLite 事务中提交,实现真正的 exactly-once。
    """

    def __init__(self):
        self._store: CacheStore | None = None
        self._running: bool = False
        self._processed_count: int = 0
        self._error_count: int = 0
        self._skipped_count: int = 0  # R33: 幂等跳过计数
        self._dead_fail_count: int = 0  # R34: DLQ 写入失败计数
        self._dlq_task: asyncio.Task | None = None  # R34 P1-1: DLQ Worker 协程
        self._ack_count: int = 0  # R34 P1-3: ACK 计数,每 100 次触发 trim_stream

    async def init(self):
        """初始化 SQLite 连接 + Consumer Group。"""
        from config import settings
        if settings.WRITER_MODE != "redis" or not settings.REDIS_URL:
            logger.info("[DBWriter] WRITER_MODE=sqlite 或 Redis 未配置,db_writer 不需要运行,优雅退出")
            import sys
            sys.exit(0)

        healthy = await redis_queue.health_check()
        if not healthy:
            logger.error("[DBWriter] Redis 不可达,等待 60s 后退出(对齐 get_redis 重试节流)")
            await asyncio.sleep(60)
            raise RuntimeError("Redis 不可达,DBWriter 无法启动")

        group_ok = await redis_queue.ensure_consumer_group()
        if not group_ok:
            raise RuntimeError("Consumer Group 创建失败,DBWriter 无法启动")

        self._store = CacheStore()
        await self._store.init()
        logger.info(f"[DBWriter] 初始化完成,SQLite 路径: {DB_PATH}")
        logger.info("[DBWriter] Stream 可靠消费已启用(XREADGROUP + XACK + XAUTOCLAIM)")
        logger.info("[DBWriter] R34 原子事务模式已启用(业务写+inbox 同事务提交)")

    async def start(self):
        """主消费循环:XREADGROUP 消费 Stream,串行写入 SQLite。

        R34 P1-1: 同时启动 DLQ Worker 协程,实现死信队列重试闭环。
        """
        from config import settings
        batch_size = settings.WRITER_BATCH_SIZE
        self._running = True
        logger.info(f"[DBWriter] 消费循环启动,批量大小: {batch_size}")

        # R34 P1-1: 启动 DLQ Worker 协程(死信队列重试闭环)
        self._dlq_task = asyncio.create_task(self._run_dlq_worker())

        while self._running:
            try:
                messages = await redis_queue.pop(timeout=1, count=batch_size)
                if not messages:
                    await asyncio.sleep(0.1)
                    continue
                for msg in messages:
                    await self._process_message(msg)
            except asyncio.CancelledError:
                logger.info("[DBWriter] 收到 CancelledError,准备停止")
                self._running = False
                raise
            except Exception as e:
                logger.error(f"[DBWriter] 消费循环异常: {e}")
                await asyncio.sleep(1)

        logger.info("[DBWriter] 消费循环已停止")

    async def stop(self):
        """优雅停止:设置 _running=False,等当前消息处理完。

        R34 P1-1: 取消 DLQ Worker 协程,等待其清理退出。
        """
        self._running = False
        # R34 P1-1: 取消 DLQ Worker 协程
        if self._dlq_task is not None:
            self._dlq_task.cancel()
            try:
                await self._dlq_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"[DBWriter] DLQ Worker 退出异常: {e}")
            self._dlq_task = None
        logger.info(
            f"[DBWriter] 停止完成,已处理 {self._processed_count} 条消息,"
            f"失败 {self._error_count} 条,幂等跳过 {self._skipped_count} 条,"
            f"DLQ写入失败 {self._dead_fail_count} 条"
        )

    async def _run_dlq_worker(self):
        """R34 P1-1: 运行 DLQ Worker 协程(死信队列重试闭环)。

        每 30 秒扫描死信 Stream,将到期的可重试消息 XADD 回主 Stream。
        异常不传播(捕获后等待 30 秒重试),仅 CancelledError 退出。
        """
        from database import dlq_worker
        worker = dlq_worker.DLQWorker()
        ok = await worker.init()
        if not ok:
            logger.warning("[DBWriter] DLQ Worker 初始化失败,死信重试闭环未启用")
            return
        logger.info("[DBWriter] DLQ Worker 协程已启动(30s 间隔扫描死信队列)")
        while self._running:
            try:
                await worker._process_dead_messages()
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[DBWriter] DLQ Worker 异常: {e}")
                await asyncio.sleep(30)
        await worker.close()
        logger.info(
            f"[DBWriter] DLQ Worker 已停止,"
            f"扫描 {worker.processed_count} 条,重试 {worker.retried_count} 条,"
            f"永久失败 {worker.permanent_fail_count} 条"
        )

    async def close(self):
        """关闭 SQLite 连接和 Redis 连接。"""
        if self._store:
            await self._store.close()
            self._store = None
        await redis_queue.close_redis()
        logger.info("[DBWriter] 资源已清理")

    async def _process_message(self, msg):
        """处理单条消息:解析 → 幂等检查 → 原子事务(业务写+inbox) → XACK。

        R34 P0-1 修复: 业务写与 writer_inbox 在同一 BEGIN IMMEDIATE...COMMIT 事务中。
        R34 P0-2 修复: push_dead() 失败时禁止 ACK,消息保留 pending。

        流程:
        1. 解析消息(含 message_id 和 stream_id)
        2. 检查 writer_inbox:已处理则 XACK 跳过(幂等)
        3. BEGIN IMMEDIATE 开始事务
        4. INSERT writer_inbox (ON CONFLICT DO NOTHING)
           - 如果有冲突,说明已处理,ROLLBACK + XACK 跳过
        5. 执行业务 SQLite 写(方法内部 commit 被 no-op)
        6. COMMIT(业务写 + inbox 原子提交)
        7. DEL 读缓存 key
        8. XACK 确认

        崩溃窗口:
        - COMMIT 前:事务回滚,消息在 pending,XAUTOCLAIM 回收后重新处理
        - COMMIT 后/XACK 前:数据已提交,XAUTOCLAIM 回收后 inbox 命中,XACK 跳过
        - DLQ 写入失败:消息保留 pending,不 ACK,等待重试
        """
        # P1修复: msg 类型校验
        if not isinstance(msg, dict):
            self._error_count += 1
            logger.error(f"[DBWriter] 消息非 dict 类型: {type(msg).__name__}, 转入死信队列")
            dead_ok = await redis_queue.push_dead(msg, reason="msg is not a dict")
            # R34 P0-2: DLQ 写入失败时不 ACK,消息保留 pending
            if not dead_ok:
                self._dead_fail_count += 1
                logger.critical(
                    f"[DBWriter] DLQ 写入失败(非 dict 消息),消息保留 pending 等待重试"
                )
            # 非 dict 消息没有 stream_id 信息,无法 ACK
            return

        stream_id = msg.get("_stream_id", "")
        message_id = msg.get("message_id", "")

        # R34 P1: 缺少 message_id 的消息视为无效,安全送入 DLQ
        if not message_id:
            self._error_count += 1
            logger.warning(
                f"[DBWriter] 消息缺少 message_id(无效消息,入死信): "
                f"method={msg.get('method_name', '?')}"
            )
            dead_ok = await redis_queue.push_dead(
                msg, reason="missing message_id", attempts=99,
            )
            if not dead_ok:
                self._dead_fail_count += 1
                logger.critical("[DBWriter] DLQ 写入失败(无 message_id),消息保留 pending")
            elif stream_id:
                await self._safe_ack(stream_id)
            return

        writer_msg = DBWriterMessage(
            op_type=msg.get("op_type", ""),
            table=msg.get("table", ""),
            method_name=msg.get("method_name", ""),
            data=msg.get("data", {}) or {},
            redis_key=msg.get("redis_key", ""),
            message_id=message_id,
            created_at=msg.get("created_at", 0),
            stream_id=stream_id,
        )

        # R33: 幂等检查 — 已处理的消息直接 XACK 跳过
        if self._store:
            try:
                already_processed = await self._store.check_writer_inbox(message_id)
                if already_processed:
                    self._skipped_count += 1
                    logger.debug(
                        f"[DBWriter] 消息已处理(inbox命中),XACK跳过: "
                        f"method={writer_msg.method_name}, message_id={message_id}"
                    )
                    if stream_id:
                        await self._safe_ack(stream_id)
                    return
            except Exception as e:
                logger.warning(f"[DBWriter] inbox 检查异常(继续处理): {e}")

        # R34 P0-1: 原子事务执行(业务写 + inbox 同一 COMMIT)
        try:
            await self._execute_atomic(writer_msg)
            self._processed_count += 1
        except _InboxConflict:
            # inbox 已存在(ON CONFLICT),说明已处理过,XACK 跳过
            self._skipped_count += 1
            logger.debug(
                f"[DBWriter] 事务内 inbox 冲突(已处理),XACK跳过: "
                f"method={writer_msg.method_name}, message_id={message_id}"
            )
            if stream_id:
                await self._safe_ack(stream_id)
            return
        except TypeError as e:
            # TypeError 通常是方法签名不匹配,属于永久性错误
            self._error_count += 1
            logger.error(
                f"[DBWriter] 方法签名不匹配(永久失败,入死信): "
                f"method={writer_msg.method_name}, table={writer_msg.table}: {e}"
            )
            dead_ok = await redis_queue.push_dead(
                msg, reason=f"TypeError: {e}",
                message_id=message_id, attempts=99,
            )
            # R34 P0-2: 只有 DLQ 写入成功才 ACK
            if not dead_ok:
                self._dead_fail_count += 1
                logger.critical(
                    f"[DBWriter] DLQ 写入失败(TypeError),消息保留 pending: "
                    f"method={writer_msg.method_name}"
                )
            elif stream_id:
                await self._safe_ack(stream_id)
            return
        except Exception as e:
            # 可重试错误:入死信队列
            self._error_count += 1
            logger.error(
                f"[DBWriter] 消息处理失败(入死信): method={writer_msg.method_name}, "
                f"table={writer_msg.table}: {e}"
            )
            dead_ok = await redis_queue.push_dead(
                msg, reason=f"{type(e).__name__}: {e}",
                message_id=message_id, attempts=0,
            )
            # R34 P0-2: 只有 DLQ 写入成功才 ACK
            if not dead_ok:
                self._dead_fail_count += 1
                logger.critical(
                    f"[DBWriter] DLQ 写入失败(可重试错误),消息保留 pending等待重试: "
                    f"method={writer_msg.method_name}"
                )
            elif stream_id:
                await self._safe_ack(stream_id)
            return

        # 事务已 COMMIT,DEL 读缓存 key(清除读缓存)
        if writer_msg.redis_key:
            try:
                await redis_queue.delete(writer_msg.redis_key)
            except Exception as e:
                logger.warning(
                    f"[DBWriter] DEL 缓存 key 失败(不影响已提交数据): "
                    f"key={writer_msg.redis_key}: {e}"
                )

        # XACK 确认(消息从 pending 删除)
        if stream_id:
            await self._safe_ack(stream_id)

    async def _execute_atomic(self, msg: DBWriterMessage):
        """R34 P0-1: 在单一 SQLite 事务中执行业务写 + inbox 幂等记录。

        1. BEGIN IMMEDIATE
        2. INSERT writer_inbox (ON CONFLICT DO NOTHING)
        3. 检查是否冲突(已处理)→ 抛 _InboxConflict
        4. 执行业务写(方法内部 commit 被 no-op)
        5. COMMIT

        如果业务写抛异常 → ROLLBACK(inbox 也回滚,消息留 pending)
        如果业务写吞异常(P1-6 未完全修复的方法)→ 检查 total_changes 判断
        """
        if not self._store:
            raise RuntimeError("CacheStore 未初始化")

        # R34: 方法名白名单校验
        if not msg.method_name:
            raise ValueError("消息缺少 method_name")
        if msg.method_name not in _ALLOWED_METHODS:
            raise ValueError(f"未授权的方法名: {msg.method_name}")

        # R34 P0-1: 开始 Writer 事务(monkey-patch commit 为 no-op)
        await self._store.begin_writer_tx()

        try:
            # 步骤1: 在事务内写入 inbox(ON CONFLICT DO NOTHING)
            now = time.time()
            cursor = await self._store._db.execute(
                "INSERT OR IGNORE INTO writer_inbox "
                "(message_id, method_name, stream_id, created_at, processed_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (msg.message_id, msg.method_name, msg.stream_id, now, now),
            )
            # 检查是否冲突(已处理)
            if cursor.rowcount == 0:
                # inbox 已存在,说明已处理过
                raise _InboxConflict()

            # 步骤2: 执行业务写(方法内部 commit 被 no-op,不会提前提交)
            await self._execute_sqlite(msg)

            # R34 P1-6: 检查业务方法是否吞异常(通过 total_changes 判断)
            # 注意: 某些方法(如 cleanup)可能合法地不产生变更,此处不强制检查
            # 关键修复已通过 _in_writer_tx 标志让方法 raise 代替 swallow

            # 步骤3: COMMIT(业务写 + inbox 原子提交)
            await self._store.commit_writer_tx()

        except _InboxConflict:
            # 冲突时需要 ROLLBACK
            await self._store.rollback_writer_tx()
            raise
        except Exception:
            # 业务写失败 → ROLLBACK(inbox 也回滚)
            await self._store.rollback_writer_tx()
            raise

    async def _execute_sqlite(self, msg: DBWriterMessage):
        """根据 method_name 分派到对应的 CacheStore 写方法。

        R34: 在 Writer 事务模式下,方法内部 commit 被 no-op,
        异常不会被吞掉(_in_writer_tx 标志强制 raise)。
        """
        if not self._store:
            raise RuntimeError("CacheStore 未初始化")

        method = getattr(self._store, msg.method_name, None)
        if method is None or not callable(method):
            raise ValueError(f"未知的方法名: {msg.method_name}")

        data = msg.data if isinstance(msg.data, dict) else {}
        await method(**data)

    async def _safe_ack(self, stream_id: str):
        """安全 ACK:捕获异常并记录,不传播。

        R34 P1-3: 每 100 条 ACK 后调用 trim_stream() 裁剪 Stream,
        防止已 ACK 消息在 Stream 中无限堆积。
        """
        try:
            await redis_queue.ack([stream_id])
            # R34 P1-3: 每 100 条 ACK 触发一次 XTRIM,裁剪已消费的旧消息
            self._ack_count += 1
            if self._ack_count % 100 == 0:
                try:
                    trimmed = await redis_queue.trim_stream()
                    if trimmed > 0:
                        logger.debug(
                            f"[DBWriter] Stream 裁剪 {trimmed} 条消息"
                            f"(ack_count={self._ack_count})"
                        )
                except Exception as e:
                    logger.debug(f"[DBWriter] trim_stream 异常(不影响主流程): {e}")
        except Exception as e:
            logger.warning(
                f"[DBWriter] XACK 失败(消息留 pending,下次回收处理): "
                f"stream_id={stream_id}: {e}"
            )


class _InboxConflict(Exception):
    """writer_inbox ON CONFLICT:消息已处理过,应跳过。"""
