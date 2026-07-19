"""R65 P0-03: 不可伪造的恢复审批/MFA capability + UoW CAS 消费。

审计背景(R65 终审报告 P0-03):
    旧 ``execute_blue_green_switch`` 仅比较 ``approval_id == operation.approval_id``
    与 ``mfa_receipt_id == operation.mfa_receipt_id`` — 这两个值都是调用方传入的
    不透明字符串,可被任意伪造。攻击者只需在 request_approval 阶段塞入任意 ID,
    再在 execute_blue_green_switch 阶段传入相同 ID 即可绕过审批/MFA。

整改方案(R65 P0-03):
    1. ``ApprovalAuthority.verify_and_consume`` + ``MFAAuthority.verify_and_consume``
       返回不可伪造的 capability(由权威层校验 + CAS 消费后才构造)
    2. 调用方在同一 ``UnitOfWork`` 中依次调用两个 authority 的 verify_and_consume,
       使 approval/MFA 的 CAS 消费与 operation phase / nonce CAS 消费原子提交/回滚
    3. 任一失败 → UoW 回滚 → approval/MFA 未消费(防重放,可安全重试)

设计原则:
    - fail-closed:任一校验失败立即 raise AppError,不返回部分结果
    - CAS 消费:UPDATE/INSERT OR IGNORE + rowcount==1 检查,防并发重放
    - UoW-aware:CAS 在调用方传入的 uow 内执行,不独立 commit
    - capability 不可变:frozen dataclass,构造后不可篡改
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any

from loguru import logger

from services.error_codes import AppError, ErrorCodes
from services.i18n import translate as _i18n_t


# ════════════════════════════════════════════════════════════════
# 不可伪造的 capability 数据类
# ════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class ApprovalCapability:
    """不可伪造的审批 capability — 由 ApprovalAuthority.verify_and_consume() 返回。

    调用方获得此对象即证明:审批行存在、decision=approved、未吊销、未消费、
    未过期、action_hash 匹配、approver != requester,且在本 UoW 中 CAS 消费成功。

    Attributes:
        approval_id: command_approvals.id (str)
        approver_id: 审批人 principal_id
        action_hash: 审批绑定的 action hash(= command_approvals.request_hash,
            must match restore payload digest / manifest_digest)
        request_hash: 同 action_hash(command_approvals.request_hash 列)
        approved_at: ISO8601 审批时间
        expires_at: ISO8601 过期时间
        consumed_at: ISO8601 消费时间(本 UoW 内 CAS 消费成功)
    """

    approval_id: str
    approver_id: int
    action_hash: str
    request_hash: str
    approved_at: str
    expires_at: str
    consumed_at: str


@dataclass(frozen=True)
class MFACapability:
    """不可伪造的 MFA capability — 由 MFAAuthority.verify_and_consume() 返回。

    调用方获得此对象即证明:MFA receipt 签名有效、sub/purpose/action_hash 匹配、
    未吊销、未过期、age 在 max_age 内,且在本 UoW 中 CAS 消费成功(jti 一次性)。

    Attributes:
        jti: MFA receipt 唯一 ID
        principal_id: 持有人 principal_id(must == operation.created_by)
        purpose: MFA 用途(must == 'restore')
        action_hash: MFA 绑定的 action hash(must match restore payload digest)
        amr: 认证方法引用列表(e.g. ('totp',))
        iat: 签发时间(unix 秒,字符串化)
        exp: 过期时间(unix 秒,字符串化)
        consumed_at: 消费时间(本 UoW 内 CAS 消费成功)
    """

    jti: str
    principal_id: int
    purpose: str
    action_hash: str
    amr: tuple[str, ...]
    iat: str
    exp: str
    consumed_at: str


# ════════════════════════════════════════════════════════════════
# ApprovalAuthority — 审批权威
# ════════════════════════════════════════════════════════════════


class ApprovalAuthority:
    """R65 P0-03: 审批权威 — 校验审批状态 + 在 UoW 内 CAS 消费。

    所有校验 fail-closed(AppError);CAS UPDATE rowcount!=1 即失败。
    """

    def __init__(self, store: Any = None):
        """初始化 ApprovalAuthority。

        Args:
            store: CacheStore 实例(保留用于未来扩展,当前 CAS 通过 uow 执行)
        """
        self._store = store

    async def verify_and_consume(
        self,
        approval_id: str,
        *,
        expected_action_hash: str,
        expected_requester: str,
        uow: Any,
    ) -> ApprovalCapability:
        """校验审批并 CAS 消费(在 uow 内,不独立 commit)。

        校验项(任一失败即 AppError fail-closed):
            1. approval_id 非空
            2. command_approvals 行存在
            3. decision == 'approved'
            4. revoked_at IS NULL(未吊销)
            5. consumed_at IS NULL(未消费 — 防重放)
            6. expires_at > now(未过期)
            7. request_hash == expected_action_hash(绑定 restore payload)
            8. approver_id != int(expected_requester)(双人审批:requester != approver)

        CAS 消费:UPDATE command_approvals SET consumed_at=?
                   WHERE id=? AND consumed_at IS NULL AND revoked_at IS NULL
                   rowcount==1 才成功(防并发重放)

        Args:
            approval_id: command_approvals.id(字符串形式)
            expected_action_hash: payload/manifest digest(SHA-256 hex,64 位小写十六进制)
            expected_requester: operation.created_by(principal_id 字符串形式)
            uow: UnitOfWork — CAS 在 uow 内执行,与其他消费原子化

        Returns:
            ApprovalCapability(不可伪造)

        Raises:
            AppError(RESTORE_APPROVAL_REQUIRED): 任一校验或 CAS 消费失败
        """
        # 1. 非空校验
        if not approval_id:
            raise AppError(
                ErrorCodes.RESTORE_APPROVAL_REQUIRED,
                params={"reason": "approval_id_empty"},
            )
        # 转换为 int(command_approvals.id 是 INTEGER PRIMARY KEY)
        try:
            approval_id_int = int(approval_id)
        except (ValueError, TypeError):
            raise AppError(
                ErrorCodes.RESTORE_APPROVAL_REQUIRED,
                params={
                    "approval_id": approval_id,
                    "reason": "approval_id_not_integer",
                },
            )

        # 2-8. 查询审批行 + 多项校验
        row = await uow.fetchone(
            """SELECT id, approver_id, action_id, decision, request_hash,
                      mfa_receipt, approved_at, expires_at, consumed_at, revoked_at
               FROM command_approvals WHERE id = ?""",
            (approval_id_int,),
        )
        if row is None:
            raise AppError(
                ErrorCodes.RESTORE_APPROVAL_REQUIRED,
                params={
                    "approval_id": approval_id,
                    "reason": "approval_not_found",
                },
            )
        (aid, approver_id, action_id, decision, request_hash,
         mfa_receipt, approved_at, expires_at, consumed_at, revoked_at) = row

        # 3. decision == 'approved'
        if decision != "approved":
            raise AppError(
                ErrorCodes.RESTORE_APPROVAL_REQUIRED,
                params={
                    "approval_id": approval_id,
                    "reason": f"decision_not_approved:{decision}",
                },
            )
        # 4. revoked_at IS NULL
        if revoked_at is not None:
            raise AppError(
                ErrorCodes.RESTORE_APPROVAL_REQUIRED,
                params={
                    "approval_id": approval_id,
                    "reason": "approval_revoked",
                },
            )
        # 5. consumed_at IS NULL(防重放)
        if consumed_at is not None:
            raise AppError(
                ErrorCodes.RESTORE_APPROVAL_REQUIRED,
                params={
                    "approval_id": approval_id,
                    "reason": "approval_already_consumed",
                },
            )
        # 6. expires_at > now(ISO8601 字符串比较;两者均为 ISO8601 UTC)
        now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
        if not expires_at or expires_at <= now_iso:
            raise AppError(
                ErrorCodes.RESTORE_APPROVAL_REQUIRED,
                params={
                    "approval_id": approval_id,
                    "reason": "approval_expired",
                },
            )
        # 7. request_hash == expected_action_hash(绑定 restore payload)
        #    command_approvals.request_hash 列存储 64-hex SHA-256,
        #    expected_action_hash = operation.manifest_digest
        if request_hash != expected_action_hash:
            raise AppError(
                ErrorCodes.RESTORE_APPROVAL_REQUIRED,
                params={
                    "approval_id": approval_id,
                    "reason": "action_hash_mismatch",
                },
            )
        # 8. 双人审批:approver_id != requester
        try:
            requester_int = int(expected_requester)
        except (ValueError, TypeError):
            # requester 非数字 → 视为不匹配(无法证明双人审批),fail-closed
            raise AppError(
                ErrorCodes.RESTORE_APPROVAL_REQUIRED,
                params={
                    "approval_id": approval_id,
                    "reason": "requester_not_numeric",
                },
            )
        if approver_id == requester_int:
            raise AppError(
                ErrorCodes.RESTORE_APPROVAL_REQUIRED,
                params={
                    "approval_id": approval_id,
                    "reason": "approver_equals_requester",
                },
            )

        # CAS 消费:UPDATE ... SET consumed_at=? WHERE id=? AND consumed_at IS NULL
        #           AND revoked_at IS NULL
        # rowcount==1 → 首次消费成功;rowcount=0 → 已被并发消费或吊销(fail-closed)
        cursor = await uow.execute(
            """UPDATE command_approvals SET consumed_at = ?
               WHERE id = ? AND consumed_at IS NULL AND revoked_at IS NULL""",
            (now_iso, aid),
        )
        rowcount = getattr(cursor, "rowcount", -1)
        if rowcount != 1:
            raise AppError(
                ErrorCodes.RESTORE_APPROVAL_REQUIRED,
                params={
                    "approval_id": approval_id,
                    "reason": f"cas_consume_failed:rowcount={rowcount}",
                },
            )
        logger.info(
            _i18n_t(
                "diagnostics.r65.p0_03.approval_cas_consumed",
                aid=aid,
                approver_id=approver_id,
                hash=request_hash[:8],
            )
        )
        return ApprovalCapability(
            approval_id=str(aid),
            approver_id=int(approver_id),
            action_hash=request_hash,
            request_hash=request_hash,
            approved_at=approved_at,
            expires_at=expires_at,
            consumed_at=now_iso,
        )


# ════════════════════════════════════════════════════════════════
# MFAAuthority — MFA 权威
# ════════════════════════════════════════════════════════════════


class MFAAuthority:
    """R65 P0-03: MFA 权威 — 校验 MFA receipt + 在 UoW 内 CAS 消费。

    所有校验 fail-closed(AppError);CAS INSERT OR IGNORE rowcount!=1 即失败。
    """

    def __init__(self, store: Any = None):
        """初始化 MFAAuthority。

        Args:
            store: CacheStore 实例(保留用于未来扩展,当前 CAS 通过 uow 执行)
        """
        self._store = store

    async def verify_and_consume(
        self,
        mfa_receipt_token: str,
        *,
        expected_principal_id: int,
        expected_purpose: str,
        expected_action_hash: str,
        uow: Any,
    ) -> MFACapability:
        """校验 MFA receipt 并 CAS 消费(在 uow 内,不独立 commit)。

        校验项(任一失败即 AppError fail-closed):
            1. token 非空 + 格式 'mfa1.<payload_b64>.<sig_b64>'
            2. HMAC-SHA256 签名有效(使用 MFA_RECEIPT_SIGNING_KEY keyring)
            3. jti 格式(32 hex 或 36 dashed UUID)
            4. iat skew ±60s + age <= max_age_seconds(默认 300s)
            5. exp 未过期
            6. sub == expected_principal_id
            7. purpose == expected_purpose
            8. action_hash == expected_action_hash
            9. jti 未被吊销(query mfa_receipt_revocations,跨进程权威)

        CAS 消费:INSERT OR IGNORE INTO mfa_receipts (jti,...) VALUES (...)
                   rowcount==1 才成功(防止重放;jti PRIMARY KEY 唯一约束)

        Args:
            mfa_receipt_token: 'mfa1.<payload_b64>.<sig_b64>' 格式
            expected_principal_id: 持有人 principal_id(必须 == operation.created_by)
            expected_purpose: 用途(restore 场景为 'restore')
            expected_action_hash: action hash(= payload_digest / manifest_digest)
            uow: UnitOfWork — CAS 在 uow 内执行,与 approval 消费原子化

        Returns:
            MFACapability(不可伪造)

        Raises:
            AppError(RESTORE_MFA_REQUIRED): 任一校验或 CAS 消费失败
        """
        if not mfa_receipt_token:
            raise AppError(
                ErrorCodes.RESTORE_MFA_REQUIRED,
                params={"reason": "mfa_token_empty"},
            )

        # 步骤 1-9: 使用现有 verify_mfa_receipt_authoritative(consume=False) 完成全部校验
        # consume=False 跳过内部 consume_mfa_receipt(jti) 的独立 commit,
        # 改由本函数在 uow 内 CAS 消费(与 approval CAS 原子化)
        # 延迟导入避免循环依赖
        from admin.mfa import verify_mfa_receipt_authoritative

        try:
            payload = await verify_mfa_receipt_authoritative(
                mfa_receipt_token,
                expected_principal_id=expected_principal_id,
                expected_purpose=expected_purpose,
                expected_action_hash=expected_action_hash,
                consume=False,  # 延迟到 uow 内消费
            )
        except AppError as e:
            # verify_mfa_receipt_authoritative 抛出的 AUTH_MFA_RECEIPT_INVALID /
            # AUTH_MFA_RECEIPT_EXPIRED 统一映射为 RESTORE_MFA_REQUIRED,
            # 使 restore 上下文的错误码一致(fail-closed)
            raise AppError(
                ErrorCodes.RESTORE_MFA_REQUIRED,
                params={
                    "reason": f"mfa_verify_failed:{e.code}",
                    "detail": str(e),
                },
            ) from e

        jti = payload.get("jti", "")
        if not jti:
            # 理论不可达(verify_mfa_receipt_authoritative 已校验 jti),fail-closed
            raise AppError(
                ErrorCodes.RESTORE_MFA_REQUIRED,
                params={"reason": "jti_missing_after_verify"},
            )

        # 步骤 9(冗余 defense-in-depth):再次检查吊销
        # verify_mfa_receipt_authoritative 内部已查 SQLite 权威层,但本检查
        # 在 CAS 消费前再确认一次,缩小 TOCTOU 窗口
        from admin.mfa import get_mfa_manager
        manager = get_mfa_manager()
        if await manager.is_mfa_receipt_revoked(jti):
            raise AppError(
                ErrorCodes.RESTORE_MFA_REQUIRED,
                params={"jti": jti, "reason": "mfa_receipt_revoked"},
            )

        # CAS 消费:INSERT OR IGNORE INTO mfa_receipts (jti,...) VALUES (...)
        # jti PRIMARY KEY 唯一约束 + INSERT OR IGNORE:
        #   rowcount=1 → 首次消费成功
        #   rowcount=0 → jti 已存在(重放/已消费)→ fail-closed
        consumed_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
        # 幂等建表(与 admin/mfa.py:1126 一致,IF NOT EXISTS 安全)
        # 防止 init 未运行时 consume 失败,便于隔离测试
        await uow.execute(
            """CREATE TABLE IF NOT EXISTS mfa_receipts (
                jti          TEXT PRIMARY KEY,
                sub          INTEGER,
                purpose      TEXT,
                action_hash  TEXT,
                amr          TEXT,
                iat          TEXT,
                exp          TEXT,
                used_at      TEXT,
                consumed_at  TEXT
            )"""
        )
        cursor = await uow.execute(
            """INSERT OR IGNORE INTO mfa_receipts
               (jti, sub, purpose, action_hash, amr, iat, exp, used_at, consumed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                jti,
                payload.get("sub"),
                payload.get("purpose"),
                payload.get("action_hash"),
                ",".join(payload.get("amr", []) or []),
                payload.get("iat"),
                payload.get("exp"),
                consumed_at,
                consumed_at,
            ),
        )
        rowcount = getattr(cursor, "rowcount", -1)
        if rowcount != 1:
            raise AppError(
                ErrorCodes.RESTORE_MFA_REQUIRED,
                params={
                    "jti": jti,
                    "reason": "mfa_receipt_already_consumed",
                },
            )
        logger.info(
            _i18n_t(
                "diagnostics.r65.p0_03.mfa_cas_consumed",
                jti=jti[:8],
                principal=expected_principal_id,
                purpose=expected_purpose,
            )
        )
        return MFACapability(
            jti=jti,
            principal_id=int(payload.get("sub", 0)),
            purpose=str(payload.get("purpose", "")),
            action_hash=str(payload.get("action_hash", "")),
            amr=tuple(payload.get("amr", []) or []),
            iat=str(payload.get("iat", "")),
            exp=str(payload.get("exp", "")),
            consumed_at=consumed_at,
        )
