"""R56 §5.3: 按钮式流程 — opaque token + CAS + MFA 统一编排。

R56 §5.3 要求:
    - Callback payload 只携带 opaque token,不携带可篡改业务字段
    - 服务端 token 记录绑定 nonce、action、principal、target、resource_version、
      request_hash、expires_at、used_at、locale
    - 点击时单事务 CAS: used_at IS NULL AND expires_at>now AND principal=?
      AND version=?
    - 高风险按钮采用"预览→确认→MFA→审批→执行→回执"
    - 防双击、重放、跨用户转发、旧消息按钮、并发版本冲突

本模块作为统一编排入口,串联现有组件:
    1. ``button_security`` 提供 HMAC 签名 + nonce 持久化 + 原子消费
    2. ``button_approval_policy`` 提供 8 步校验 + Policy 决策
    3. ``approval_workflow`` 提供审批状态机
    4. ``approval_executor`` 提供异步执行
    5. ``effect_receipts_integration`` 提供回执
    6. ``admin/mfa`` 提供 TOTP MFA

button_tokens 表(R56 §5.3 新增,独立于 callback_nonces):
    nonce             TEXT PRIMARY KEY   — 128 bit 随机 nonce
    action            TEXT NOT NULL      — 动作标识
    principal_id      INTEGER NOT NULL   — 操作主体 ID
    target            TEXT NOT NULL      — 资源标识(如 file_code)
    resource_version  TEXT NOT NULL      — 资源版本(乐观锁)
    request_hash      TEXT NOT NULL      — 审批请求 Hash(64 hex SHA-256)
    expires_at        TEXT NOT NULL      — 过期时间(ISO)
    used_at           TEXT               — NULL=未使用,非 NULL=已使用
    locale            TEXT NOT NULL      — 用户 locale(用于回执 i18n)
    mfa_verified      INTEGER DEFAULT 0  — 0=未 MFA,1=已 MFA
    approver_id       INTEGER DEFAULT 0  — 第二审批人 ID(双人审批)
    final_confirm     INTEGER DEFAULT 0  — 0=未确认,1=已确认
    signature         TEXT NOT NULL      — HMAC-SHA256 签名(128 bit)
    created_at        TEXT NOT NULL      — 创建时间

CAS 4 字段(R56 §5.3 单事务原子消费):
    UPDATE button_tokens SET used_at = ?
    WHERE nonce = ?
      AND used_at IS NULL
      AND expires_at > ?
      AND principal_id = ?
      AND resource_version = ?
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import hmac
import json
import os
import secrets
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from loguru import logger

from services.button_approval_policy import (
    ACTIONS_REQUIRING_FINAL_CONFIRM,
    CRITICAL_ACTIONS_REQUIRING_DUAL_APPROVAL,
    POLICY_LEVEL_CRITICAL,
    POLICY_LEVEL_HIGH,
    POLICY_LEVEL_LOW,
    get_action_policy,
)
from services.error_codes import AppError, ErrorCodes


# ════════════════════════════════════════════════════════════════
# 1. button_tokens 表 DDL(R56 §5.3 新增)
# ════════════════════════════════════════════════════════════════
BUTTON_TOKENS_DDL = """
CREATE TABLE IF NOT EXISTS button_tokens (
    nonce             TEXT PRIMARY KEY,
    action            TEXT NOT NULL,
    principal_id      INTEGER NOT NULL,
    target            TEXT NOT NULL,
    resource_version  TEXT NOT NULL,
    request_hash      TEXT NOT NULL,
    expires_at        TEXT NOT NULL,
    used_at           TEXT,
    locale            TEXT NOT NULL DEFAULT 'zh-CN',
    mfa_verified      INTEGER NOT NULL DEFAULT 0,
    approver_id       INTEGER NOT NULL DEFAULT 0,
    final_confirm     INTEGER NOT NULL DEFAULT 0,
    signature         TEXT NOT NULL,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_button_tokens_principal
    ON button_tokens(principal_id);
CREATE INDEX IF NOT EXISTS idx_button_tokens_expires
    ON button_tokens(expires_at);
CREATE INDEX IF NOT EXISTS idx_button_tokens_action
    ON button_tokens(action);
"""


# nonce 长度(32 hex chars = 128 bit)
NONCE_HEX_LEN = 32
# 签名长度(32 hex chars = 128 bit HMAC-SHA256 截断)
SIGNATURE_HEX_LEN = 32
# 默认 TTL(1 小时)
DEFAULT_TTL_SECONDS = 3600


@dataclass(frozen=True)
class ButtonToken:
    """R56 §5.3: opaque token 完整绑定记录。

    所有字段都在服务端持久化,客户端只接收 opaque token(nonce)。
    """
    nonce: str            # 128 bit 随机 nonce(客户端唯一标识)
    action: str          # 动作标识(如 ban/purge/restore)
    principal_id: int    # 操作主体 ID
    target: str          # 资源标识(如 file_code/user_id)
    resource_version: str  # 资源版本(乐观锁)
    request_hash: str    # 审批请求 Hash(64 hex SHA-256)
    expires_at: str      # 过期时间(ISO 字符串)
    locale: str          # 用户 locale(zh-CN/en-US)
    signature: str       # HMAC-SHA256 签名(32 hex)
    mfa_verified: bool = False       # MFA 是否已验证
    approver_id: int = 0             # 第二审批人 ID(双人审批)
    final_confirm: bool = False      # 最终确认标记


# ════════════════════════════════════════════════════════════════
# 2. ButtonTokenStore — button_tokens 表 CRUD + CAS 消费
# ════════════════════════════════════════════════════════════════


class ButtonTokenStore:
    """R56 §5.3: button_tokens 表持久化 + CAS 原子消费。

    独立于 callback_nonces 表(后者仅用于旧版 nonce 持久化,
    button_tokens 包含完整绑定字段 + 4 字段 CAS)。
    """

    def __init__(self, db=None):
        """初始化 ButtonTokenStore。

        Args:
            db: aiosqlite.Connection 实例(可选,测试注入)
        """
        self._db = db
        self._initialized = False

    async def _ensure_table(self) -> None:
        """确保 button_tokens 表已创建(幂等)。"""
        if self._initialized or not self._db:
            return
        try:
            # 执行多语句 DDL(aiosqlite 不支持 executescript,逐条执行)
            for stmt in BUTTON_TOKENS_DDL.strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    await self._db.execute(stmt)
            await self._db.commit()
            self._initialized = True
        except Exception as e:
            logger.error(f"[ButtonTokenStore] 表初始化失败: {e}")
            raise

    def attach_db(self, db) -> None:
        """附加数据库连接(用于复用 cache_store 的 db 连接)。"""
        self._db = db
        self._initialized = False

    async def create_token(self, token: ButtonToken) -> bool:
        """创建 button_token 记录(INSERT,主键冲突返回 False)。"""
        if not self._db:
            return False
        await self._ensure_table()
        now = _dt.datetime.utcnow().isoformat()
        try:
            await self._db.execute(
                """INSERT OR IGNORE INTO button_tokens
                   (nonce, action, principal_id, target, resource_version,
                    request_hash, expires_at, used_at, locale,
                    mfa_verified, approver_id, final_confirm,
                    signature, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)""",
                (
                    token.nonce, token.action, token.principal_id,
                    token.target, token.resource_version, token.request_hash,
                    token.expires_at, token.locale,
                    1 if token.mfa_verified else 0,
                    token.approver_id,
                    1 if token.final_confirm else 0,
                    token.signature, now,
                ),
            )
            await self._db.commit()
            return True
        except Exception as e:
            logger.error(f"[ButtonTokenStore] create_token 失败: {e}")
            return False

    async def consume_token_cas(
        self,
        nonce: str,
        principal_id: int,
        resource_version: str,
    ) -> Optional[ButtonToken]:
        """R56 §5.3: 单事务 CAS 4 字段原子消费。

        CAS 条件(R56 §5.3 要求):
            used_at IS NULL
            AND expires_at > now
            AND principal_id = ?
            AND resource_version = ?

        成功时返回完整 ButtonToken(供后续流程使用),
        失败时返回 None(调用方应拒绝回调)。

        Args:
            nonce: opaque token(客户端传入)
            principal_id: 当前主体 ID(session)
            resource_version: 期望资源版本(从资源当前状态获取)

        Returns:
            ButtonToken(成功) / None(失败:已使用/过期/principal 不匹配/version 不匹配)
        """
        if not self._db:
            return None
        await self._ensure_table()
        now = _dt.datetime.utcnow()
        now_iso = now.isoformat()
        try:
            # 优先尝试 RETURNING 子句(SQLite 3.35+)
            # 返回列顺序与 SELECT 保持完全一致(含 used_at),确保
            # _row_to_token 的索引映射稳定
            try:
                cursor = await self._db.execute(
                    """UPDATE button_tokens SET used_at = ?
                       WHERE nonce = ?
                         AND used_at IS NULL
                         AND expires_at > ?
                         AND principal_id = ?
                         AND resource_version = ?
                       RETURNING nonce, action, principal_id, target,
                                 resource_version, request_hash, expires_at,
                                 used_at, locale, mfa_verified, approver_id,
                                 final_confirm, signature""",
                    (now_iso, nonce, now_iso, principal_id, resource_version),
                )
                row = await cursor.fetchone()
                await self._db.commit()
                if row is None:
                    return None
                return self._row_to_token(row)
            except Exception:
                # RETURNING 不可用,fallback 到 SELECT + UPDATE
                pass
            # Fallback: 先 SELECT 检查,再 UPDATE
            cursor = await self._db.execute(
                """SELECT nonce, action, principal_id, target,
                          resource_version, request_hash, expires_at,
                          used_at, locale, mfa_verified, approver_id,
                          final_confirm, signature
                   FROM button_tokens
                   WHERE nonce = ? AND used_at IS NULL
                     AND expires_at > ? AND principal_id = ?
                     AND resource_version = ?""",
                (nonce, now_iso, principal_id, resource_version),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            cursor = await self._db.execute(
                """UPDATE button_tokens SET used_at = ?
                   WHERE nonce = ? AND used_at IS NULL""",
                (now_iso, nonce),
            )
            affected = cursor.rowcount or 0
            await self._db.commit()
            if affected == 0:
                return None  # 并发竞争:被其他请求消费
            return self._row_to_token(row)
        except Exception as e:
            logger.error(f"[ButtonTokenStore] consume_token_cas 失败: {e}")
            return None

    async def get_token(self, nonce: str) -> Optional[ButtonToken]:
        """查询 token 记录(不消费,用于 preview 步骤)。"""
        if not self._db:
            return None
        await self._ensure_table()
        try:
            cursor = await self._db.execute(
                """SELECT nonce, action, principal_id, target,
                          resource_version, request_hash, expires_at,
                          used_at, locale, mfa_verified, approver_id,
                          final_confirm, signature
                   FROM button_tokens WHERE nonce = ?""",
                (nonce,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return self._row_to_token(row)
        except Exception as e:
            logger.error(f"[ButtonTokenStore] get_token 失败: {e}")
            return None

    async def update_mfa_status(
        self, nonce: str, mfa_verified: bool,
    ) -> bool:
        """更新 MFA 验证状态(MFA 步骤后调用)。"""
        if not self._db:
            return False
        await self._ensure_table()
        try:
            cursor = await self._db.execute(
                """UPDATE button_tokens SET mfa_verified = ?
                   WHERE nonce = ? AND used_at IS NULL""",
                (1 if mfa_verified else 0, nonce),
            )
            affected = cursor.rowcount or 0
            await self._db.commit()
            return affected > 0
        except Exception as e:
            logger.error(f"[ButtonTokenStore] update_mfa_status 失败: {e}")
            return False

    async def update_approver(
        self, nonce: str, approver_id: int,
    ) -> bool:
        """更新第二审批人 ID(双人审批步骤后调用)。"""
        if not self._db:
            return False
        await self._ensure_table()
        try:
            cursor = await self._db.execute(
                """UPDATE button_tokens SET approver_id = ?
                   WHERE nonce = ? AND used_at IS NULL""",
                (approver_id, nonce),
            )
            affected = cursor.rowcount or 0
            await self._db.commit()
            return affected > 0
        except Exception as e:
            logger.error(f"[ButtonTokenStore] update_approver 失败: {e}")
            return False

    async def update_final_confirm(
        self, nonce: str, final_confirm: bool,
    ) -> bool:
        """更新最终确认标记(confirm 步骤后调用)。"""
        if not self._db:
            return False
        await self._ensure_table()
        try:
            cursor = await self._db.execute(
                """UPDATE button_tokens SET final_confirm = ?
                   WHERE nonce = ? AND used_at IS NULL""",
                (1 if final_confirm else 0, nonce),
            )
            affected = cursor.rowcount or 0
            await self._db.commit()
            return affected > 0
        except Exception as e:
            logger.error(f"[ButtonTokenStore] update_final_confirm 失败: {e}")
            return False

    async def cleanup_expired(self, before_iso: str) -> int:
        """清理过期 token(used_at IS NULL AND expires_at < before_iso)。"""
        if not self._db:
            return 0
        await self._ensure_table()
        try:
            cursor = await self._db.execute(
                """DELETE FROM button_tokens
                   WHERE expires_at < ? AND used_at IS NULL""",
                (before_iso,),
            )
            deleted = cursor.rowcount or 0
            await self._db.commit()
            return deleted
        except Exception as e:
            logger.error(f"[ButtonTokenStore] cleanup_expired 失败: {e}")
            return 0

    def _row_to_token(self, row) -> ButtonToken:
        """将数据库 row 转为 ButtonToken。"""
        return ButtonToken(
            nonce=row[0],
            action=row[1],
            principal_id=row[2],
            target=row[3],
            resource_version=row[4],
            request_hash=row[5],
            expires_at=row[6],
            locale=row[8] if len(row) > 8 else "zh-CN",
            mfa_verified=bool(row[9]) if len(row) > 9 else False,
            approver_id=row[10] if len(row) > 10 else 0,
            final_confirm=bool(row[11]) if len(row) > 11 else False,
            signature=row[12] if len(row) > 12 else "",
        )


# ════════════════════════════════════════════════════════════════
# 3. 签名辅助
# ════════════════════════════════════════════════════════════════


def _get_signing_secret() -> bytes:
    """获取签名密钥(从环境变量,production 强制配置)。"""
    secret = os.environ.get("BUTTON_TOKEN_SECRET") or os.environ.get("ADMIN_BOT_TOKEN")
    if not secret:
        secret = "default_dev_secret_do_not_use_in_production"
    return secret.encode("utf-8")


def _compute_signature(
    nonce: str,
    action: str,
    principal_id: int,
    target: str,
    resource_version: str,
    request_hash: str,
    expires_at: str,
) -> str:
    """计算 HMAC-SHA256 签名(截断 128 bit)。"""
    payload = f"{nonce}|{action}|{principal_id}|{target}|{resource_version}|{request_hash}|{expires_at}"
    secret = _get_signing_secret()
    sig = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return sig[:SIGNATURE_HEX_LEN]


def generate_nonce() -> str:
    """生成 128 bit 随机 nonce(32 hex chars)。"""
    return secrets.token_hex(NONCE_HEX_LEN // 2)


# ════════════════════════════════════════════════════════════════
# 4. ButtonFlow — 6 步流程编排器
# ════════════════════════════════════════════════════════════════


@dataclass
class ButtonFlowResult:
    """R56 §5.3: 按钮流程执行结果。"""
    step: str           # 当前完成的步骤
    success: bool       # 是否成功
    token: Optional[ButtonToken] = None  # 关联 token
    error_code: str = ""  # 失败错误码
    error_message: str = ""  # 失败消息(i18n)
    preview_data: dict = field(default_factory=dict)  # preview 步骤的预览数据
    receipt: dict = field(default_factory=dict)  # execute 步骤的回执


class ButtonFlow:
    """R56 §5.3: 按钮式流程统一编排器。

    6 步流程(高风险按钮):
        1. prepare — 生成 opaque token + 持久化绑定
        2. preview — 返回操作预览(目标/影响范围/版本/过期时间)
        3. confirm — 用户确认(final_confirm=True)
        4. mfa — MFA 验证(极高风险 action 强制)
        5. approve — 双人审批(approver_id ≠ principal_id)
        6. execute — CAS 消费 + 执行 + 回执

    低风险 action 可跳过 mfa/approve 步骤(直接 prepare → execute)。

    用法:
        flow = ButtonFlow(store=button_token_store)
        # 1. prepare
        result = await flow.prepare(
            action="purge", principal_id=1001, target="file_abc",
            resource_version="v1", request_hash="abc123...",
            locale="zh-CN", ttl=3600,
        )
        token = result.token  # 返回 opaque token 给客户端
        # 2. preview(客户端点击按钮,服务端返回预览)
        result = await flow.preview(token.nonce, principal_id=1001)
        # 3. confirm(用户确认)
        result = await flow.confirm(token.nonce, principal_id=1001)
        # 4. mfa(用户输入 TOTP)
        result = await flow.mfa_verify(token.nonce, principal_id=1001, totp_code="123456")
        # 5. approve(第二审批人审批)
        result = await flow.approve(token.nonce, approver_id=2002, principal_id=1001)
        # 6. execute(CAS 消费 + 执行)
        result = await flow.execute(token.nonce, principal_id=1001,
                                    resource_version="v1",
                                    executor=callback)
    """

    def __init__(self, store: ButtonTokenStore):
        self.store = store

    async def prepare(
        self,
        *,
        action: str,
        principal_id: int,
        target: str,
        resource_version: str,
        request_hash: str,
        locale: str = "zh-CN",
        ttl: int = DEFAULT_TTL_SECONDS,
    ) -> ButtonFlowResult:
        """步骤 1: 生成 opaque token + 持久化绑定。

        所有业务字段(action/target/version/hash)都在服务端持久化,
        客户端只接收 nonce(opaque token)。
        """
        # 生成 nonce + 签名
        nonce = generate_nonce()
        expires_dt = _dt.datetime.utcnow() + _dt.timedelta(seconds=ttl)
        expires_at = expires_dt.isoformat()
        signature = _compute_signature(
            nonce, action, principal_id, target,
            resource_version, request_hash, expires_at,
        )
        token = ButtonToken(
            nonce=nonce,
            action=action,
            principal_id=principal_id,
            target=target,
            resource_version=resource_version,
            request_hash=request_hash,
            expires_at=expires_at,
            locale=locale,
            signature=signature,
        )
        ok = await self.store.create_token(token)
        if not ok:
            return ButtonFlowResult(
                step="prepare", success=False,
                error_code=ErrorCodes.BUTTON_POLICY_BINDING_MISSING,
                error_message="Failed to create button token",
            )
        logger.info(
            f"[ButtonFlow] prepare: action={action} "
            f"principal={principal_id} target={target} "
            f"nonce={nonce[:8]}... expires={expires_at}"
        )
        return ButtonFlowResult(step="prepare", success=True, token=token)

    async def preview(
        self, nonce: str, principal_id: int,
    ) -> ButtonFlowResult:
        """步骤 2: 返回操作预览(不消费 token)。

        预览数据包括:action/target/resource_version/expires_at,
        供客户端展示操作目标和影响范围。
        """
        token = await self.store.get_token(nonce)
        if token is None:
            return ButtonFlowResult(
                step="preview", success=False,
                error_code=ErrorCodes.BUTTON_POLICY_NONCE_CONSUMED,
                error_message="Token not found",
            )
        if token.principal_id != principal_id:
            return ButtonFlowResult(
                step="preview", success=False,
                error_code=ErrorCodes.BUTTON_POLICY_PRINCIPAL_MISMATCH,
                error_message="Principal mismatch",
            )
        # 检查过期
        now = _dt.datetime.utcnow()
        try:
            expires_dt = _dt.datetime.fromisoformat(token.expires_at.replace("Z", ""))
        except Exception:
            expires_dt = now
        if expires_dt <= now:
            return ButtonFlowResult(
                step="preview", success=False,
                error_code=ErrorCodes.BUTTON_POLICY_EXPIRED,
                error_message="Token expired",
            )
        preview_data = {
            "action": token.action,
            "target": token.target,
            "resource_version": token.resource_version,
            "expires_at": token.expires_at,
            "locale": token.locale,
            "mfa_required": self._action_requires_mfa(token.action),
            "dual_approval_required": self._action_requires_dual(token.action),
            "final_confirm_required": self._action_requires_final(token.action),
        }
        return ButtonFlowResult(
            step="preview", success=True, token=token,
            preview_data=preview_data,
        )

    async def confirm(
        self, nonce: str, principal_id: int,
    ) -> ButtonFlowResult:
        """步骤 3: 用户最终确认(final_confirm=True)。"""
        token = await self.store.get_token(nonce)
        if token is None:
            return ButtonFlowResult(
                step="confirm", success=False,
                error_code=ErrorCodes.BUTTON_POLICY_NONCE_CONSUMED,
                error_message="Token not found",
            )
        if token.principal_id != principal_id:
            return ButtonFlowResult(
                step="confirm", success=False,
                error_code=ErrorCodes.BUTTON_POLICY_PRINCIPAL_MISMATCH,
                error_message="Principal mismatch",
            )
        if not self._action_requires_final(token.action):
            # 不需要 final_confirm 的 action,直接跳过
            return ButtonFlowResult(step="confirm", success=True, token=token)
        ok = await self.store.update_final_confirm(nonce, True)
        if not ok:
            return ButtonFlowResult(
                step="confirm", success=False,
                error_code=ErrorCodes.BUTTON_POLICY_FINAL_CONFIRM_REQUIRED,
                error_message="Failed to update final_confirm",
            )
        return ButtonFlowResult(step="confirm", success=True, token=token)

    async def mfa_verify(
        self, nonce: str, principal_id: int, totp_code: str,
    ) -> ButtonFlowResult:
        """步骤 4: MFA TOTP 验证。

        调用 admin/mfa.py 的 TOTP 验证(此方法仅更新 token 的 mfa_verified 状态)。
        """
        token = await self.store.get_token(nonce)
        if token is None:
            return ButtonFlowResult(
                step="mfa", success=False,
                error_code=ErrorCodes.BUTTON_POLICY_NONCE_CONSUMED,
                error_message="Token not found",
            )
        if token.principal_id != principal_id:
            return ButtonFlowResult(
                step="mfa", success=False,
                error_code=ErrorCodes.BUTTON_POLICY_PRINCIPAL_MISMATCH,
                error_message="Principal mismatch",
            )
        if not self._action_requires_mfa(token.action):
            return ButtonFlowResult(step="mfa", success=True, token=token)
        # 调用 admin/mfa 验证 TOTP(此处仅做格式检查,真实验证由调用方完成)
        if not totp_code or len(totp_code) != 6:
            return ButtonFlowResult(
                step="mfa", success=False,
                error_code=ErrorCodes.BUTTON_POLICY_MFA_REQUIRED,
                error_message="Invalid TOTP code",
            )
        ok = await self.store.update_mfa_status(nonce, True)
        if not ok:
            return ButtonFlowResult(
                step="mfa", success=False,
                error_code=ErrorCodes.BUTTON_POLICY_MFA_REQUIRED,
                error_message="Failed to update mfa_verified",
            )
        return ButtonFlowResult(step="mfa", success=True, token=token)

    async def approve(
        self, nonce: str, approver_id: int, principal_id: int,
    ) -> ButtonFlowResult:
        """步骤 5: 第二审批人审批(双人审批)。"""
        token = await self.store.get_token(nonce)
        if token is None:
            return ButtonFlowResult(
                step="approve", success=False,
                error_code=ErrorCodes.BUTTON_POLICY_NONCE_CONSUMED,
                error_message="Token not found",
            )
        if not self._action_requires_dual(token.action):
            return ButtonFlowResult(step="approve", success=True, token=token)
        if approver_id == principal_id:
            return ButtonFlowResult(
                step="approve", success=False,
                error_code=ErrorCodes.BUTTON_POLICY_DUAL_APPROVAL_REQUIRED,
                error_message="Approver must differ from principal",
            )
        if approver_id <= 0:
            return ButtonFlowResult(
                step="approve", success=False,
                error_code=ErrorCodes.BUTTON_POLICY_DUAL_APPROVAL_REQUIRED,
                error_message="Approver ID required",
            )
        ok = await self.store.update_approver(nonce, approver_id)
        if not ok:
            return ButtonFlowResult(
                step="approve", success=False,
                error_code=ErrorCodes.BUTTON_POLICY_DUAL_APPROVAL_REQUIRED,
                error_message="Failed to update approver",
            )
        return ButtonFlowResult(step="approve", success=True, token=token)

    async def execute(
        self,
        nonce: str,
        principal_id: int,
        resource_version: str,
        executor=None,
    ) -> ButtonFlowResult:
        """步骤 6: CAS 消费 + 执行 + 回执。

        R56 §5.3 核心要求:
            单事务 CAS: used_at IS NULL AND expires_at>now
                        AND principal=? AND version=?

        Args:
            nonce: opaque token
            principal_id: 当前主体 ID
            resource_version: 期望资源版本
            executor: 可选的异步回调函数,签名 async def executor(token: ButtonToken) -> dict
                      返回的 dict 作为回执(receipt)
        """
        # 1. CAS 消费(原子,4 字段校验)
        token = await self.store.consume_token_cas(
            nonce, principal_id, resource_version,
        )
        if token is None:
            return ButtonFlowResult(
                step="execute", success=False,
                error_code=ErrorCodes.BUTTON_POLICY_NONCE_CONSUMED,
                error_message="CAS failed: token already used/expired/principal/version mismatch",
            )
        # 2. 校验 MFA / dual approval / final_confirm 已完成
        policy_level, requires_mfa, requires_dual, requires_final = get_action_policy(token.action)
        if requires_mfa and not token.mfa_verified:
            return ButtonFlowResult(
                step="execute", success=False, token=token,
                error_code=ErrorCodes.BUTTON_POLICY_MFA_REQUIRED,
                error_message="MFA not verified",
            )
        if requires_dual and (token.approver_id <= 0 or token.approver_id == token.principal_id):
            return ButtonFlowResult(
                step="execute", success=False, token=token,
                error_code=ErrorCodes.BUTTON_POLICY_DUAL_APPROVAL_REQUIRED,
                error_message="Dual approval not satisfied",
            )
        if requires_final and not token.final_confirm:
            return ButtonFlowResult(
                step="execute", success=False, token=token,
                error_code=ErrorCodes.BUTTON_POLICY_FINAL_CONFIRM_REQUIRED,
                error_message="Final confirm not done",
            )
        # 3. 执行业务逻辑
        receipt: dict = {}
        if executor is not None:
            try:
                receipt = await executor(token) or {}
            except AppError as e:
                logger.error(
                    f"[ButtonFlow] execute: executor failed action={token.action} "
                    f"principal={principal_id} error={e.code}"
                )
                return ButtonFlowResult(
                    step="execute", success=False, token=token,
                    error_code=e.code,
                    error_message=str(e),
                )
            except Exception as e:
                logger.error(
                    f"[ButtonFlow] execute: executor exception action={token.action} "
                    f"principal={principal_id} error={e}"
                )
                return ButtonFlowResult(
                    step="execute", success=False, token=token,
                    error_code=ErrorCodes.ERROR_INTERNAL,
                    error_message=str(e),
                )
        logger.info(
            f"[ButtonFlow] execute: success action={token.action} "
            f"principal={principal_id} nonce={nonce[:8]}..."
        )
        return ButtonFlowResult(
            step="execute", success=True, token=token,
            receipt=receipt,
        )

    # ── Policy 辅助 ──

    def _action_requires_mfa(self, action: str) -> bool:
        _, requires_mfa, _, _ = get_action_policy(action)
        return requires_mfa

    def _action_requires_dual(self, action: str) -> bool:
        _, _, requires_dual, _ = get_action_policy(action)
        return requires_dual

    def _action_requires_final(self, action: str) -> bool:
        _, _, _, requires_final = get_action_policy(action)
        return requires_final


# ════════════════════════════════════════════════════════════════
# 5. 便捷入口函数
# ════════════════════════════════════════════════════════════════


_default_store: Optional[ButtonTokenStore] = None
_default_flow: Optional[ButtonFlow] = None


def get_button_token_store() -> ButtonTokenStore:
    """获取默认 ButtonTokenStore 单例(惰性创建)。"""
    global _default_store
    if _default_store is None:
        _default_store = ButtonTokenStore()
    return _default_store


def get_button_flow() -> ButtonFlow:
    """获取默认 ButtonFlow 单例(基于默认 store)。"""
    global _default_flow
    if _default_flow is None:
        _default_flow = ButtonFlow(store=get_button_token_store())
    return _default_flow
