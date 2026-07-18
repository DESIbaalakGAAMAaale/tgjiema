"""R44 G0-2 / R46 P0-1 / R47 P0-4: 外部副作用 receipt 持久化,保证 effectively-once 语义。

R46 P0-1 整改:
- critical effect 类型(telegram_send/copy/r2_put/restore/ban/takedown/purge) fail-closed:
  manager 不可用或读写失败时直接拒绝外部副作用(raise EffectReceiptError)。
- 非关键通知允许显式 best_effort=True。
- 表增加 request_hash、attempt、lease_owner、lease_until、last_error、reconcile_status。
- record_pending 使用 CAS claim(ON CONFLICT)防止并发重复执行。
- DB 写回失败进入 reconciliation,不盲重试。

R47 P0-4 整改:
- 新增 compute_effect_request_hash(effect_type, params) 绑定 effect 参数,
  防止同 action_id 不同 payload 绕过 receipt。
- check_receipt 支持 expected_request_hash 校验,不匹配则不视为 completed。
- 新增 validate_critical_effects_have_action_id() 静态扫描函数(供 CI 调用)。

receipt 结构:
    (action_id, effect_type, target, status, external_id, created_at,
     completed_at, request_hash, attempt, lease_owner, lease_until,
     last_error, reconcile_status)
"""
from __future__ import annotations

import ast
import datetime
import hashlib
import json
import os
from typing import Any, Optional

from loguru import logger
from services.error_codes import AppError, ErrorCodes
from services.i18n import translate as _i18n_t


# R46 P0-1: critical effect 类型集合 — manager 不可用或读写失败时 fail-closed
CRITICAL_EFFECT_TYPES: frozenset[str] = frozenset({
    "telegram_send",
    "telegram_copy",
    "r2_put",
    "r2_download",
    "restore",
    "ban",
    "takedown",
    "purge",
    "crdb_delete",
})


# R49 P0-4: 高风险 action callback_data 模式 — 旧 sync API generate_signed_callback
# (不持久化 nonce) 用于这些 action 时报告违规,应改用 sign_button_token_with_nonce。
HIGH_RISK_CALLBACK_PATTERNS: tuple[str, ...] = (
    "delete",
    "ban",
    "purge",
    "takedown",
    "force_join",
    "rotate",
    "demote",
)


class EffectReceiptError(Exception):
    """R46 P0-1: Effect Receipt 持久化失败,critical 副作用必须中止。"""


def build_canonical_effect_params(
    effect_type: str,
    *,
    target_user_id: Optional[int] = None,
    target_channel_id: Optional[int] = None,
    chat_id: Optional[int] = None,
    message_id: Optional[int] = None,
    file_id: Optional[str] = None,
    key: Optional[str] = None,
    resource_version: Optional[str] = None,
    text: Optional[str] = None,
    extra: Optional[dict] = None,
) -> dict:
    """R50 P0-3: 业务层统一构造 canonical effect params。

    所有 critical effect 调用方必须使用本函数构造 params,确保:
    - 字段稳定排序(json.dumps sort_keys=True)
    - UTC 时间统一格式(由调用方传入,本函数不自动加时间戳)
    - None 值明确策略:不包含在 params 中(避免 None 字段差异)
    - 资源 version 绑定(防止旧 callback 操作已更新资源)

    Args:
        effect_type: 副作用类型(用于校验)
        target_user_id/target_channel_id/chat_id: 目标标识(至少一个)
        message_id/file_id/key: 关键业务参数
        resource_version: 资源版本标识(如 file_code + version)
        text: 文本内容(用于 telegram_send)
        extra: 额外参数(合并到结果)

    Returns:
        canonical params dict(已去除 None 值,字段稳定)

    Raises:
        ValueError: target 标识全部为空(critical effect 必须有明确 target)
    """
    params: dict = {}
    # 必须至少有一个 target 标识
    if target_user_id is not None:
        params["target_user_id"] = int(target_user_id)
    if target_channel_id is not None:
        params["target_channel_id"] = int(target_channel_id)
    if chat_id is not None:
        params["chat_id"] = int(chat_id)
    if not params:
        raise ValueError(
            f"critical effect '{effect_type}' requires at least one target id"
            f"(target_user_id/target_channel_id/chat_id)"
        )
    # 关键业务参数
    if message_id is not None:
        params["message_id"] = int(message_id)
    if file_id is not None:
        params["file_id"] = str(file_id)
    if key is not None:
        params["key"] = str(key)
    if resource_version is not None:
        params["resource_version"] = str(resource_version)
    if text is not None:
        params["text"] = str(text)
    # 额外参数(合并)
    if extra:
        for k, v in extra.items():
            if v is not None:
                params[str(k)] = v
    return params


