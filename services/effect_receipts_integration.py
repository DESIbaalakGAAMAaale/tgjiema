"""R44 G0-2 / R46 P0-1 / R47 P0-4 / R48 P0-4: Effect Receipts 集成辅助函数。

R46 P0-1 整改:
- critical effect 类型(telegram_send/copy/r2_put/restore/ban/takedown/purge) fail-closed:
  manager 不可用或读写失败时直接 raise EffectReceiptError,拒绝执行外部副作用。
- 非关键通知允许显式 best_effort=True(向后兼容)。
- 装饰器/上下文管理器接收 best_effort 参数。

R47 P0-4 整改:
- critical effect 无 action_id 时直接 raise EffectReceiptError(拒绝执行),
  防止关键副作用绕过 Effect Receipt。
- 非关键 effect 无 action_id 时仍允许直执(向后兼容,仅记录 warning 日志)。
- 装饰器新增 params_fn 参数,上下文管理器新增 params 参数,
  用于计算 request_hash 绑定 effect 参数,防止同 action_id 不同 payload 绕过。

R48 P0-4 整改:
- critical effect 必须提供 params(上下文)或 params_fn(装饰器),
  否则 raise EffectReceiptError(防止 request_hash 为空时同 action_id 不同参数共用 receipt)。
- params_fn 调用异常时:critical effect 直接 raise EffectReceiptError;
  非 critical effect warning 并继续(允许 request_hash 为空)。
- compute_effect_request_hash 返回空时:critical effect 拒绝执行;非 critical 允许。
"""
from __future__ import annotations

import functools
from typing import Any, Awaitable, Callable, Optional

from loguru import logger

from services.effect_receipts import (
    CRITICAL_EFFECT_TYPES,
    EffectReceiptError,
    compute_effect_request_hash,
    get_receipt_manager,
)


def _is_critical(effect_type: str) -> bool:
    """判断是否为 critical effect 类型。"""
    return effect_type in CRITICAL_EFFECT_TYPES


