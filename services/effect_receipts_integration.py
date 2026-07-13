"""R45: Effect Receipts 集成辅助函数。

提供装饰器和上下文管理器,简化外部副作用接入 effect receipt。

设计目标:
    - **不破坏现有 CommandBus 的 RBAC/审批/审计/幂等逻辑**;
    - 仅在"外部副作用执行环节"添加 effect receipt 包装(check→pending→completed/failed);
    - 已完成(completed)的副作用被跳过,实现 effectively-once 语义;
    - manager 不可用时 fail-open(记录 warning 后直接执行),不影响主流程。

依赖:
    - services.effect_receipts.EffectReceiptManager(check_receipt / record_pending /
      record_completed / record_failed)
    - manager 由 ``get_receipt_manager(cache_store)`` 单例化,首次调用时 cache_store
      必须已初始化;后续调用可不传 cache_store。
"""
from __future__ import annotations

import functools
from typing import Any, Awaitable, Callable, Optional

from loguru import logger

from services.effect_receipts import get_receipt_manager


def with_effect_receipt(effect_type: str, target_fn: Optional[Callable] = None):
    """装饰器:为外部副作用函数自动添加 effect receipt 包装。

    Args:
        effect_type: 副作用类型('telegram_send' / 'r2_upload' / 'crdb_upsert' 等)
        target_fn: 返回 target 字符串的可调用对象(默认用函数名)

    用法:
        @with_effect_receipt("telegram_send", lambda self, chat_id, **kw: f"chat:{chat_id}")
        async def send_message(self, chat_id, text, **kwargs):
            ...

    调用时通过 ``action_id=`` 关键字参数传入幂等 ID:
        await obj.send_message(chat_id, text, action_id="dsp_job_42")

    若未传 ``action_id``(向后兼容),则直接执行原函数,不进行 receipt 包装。
    """
    def decorator(func: Callable[..., Awaitable[Any]]):
        @functools.wraps(func)
        async def wrapper(*args, action_id: Optional[str] = None, **kwargs):
            # 无 action_id 时直接执行(向后兼容)
            if not action_id:
                return await func(*args, **kwargs)

            manager = get_receipt_manager()
            if manager is None:
                # manager 不可用 → fail-open(记录 warning 后直接执行)
                logger.warning(
                    f"[effect_receipt] manager 不可用,直接执行 {func.__name__}"
                )
                return await func(*args, **kwargs)

            # 计算 target(优先 target_fn,失败则用函数名)
            target = func.__name__
            if target_fn is not None:
                try:
                    target = str(target_fn(*args, **kwargs))
                except Exception:
                    target = func.__name__

            # 1. 检查是否已完成 → 跳过(幂等)
            receipt = await manager.check_receipt(action_id, effect_type, target)
            if receipt is not None and receipt.get("status") == "completed":
                logger.info(
                    f"[effect_receipt] 跳过已完成副作用: "
                    f"action={action_id}, type={effect_type}, target={target}"
                )
                return {
                    "skipped": True,
                    "external_id": receipt.get("external_id", ""),
                }

            # 2. 记录 pending(开始执行)
            await manager.record_pending(action_id, effect_type, target)
            try:
                result = await func(*args, **kwargs)
                # 3. 提取 external_id(支持 dict 形式的返回值)
                external_id = ""
                if isinstance(result, dict):
                    external_id = str(
                        result.get("external_id")
                        or result.get("message_id")
                        or ""
                    )
                await manager.record_completed(
                    action_id, effect_type, target, external_id,
                )
                return result
            except Exception as e:
                # 4. 异常 → 记录 failed 后重新抛出
                await manager.record_failed(action_id, effect_type, target)
                raise

        return wrapper
    return decorator


class EffectReceiptContext:
    """上下文管理器:为代码块添加 effect receipt 包装。

    用法:
        async with EffectReceiptContext(
            action_id="dsp_job_42",
            effect_type="telegram_send",
            target=f"chat:{chat_id}",
        ) as receipt:
            if receipt.skipped:
                return receipt.external_id  # 已完成,跳过
            result = await bot.send_message(chat_id, text)
            receipt.set_external_id(str(result.message_id))

    特性:
        - manager 不可用时 fail-open,skipped 永远为 False(继续执行原逻辑);
        - 已 completed 时 skipped=True,调用方应检查并跳过副作用;
        - 异常退出时自动 record_failed;正常退出时 record_completed。
    """

    def __init__(self, action_id: str, effect_type: str, target: str):
        self.action_id = action_id
        self.effect_type = effect_type
        self.target = target
        self.manager: Optional[Any] = None
        self.skipped: bool = False
        self.external_id: str = ""
        # R45-dsp_bot: 标记 with 块内未实际执行副作用(早返回场景),
        # __aexit__ 时跳过 record_completed/record_failed,允许下一轮重试
        self._no_record: bool = False

    async def __aenter__(self) -> "EffectReceiptContext":
        self.manager = get_receipt_manager()
        if self.manager is None:
            # manager 不可用 → fail-open,直接进入 with 块
            logger.warning(
                f"[effect_receipt] manager 不可用,直接执行 "
                f"action={self.action_id} type={self.effect_type}"
            )
            return self

        # 检查是否已完成 → 跳过
        receipt = await self.manager.check_receipt(
            self.action_id, self.effect_type, self.target,
        )
        if receipt is not None and receipt.get("status") == "completed":
            self.skipped = True
            self.external_id = receipt.get("external_id", "") or ""
            logger.info(
                f"[effect_receipt] 跳过已完成副作用: "
                f"action={self.action_id}, type={self.effect_type}, "
                f"target={self.target}"
            )
            return self

        # 记录 pending
        await self.manager.record_pending(
            self.action_id, self.effect_type, self.target,
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        # manager 不可用 / 已跳过 / 标记 no_record → 不写入
        if self.manager is None or self.skipped or self._no_record:
            return False

        if exc_type is None:
            # 正常退出 → 记录 completed
            await self.manager.record_completed(
                self.action_id, self.effect_type, self.target,
                self.external_id,
            )
        else:
            # 异常退出 → 记录 failed(不吞异常)
            await self.manager.record_failed(
                self.action_id, self.effect_type, self.target,
            )
        return False  # 不吞异常,继续向上抛

    def set_external_id(self, external_id: str) -> None:
        """设置 external_id(在 with 块内调用,用于 record_completed 时携带)。

        Args:
            external_id: 外部系统返回的 ID(如 Telegram message_id)
        """
        self.external_id = str(external_id) if external_id is not None else ""

    def mark_no_record(self) -> None:
        """标记 with 块内未实际执行副作用(早返回场景)。

        调用后 ``__aexit__`` 会跳过 record_completed/record_failed,
        允许下一轮重试时重新进入 pending 状态。

        适用场景:
            - dsp_bot 中 msg_id 为 0、Resolver fail-closed 等早返回;
            - 已通过 delivery_receipts 幂等命中,无需再写 effect receipt。
        """
        self._no_record = True