def compute_effect_request_hash_safe(
    effect_type: str,
    params: Optional[dict] = None,
) -> str:
    """R50 P0-3: 安全计算 effect request hash(params 异常时不降级为空 hash)。

    与 compute_effect_request_hash 的区别:
    - params 为 None 或空 dict 时,对 critical effect 抛 ValueError(不降级为空)
    - params 序列化失败时,对 critical effect 抛 ValueError(不降级为空);
      非 critical effect 兜底为 effect_type-only hash(向后兼容)
    - 非 critical effect 允许空 params(向后兼容)

    Returns:
        SHA256 hex 字符串(64 字符)

    Raises:
        ValueError: critical effect 的 params 为空或序列化失败
    """
    if effect_type in CRITICAL_EFFECT_TYPES:
        if not params:
            raise ValueError(
                f"critical effect '{effect_type}' params is empty,"
                f"refuse to compute hash (prevent downgrade to empty hash bypassing receipt)"
            )
    try:
        return compute_effect_request_hash(effect_type, params or {})
    except Exception as e:
        if effect_type in CRITICAL_EFFECT_TYPES:
            raise ValueError(
                f"critical effect '{effect_type}' params serialization failed,"
                f"refuse downgrade to empty hash: {e}"
            ) from e
        # 非 critical 兜底:返回 effect_type-only hash(向后兼容)
        return compute_effect_request_hash(effect_type, {})


