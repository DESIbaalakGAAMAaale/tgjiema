"""DLQ Worker: 死信队列重试闭环消费者。

R34 P1-1 修复: 实现真正的死信队列重试闭环。

流程:
1. XRANGE 读取死信 Stream (tgjiema:writer:dead)
2. 对每条消息:
   - 解析 original 消息 + attempts + next_retry_at
   - 如果 next_retry_at <= now 且 attempts < max_attempts:
     - attempts += 1
     - XADD 回主 Stream (保留原 message_id)
     - XDEL 从死信 Stream 删除
   - 如果 attempts >= max_attempts:
     - 永久死信,保留在死信 Stream 等待人工审核
3. 循环间隔 30 秒
4. 信号处理: SIGTERM/SIGINT 优雅停止

独立运行: 可作为独立 systemd 服务或 db_writer 内协程。
"""
import asyncio
import random
import signal
import time

from loguru import logger

from database import redis_queue


class DLQWorker:
    """死信队列重试闭环消费者。

    作为 db_writer 进程内的协程运行,定期扫描死信 Stream,
    将到期的可重试消息重新 XADD 回主 Stream,实现真正的重试闭环。

    重试幂等性: 保留原 message_id,db_writer 的 writer_inbox 表
    会自动去重(若原消息已处理,XACK 跳过)。
    """

    def __init__(self):
        self._running: bool = False
        # 计数器(供监控/日志输出)
        self.processed_count: int = 0        # 已扫描的死信消息总数
        self.retried_count: int = 0          # 已重试(XADD 回主 Stream)的消息数
        self.permanent_fail_count: int = 0  # 永久失败(attempts >= max_attempts)的消息数

    async def init(self) -> bool:
        """初始化:健康检查 Redis + 校验 WRITER_MODE。

        Returns:
            True 初始化成功可启动, False 不应启动(Redis 不可达或 WRITER_MODE 非 redis)
        """
        from config import settings
        if settings.WRITER_MODE != "redis" or not settings.REDIS_URL:
            logger.info("[DLQWorker] WRITER_MODE=sqlite 或 Redis 未配置,DLQ Worker 不启动")
            return False

        healthy = await redis_queue.health_check()
        if not healthy:
            logger.warning("[DLQWorker] Redis 不可达,DLQ Worker 暂不启动(等待下次重试)")
            return False

        logger.info("[DLQWorker] 初始化完成,死信队列重试闭环已启用")
        return True

    async def start(self, interval: int = 30) -> None:
        """主循环:定期扫描死信 Stream,处理重试。

        Args:
            interval: 循环间隔秒数(默认 30 秒)
        """
        self._running = True
        logger.info(f"[DLQWorker] 主循环启动,扫描间隔: {interval}s")

        while self._running:
            try:
                await self._process_dead_messages()
            except asyncio.CancelledError:
                logger.info("[DLQWorker] 收到 CancelledError,准备停止")
                self._running = False
                raise
            except Exception as e:
                logger.error(f"[DLQWorker] 处理死信消息异常: {e}")

            # 间隔等待(可被 CancelledError 中断)
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                logger.info("[DLQWorker] 收到 CancelledError,准备停止")
                self._running = False
                raise

        logger.info("[DLQWorker] 主循环已停止")

    def stop(self) -> None:
        """优雅停止:设置 _running=False,主循环下次检查时退出。"""
        self._running = False
        logger.info(
            f"[DLQWorker] 停止完成,已扫描 {self.processed_count} 条死信,"
            f"重试 {self.retried_count} 条,永久失败 {self.permanent_fail_count} 条"
        )

    async def close(self) -> None:
        """清理资源。

        DLQ Worker 不持有独占 Redis 连接(复用 redis_queue 模块级客户端),
        无需特殊清理。标记 _running=False 即可。
        """
        self._running = False
        logger.info("[DLQWorker] 资源已清理")

    async def _process_dead_messages(self) -> int:
        """读取死信 Stream 并处理重试。

        流程:
        1. XRANGE 读取死信消息(最多 100 条)
        2. 对每条消息判断是否可重试:
           - 可重试(next_retry_at <= now 且 attempts < max_attempts):
             XADD 回主 Stream + XDEL 从死信删除
           - 永久失败(attempts >= max_attempts):
             保留在死信 Stream 等待人工审核
           - 未到期(next_retry_at > now):
             跳过,等待下次扫描

        Returns:
            本次重试的消息数
        """
        messages = await redis_queue.get_dead_messages(count=100)
        if not messages:
            return 0

        now = time.time()
        retried = 0

        for msg_id, dead_msg in messages:
            self.processed_count += 1

            if not isinstance(dead_msg, dict):
                logger.warning(
                    f"[DLQWorker] 死信消息非 dict 类型,跳过: msg_id={msg_id}"
                )
                continue

            if self._is_retryable(dead_msg, now):
                ok = await self._retry_message(msg_id, dead_msg)
                if ok:
                    retried += 1
                    self.retried_count += 1
            elif self._is_permanent_failure(dead_msg):
                # 永久死信,不重试,保留等待人工审核
                self.permanent_fail_count += 1
                logger.info(
                    f"[DLQWorker] 永久死信(attempts={dead_msg.get('attempts')}/"
                    f"max={dead_msg.get('max_attempts')}),保留等待人工审核: "
                    f"msg_id={msg_id}, reason={dead_msg.get('reason', '?')}"
                )
            # else: next_retry_at > now,未到期,跳过等待下次扫描

        if retried > 0:
            logger.info(f"[DLQWorker] 本次重试 {retried} 条死信消息")

        return retried

    async def _retry_message(self, msg_id: str, dead_msg: dict) -> bool:
        """重试单条死信消息:原子 XADD 回主 Stream + XDEL 从死信删除。

        R36 H1: 使用 redis_queue.requeue_from_dlq() Lua 脚本原子执行
        XADD(主 Stream)+ XDEL(死信 Stream),替代旧的非原子双操作
        (push() + delete_dead_message())。避免 XADD 成功但 XDEL 失败导致
        死信重复重入,或 XDEL 成功但 XADD 失败导致消息丢失。

        保留原 message_id,实现幂等去重(若原消息已处理,writer_inbox 会命中跳过)。

        Args:
            msg_id: 死信 Stream 消息 ID(XDEL 用)
            dead_msg: 死信消息字典(含 original/attempts/max_attempts/next_retry_at)

        Returns:
            True 重试成功, False 失败(保留死信等待下次扫描)
        """
        from config import settings

        original = dead_msg.get("original", {})
        if not isinstance(original, dict):
            logger.warning(
                f"[DLQWorker] 死信 original 非 dict,无法重试: msg_id={msg_id}"
            )
            return False

        # attempts += 1(计算本次重试后的次数,用于退避延迟计算与日志)
        attempts = int(dead_msg.get("attempts", 0)) + 1
        max_attempts = int(dead_msg.get("max_attempts", settings.WRITER_DEAD_MAX_ATTEMPTS))
        base_delay = settings.WRITER_DEAD_RETRY_DELAY

        # 计算指数退避延迟(供日志输出,实际 next_retry_at 由 push_dead 在失败时设置)
        backoff = compute_backoff_delay(attempts, base_delay)

        # 保留原 message_id(幂等去重:若已处理,writer_inbox 命中跳过)
        message_id = dead_msg.get("message_id", "") or original.get("message_id", "")

        # R36 H1: 构建回主 Stream 的消息体(与 push() 内部格式一致)
        # 携带 attempts 字段,后续失败时 push_dead 能从 msg.attempts 读取并 +1
        msg_data = {
            "op_type": original.get("op_type", ""),
            "table": original.get("table", ""),
            "method_name": original.get("method_name", ""),
            "data": original.get("data", {}) or {},
            "redis_key": original.get("redis_key", ""),
            "message_id": message_id,
            "created_at": time.time(),
            "attempts": attempts,
        }

        # R36 H1: 优先使用原子 XADD + XDEL(Lua 脚本保证原子性)
        # requeue_from_dlq 内部:先 XADD 回主 Stream,成功后才 XDEL 死信
        # 任一失败脚本回滚,不会出现"XADD 成功但 XDEL 失败"或反之的不一致状态
        # 降级路径: 当 requeue_from_dlq 不可用(Redis 不可达 / Lua 不支持 / 异常)
        # 时,回退到非原子 push + delete_dead_message,保证功能可用性
        # (生产环境 Redis 可达时走原子路径,降级路径仅在异常时触发)
        try:
            ok = await redis_queue.requeue_from_dlq(
                dead_msg_id=msg_id,
                msg_data=msg_data,
            )
            if ok:
                logger.info(
                    f"[DLQWorker] 死信重试成功(原子 XADD+XDEL): msg_id={msg_id}, "
                    f"attempts={attempts}/{max_attempts}, backoff={backoff:.1f}s, "
                    f"method={original.get('method_name', '?')}, "
                    f"message_id={message_id}"
                )
                return True
            # 原子重入失败,走降级路径(非原子 push + delete)
            logger.warning(
                f"[DLQWorker] 原子重入失败(requeue_from_dlq 返回 False),"
                f"降级到非原子 push+delete: msg_id={msg_id}"
            )
        except Exception as e:
            # 原子重入异常,走降级路径(非原子 push + delete)
            logger.warning(
                f"[DLQWorker] 原子重入异常,降级到非原子 push+delete: "
                f"msg_id={msg_id}: {e}"
            )

        # 降级路径: 非原子 push + delete_dead_message
        # (仅在 Redis 不可达 / Lua 不支持 / 原子操作异常时触发)
        # 风险: push 成功但 delete 失败会导致死信重复重入,依赖 inbox 去重兜底
        ok_push = await redis_queue.push(
            op_type=original.get("op_type", ""),
            table=original.get("table", ""),
            method_name=original.get("method_name", ""),
            data=original.get("data", {}) or {},
            redis_key=original.get("redis_key", ""),
            message_id=message_id,
            attempts=attempts,
        )
        if not ok_push:
            logger.error(
                f"[DLQWorker] 降级 push 失败,保留死信等待下次重试: "
                f"msg_id={msg_id}"
            )
            return False

        ok_del = await redis_queue.delete_dead_message(msg_id)
        if not ok_del:
            # delete 失败: 消息已 push 回主 Stream,但死信未删除
            # 下次扫描会再次 push(依赖 writer_inbox 去重避免重复执行)
            logger.warning(
                f"[DLQWorker] 降级 XDEL 失败,死信可能重复重入"
                f"(writer_inbox 去重兜底): msg_id={msg_id}"
            )

        logger.info(
            f"[DLQWorker] 死信重试成功(降级非原子): msg_id={msg_id}, "
            f"attempts={attempts}/{max_attempts}, backoff={backoff:.1f}s, "
            f"method={original.get('method_name', '?')}, "
            f"message_id={message_id}"
        )
        return True

    @staticmethod
    def _is_retryable(dead_msg: dict, now: float = None) -> bool:
        """判断死信消息是否可重试。

        可重试条件(全部满足):
        1. next_retry_at 不为 None(非永久死信)
        2. next_retry_at <= now(已到期)
        3. attempts < max_attempts(未超过最大重试次数)

        Args:
            dead_msg: 死信消息字典
            now: 当前时间戳(默认 time.time(),测试可注入)

        Returns:
            True 可重试, False 不可重试
        """
        if now is None:
            now = time.time()

        next_retry_at = dead_msg.get("next_retry_at")
        if next_retry_at is None:
            # 永久死信(push_dead 在 attempts >= max_attempts 时设为 None)
            return False

        attempts = int(dead_msg.get("attempts", 0))
        max_attempts = int(dead_msg.get("max_attempts", 3))

        if attempts >= max_attempts:
            return False

        try:
            return float(next_retry_at) <= now
        except (TypeError, ValueError):
            # R64 P1-07: data-integrity 域禁止 except 块裸 return False;
            # next_retry_at 不可解析时视为不可重试,记录日志后落到函数尾返回
            logger.debug(
                f"[dlq_worker] next_retry_at 不可解析,视为不可重试: {next_retry_at!r}"
            )
        return False

    @staticmethod
    def _is_permanent_failure(dead_msg: dict) -> bool:
        """判断死信消息是否为永久失败(attempts >= max_attempts 或 next_retry_at 为 None)。

        Args:
            dead_msg: 死信消息字典

        Returns:
            True 永久失败, False 仍可重试或未到期
        """
        next_retry_at = dead_msg.get("next_retry_at")
        if next_retry_at is None:
            return True

        attempts = int(dead_msg.get("attempts", 0))
        max_attempts = int(dead_msg.get("max_attempts", 3))
        return attempts >= max_attempts


