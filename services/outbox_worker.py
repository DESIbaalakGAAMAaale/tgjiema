"""R36 B0-2: Upload Outbox Worker — upload_outbox 表的唯一副作用驱动器。

[DEPRECATED — R64 P0-04] 本模块为 R36 旧 outbox worker,仅处理 upload_outbox
(注册 manifest / R100 归档 / 上传失败通知)三类事件。R62 P0-05 + R63 P0-05 +
R64 P0-04 起,事务性 outbox 的统一生产闭环由
``services.data_lifecycle.OutboxWorker`` 承担(基于 outbox_events 表 + lease
fencing token CAS + provider registry + DLQ 审计)。

新代码请迁移到 ``services.data_lifecycle.OutboxWorker``:
    from services.data_lifecycle import OutboxWorker, OutboxEnvelope
    worker = OutboxWorker(provider_registry={...})
    await worker.run_once()

本旧 worker 暂保留以兼容 upload_outbox 既有部署,但已对未知 event_type 采取
fail-closed 策略(进入 DLQ),不再静默视为完成。后续应将 upload_outbox 三类
事件也迁入 outbox_events 表后删除本模块。

职责:
- 定期扫描 upload_outbox 表中 status='PENDING' 的条目
- CAS claim 获取独占执行权(lease_owner + lease_until)
- 按事件类型分派到对应处理器(REGISTER_MANIFEST / ARCHIVE_R100 / UPLOAD_FAILED)
- 成功: mark_outbox_done()
- 失败: mark_outbox_failed()(attempts+1, 指数退避)
- 超过 max_attempts: 自动置为 DEAD
- 重启时清理 stale DISPATCHED 状态(lease 过期但未恢复)
- 信号处理: SIGTERM/SIGINT 优雅停止

可作为独立 systemd 服务或 up_bot 内协程运行:
    worker = OutboxWorker(store, register_manifest_fn=..., archive_to_r100_fn=...)
    await worker.start()
    ...
    await worker.stop()
"""
import asyncio
import os
import signal
import socket
import time
import warnings
from typing import Awaitable, Callable, Optional

from loguru import logger

from database.cache_store import CacheStore

# R64 P0-04: 模块加载时发出 DeprecationWarning(仅一次),提示迁移到新 OutboxWorker
warnings.warn(
    "services.outbox_worker.OutboxWorker is deprecated (R64 P0-04); "
    "migrate to services.data_lifecycle.OutboxWorker which provides "
    "lease fencing token CAS + provider registry + DLQ audit closure. "
    "Unknown event_type now raises AppError(OUTBOX_EVENT_UNKNOWN) "
    "instead of being silently treated as completed.",
    DeprecationWarning,
    stacklevel=2,
)


# ─── 默认参数 ───
DEFAULT_SCAN_INTERVAL: float = 2.0  # 主循环扫描间隔(秒)
DEFAULT_LEASE_SECONDS: int = 60  # 单次 claim 租约时长(秒)
DEFAULT_MAX_ATTEMPTS: int = 5  # 最大重试次数(超过则置 DEAD)
DEFAULT_BACKOFF_BASE: float = 5.0  # 指数退避基准(秒)
DEFAULT_BACKOFF_MAX: float = 300.0  # 指数退避上限(秒)
DEFAULT_BATCH_SIZE: int = 10  # 每轮扫描的批量大小区


# ─── 回调函数类型签名 ───
RegisterManifestFn = Callable[[int, int, dict], Awaitable[None]]
ArchiveToR100Fn = Callable[[int, int], Awaitable[None]]
NotifyUploadFailedFn = Callable[[int, str], Awaitable[None]]