def with_effect_receipt(
    effect_type: str,
    target_fn: Optional[Callable] = None,
    *,
    best_effort: bool = False,
    params_fn: Optional[Callable[..., dict]] = None,
):
    """装饰器:为外部副作用函数自动添加 effect receipt 包装。

    Args:
        effect_type: 副作用类型('telegram_send' / 'r2_upload' 等)
        target_fn: 返回 target 字符串的可调用对象
        best_effort: True 时 manager 不可用也执行(仅用于非关键通知);
                     False 时 critical 类型 manager 不可用直接 raise。
        params_fn: R47 P0-4 返回 effect 参数字典的可调用对象(用于计算 request_hash),
                   签名同 target_fn(*args, **kwargs);None 时不校验 request_hash。

    R47 P0-4:
        - critical effect 无 action_id → raise EffectReceiptError(拒绝执行)。
        - 非关键 effect 无 action_id → 直执(向后兼容,warning 日志)。

    R48 P0-4:
        - critical effect 无 params_fn → raise EffectReceiptError(拒绝执行),
          防止 request_hash 为空时同 action_id 不同参数共用 receipt。
        - params_fn 异常:critical → raise;非 critical → warning 并继续。
        - request_hash 为空:critical → raise;非 critical → 允许。
    """
    def decorator(func: Callable[..., Awaitable[Any]]):
        @functools.wraps(func)
        async def wrapper(*args, action_id: Optional[str] = None, tx=None, **kwargs):
            is_critical = _is_critical(effect_type)

            # R47 P0-4: critical effect 无 action_id → 拒绝执行
            if not action_id:
                if is_critical:
                    raise EffectReceiptError(
                        f"critical effect '{effect_type}' requires action_id, got empty"
                    )
                # 非关键 + 无 action_id → 直执(向后兼容)
                logger.warning(
                    f"[effect_receipt] 无 action_id,非关键副作用直执 "
                    f"{func.__name__}(effect_type={effect_type})"
                )
                return await func(*args, **kwargs)

            # R48 P0-4: critical effect 必须提供 params_fn 用于 request_hash 绑定
            if is_critical and params_fn is None:
                raise EffectReceiptError(
                    f"critical effect '{effect_type}' requires params or params_fn "
                    f"for request_hash binding"
                )

            # R46 P0-1: critical 副作用且非 best_effort → fail-closed
            fail_closed = is_critical and not best_effort

            manager = get_receipt_manager()
            if manager is None:
                if fail_closed:
                    raise EffectReceiptError(
                        f"[effect_receipt] manager 不可用,critical 副作用拒绝执行 "
                        f"{func.__name__}(effect_type={effect_type})"
                    )
                # 非关键或 best_effort → fail-open
                logger.warning(
                    f"[effect_receipt] manager 不可用,直接执行 {func.__name__}"
                )
                return await func(*args, **kwargs)

            # 计算 target
            target = func.__name__
            if target_fn is not None:
                try:
                    target = str(target_fn(*args, **kwargs))
                except Exception:
                    target = func.__name__

            # R47 P0-4 / R48 P0-4: 计算 request_hash(绑定 effect 参数)
            request_hash = ""
            if params_fn is not None:
                try:
                    params_dict = params_fn(*args, **kwargs) or {}
                    request_hash = compute_effect_request_hash(effect_type, params_dict)
                except EffectReceiptError:
                    raise
                except Exception as e:
                    # R48 P0-4: params_fn 异常 — critical 拒绝,非 critical 继续
                    if is_critical:
                        raise EffectReceiptError(
                            f"critical effect params_fn failed: {e}"
                        ) from e
                    logger.warning(
                        f"[effect_receipt] params_fn 异常(非关键,继续执行) "
                        f"{func.__name__}: {e}"
                    )
                    request_hash = ""

            # R48 P0-4: critical effect 必须有非空 request_hash
            if is_critical and not request_hash:
                raise EffectReceiptError(
                    f"critical effect '{effect_type}' request_hash 为空,拒绝执行"
                )

            # 1. 检查是否已完成 → 跳过(幂等)
            receipt = await manager.check_receipt(
                action_id, effect_type, target,
                fail_closed=fail_closed,
                expected_request_hash=request_hash,
                tx=tx,  # R52 P0-4: 传递外层事务连接
            )
            if receipt is not None and receipt.get("status") == "completed":
                logger.info(
                    f"[effect_receipt] 跳过已完成副作用: "
                    f"action={action_id}, type={effect_type}, target={target}"
                )
                return {
                    "skipped": True,
                    "external_id": receipt.get("external_id", ""),
                }
            # R50 P0-3: hash mismatch → enter DLQ, refuse to retry external side effects
            if receipt is not None and receipt.get("status") == "hash_mismatch":
                logger.error(
                    f"[effect_receipt] hash mismatch, refuse retry (enter DLQ): "
                    f"action={action_id}, type={effect_type}, target={target}"
                )
                raise EffectReceiptError(
                    f"effect receipt hash mismatch for action={action_id} "
                    f"type={effect_type} target={target} — "
                    f"marked hash_mismatch_needs_reconcile, refuse to retry external side effects"
                )

            # 2. 记录 pending(开始执行) — CAS claim
            claim_ok = await manager.record_pending(
                action_id, effect_type, target,
                request_hash=request_hash,
                fail_closed=fail_closed,
                tx=tx,  # R52 P0-4: 传递外层事务连接
            )
            if not claim_ok:
                # 已 completed(竞态),跳过
                return {"skipped": True, "external_id": ""}

            try:
                result = await func(*args, **kwargs)
                # 3. 提取 external_id
                external_id = ""
                if isinstance(result, dict):
                    external_id = str(
                        result.get("external_id")
                        or result.get("message_id")
                        or ""
                    )
                await manager.record_completed(
                    action_id, effect_type, target, external_id,
                    fail_closed=fail_closed,
                )
                return result
            except Exception as e:
                # 4. 异常 → 记录 failed 后重新抛出
                try:
                    await manager.record_failed(
                        action_id, effect_type, target,
                        error_msg=str(e), fail_closed=fail_closed,
                    )
                except EffectReceiptError:
                    pass  # record_failed 失败不掩盖原异常
                raise

        return wrapper
    return decorator