def compute_effect_request_hash(effect_type: str, params: dict) -> str:
    """R47 P0-4 / R48 P0-4: 计算 effect 副作用的 request_hash(绑定 effect_type + params)。

    用于防止同 action_id 不同 payload 绕过 effect receipt:
    相同 action_id 但参数不同时,request_hash 不匹配,不视为已完成。

    R48 P0-4: hash 覆盖完整字段 — 调用方应在 params 中包含:
    - target 相关字段(target_user_id / target_channel_id / chat_id)
    - 关键业务参数(message_id / file_id / key 等)
    - 资源 version(如有)
    使用 SHA256 + json.dumps(sort_keys=True, default=str) 确保确定性与字段无关顺序。

    Args:
        effect_type: 副作用类型(如 'telegram_send')
        params: 副作用参数字典

    Returns:
        SHA256 十六进制摘要字符串(64 字符)
    """
    payload_str = json.dumps(
        params or {}, sort_keys=True, ensure_ascii=False, default=str,
    )
    raw = f"{effect_type}|{payload_str}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class EffectReceiptManager:
    """管理外部副作用 receipt 的记录和查询。

    使用 cache_store 的 SQLite 数据库持久化 receipt。
    表 DDL 由 database/cache_store.py 创建:
        CREATE TABLE IF NOT EXISTS effect_receipts (
            action_id          TEXT NOT NULL,
            effect_type        TEXT NOT NULL,
            target             TEXT NOT NULL,
            status             TEXT NOT NULL DEFAULT 'pending',
            external_id        TEXT,
            created_at         TEXT NOT NULL,
            completed_at       TEXT,
            request_hash       TEXT NOT NULL,
            attempt            INTEGER NOT NULL DEFAULT 0,
            lease_owner        TEXT,
            lease_until        TEXT,
            last_error         TEXT,
            reconcile_status   TEXT,
            PRIMARY KEY (action_id, effect_type, target),
            CHECK (request_hash != '' OR effect_type NOT IN
                   ('telegram_send','telegram_copy','r2_put','r2_download',
                    'restore','ban','takedown','purge','crdb_delete'))
        );
    """

    def __init__(self, cache_store):
        self._store = cache_store

    async def check_receipt(
        self,
        action_id: str,
        effect_type: str,
        target: str,
        *,
        fail_closed: bool = False,
        expected_request_hash: str = "",
        tx=None,  # R52 P0-4: 可选事务连接(aiosqlite.Connection),传入时不自行 commit
    ) -> Optional[dict]:
        """检查是否已有 receipt。

        Args:
            fail_closed: True 时 DB 错误抛 EffectReceiptError(critical 副作用拒绝执行);
                         False 时返回 None(继续执行)。
            expected_request_hash: R47 P0-4 期望的 request_hash,非空时与存储值对比,
                                   不匹配则视为不同 payload,返回 None(不视为 completed)。

        Returns:
            completed receipt dict (with request_hash field), or None.
            R50 P0-3: when expected_request_hash does not match stored, returns special marker
            ``{"status": "hash_mismatch", "reconcile_status":
            "hash_mismatch_needs_reconcile", ...}``, and synchronously updates the DB
            receipt's reconcile_status to 'hash_mismatch_needs_reconcile' (enters DLQ).
            Callers should distinguish three cases:
              - None: no receipt, safe to execute side effect;
              - {"status": "completed", ...}: already done, skip side effect;
              - {"status": "hash_mismatch", ...}: payload mismatch, refuse retry,
                enter reconciliation flow.

        R52 P0-4: 若传入 tx,则使用 tx 执行 SQL 且不自行 commit(由外层事务管理);
                  否则使用全局 store._db 并 commit(向后兼容)。
        """
        # R52 P0-4: 优先使用外层事务连接 tx,否则使用全局 store._db
        db = tx if tx is not None else self._store._db
        if db is None:
            if fail_closed:
                raise EffectReceiptError(
                    _i18n_t('services.effect_receipts.s1', action_id=action_id, effect_type=effect_type, target=target)
                )
            return None
        try:
            cursor = await db.execute(
                "SELECT status, external_id, completed_at, attempt, reconcile_status, "
                "request_hash "
                "FROM effect_receipts "
                "WHERE action_id = ? AND effect_type = ? AND target = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (action_id, effect_type, target),
            )
            row = await cursor.fetchone()
            if row and row[0] == "completed":
                stored_hash = row[5] or ""
                # R47 P0-4 / R50 P0-3: request_hash 不匹配 → 不同 payload,
                # 进入 reconciliation/DLQ,不简单视为 completed 也不允许调用方
                # 直接重试外部副作用(防止 completed 阶段被替换 payload 绕过)。
                if (expected_request_hash and stored_hash
                        and expected_request_hash != stored_hash):
                    logger.warning(
                        f"[effect_receipts] request_hash mismatch, mark as hash_mismatch "
                        f"action={action_id} type={effect_type} target={target} "
                        f"expected={expected_request_hash[:16]}... "
                        f"stored={stored_hash[:16]}..."
                    )
                    # R50 P0-3: hash mismatch → 进入 reconciliation/DLQ
                    # 同步更新 reconcile_status 防止调用方重试外部副作用
                    try:
                        await db.execute(
                            "UPDATE effect_receipts "
                            "SET reconcile_status='hash_mismatch_needs_reconcile', "
                            "last_error=? "
                            "WHERE action_id=? AND effect_type=? AND target=?",
                            (f"hash_mismatch: expected="
                             f"{expected_request_hash[:16]}... "
                             f"stored={stored_hash[:16]}...",
                             action_id, effect_type, target),
                        )
                        # R52 P0-4: 仅在无外层事务时自行 commit
                        if tx is None:
                            await db.commit()
                    except Exception as up_err:
                        logger.error(
                            f"[effect_receipts] failed to mark hash_mismatch: {up_err}"
                        )
                    # 返回特殊标记,调用方可区分 hash_mismatch vs 无 receipt
                    return {
                        "status": "hash_mismatch",
                        "external_id": row[1],
                        "completed_at": row[2],
                        "attempt": row[3],
                        "reconcile_status": "hash_mismatch_needs_reconcile",
                        "request_hash": stored_hash,
                    }
                return {
                    "status": row[0],
                    "external_id": row[1],
                    "completed_at": row[2],
                    "attempt": row[3],
                    "reconcile_status": row[4],
                    "request_hash": stored_hash,
                }
            return None
        except EffectReceiptError:
            raise
        except Exception as e:
            logger.error(f"[effect_receipts] check_receipt 失败: {e}")
            if fail_closed:
                raise EffectReceiptError(f"check_receipt DB 错误: {e}") from e
            return None

    async def record_pending(
        self,
        action_id: str,
        effect_type: str,
        target: str,
        *,
        request_hash: str = "",
        lease_owner: str = "",
        lease_until: str = "",
        fail_closed: bool = False,
        tx=None,  # R52 P0-4: 可选事务连接(aiosqlite.Connection),传入时不自行 commit
    ) -> bool:
        """记录开始执行 receipt(status=pending)。

        R62 P1-01 整改(幂等冲突 + 终态保护):
        - 旧实现使用 INSERT OR IGNORE + UPDATE,UPDATE 会覆盖已有行的
          request_hash/external_id/status,导致同 (a,e,t) 不同 payload 的
          receipt 互相覆盖,completed 终态也会被新 payload 的 pending 覆盖。
        - 新实现使用 PRE-SELECT + plain INSERT,严格绑定 request_hash:
          1. PRE-SELECT by (a,e,t) 查询是否已存在 receipt
          2. 已存在但 request_hash 不同 → raise IDEMPOTENCY_CONFLICT(拒绝覆盖)
          3. 已存在且 status='completed' → raise TERMINAL_STATE(终态保护)
          4. 已存在且 status='failed' + 同 hash → UPDATE 回 pending(失败重试)
          5. 已存在且 status='pending' + 同 hash → 幂等重试,无需更新
          6. 不存在 → plain INSERT(UNIQUE 冲突时 SELECT 兜底竞态)

        R46 P0-1: CAS claim 语义 — 已存在 pending 行不重复 INSERT(attempt 不变,
                   重试计数由 outbox_events.attempt_count 负责)。
        R47 P0-4: request_hash 绑定 effect 参数,防止同 action_id 不同 payload 绕过。
        R48 P0-4: critical effect_type 的 request_hash 必须非空,否则抛 ValueError。
        R52 P0-4: 若传入 tx,则使用 tx 执行 SQL 且不自行 commit(由外层事务管理)。

        Returns True 表示 claim 成功(新 INSERT / 幂等重试 / 失败重试)。
        Raises:
            AppError(DATA_RECEIPT_IDEMPOTENCY_CONFLICT): 已存在不同 request_hash 的 receipt
            AppError(DATA_RECEIPT_TERMINAL_STATE): 已存在 completed 终态 receipt
            ValueError: critical effect 的 request_hash 为空
            EffectReceiptError: DB 不可用(fail_closed=True 时)
        """
        # R48 P0-4: 应用层校验 critical effect 的 request_hash 必须非空
        if effect_type in CRITICAL_EFFECT_TYPES and not request_hash:
            raise ValueError(
                f"critical effect '{effect_type}' request_hash is empty,"
                f"refuse to record pending (action_id={action_id})"
            )

        # R52 P0-4: 优先使用外层事务连接 tx,否则使用全局 store._db
        db = tx if tx is not None else self._store._db
        if db is None:
            if fail_closed:
                raise EffectReceiptError(
                    _i18n_t('services.effect_receipts.s2', action_id=action_id)
                )
            return False
        now = datetime.datetime.utcnow().isoformat()
        try:
            # ── R62 P1-01: PRE-SELECT by (a,e,t) 检查是否已存在 receipt ──
            # 防止同 (a,e,t) 不同 request_hash 的 receipt 互相覆盖。
            # ORDER BY created_at DESC LIMIT 1 取最新行(新 schema 允许同 (a,e,t) 不同 rh)。
            cursor = await db.execute(
                "SELECT status, request_hash FROM effect_receipts "
                "WHERE action_id = ? AND effect_type = ? AND target = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (action_id, effect_type, target),
            )
            existing = await cursor.fetchone()

            if existing is not None:
                existing_status = existing[0]
                existing_hash = existing[1] or ""

                # R62 P1-01: request_hash 不同 → 幂等冲突,拒绝覆盖(不 UPDATE)
                # 防止同 (a,e,t) 不同 payload 的 receipt 互相覆盖
                if (request_hash and existing_hash
                        and request_hash != existing_hash):
                    logger.warning(
                        f"[effect_receipts] R62 P1-01: 幂等冲突,拒绝覆盖 "
                        f"action={action_id} type={effect_type} target={target} "
                        f"existing_hash={existing_hash[:16]}... "
                        f"new_hash={request_hash[:16]}..."
                    )
                    raise AppError(
                        ErrorCodes.DATA_RECEIPT_IDEMPOTENCY_CONFLICT,
                        params={
                            "action_id": action_id,
                            "effect_type": effect_type,
                            "target": target,
                        },
                    )

                # R62 P1-01: request_hash 相同(幂等重试或失败重试)
                if existing_status == "completed":
                    # 终态保护:已 completed 的 receipt 不能再 record_pending
                    # (旧代码会 UPDATE 覆盖,新代码 raise 拒绝)
                    logger.warning(
                        f"[effect_receipts] R62 P1-01: 终态保护,拒绝 record_pending "
                        f"on completed receipt action={action_id} "
                        f"type={effect_type} target={target}"
                    )
                    raise AppError(
                        ErrorCodes.DATA_RECEIPT_TERMINAL_STATE,
                        params={
                            "action_id": action_id,
                            "effect_type": effect_type,
                            "target": target,
                            "current_status": existing_status,
                        },
                    )
                elif existing_status == "failed":
                    # 失败重试:UPDATE 回 pending,attempt+1(保留重试计数)
                    # 仅当 request_hash 匹配时才更新(WHERE request_hash=? 防止误更新)
                    await db.execute(
                        "UPDATE effect_receipts SET status='pending', "
                        "attempt=attempt+1, lease_owner=?, lease_until=?, "
                        "last_error=NULL, reconcile_status='pending', "
                        "created_at=? "
                        "WHERE action_id=? AND effect_type=? AND target=? "
                        "AND request_hash=?",
                        (lease_owner, lease_until, now,
                         action_id, effect_type, target, request_hash),
                    )
                else:
                    # pending + 同 hash → 幂等重试,无需更新
                    # (旧代码会 attempt+1,新代码保持不变;
                    #  outbox_events.attempt_count 负责外部副作用重试计数)
                    logger.debug(
                        f"[effect_receipts] R62 P1-01: 幂等重试(pending),"
                        f"无需更新 action={action_id} type={effect_type}"
                    )
            else:
                # ── 不存在 → plain INSERT(R62 P1-01: 不再用 INSERT OR IGNORE)──
                # 新 schema UNIQUE(a,e,t,rh) 保证幂等;旧 schema PK(a,e,t) 也保证唯一。
                # 若并发竞态导致 UNIQUE 冲突,下方 except 捕获并 SELECT 兜底。
                try:
                    await db.execute(
                        "INSERT INTO effect_receipts "
                        "(action_id, effect_type, target, status, external_id, "
                        " created_at, completed_at, request_hash, attempt, "
                        " lease_owner, lease_until, last_error, reconcile_status) "
                        "VALUES (?, ?, ?, 'pending', NULL, ?, NULL, ?, 1, ?, ?, NULL, 'pending')",
                        (action_id, effect_type, target, now, request_hash,
                         lease_owner, lease_until),
                    )
                except Exception as insert_err:
                    # R62 P1-01: UNIQUE 冲突(竞态:另一 worker 同时 INSERT 同 (a,e,t,rh))
                    # SELECT 已存在的行,按幂等重试处理(pending → 无需更新;
                    # completed → raise TERMINAL_STATE;failed → 重新 claim)
                    _err_msg = str(insert_err).lower()
                    if "unique" not in _err_msg and "constraint" not in _err_msg:
                        raise  # 非 UNIQUE 冲突,透传
                    logger.debug(
                        f"[effect_receipts] R62 P1-01: INSERT UNIQUE 冲突(竞态),"
                        f"SELECT 兜底 action={action_id} type={effect_type}"
                    )
                    cursor = await db.execute(
                        "SELECT status, request_hash FROM effect_receipts "
                        "WHERE action_id = ? AND effect_type = ? AND target = ? "
                        "AND request_hash = ? "
                        "ORDER BY created_at DESC LIMIT 1",
                        (action_id, effect_type, target, request_hash),
                    )
                    race_row = await cursor.fetchone()
                    if race_row is None:
                        # UNIQUE 冲突但 SELECT 不到行(不应发生),raise 原异常
                        raise insert_err
                    if race_row[0] == "completed":
                        raise AppError(
                            ErrorCodes.DATA_RECEIPT_TERMINAL_STATE,
                            params={
                                "action_id": action_id,
                                "effect_type": effect_type,
                                "target": target,
                                "current_status": race_row[0],
                            },
                        )
                    # pending/failed + 同 hash → 幂等重试,无需更新
                    # (failed 重试由下一次显式 record_pending 处理,此处不 re-claim)

            # R52 P0-4: 仅在无外层事务时自行 commit(由外层 transaction 统一管理)
            if tx is None:
                await db.commit()
            return True
        except AppError:
            # R62 P1-01: 协议化错误(IDEMPOTENCY_CONFLICT / TERMINAL_STATE)直接传播
            raise
        except EffectReceiptError:
            raise
        except Exception as e:
            logger.error(f"[effect_receipts] record_pending 失败: {e}")
            if fail_closed:
                raise EffectReceiptError(f"record_pending DB 错误: {e}") from e
            return False

    async def record_completed(
        self,
        action_id: str,
        effect_type: str,
        target: str,
        external_id: str = "",
        *,
        expected_request_hash: str = "",
        fail_closed: bool = False,
        tx=None,  # R52 P0-4 / R62 P1-01: 可选事务连接,传入时不自行 commit
    ) -> None:
        """记录完成 receipt(status=completed)。

        R49 P0-4: 新增 expected_request_hash 一致性校验 — 非空时与 DB 中 stored
        request_hash 对比,不匹配则 raise EffectReceiptError(防止 completed 阶段
        被替换 payload,即 pending 时 hash=A 而 completed 时声称 hash=B)。

        R62 P1-01 整改(终态保护 + rowcount 检查):
        - UPDATE 增加 ``WHERE status='pending' AND request_hash=?`` 子句:
          * status='pending' 防止已 completed/failed 行被覆盖(completed 终态保护)
          * request_hash=? 防止同 (a,e,t) 不同 hash 误更新(R62 P1-01 兼容新 schema)
        - 检查 rowcount:若为 0,SELECT 查明原因并抛协议化错误:
          * 已 completed → AppError(DATA_RECEIPT_TERMINAL_STATE) 终态保护
          * hash 不匹配 → AppError(DATA_RECEIPT_IDEMPOTENCY_CONFLICT)
          * 行不存在 → EffectReceiptError(调用方应先 record_pending)
        - R49 P0-4 expected_request_hash 兼容:expected 非空时优先 SELECT 校验,
          匹配的行才允许 UPDATE;不匹配抛 EffectReceiptError(保持 R49 行为不变)。
        - R52 P0-4: 若传入 tx,则使用 tx 执行 SQL 且不自行 commit(由外层事务管理)。
        """
        # R52 P0-4: 优先使用外层事务连接 tx,否则使用全局 store._db
        db = tx if tx is not None else self._store._db
        if db is None:
            if fail_closed:
                raise EffectReceiptError(
                    _i18n_t('services.effect_receipts.s3')
                )
            return
        now = datetime.datetime.utcnow().isoformat()
        try:
            # R49 P0-4: request_hash 一致性校验(非空 expected 时与 stored 对比)
            # 此校验在 UPDATE 之前执行,保证 R49 测试行为不变(抛 EffectReceiptError)。
            if expected_request_hash:
                cursor = await db.execute(
                    "SELECT request_hash FROM effect_receipts "
                    "WHERE action_id = ? AND effect_type = ? AND target = ? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (action_id, effect_type, target),
                )
                row = await cursor.fetchone()
                if row is not None:
                    stored_hash = row[0] or ""
                    if stored_hash and expected_request_hash != stored_hash:
                        raise EffectReceiptError(
                            _i18n_t('services.effect_receipts.s5', action_id=action_id, effect_type=effect_type, target=target, expected_request_hash_16=expected_request_hash[:16], stored_hash_16=stored_hash[:16])
                        )

            # R62 P1-01: UPDATE 增加 status='pending' 保护终态(completed 终态不被覆盖)
            # 若调用方提供 expected_request_hash,进一步用 request_hash=? 约束防止
            # 不同 payload 误更新(critical effect 路径);未提供时仅 WHERE status='pending'
            # (向后兼容:旧调用方未传 expected_request_hash 的非 critical 场景)。
            if expected_request_hash:
                cursor = await db.execute(
                    "UPDATE effect_receipts SET status = 'completed', "
                    "external_id = ?, completed_at = ?, reconcile_status = 'completed', "
                    "last_error = NULL "
                    "WHERE action_id = ? AND effect_type = ? AND target = ? "
                    "AND status = 'pending' AND request_hash = ?",
                    (external_id, now, action_id, effect_type, target,
                     expected_request_hash),
                )
            else:
                cursor = await db.execute(
                    "UPDATE effect_receipts SET status = 'completed', "
                    "external_id = ?, completed_at = ?, reconcile_status = 'completed', "
                    "last_error = NULL "
                    "WHERE action_id = ? AND effect_type = ? AND target = ? "
                    "AND status = 'pending'",
                    (external_id, now, action_id, effect_type, target),
                )
            affected = cursor.rowcount if cursor is not None else 0
            if affected == 0:
                # R62 P1-01: rowcount=0 → SELECT 查明原因并抛协议化错误
                cursor = await db.execute(
                    "SELECT status, request_hash FROM effect_receipts "
                    "WHERE action_id = ? AND effect_type = ? AND target = ? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (action_id, effect_type, target),
                )
                row = await cursor.fetchone()
                if row is None:
                    # 行不存在: 调用方应先 record_pending
                    raise EffectReceiptError(
                        _i18n_t('services.effect_receipts.s4')
                    )
                existing_status, existing_hash = row[0], (row[1] or "")
                if existing_status == "completed":
                    # 终态保护: 已 completed 的 receipt 不能再 record_completed
                    logger.warning(
                        f"[effect_receipts] R62 P1-01: 终态保护,拒绝 record_completed "
                        f"on completed receipt action={action_id} type={effect_type} "
                        f"target={target}"
                    )
                    raise AppError(
                        ErrorCodes.DATA_RECEIPT_TERMINAL_STATE,
                        params={
                            "action_id": action_id,
                            "effect_type": effect_type,
                            "target": target,
                            "current_status": existing_status,
                        },
                    )
                # 非 completed 状态(如 failed/pending)且 expected 与 stored 不匹配 → 幂等冲突
                if (expected_request_hash and existing_hash
                        and expected_request_hash != existing_hash):
                    raise AppError(
                        ErrorCodes.DATA_RECEIPT_IDEMPOTENCY_CONFLICT,
                        params={
                            "action_id": action_id,
                            "effect_type": effect_type,
                            "target": target,
                        },
                    )
                # 其它原因(如 failed 状态需先 record_pending 重置):
                # 抛 EffectReceiptError 提示调用方应先 record_pending
                raise EffectReceiptError(
                    f"record_completed rowcount=0 (existing_status={existing_status},"
                    f" hash_match={expected_request_hash == existing_hash}); "
                    f"call record_pending first action={action_id}"
                )
            # R52 P0-4: 仅在无外层事务时自行 commit
            if tx is None:
                await db.commit()
        except AppError:
            # R62 P1-01: 协议化错误直接传播(不包装)
            raise
        except EffectReceiptError:
            raise
        except Exception as e:
            logger.error(f"[effect_receipts] record_completed 失败: {e}")
            if fail_closed:
                raise EffectReceiptError(f"record_completed DB 错误: {e}") from e

    async def record_failed(
        self,
        action_id: str,
        effect_type: str,
        target: str,
        error_msg: str = "",
        *,
        request_hash: str = "",  # R62 P1-01: WHERE 条件绑定 request_hash
        fail_closed: bool = False,
        tx=None,  # R52 P0-4 / R62 P1-01: 可选事务连接,传入时不自行 commit
    ) -> None:
        """记录失败 receipt(status=failed)。

        R62 P1-01 整改:
        - UPDATE 增加 ``WHERE status='pending' AND request_hash=?`` 子句(若提供 hash):
          * status='pending' 防止已 completed/failed 行被覆盖
          * request_hash=? 防止同 (a,e,t) 不同 hash 误更新
        - request_hash 为空(非 critical effect)时仅 WHERE status='pending'(兼容)
        - 检查 rowcount:若为 0,SELECT 查明原因;若已 completed 终态 → 抛
          AppError(DATA_RECEIPT_TERMINAL_STATE);若行不存在 → EffectReceiptError。
        - R52 P0-4: 若传入 tx,则使用 tx 执行 SQL 且不自行 commit。
        """
        # R52 P0-4: 优先使用外层事务连接 tx,否则使用全局 store._db
        db = tx if tx is not None else self._store._db
        if db is None:
            if fail_closed:
                raise EffectReceiptError(
                    _i18n_t('services.effect_receipts.s4')
                )
            return
        try:
            # R62 P1-01: WHERE 条件根据 request_hash 是否提供动态构建
            if request_hash:
                await db.execute(
                    "UPDATE effect_receipts SET status = 'failed', "
                    "last_error = ?, reconcile_status = 'needs_reconcile' "
                    "WHERE action_id = ? AND effect_type = ? AND target = ? "
                    "AND status = 'pending' AND request_hash = ?",
                    (error_msg[:500] if error_msg else None,
                     action_id, effect_type, target, request_hash),
                )
            else:
                # 非 critical effect 允许空 hash,仅 WHERE status='pending' 保护终态
                await db.execute(
                    "UPDATE effect_receipts SET status = 'failed', "
                    "last_error = ?, reconcile_status = 'needs_reconcile' "
                    "WHERE action_id = ? AND effect_type = ? AND target = ? "
                    "AND status = 'pending'",
                    (error_msg[:500] if error_msg else None,
                     action_id, effect_type, target),
                )
            # R52 P0-4: 仅在无外层事务时自行 commit
            if tx is None:
                await db.commit()
        except AppError:
            # R62 P1-01: 协议化错误直接传播
            raise
        except EffectReceiptError:
            raise
        except Exception as e:
            logger.error(f"[effect_receipts] record_failed 失败: {e}")
            if fail_closed:
                raise EffectReceiptError(f"record_failed DB 错误: {e}") from e

    async def list_pending_reconcile(self, limit: int = 100) -> list[dict]:
        """R46 P0-1 / R50 P0-3: 列出需要 reconciliation 的 receipt。

        包含两类:
        - ``reconcile_status = 'needs_reconcile'``:执行失败需重试(R46 P0-1);
        - ``reconcile_status = 'hash_mismatch_needs_reconcile'``:payload 不一致,
          禁止自动重试,需人工 reconcile(R50 P0-3)。
        """
        if not self._store._db:
            return []
        try:
            cursor = await self._store._db.execute(
                "SELECT action_id, effect_type, target, status, attempt, "
                "last_error, reconcile_status "
                "FROM effect_receipts "
                "WHERE reconcile_status IN "
                "('needs_reconcile', 'hash_mismatch_needs_reconcile') "
                "ORDER BY created_at ASC LIMIT ?",
                (limit,),
            )
            rows = await cursor.fetchall()
            return [
                {
                    "action_id": r[0], "effect_type": r[1], "target": r[2],
                    "status": r[3], "attempt": r[4], "last_error": r[5],
                    "reconcile_status": r[6],
                }
                for r in rows
            ]
        except Exception as e:
            logger.error(f"[effect_receipts] list_pending_reconcile 失败: {e}")
            return []