class OutboxWorker:
    """upload_outbox 表的唯一副作用驱动器。

    通过回调注入 Manifest/R100 逻辑,避免循环 import(up_bot 持有 worker 引用)。
    """

    def __init__(
        self,
        store: CacheStore,
        register_manifest_fn: Optional[RegisterManifestFn] = None,
        archive_to_r100_fn: Optional[ArchiveToR100Fn] = None,
        notify_upload_failed_fn: Optional[NotifyUploadFailedFn] = None,
        owner: str = "",
        scan_interval: float = DEFAULT_SCAN_INTERVAL,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        backoff_max: float = DEFAULT_BACKOFF_MAX,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ):
        self._store = store
        self._register_manifest_fn = register_manifest_fn
        self._archive_to_r100_fn = archive_to_r100_fn
        self._notify_upload_failed_fn = notify_upload_failed_fn
        # owner 默认: hostname + pid,确保多 worker 进程互斥
        self._owner = owner or f"obx-worker-{socket.gethostname()}-{os.getpid()}"
        self._scan_interval = scan_interval
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._batch_size = batch_size

        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._signal_handlers_installed = False

    # ─── 公开接口 ───

    async def start(self) -> None:
        """启动 worker 主循环(协程方式)。"""
        if self._task is not None and not self._task.done():
            logger.warning(f"[OutboxWorker] 已在运行(owner={self._owner})")
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="outbox-worker")
        logger.info(
            f"[OutboxWorker] 启动 owner={self._owner} "
            f"scan_interval={self._scan_interval}s lease={self._lease_seconds}s "
            f"max_attempts={self._max_attempts}"
        )

    async def stop(self, timeout: float = 15.0) -> None:
        """请求停止并等待主循环退出。"""
        if self._task is None:
            return
        self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"[OutboxWorker] 停止超时({timeout}s),取消任务")
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
        logger.info(f"[OutboxWorker] 已停止 owner={self._owner}")

    def install_signal_handlers(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """注册 SIGTERM/SIGINT 信号处理(仅在独立进程模式下使用)。

        up_bot 内协程模式不调用此方法(由 up_bot 自身管理信号)。
        """
        if self._signal_handlers_installed:
            return
        loop = loop or asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._stop_event.set)
            except (NotImplementedError, RuntimeError):
                # Windows 不支持 add_signal_handler,降级为 signal.signal
                signal.signal(sig, lambda *_: self._stop_event.set())
        self._signal_handlers_installed = True

    @property
    def owner(self) -> str:
        return self._owner

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ─── 主循环 ───

    async def _run(self) -> None:
        """主循环:扫描 → claim → dispatch → mark_done/failed。"""
        # 启动时清理残留的 DISPATCHED 状态(lease 过期但未恢复)
        try:
            reset_count = await self._store.reset_stale_outbox(self._owner)
            if reset_count > 0:
                logger.info(f"[OutboxWorker] 重启清理 {reset_count} 个 stale DISPATCHED 条目")
        except Exception as e:
            logger.warning(f"[OutboxWorker] 启动清理 stale 异常(忽略): {e}")

        while not self._stop_event.is_set():
            try:
                # 每轮先清理 stale DISPATCHED(其他 worker 崩溃遗留)
                try:
                    await self._store.reset_stale_outbox(self._owner)
                except Exception as e:
                    logger.debug(f"[OutboxWorker] reset_stale_outbox 异常(忽略): {e}")

                # 扫描 PENDING 条目
                pending = await self._store.get_pending_outbox(limit=self._batch_size)
                if pending:
                    logger.debug(f"[OutboxWorker] 扫描到 {len(pending)} 个 PENDING 条目")
                for entry in pending:
                    if self._stop_event.is_set():
                        break
                    await self._process_entry(entry)
            except asyncio.CancelledError:
                logger.info("[OutboxWorker] 收到 CancelledError,准备退出")
                break
            except Exception as e:
                logger.error(f"[OutboxWorker] 主循环异常: {e}", exc_info=True)

            # 等待下一轮(响应停止信号)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._scan_interval,
                )
            except asyncio.TimeoutError:
                pass

        logger.info(f"[OutboxWorker] 主循环已退出 owner={self._owner}")

    # ─── 单条目处理 ───

    async def _process_entry(self, entry: dict) -> None:
        """处理单个 outbox 条目:claim → dispatch → mark_done/failed。"""
        outbox_id = entry.get("outbox_id", "")
        if not outbox_id:
            return

        # CAS claim 独占执行权
        try:
            claimed = await self._store.claim_outbox_entry(
                outbox_id, self._owner, self._lease_seconds,
            )
        except Exception as e:
            logger.warning(f"[OutboxWorker] claim {outbox_id} 异常: {e}")
            return
        if not claimed:
            # 已被其他 worker 抢占,跳过
            logger.debug(f"[OutboxWorker] claim 失败(被抢占或状态已变更): {outbox_id}")
            return

        event_type = entry.get("event_type", "")
        attempts = entry.get("attempts", 0) or 0

        try:
            await self._dispatch_event(event_type, entry)
            # 成功:标记 DONE
            ok = await self._store.mark_outbox_done(outbox_id)
            if ok:
                logger.info(
                    f"[OutboxWorker] outbox {outbox_id} 处理完成 "
                    f"(event={event_type}, attempts={attempts})"
                )
            else:
                logger.warning(
                    f"[OutboxWorker] outbox {outbox_id} mark_done 失败"
                    f"(可能已被其他流程处理)"
                )
        except Exception as e:
            # 失败:attempts+1 + 指数退避 + 可能 DEAD
            next_retry_at = self._compute_next_retry(attempts + 1)
            logger.warning(
                f"[OutboxWorker] outbox {outbox_id} 处理失败 "
                f"(event={event_type}, attempts={attempts + 1}): {e}"
            )
            try:
                await self._store.mark_outbox_failed(
                    outbox_id, str(e), next_retry_at,
                    max_attempts=self._max_attempts,
                )
                # 重新查询确认是否进入 DEAD
                updated = await self._store.get_outbox_by_upload(entry.get("upload_id", ""))
                cur = next((x for x in updated if x["outbox_id"] == outbox_id), None)
                if cur and cur.get("status") == "DEAD":
                    logger.error(
                        f"[OutboxWorker] outbox {outbox_id} 进入 DEAD "
                        f"(attempts={cur.get('attempts', 0)} >= max={self._max_attempts})"
                    )
            except Exception as inner:
                logger.error(
                    f"[OutboxWorker] outbox {outbox_id} mark_failed 异常: {inner}"
                )

    # ─── 事件分派 ───

    async def _dispatch_event(self, event_type: str, entry: dict) -> None:
        """按 event_type 分派到对应处理器。

        R64 P0-04 整改(未知 event_type fail-closed):
        - 旧实现遇到未知 event_type 仅 ``logger.warning`` 后视为完成(fail-open),
          生产环境若出现新 event_type 未及时升级 worker,事件会被静默标记成功,
          外部副作用丢失且无法重放(completed 是终态)。
        - 新实现:未知 event_type 必须 ``raise AppError(OUTBOX_EVENT_UNKNOWN)``,
          由外层 ``_process_entry`` 捕获并调用 ``mark_outbox_failed`` 进入 DLQ
          (达到 max_attempts 后置 DEAD,人工审批 replay)。
        """
        if event_type == "REGISTER_MANIFEST":
            await self._dispatch_register_manifest(entry)
        elif event_type == "ARCHIVE_R100":
            await self._dispatch_archive_r100(entry)
        elif event_type == "UPLOAD_FAILED":
            await self._dispatch_upload_failed(entry)
        else:
            # R64 P0-04: 未知 event_type 严禁标记成功,必须进入 DLQ
            from services.error_codes import AppError, ErrorCodes
            logger.error(
                f"[OutboxWorker] 未知 event_type={event_type} "
                f"(outbox_id={entry.get('outbox_id')}),"
                f"raise AppError 进入 DLQ(R64 P0-04 fail-closed)"
            )
            raise AppError(
                ErrorCodes.OUTBOX_EVENT_UNKNOWN,
                params={
                    "event_type": event_type,
                    "outbox_id": entry.get("outbox_id", ""),
                },
            )

    async def _dispatch_register_manifest(self, entry: dict) -> None:
        """执行 REGISTER_MANIFEST 事件:对每个 storage_msg_id 调用 manifest 注册。

        幂等保证: register_manifest 回调内部使用 upsert_manifest(INSERT OR REPLACE),
        重复调用不会创建重复记录。
        """
        if self._register_manifest_fn is None:
            logger.warning("[OutboxWorker] register_manifest_fn 未注入,跳过")
            return
        storage_channel_id = entry.get("storage_channel_id") or 0
        storage_msg_ids = entry.get("storage_msg_ids") or []
        file_meta_list = entry.get("batch_file_meta") or []
        if not storage_channel_id or not storage_msg_ids:
            logger.warning(
                f"[OutboxWorker] REGISTER_MANIFEST 数据缺失 "
                f"(outbox_id={entry.get('outbox_id')}): "
                f"channel={storage_channel_id} msg_ids={storage_msg_ids}"
            )
            return
        # storage_msg_ids 与 file_meta_list 一一对应(由 _finalize_upload 写入)
        # 若长度不匹配,file_meta 用空 dict 作为 fallback
        for idx, msg_id in enumerate(storage_msg_ids):
            fm = file_meta_list[idx] if idx < len(file_meta_list) else {}
            if not isinstance(fm, dict):
                fm = {}
            try:
                await self._register_manifest_fn(storage_channel_id, msg_id, fm)
            except Exception as e:
                logger.warning(
                    f"[OutboxWorker] register_manifest 单条失败 "
                    f"(channel={storage_channel_id}, msg_id={msg_id}): {e}"
                )
                # 不立即抛出,继续处理后续文件,失败统计由上层 mark_outbox_failed 决定
                # 但如果任意一条失败,整个 outbox 应视为失败(便于重试)
                raise

    async def _dispatch_archive_r100(self, entry: dict) -> None:
        """执行 ARCHIVE_R100 事件:从 storage 频道复制到 R100 归档频道。

        R100 归档与主取件解耦(R36 B0-2: R100 不阻塞主取件 READY)。
        """
        if self._archive_to_r100_fn is None:
            logger.warning("[OutboxWorker] archive_to_r100_fn 未注入,跳过")
            return
        storage_channel_id = entry.get("storage_channel_id") or 0
        storage_msg_ids = entry.get("storage_msg_ids") or []
        if not storage_channel_id or not storage_msg_ids:
            logger.warning(
                f"[OutboxWorker] ARCHIVE_R100 数据缺失 "
                f"(outbox_id={entry.get('outbox_id')}): "
                f"channel={storage_channel_id} msg_ids={storage_msg_ids}"
            )
            return
        for msg_id in storage_msg_ids:
            try:
                await self._archive_to_r100_fn(storage_channel_id, msg_id)
            except Exception as e:
                logger.warning(
                    f"[OutboxWorker] archive_to_r100 单条失败 "
                    f"(channel={storage_channel_id}, msg_id={msg_id}): {e}"
                )
                raise

    async def _dispatch_upload_failed(self, entry: dict) -> None:
        """执行 UPLOAD_FAILED 事件:通知用户上传失败。"""
        if self._notify_upload_failed_fn is None:
            logger.warning("[OutboxWorker] notify_upload_failed_fn 未注入,跳过")
            return
        target_user_id = entry.get("target_user_id") or 0
        if not target_user_id:
            logger.warning(
                f"[OutboxWorker] UPLOAD_FAILED target_user_id 缺失 "
                f"(outbox_id={entry.get('outbox_id')})"
            )
            return
        try:
            await self._notify_upload_failed_fn(
                target_user_id, "upload_failed_outbox"
            )
        except Exception as e:
            logger.warning(
                f"[OutboxWorker] notify_upload_failed 失败 "
                f"(user_id={target_user_id}): {e}"
            )
            raise

    # ─── 工具方法 ───

    def _compute_next_retry(self, new_attempts: int) -> float:
        """计算指数退避的 next_retry_at 时间戳。"""
        # backoff = base * 2^(attempts-1),封顶 max
        delay = self._backoff_base * (2 ** max(0, new_attempts - 1))
        delay = min(delay, self._backoff_max)
        return time.time() + delay