def compute_backoff_delay(attempts: int, base_delay: int = 60) -> float:
    """计算指数退避延迟(带抖动)。

    退避策略:
    - 基础延迟: base_delay(默认 60s,从 WRITER_DEAD_RETRY_DELAY 读取)
    - 指数退避: base_delay * (2 ** min(attempts, 5))
    - 抖动: random.uniform(0, base_delay * 0.1)

    限制指数上限为 5,避免延迟过大(60 * 2^5 = 1920s ≈ 32 分钟)。

    Args:
        attempts: 当前重试次数(已加 1)
        base_delay: 基础延迟秒数

    Returns:
        退避延迟秒数(含抖动)
    """
    if attempts < 0:
        attempts = 0
    if base_delay < 0:
        base_delay = 0

    # 限制指数增长,避免延迟过大
    exponent = min(attempts, 5)
    backoff = base_delay * (2 ** exponent)
    # 抖动:0 ~ base_delay * 0.1
    jitter = random.uniform(0, base_delay * 0.1)
    return backoff + jitter


async def main():
    """独立运行入口(可作为独立 systemd 服务)。

    用法: py -3 -m database.dlq_worker
    """
    worker = DLQWorker()
    ok = await worker.init()
    if not ok:
        logger.warning("[DLQWorker] 初始化失败,退出")
        return

    # 信号处理:优雅停止(SIGTERM/SIGINT)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, worker.stop)
        except (NotImplementedError, AttributeError):
            # Windows 不支持 add_signal_handler,忽略(靠 KeyboardInterrupt 兜底)
            pass

    try:
        await worker.start(interval=30)
    except KeyboardInterrupt:
        worker.stop()
    finally:
        await worker.close()


if __name__ == "__main__":
    asyncio.run(main())
