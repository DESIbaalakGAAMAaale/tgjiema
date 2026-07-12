"""DBWriter 进程(方案B v2: Redis Streams 可靠消费)

R33 P0 修复: 从 List BRPOP 改为 Streams XREADGROUP,消息进入 pending 不删除,
SQLite 提交后 XACK 确认。崩溃后 XAUTOCLAIM 回收 pending 消息,配合 writer_inbox
幂等表实现 exactly-once 语义。

设计:
- XREADGROUP 消费 Stream(消息进入 pending,不删除)
- 检查 writer_inbox 幂等表:已处理则 XACK 跳过
- 执行 SQLite 写操作
- 写入 writer_inbox(message_id, method_name)
- XACK 确认(消息从 pending 删除)
- 信号处理:SIGTERM/SIGINT 触发优雅停止
- 崩溃恢复:启动时 XAUTOCLAIM 回收 pending >30s 的消息

架构:
  Redis Stream ─> XREADGROUP ─> DBWriter ─> SQLite 写
  (pending)        (不删除)      (串行)      (WAL)
       ↓                                      ↓
  XAUTOCLAIM                              writer_inbox
  (崩溃恢复)                              (幂等去重)
       ↓                                      ↓
  重启后重处理 ←── inbox 检查 ──── 已处理则 XACK 跳过
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
    "batch_update_cells_local",  # 需要事务(BEGIN IMMEDIATE)
    "delete_cell_local",         # 需要事务(BEGIN IMMEDIATE)
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

    R33: 使用 Streams Consumer Group 实现可靠消费,
    配合 writer_inbox 表实现幂等,崩溃不丢数据。
    """

    def __init__(self):
        self._store: CacheStore | None = None
        self._running: bool = False
        self._processed_count: int = 0
        self._error_count: int = 0
        self._skipped_count: int = 0  # R33: 幂等跳过计数

    async def init(self):
        """初始化 SQLite 连接 + Consumer Group。

        R33: 启动时确保 Consumer Group 存在,XAUTOCLAIM 会自动回收
        上一个 db_writer 崩溃遗留的 pending 消息。
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
            # P2修复: 等待 60s 对齐 get_redis 重试节流,避免 systemd 紧密重启循环
            logger.error("[DBWriter] Redis 不可达,等待 60s 后退出(对齐 get_redis 重试节流)")
            await asyncio.sleep(60)
            raise RuntimeError("Redis 不可达,DBWriter 无法启动")

        # R33: 确保 Consumer Group 存在
        group_ok = await redis_queue.ensure_consumer_group()
        if not group_ok:
            raise RuntimeError("Consumer Group 创建失败,DBWriter 无法启动")

        # 初始化 CacheStore(复用其 SQLite 连接 + DDL + 写方法)
        self._store = CacheStore()
        await self._store.init()
        logger.info(f"[DBWriter] 初始化完成,SQLite 路径: {DB_PATH}")
        logger.info("[DBWriter] Stream 可靠消费已启用(XREADGROUP + XACK + XAUTOCLAIM)")

    async def start(self):
        """主消费循环:XREADGROUP 消费 Stream,串行写入 SQLite。

        R33: 消息进入 pending 不会被删除,SQLite 提交后 XACK 确认。
        如果 XACK 失败,消息留在 pending,下次启动时 XAUTOCLAIM 回收,
        writer_inbox 幂等表确保不会重复执行。
        """
        from config import settings
        batch_size = settings.WRITER_BATCH_SIZE
        self._running = True
        logger.info(f"[DBWriter] 消费循环启动,批量大小: {batch_size}")

        while self._running:
            try:
                # XREADGROUP 消费(含 XAUTOCLAIM 回收)
                messages = await redis_queue.pop(timeout=1, count=batch_size)
                if not messages:
                    # P1修复: Redis 宕机/降级时防止 100% CPU 忙等
                    await asyncio.sleep(0.1)
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
        """优雅停止:设置 _running=False,等当前消息处理完。"""
        self._running = False
        logger.info(
            f"[DBWriter] 停止完成,已处理 {self._processed_count} 条消息,"
            f"失败 {self._error_count} 条,幂等跳过 {self._skipped_count} 条"
        )

    async def close(self):
        """关闭 SQLite 连接和 Redis 连接。"""
        if self._store:
            await self._store.close()
            self._store = None
        await redis_queue.close_redis()
        logger.info("[DBWriter] 资源已清理")

    async def _process_message(self, msg):
        """处理单条消息:解析 → 幂等检查 → 执行 SQLite → 写inbox → XACK。

        R33 P0修复: 可靠消费流程
        1. 解析消息(含 message_id 和 stream_id)
        2. 检查 writer_inbox:已处理则 XACK 跳过(幂等)
        3. 执行 SQLite 写操作
        4. 写入 writer_inbox(message_id, method_name, stream_id)
        5. DEL 读缓存 key(如有)
        6. XACK 确认(消息从 pending 删除)

        如果步骤 3-4 之间崩溃:消息留在 pending,XAUTOCLAIM 回收后重新处理,
        inbox 检查通过则跳过(但数据已写入,SQLite 的 upsert 是幂等的)。

        如果步骤 4-6 之间崩溃:inbox 已写入但 XACK 未执行,
        XAUTOCLAIM 回收后 inbox 检查命中,XACK 跳过。

        CancelledError 不被捕获(它是 BaseException 的子类),会向上传播触发优雅停止。
        """
        # P1修复: msg 类型校验(防止非 dict 导致 msg.get 崩溃)
        if not isinstance(msg, dict):
            self._error_count += 1
            logger.error(f"[DBWriter] 消息非 dict 类型: {type(msg).__name__}, 转入死信队列")
            await redis_queue.push_dead(msg, reason="msg is not a dict")
            return

        stream_id = msg.get("_stream_id", "")
        message_id = msg.get("message_id", "")

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

        # R33 P1修复: 幂等检查 — 已处理的消息直接 XACK 跳过
        if message_id and self._store:
            try:
                already_processed = await self._store.check_writer_inbox(message_id)
                if already_processed:
                    self._skipped_count += 1
                    logger.debug(
                        f"[DBWriter] 消息已处理(inbox命中),XACK跳过: "
                        f"method={writer_msg.method_name}, message_id={message_id}"
                    )
                    if stream_id:
                        await redis_queue.ack([stream_id])
                    return
            except Exception as e:
                logger.warning(f"[DBWriter] inbox 检查异常(继续处理): {e}")

        # 执行 SQLite 写操作
        try:
            await self._execute_sqlite(writer_msg)
            self._processed_count += 1
        except TypeError as e:
            # TypeError 通常是方法签名不匹配,属于永久性错误,直接入死信队列
            self._error_count += 1
            logger.error(
                f"[DBWriter] 方法签名不匹配(永久失败,入死信): "
                f"method={writer_msg.method_name}, table={writer_msg.table}: {e}"
            )
            await redis_queue.push_dead(
                msg, reason=f"TypeError: {e}",
                message_id=message_id, attempts=99,  # TypeError 永久失败,不重试
            )
            # ACK 移出 pending(已入死信,不需要重处理)
            if stream_id:
                try:
                    await redis_queue.ack([stream_id])
                except Exception as ack_e:
                    logger.warning(f"[DBWriter] TypeError 后 XACK 失败: {ack_e}")
            return
        except Exception as e:
            # 可重试错误:入死信队列(带重试计数)
            self._error_count += 1
            logger.error(
                f"[DBWriter] 消息处理失败(入死信): method={writer_msg.method_name}, "
                f"table={writer_msg.table}: {e}"
            )
            await redis_queue.push_dead(
                msg, reason=f"{type(e).__name__}: {e}",
                message_id=message_id, attempts=0,
            )
            # ACK 移出 pending(已入死信队列,不需要原地重处理)
            # 注意:死信队列有自己的重试逻辑(延迟 XADD 回主队列)
            if stream_id:
                try:
                    await redis_queue.ack([stream_id])
                except Exception as ack_e:
                    logger.warning(f"[DBWriter] 错误后 XACK 失败: {ack_e}")
            return

        # R33 P1修复: 写入 inbox(幂等键)— SQLite 写成功后立即写入
        if message_id and self._store:
            try:
                await self._store.write_writer_inbox(message_id, writer_msg.method_name, stream_id)
            except Exception as e:
                # inbox 写入失败不影响已写入的数据,但可能导致重放时重复执行
                # 对于 upsert 操作是幂等的,对于非幂等操作已移至 DIRECT_WRITE
                logger.warning(
                    f"[DBWriter] inbox 写入失败(可能导致重放时重复执行): {e}"
                )

        # SQLite 写成功后,DEL 读缓存 key(清除读缓存,以 SQLite 为权威)
        if writer_msg.redis_key:
            try:
                await redis_queue.delete(writer_msg.redis_key)
            except Exception as e:
                logger.warning(
                    f"[DBWriter] DEL 缓存 key 失败(不影响已写入数据): "
                    f"key={writer_msg.redis_key}: {e}"
                )

        # R33 P0修复: XACK 确认 — SQLite 提交 + inbox 写入后才 ACK
        # 如果 ACK 失败,消息留在 pending,下次 XAUTOCLAIM 回收后
        # inbox 检查命中,XACK 跳过(不会重复执行)
        if stream_id:
            try:
                await redis_queue.ack([stream_id])
            except Exception as e:
                logger.warning(
                    f"[DBWriter] XACK 失败(消息留在 pending,下次回收处理): "
                    f"stream_id={stream_id}: {e}"
                )

    async def _execute_sqlite(self, msg: DBWriterMessage):
        """根据 method_name 分派到对应的 CacheStore 写方法。

        复用 CacheStore 的写方法,确保 SQL 语句与 bot 进程直写时一致。
        Writer 独占连接,无锁冲突,CacheStore 的 "locked" 重试逻辑不会触发。
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
        data = msg.data if isinstance(msg.data, dict) else {}
        await method(**data)