async def run_standalone(
    store: CacheStore,
    register_manifest_fn: Optional[RegisterManifestFn] = None,
    archive_to_r100_fn: Optional[ArchiveToR100Fn] = None,
    notify_upload_failed_fn: Optional[NotifyUploadFailedFn] = None,
) -> None:
    """独立进程模式启动 OutboxWorker(用于 systemd 部署)。

    用法:
        python -m services.outbox_worker
    """
    worker = OutboxWorker(
        store,
        register_manifest_fn=register_manifest_fn,
        archive_to_r100_fn=archive_to_r100_fn,
        notify_upload_failed_fn=notify_upload_failed_fn,
    )
    worker.install_signal_handlers()
    await worker.start()
    # 永久运行直到收到信号
    try:
        while worker.is_running:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await worker.stop()


if __name__ == "__main__":
    # 独立进程入口:仅作占位,实际部署应通过 up_bot 协程或 systemd 服务运行
    from database.cache_store import get_cache_store

    async def _main():
        await (await get_cache_store()).init()
        # 注: 独立进程模式需要自行注入 register_manifest_fn / archive_to_r100_fn
        # 通常由 up_bot 内协程模式运行(共享 bot 引用)
        logger.warning("[OutboxWorker] 独立进程模式未注入回调,仅扫描不执行副作用")
        worker = OutboxWorker(await get_cache_store())
        worker.install_signal_handlers()
        await worker.start()
        while worker.is_running:
            await asyncio.sleep(3600)

    asyncio.run(_main())