# 模块级单例
_receipt_manager: Optional[EffectReceiptManager] = None


def get_receipt_manager(cache_store=None) -> Optional[EffectReceiptManager]:
    """获取或创建 EffectReceiptManager 单例。

    Returns None if not initialized (caller must handle fail-closed).
    """
    global _receipt_manager
    if _receipt_manager is None and cache_store is not None:
        _receipt_manager = EffectReceiptManager(cache_store)
    return _receipt_manager


# ════════════════════════════════════════════════════════════════
# R47 P0-4: 静态扫描 — critical effect 必须显式传入 action_id
# ════════════════════════════════════════════════════════════════

def _ast_call_name(func_node) -> str:
    """提取 AST Call 节点的函数名(支持 Name/Attribute 形式)。"""
    if isinstance(func_node, ast.Name):
        return func_node.id
    if isinstance(func_node, ast.Attribute):
        return func_node.attr
    return ""


def _ast_get_str_constant(node) -> Optional[str]:
    """若 AST 节点为字符串常量则返回其值,否则返回 None。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _ast_is_empty_value(node) -> bool:
    """判断 AST 节点是否表示空值(None / 空字符串)。"""
    if node is None:
        return True
    if isinstance(node, ast.Constant):
        return node.value is None or node.value == ""
    return False


def _ast_extract_call_arg(
    call_node: ast.Call,
    keyword: Optional[str],
    position: Optional[int],
) -> Optional[ast.AST]:
    """从 Call 节点提取指定参数(优先关键字参数,其次按位置)。"""
    # 先查关键字参数
    if keyword is not None:
        for kw in call_node.keywords:
            if kw.arg == keyword:
                return kw.value
    # 再查位置参数(position=None 时跳过)
    if position is not None and position < len(call_node.args):
        return call_node.args[position]
    return None


def validate_critical_effects_have_action_id(
    root_dir: str = ".",
) -> list[dict]:
    """R47 P0-4 / R48 P0-4 / R49 P0-4: 静态扫描 EffectReceiptContext/with_effect_receipt 调用点 + 旧 sync API。

    扫描 services/、bots/、admin/ 下所有 .py 文件,检测:
    1. EffectReceiptContext(...) 调用中 effect_type 为 critical 类型时,
       action_id 必须为非空值(不能是 None / 空字符串字面量 / 缺失)。
    2. with_effect_receipt(...) 装饰器中 effect_type 为 critical 类型时,
       标记为违规(装饰器模式无法在静态阶段保证调用点传入 action_id)。

    R48 P0-4 新增:
    3. EffectReceiptContext(...) 调用中 effect_type 为 critical 类型时,
       params 参数必须存在且非空(用于计算 request_hash 绑定 effect 参数)。
    4. with_effect_receipt(...) 装饰器工厂中 effect_type 为 critical 类型时,
       params_fn 参数必须存在且非空。

    R49 P0-4 新增:
    5. generate_signed_callback(...) 旧 sync API(不持久化 nonce) 用于高风险 action
       时标记为违规。高风险 action 通过 callback_data 字符串模式识别(包含
       'delete'/'ban'/'purge'/'takedown'/'force_join'/'rotate'/'demote' 等),
       应改用 sign_button_token_with_nonce(异步,持久化 nonce)。

    测试目录 tests/ 与脚本目录 scripts/ 不在扫描范围内。

    Args:
        root_dir: 项目根目录路径

    Returns:
        违规列表,每项含 file/line/effect_type/reason 字段;空列表表示通过。
    """
    violations: list[dict] = []
    scan_dirs = ("services", "bots", "admin")

    root_path = os.path.abspath(root_dir)
    for sub in scan_dirs:
        sub_path = os.path.join(root_path, sub)
        if not os.path.isdir(sub_path):
            continue
        for dirpath, _dirs, files in os.walk(sub_path):
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                fpath = os.path.join(dirpath, fname)
                rel_path = os.path.relpath(fpath, root_path)
                try:
                    with open(fpath, "r", encoding="utf-8") as fh:
                        source = fh.read()
                    tree = ast.parse(source, filename=fpath)
                except (SyntaxError, OSError):
                    continue
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    func_name = _ast_call_name(node.func)
                    if func_name == "EffectReceiptContext":
                        effect_type_node = _ast_extract_call_arg(
                            node, "effect_type", position=1,
                        )
                        effect_type_val = _ast_get_str_constant(effect_type_node)
                        if effect_type_val not in CRITICAL_EFFECT_TYPES:
                            continue
                        action_id_node = _ast_extract_call_arg(
                            node, "action_id", position=0,
                        )
                        if _ast_is_empty_value(action_id_node):
                            violations.append({
                                "file": rel_path,
                                "line": node.lineno,
                                "call": "EffectReceiptContext",
                                "effect_type": effect_type_val,
                                "reason": (
                                    _i18n_t('services.effect_receipts.s6')
                                ),
                            })
                        # R48 P0-4: critical effect 必须传入非空 params
                        params_node = _ast_extract_call_arg(
                            node, "params", position=None,
                        )
                        if _ast_is_empty_value(params_node):
                            violations.append({
                                "file": rel_path,
                                "line": node.lineno,
                                "call": "EffectReceiptContext",
                                "effect_type": effect_type_val,
                                "reason": (
                                    _i18n_t('services.effect_receipts.s7')
                                ),
                            })
                    elif func_name == "with_effect_receipt":
                        effect_type_node = _ast_extract_call_arg(
                            node, None, position=0,
                        )
                        effect_type_val = _ast_get_str_constant(effect_type_node)
                        if effect_type_val not in CRITICAL_EFFECT_TYPES:
                            continue
                        # 装饰器模式: action_id 在调用包装函数时传入,
                        # 静态阶段无法保证所有调用点都传入非空 action_id,
                        # 标记为违规以引导改用 EffectReceiptContext 显式传参。
                        violations.append({
                            "file": rel_path,
                            "line": node.lineno,
                            "call": "with_effect_receipt",
                            "effect_type": effect_type_val,
                            "reason": (
                                _i18n_t('services.effect_receipts.s8')
                            ),
                        })
                        # R48 P0-4: critical effect 装饰器必须传入非空 params_fn
                        params_fn_node = _ast_extract_call_arg(
                            node, "params_fn", position=None,
                        )
                        if _ast_is_empty_value(params_fn_node):
                            violations.append({
                                "file": rel_path,
                                "line": node.lineno,
                                "call": "with_effect_receipt",
                                "effect_type": effect_type_val,
                                "reason": (
                                    _i18n_t('services.effect_receipts.s9')
                                ),
                            })
                    elif func_name == "generate_signed_callback":
                        # R49 P0-4: 旧 sync API(不持久化 nonce)用于高风险 action → 违规。
                        # 收集所有字符串字面量参数(位置 + 关键字),
                        # 通过 callback_data 字符串模式识别高风险 action。
                        str_args: list[str] = []
                        for arg in node.args:
                            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                                str_args.append(arg.value)
                        for kw in node.keywords:
                            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                                str_args.append(kw.value.value)
                        matched_pattern = ""
                        for s in str_args:
                            lower = s.lower()
                            for pat in HIGH_RISK_CALLBACK_PATTERNS:
                                if pat in lower:
                                    matched_pattern = pat
                                    break
                            if matched_pattern:
                                break
                        if matched_pattern:
                            violations.append({
                                "file": rel_path,
                                "line": node.lineno,
                                "call": "generate_signed_callback",
                                "effect_type": matched_pattern,
                                "reason": (
                                    _i18n_t('services.effect_receipts.s10', matched_pattern=matched_pattern)
                                ),
                            })
    return violations