class EffectReceiptContext:
    """上下文管理器:为代码块添加 effect receipt 包装。

    R46 P0-1: critical 副作用 manager 不可用时 raise EffectReceiptError,
    非关键或 best_effort=True 时 fail-open(继续执行)。

    R47 P0-4:
        - critical effect 无 action_id → raise EffectReceiptError(拒绝执行)。
        - 非关键 effect 无 action_id → 不记录 receipt,直接执行(向后兼容)。
        - params 参数用于计算 request_hash,绑定 effect 参数防篡改。

    R48 P0-4:
        - critical effect 无 params → raise EffectReceiptError(拒绝执行),
          防止 request_hash 为空时同 action_id 不同参数共用 receipt。
        - request_hash 为空:critical → raise;非 critical → 允许。
    """

    def __init__(
        self,
        action_id: str,
        effect_type: str,
        target: str,
        *,
        best_effort: bool = False,
        params: Optional[dict] = None,
        tx=None,  # R52 P0-4: 可选事务连接(aiosqlite.Connection),传入时不自行 commit
    ):
        self.action_id = action_id or ""
        self.effect_type = effect_type
        self.target = target
        self.best_effort = best_effort
        self.is_critical = _is_critical(effect_type)
        self.fail_closed = self.is_critical and not best_effort
        self.manager: Optional[Any] = None
        self.skipped: bool = False
        self.external_id: str = ""
        self._no_record: bool = False
        # R48 P0-4: 保存 params 供 __aenter__ 校验
        self._params: Optional[dict] = params
        # R52 P0-4: 保存外层事务连接,传递给 check_receipt / record_pending
        self._tx = tx
        # R47 P0-4: 计算 request_hash 绑定 effect 参数
        self.request_hash: str = ""
        if params is not None:
            self.request_hash = compute_effect_request_hash(effect_type, params)

    async def __aenter__(self) -> "EffectReceiptContext":
        # R47 P0-4: critical effect 无 action_id → 拒绝执行
        if self.is_critical and not self.action_id:
            raise EffectReceiptError(
                f"critical effect '{self.effect_type}' requires action_id, got empty"
            )

        # R48 P0-4: critical effect 必须提供 params 用于 request_hash 绑定
        if self.is_critical and self._params is None:
            raise EffectReceiptError(
                f"critical effect '{self.effect_type}' requires params or params_fn "
                f"for request_hash binding"
            )

        # R48 P0-4: critical effect 必须有非空 request_hash
        if self.is_critical and not self.request_hash:
            raise EffectReceiptError(
                f"critical effect '{self.effect_type}' request_hash 为空,拒绝执行"
            )

        if not self.action_id:
            # 非关键 + 无 action_id → 不记录 receipt(向后兼容)
            self._no_record = True
            self.manager = None
            logger.warning(
                f"[effect_receipt] 无 action_id,非关键副作用直执 "
                f"type={self.effect_type}"
            )
            return self

        self.manager = get_receipt_manager()
        if self.manager is None:
            if self.fail_closed:
                raise EffectReceiptError(
                    f"[effect_receipt] manager 不可用,critical 副作用拒绝执行 "
                    f"action={self.action_id} type={self.effect_type}"
                )
            logger.warning(
                f"[effect_receipt] manager 不可用,直接执行 "
                f"action={self.action_id} type={self.effect_type}"
            )
            return self

        # 检查是否已完成 → 跳过
        receipt = await self.manager.check_receipt(
            self.action_id, self.effect_type, self.target,
            fail_closed=self.fail_closed,
            expected_request_hash=self.request_hash,
            tx=self._tx,  # R52 P0-4: 传递外层事务连接
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
        # R50 P0-3: hash mismatch → enter DLQ, refuse to retry external side effects
        if receipt is not None and receipt.get("status") == "hash_mismatch":
            logger.error(
                f"[effect_receipt] hash mismatch, refuse retry (enter DLQ): "
                f"action={self.action_id}, type={self.effect_type}, "
                f"target={self.target}"
            )
            raise EffectReceiptError(
                f"effect receipt hash mismatch for action={self.action_id} "
                f"type={self.effect_type} target={self.target} — "
                f"marked hash_mismatch_needs_reconcile, refuse to retry external side effects"
            )

        # 记录 pending — CAS claim
        claim_ok = await self.manager.record_pending(
            self.action_id, self.effect_type, self.target,
            request_hash=self.request_hash,
            fail_closed=self.fail_closed,
            tx=self._tx,  # R52 P0-4: 传递外层事务连接
        )
        if not claim_ok:
            self.skipped = True
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        if self.manager is None or self.skipped or self._no_record:
            return False

        if exc_type is None:
            # 正常退出 → 记录 completed
            try:
                await self.manager.record_completed(
                    self.action_id, self.effect_type, self.target,
                    self.external_id, fail_closed=self.fail_closed,
                )
            except EffectReceiptError:
                if self.fail_closed:
                    raise
                logger.warning("[effect_receipt] record_completed 失败(fail-open)")
        else:
            # 异常退出 → 记录 failed(不吞异常)
            try:
                await self.manager.record_failed(
                    self.action_id, self.effect_type, self.target,
                    error_msg=str(exc_val) if exc_val else "",
                    fail_closed=self.fail_closed,
                )
            except EffectReceiptError:
                if self.fail_closed:
                    raise
                logger.warning("[effect_receipt] record_failed 失败(fail-open)")
        return False  # 不吞异常

    def set_external_id(self, external_id: str) -> None:
        self.external_id = str(external_id) if external_id is not None else ""

    def mark_no_record(self) -> None:
        self._no_record = True
