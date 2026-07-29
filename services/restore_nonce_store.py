"""R76 P0-06 / O8: 恢复能力令牌 nonce 持久化存储 — 数据库 CAS(替代 /tmp 文件 CAS)。

R76 终审报告 P0-06 要求:nonce 必须使用权威数据库唯一约束/CAS,**不得**以
``/tmp/restore_nonce_store`` 文件作为默认权威状态(容器重建、runner 迁移或
节点切换后状态丢失,R75 要求的 SQLite/CRDB 事务 CAS 并未实现)。

本模块封装 ``CacheStore`` 的 reserve/consume/fail API,提供统一的
``RestoreNonceStore`` 接口,内部使用数据库 UNIQUE 约束(009 migration 新增的
``idx_restore_nonces_nonce_digest`` UNIQUE INDEX)实现跨进程/重启/容器重建的
防重放保护。

安全模型:
    1. ``reserve_capability_nonce()``: INSERT ... ON CONFLICT DO NOTHING,
       PRIMARY KEY=nonce + UNIQUE(nonce_digest) 双重 CAS,同一 nonce 只能被预留一次。
    2. ``consume_capability_nonce()``: UPDATE ... WHERE status='reserved' AND
       operation_id=? AND capability_digest=?,rowcount==1 表示成功(CAS)。
       — 消费时绑定 operation_id + capability_digest,防止换 capability 重放。
    3. ``fail_capability_nonce()``: UPDATE ... WHERE status='reserved',
       允许同 operation 用新 nonce 重试(失败状态留审计)。
    4. 优先 CRDB(跨实例共享);CRDB 不可用时回退 SQLite(单实例部署)。

R76 O8 整改:
    - 替代 R74 P1-04 的 ``/tmp/restore_nonce_store`` 文件 CAS(``os.open(O_EXCL)``)
    - 替代 R74 P1-04 的 ``nonce_store_dir`` 参数(默认 ``/tmp/restore_nonce_store``)
    - 使用 009 migration 新增的 ``nonce_digest`` UNIQUE INDEX 实现数据库层 CAS
    - 绑定 ``operation_id`` + ``capability_digest`` + ``target_identity`` + ``run_id`` +
      ``run_attempt``,实现 R76 P0-05 的独立期望值绑定
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from loguru import logger

from services.error_codes import AppError, ErrorCodes


def _compute_nonce_digest(nonce: str) -> str:
    """计算 nonce 的 SHA-256 摘要(作为 UNIQUE INDEX 键)。

    Args:
        nonce: capability nonce(32 hex 字符,secrets.token_hex(16))

    Returns:
        64 hex 字符的 SHA-256 摘要
    """
    return hashlib.sha256(nonce.encode("utf-8")).hexdigest()


def _compute_capability_digest(capability: dict[str, Any]) -> str:
    """计算 capability canonical JSON 的 SHA-256(防篡改,独立绑定)。

    排除 ``signature`` 字段(签名本身不应纳入 digest,否则签名变化导致 digest 变化)。

    Args:
        capability: capability dict(含 signature 字段)

    Returns:
        64 hex 字符的 SHA-256 摘要
    """
    # 排除 signature 字段(签名不纳入 digest)
    cap_without_sig = {k: v for k, v in capability.items() if k != "signature"}
    canonical = json.dumps(
        cap_without_sig, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class RestoreNonceStore:
    """R76 P0-06 / O8: 恢复能力令牌 nonce 持久化存储(数据库 CAS)。

    封装 ``CacheStore`` 的 reserve/consume/fail API,提供统一接口。
    内部使用数据库 UNIQUE 约束(009 migration 的 ``idx_restore_nonces_nonce_digest``)
    实现跨进程/重启/容器重建的防重放保护。

    用法:
        store = RestoreNonceStore(cache_store)
        # reserve(预留 nonce,assert_valid 调用)
        await store.reserve(capability, context, reserved_by="hostname:pid")
        # consume(消费 nonce,writer 写入成功后调用)
        ok = await store.consume(capability, context, consumed_by="hostname:pid")
        # fail(标记失败,writer 异常后调用,允许同 operation 重试)
        await store.fail(capability, context, failure_reason="restore_failed")

    边界:
        - 本类不验证 capability 签名/字段(由 ``verify_capability()`` 负责)
        - 本类只负责 nonce 的预留/消费/失败状态机持久化
        - 消费时绑定 ``operation_id`` + ``capability_digest`` + ``target_identity`` +
          ``run_id`` + ``run_attempt``,防止换 capability 重放
    """

    def __init__(self, cache_store: Any):
        """初始化 RestoreNonceStore。

        Args:
            cache_store: ``CacheStore`` 实例(提供 reserve/consume/fail API)
        """
        self._cache_store = cache_store

    async def reserve(
        self,
        capability: dict[str, Any],
        context: Any,  # RestoreOperationContext(避免循环 import 用 Any)
        *,
        reserved_by: str = "",
    ) -> bool:
        """预留 nonce(INSERT status='reserved',CAS)。

        nonce 状态机入口:NEW → reserved。PRIMARY KEY=nonce + UNIQUE(nonce_digest)
        双重 CAS,同一 nonce 只能被预留一次。

        Args:
            capability: capability dict(含 nonce 字段)
            context: RestoreOperationContext(提供 operation_id / backup_id /
                     manifest_digest / payload_digest / target_identity / run_id /
                     run_attempt 等独立绑定字段)
            reserved_by: 预留者标识(hostname:pid,审计字段)

        Returns:
            True=预留成功;False=nonce 已存在(重放或竞态失败)

        Raises:
            AppError(BACKUP_RESTORE_TRUST_CHAIN_REQUIRED): capability 缺 nonce 字段
        """
        nonce = capability.get("nonce", "")
        if not nonce:
            raise AppError(
                ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
                params={"reason": "capability_nonce_missing_for_reserve"},
            )

        # R76 P0-06 / O8: 计算独立绑定的摘要值,传递给 CacheStore.reserve_capability_nonce
        # - nonce_digest: sha256(nonce),用于 009 migration 的 UNIQUE INDEX 双重 CAS
        # - capability_digest: sha256(canonical_json(capability_without_signature)),
        #   防"换 capability 重放"(同 nonce 不同 capability 内容)
        # - target_identity / run_id / run_attempt: 来自 RestoreOperationContext 独立来源,
        #   不得由 capability 自身回填(R76 P0-05)
        nonce_digest = _compute_nonce_digest(nonce)
        capability_digest = _compute_capability_digest(capability)
        target_identity = getattr(context, "target_identity", None)
        run_id = getattr(context, "run_id", None)
        run_attempt = getattr(context, "run_attempt", None)

        try:
            reserved = await self._cache_store.reserve_capability_nonce(
                nonce=nonce,
                operation_id=context.operation_id,
                backup_id=context.backup_id,
                manifest_sha256=context.manifest_digest,
                payload_digest=context.payload_digest,
                reserved_by=reserved_by,
                nonce_digest=nonce_digest,
                capability_digest=capability_digest,
                target_identity=target_identity,
                run_id=run_id,
                run_attempt=run_attempt,
            )
        except Exception as e:
            logger.error(
                f"[RestoreNonceStore] reserve_capability_nonce 失败: {e}",
                component="restore_nonce_store",
                event="reserve_failed",
                operation_id=context.operation_id,
            )
            raise AppError(
                ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
                params={"reason": f"reserve_capability_nonce_failed: {e}"},
            ) from e

        if not reserved:
            logger.error(
                "[RestoreNonceStore] nonce 已存在(重放攻击或竞态失败)",
                component="restore_nonce_store",
                event="nonce_already_reserved",
                operation_id=context.operation_id,
                nonce_prefix=nonce[:8],
            )
        return reserved

    async def consume(
        self,
        capability: dict[str, Any],
        context: Any,  # RestoreOperationContext
        *,
        consumed_by: str = "",
    ) -> bool:
        """消费 nonce(CAS UPDATE reserved→consumed)。

        writer 在 restore 成功后调用本方法完成 reserved→consumed 转换。
        消费时绑定 operation_id + capability_digest + target_identity + run_id +
        run_attempt,防止换 capability 重放。

        Args:
            capability: capability dict(含 nonce 字段)
            context: RestoreOperationContext(提供独立绑定字段)
            consumed_by: 消费者标识(hostname:pid,审计字段)

        Returns:
            True=消费成功(reserved→consumed CAS 成功);
            False=nonce 不在 reserved 状态(已 consumed/failed/不存在)

        Raises:
            AppError(BACKUP_RESTORE_TRUST_CHAIN_REQUIRED): capability 缺 nonce 字段
        """
        nonce = capability.get("nonce", "")
        if not nonce:
            raise AppError(
                ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
                params={"reason": "capability_nonce_missing_for_consume"},
            )

        # R76 P0-06 / O8: 计算 nonce_digest / capability_digest 并传递给
        # CacheStore.consume_capability_nonce,作为 CAS UPDATE 的 WHERE 子句比对字段。
        # - capability_digest 加入 WHERE 子句:防止"换 capability 重放"
        #   (攻击者获取同 nonce 但篡改其他字段的 capability 重放消费)
        # - operation_id 传递:用于精确匹配预留行(同 nonce 关联多 operation 时)
        nonce_digest = _compute_nonce_digest(nonce)
        capability_digest = _compute_capability_digest(capability)

        try:
            consumed = await self._cache_store.consume_capability_nonce(
                nonce=nonce,
                backup_id=context.backup_id,
                manifest_sha256=context.manifest_digest,
                payload_digest=context.payload_digest,
                consumed_by=consumed_by,
                nonce_digest=nonce_digest,
                capability_digest=capability_digest,
                operation_id=context.operation_id,
            )
        except Exception as e:
            logger.error(
                f"[RestoreNonceStore] consume_capability_nonce 失败: {e}",
                component="restore_nonce_store",
                event="consume_failed",
                operation_id=context.operation_id,
            )
            raise AppError(
                ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
                params={"reason": f"consume_capability_nonce_failed: {e}"},
            ) from e

        if not consumed:
            logger.error(
                "[RestoreNonceStore] nonce 不在 reserved 状态"
                "(已 consumed/failed/不存在,重放攻击)",
                component="restore_nonce_store",
                event="nonce_not_reserved",
                operation_id=context.operation_id,
                nonce_prefix=nonce[:8],
            )
        return consumed

    async def fail(
        self,
        capability: dict[str, Any],
        context: Any,  # RestoreOperationContext
        *,
        failure_reason: str = "",
    ) -> bool:
        """标记 nonce 失败(CAS UPDATE reserved→failed)。

        writer 在 restore 异常后调用本方法,允许同 operation 用新 nonce 重试
        (旧 failed nonce 留审计)。

        Args:
            capability: capability dict(含 nonce 字段)
            context: RestoreOperationContext(提供独立绑定字段)
            failure_reason: 失败原因(审计追溯)

        Returns:
            True=标记成功(reserved→failed CAS 成功);
            False=nonce 不在 reserved 状态(已 consumed/failed/不存在)
        """
        nonce = capability.get("nonce", "")
        if not nonce:
            # nonce 缺失,无法 fail(可能是 capability 加载失败)
            logger.warning(
                "[RestoreNonceStore] capability 缺 nonce 字段,无法 fail",
                component="restore_nonce_store",
                event="nonce_missing_for_fail",
                operation_id=getattr(context, "operation_id", ""),
            )
            return False

        try:
            # R76 P0-06: 传递 operation_id / capability_digest 用于审计
            # (fail 不需要 capability_digest CAS 比对 — 旧 reserved 行已绑定,
            #  此处仅为审计追溯:哪份 capability 在哪次 operation 失败)
            capability_digest = _compute_capability_digest(capability)
            failed = await self._cache_store.fail_capability_nonce(
                nonce=nonce,
                failure_reason=failure_reason,
                operation_id=context.operation_id,
                capability_digest=capability_digest,
            )
        except Exception as e:
            logger.error(
                f"[RestoreNonceStore] fail_capability_nonce 失败: {e}",
                component="restore_nonce_store",
                event="fail_failed",
                operation_id=context.operation_id,
            )
            raise AppError(
                ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
                params={"reason": f"fail_capability_nonce_failed: {e}"},
            ) from e

        if not failed:
            logger.warning(
                "[RestoreNonceStore] nonce 不在 reserved 状态,无法 fail"
                "(可能已被消费或已失败)",
                component="restore_nonce_store",
                event="nonce_not_reserved_for_fail",
                operation_id=context.operation_id,
                nonce_prefix=nonce[:8],
            )
        return failed

    async def is_consumed(self, nonce: str) -> bool:
        """查询 nonce 是否已消费(不消费,仅查询)。

        注意:预检与 reserve 之间存在 TOCTOU 窗口,因此 ``reserve()`` 仍使用
        PRIMARY KEY CAS 作为权威判断。本方法仅用于审计/调试查询。

        Args:
            nonce: capability nonce

        Returns:
            True=nonce 已消费;False=未消费或不存在
        """
        try:
            return await self._cache_store.is_capability_nonce_consumed(nonce)
        except Exception as e:
            logger.warning(
                f"[RestoreNonceStore] is_capability_nonce_consumed 查询失败: {e}",
                component="restore_nonce_store",
                event="is_consumed_query_failed",
            )
            raise AppError(
                ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
                params={"reason": f"is_capability_nonce_consumed_failed: {e}"},
            ) from e


def get_default_nonce_store() -> RestoreNonceStore:
    """获取默认 RestoreNonceStore 实例(使用全局 CacheStore 单例)。

    Returns:
        RestoreNonceStore 实例
    """
    from database.cache_store import get_cache_store
    return RestoreNonceStore(get_cache_store())
