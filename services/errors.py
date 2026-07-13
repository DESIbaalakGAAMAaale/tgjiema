"""R40 P2-6: 领域错误码 — 统一错误模型。

职责:
    为系统所有业务错误提供统一的错误码、错误信息和上下文细节:
    1. ErrorCode — 枚举所有领域错误码(便于前端按码处理)
    2. DomainError — 异常类(含 code/message/details/trace_id)
    3. to_dict() — 序列化为 API 响应(JSON 兼容)
    4. from_exception() — 从任意异常构造 DomainError(降级为 INTERNAL)

设计原则:
    - 错误码格式 <DOMAIN>.<OPERATION>.<REASON>(如 quota.decode.exceeded)
    - 每个错误码对应一个默认中英文消息(后续接入 i18n)
    - trace_id 贯穿日志/审计/响应,便于跨服务追踪
    - 不依赖外部库(纯 Python,兼容 3.9)
    - 中文注释,loguru 日志
"""
from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from loguru import logger


class ErrorSeverity(enum.Enum):
    """错误严重级别。"""
    INFO = "info"          # 信息性错误(如配额提示)
    WARNING = "warning"    # 警告(可恢复,需关注)
    ERROR = "error"        # 错误(业务失败)
    CRITICAL = "critical"  # 严重错误(系统级故障)


class ErrorCode(str, enum.Enum):
    """R40 P2-6: 领域错误码枚举。

    命名规则: <DOMAIN>.<OPERATION>.<REASON>
    使用 str 枚举便于 JSON 序列化和前端按字符串比较。
    """

    # ─── 配额相关 ───
    QUOTA_DECODE_EXCEEDED = "quota.decode.exceeded"
    QUOTA_UPLOAD_EXCEEDED = "quota.upload.exceeded"
    QUOTA_EXTERNAL_EXCEEDED = "quota.external.exceeded"
    QUOTA_INSUFFICIENT = "quota.balance.insufficient"

    # ─── 文件相关 ───
    FILE_NOT_FOUND = "file.not_found"
    FILE_EXPIRED = "file.expired"
    FILE_DELETED = "file.deleted"
    FILE_TOO_LARGE = "file.too_large"
    FILE_HASH_MISMATCH = "file.hash_mismatch"
    FILE_CODE_INVALID = "file.code.invalid"
    FILE_CODE_USED = "file.code.used"

    # ─── 用户相关 ───
    USER_NOT_FOUND = "user.not_found"
    USER_BANNED = "user.banned"
    USER_TEMP_BANNED = "user.temp_banned"
    USER_UNAUTHORIZED = "user.unauthorized"
    USER_FORBIDDEN = "user.forbidden"

    # ─── 审批相关 ───
    APPROVAL_NOT_FOUND = "approval.not_found"
    APPROVAL_REQUIRED = "approval.required"
    APPROVAL_REJECTED = "approval.rejected"
    APPROVAL_EXPIRED = "approval.expired"

    # ─── 系统相关 ───
    INTERNAL_ERROR = "system.internal"
    SERVICE_UNAVAILABLE = "system.unavailable"
    RATE_LIMITED = "system.rate_limited"
    MAINTENANCE_MODE = "system.maintenance"

    # ─── 存储相关 ───
    STORAGE_R2_FAILED = "storage.r2.failed"
    STORAGE_CRDB_FAILED = "storage.crdb.failed"
    STORAGE_REDIS_FAILED = "storage.redis.failed"
    STORAGE_QUOTA_EXCEEDED = "storage.quota_exceeded"

    # ─── 复制/同步相关 ───
    REPLICATION_FAILED = "replication.failed"
    REPLICATION_TIMEOUT = "replication.timeout"
    SYNC_DIRTY_FAILED = "sync.dirty.failed"

    # ─── Relay 账号相关 ───
    RELAY_FLOOD_WAIT = "relay.flood_wait"
    RELAY_BANNED = "relay.banned"
    RELAY_RESTRICTED = "relay.restricted"
    RELAY_NO_AVAILABLE = "relay.no_available"

    # ─── 解码相关 ───
    DECODE_FAILED = "decode.failed"
    DECODE_TIMEOUT = "decode.timeout"
    DECODE_INVALID_INPUT = "decode.invalid_input"

    # ─── 参数校验相关 ───
    VALIDATION_FAILED = "validation.failed"
    VALIDATION_MISSING_FIELD = "validation.missing_field"
    VALIDATION_INVALID_FORMAT = "validation.invalid_format"


# ─── 默认消息(中英文) ─────────────────────────────────────────────
# 后续接入 i18n 后改为从 locale 文件加载
_DEFAULT_MESSAGES: dict[ErrorCode, dict[str, str]] = {
    ErrorCode.QUOTA_DECODE_EXCEEDED: {
        "zh-CN": "今日解码次数已达上限",
        "en-US": "Daily decode quota exceeded",
    },
    ErrorCode.QUOTA_UPLOAD_EXCEEDED: {
        "zh-CN": "今日上传次数已达上限",
        "en-US": "Daily upload quota exceeded",
    },
    ErrorCode.QUOTA_EXTERNAL_EXCEEDED: {
        "zh-CN": "今日外部解码次数已达上限",
        "en-US": "Daily external decode quota exceeded",
    },
    ErrorCode.QUOTA_INSUFFICIENT: {
        "zh-CN": "配额不足",
        "en-US": "Insufficient quota",
    },
    ErrorCode.FILE_NOT_FOUND: {
        "zh-CN": "文件不存在",
        "en-US": "File not found",
    },
    ErrorCode.FILE_EXPIRED: {
        "zh-CN": "文件已过期",
        "en-US": "File expired",
    },
    ErrorCode.FILE_DELETED: {
        "zh-CN": "文件已被删除",
        "en-US": "File has been deleted",
    },
    ErrorCode.FILE_TOO_LARGE: {
        "zh-CN": "文件大小超过限制",
        "en-US": "File size exceeds limit",
    },
    ErrorCode.FILE_HASH_MISMATCH: {
        "zh-CN": "文件哈希校验失败",
        "en-US": "File hash mismatch",
    },
    ErrorCode.FILE_CODE_INVALID: {
        "zh-CN": "文件码无效",
        "en-US": "Invalid file code",
    },
    ErrorCode.FILE_CODE_USED: {
        "zh-CN": "文件码已被使用",
        "en-US": "File code already used",
    },
    ErrorCode.USER_NOT_FOUND: {
        "zh-CN": "用户不存在",
        "en-US": "User not found",
    },
    ErrorCode.USER_BANNED: {
        "zh-CN": "用户已被封禁",
        "en-US": "User has been banned",
    },
    ErrorCode.USER_TEMP_BANNED: {
        "zh-CN": "用户被临时封禁",
        "en-US": "User temporarily banned",
    },
    ErrorCode.USER_UNAUTHORIZED: {
        "zh-CN": "未授权访问",
        "en-US": "Unauthorized access",
    },
    ErrorCode.USER_FORBIDDEN: {
        "zh-CN": "禁止访问",
        "en-US": "Forbidden access",
    },
    ErrorCode.APPROVAL_NOT_FOUND: {
        "zh-CN": "审批记录不存在",
        "en-US": "Approval record not found",
    },
    ErrorCode.APPROVAL_REQUIRED: {
        "zh-CN": "此操作需要审批",
        "en-US": "This operation requires approval",
    },
    ErrorCode.APPROVAL_REJECTED: {
        "zh-CN": "审批已被拒绝",
        "en-US": "Approval has been rejected",
    },
    ErrorCode.APPROVAL_EXPIRED: {
        "zh-CN": "审批已过期",
        "en-US": "Approval has expired",
    },
    ErrorCode.INTERNAL_ERROR: {
        "zh-CN": "内部服务器错误",
        "en-US": "Internal server error",
    },
    ErrorCode.SERVICE_UNAVAILABLE: {
        "zh-CN": "服务暂不可用",
        "en-US": "Service unavailable",
    },
    ErrorCode.RATE_LIMITED: {
        "zh-CN": "请求过于频繁,请稍后再试",
        "en-US": "Too many requests, please try again later",
    },
    ErrorCode.MAINTENANCE_MODE: {
        "zh-CN": "系统维护中,请稍后再试",
        "en-US": "System under maintenance, please try again later",
    },
    ErrorCode.STORAGE_R2_FAILED: {
        "zh-CN": "R2 存储操作失败",
        "en-US": "R2 storage operation failed",
    },
    ErrorCode.STORAGE_CRDB_FAILED: {
        "zh-CN": "CRDB 数据库操作失败",
        "en-US": "CRDB database operation failed",
    },
    ErrorCode.STORAGE_REDIS_FAILED: {
        "zh-CN": "Redis 操作失败",
        "en-US": "Redis operation failed",
    },
    ErrorCode.STORAGE_QUOTA_EXCEEDED: {
        "zh-CN": "存储配额已超限",
        "en-US": "Storage quota exceeded",
    },
    ErrorCode.REPLICATION_FAILED: {
        "zh-CN": "数据复制失败",
        "en-US": "Data replication failed",
    },
    ErrorCode.REPLICATION_TIMEOUT: {
        "zh-CN": "数据复制超时",
        "en-US": "Data replication timed out",
    },
    ErrorCode.SYNC_DIRTY_FAILED: {
        "zh-CN": "脏数据同步失败",
        "en-US": "Dirty data sync failed",
    },
    ErrorCode.RELAY_FLOOD_WAIT: {
        "zh-CN": "Relay 账号触发 FloodWait",
        "en-US": "Relay account hit FloodWait",
    },
    ErrorCode.RELAY_BANNED: {
        "zh-CN": "Relay 账号已被封禁",
        "en-US": "Relay account banned",
    },
    ErrorCode.RELAY_RESTRICTED: {
        "zh-CN": "Relay 账号受限",
        "en-US": "Relay account restricted",
    },
    ErrorCode.RELAY_NO_AVAILABLE: {
        "zh-CN": "无可用 Relay 账号",
        "en-US": "No available relay account",
    },
    ErrorCode.DECODE_FAILED: {
        "zh-CN": "解码失败",
        "en-US": "Decode failed",
    },
    ErrorCode.DECODE_TIMEOUT: {
        "zh-CN": "解码超时",
        "en-US": "Decode timed out",
    },
    ErrorCode.DECODE_INVALID_INPUT: {
        "zh-CN": "解码输入无效",
        "en-US": "Invalid decode input",
    },
    ErrorCode.VALIDATION_FAILED: {
        "zh-CN": "参数校验失败",
        "en-US": "Validation failed",
    },
    ErrorCode.VALIDATION_MISSING_FIELD: {
        "zh-CN": "缺少必填字段",
        "en-US": "Missing required field",
    },
    ErrorCode.VALIDATION_INVALID_FORMAT: {
        "zh-CN": "字段格式无效",
        "en-US": "Invalid field format",
    },
}


def get_default_message(code: ErrorCode, locale: str = "zh-CN") -> str:
    """获取错误码的默认消息。

    Args:
        code: 错误码
        locale: 语言(zh-CN / en-US)

    Returns:
        默认消息字符串;若未定义返回 code.value
    """
    msgs = _DEFAULT_MESSAGES.get(code, {})
    return msgs.get(locale, msgs.get("zh-CN", code.value))


@dataclass
class DomainError(Exception):
    """R40 P2-6: 领域错误异常类。

    字段:
        code: ErrorCode 枚举值
        message: 人类可读消息(默认从 _DEFAULT_MESSAGES 取)
        details: 额外上下文细节(dict,JSON 序列化)
        trace_id: 贯穿日志/审计/响应的追踪 ID(UUID)
        severity: 错误严重级别
        cause: 原始异常(可选,不序列化到响应)

    用法:
        raise DomainError(
            code=ErrorCode.QUOTA_DECODE_EXCEEDED,
            details={"used": 5, "quota": 5},
        )

        # 从未知异常构造
        try:
            risky_operation()
        except Exception as e:
            raise DomainError.from_exception(e) from e
    """
    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    trace_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    severity: ErrorSeverity = ErrorSeverity.ERROR
    cause: Optional[BaseException] = None

    def __post_init__(self):
        """初始化后处理:确保 message 非空,继承 Exception。"""
        if not self.message:
            self.message = get_default_message(self.code)
        # 调用 Exception.__init__ 以支持 raise 语法
        super().__init__(self.message)

    def to_dict(self, locale: str = "zh-CN", include_trace: bool = True) -> dict[str, Any]:
        """序列化为 API 响应字典(JSON 兼容)。

        Args:
            locale: 消息语言(zh-CN / en-US)
            include_trace: 是否包含 trace_id(生产环境可关闭以避免泄露)

        Returns:
            {
                "code": "quota.decode.exceeded",
                "message": "今日解码次数已达上限",
                "details": {"used": 5, "quota": 5},
                "trace_id": "uuid-...",
                "severity": "error"
            }
        """
        result = {
            "code": self.code.value,
            "message": self.message,
            "details": dict(self.details) if self.details else {},
            "severity": self.severity.value,
        }
        if include_trace:
            result["trace_id"] = self.trace_id
        return result

    @classmethod
    def from_exception(
        cls,
        exc: BaseException,
        code: Optional[ErrorCode] = None,
        details: Optional[dict] = None,
    ) -> "DomainError":
        """从任意异常构造 DomainError。

        若原异常已是 DomainError,直接返回(保留原 trace_id)。
        否则降级为 INTERNAL_ERROR,保留 cause 引用。

        Args:
            exc: 原始异常
            code: 自定义错误码(可选,默认 INTERNAL_ERROR)
            details: 额外细节(可选)

        Returns:
            DomainError 实例
        """
        if isinstance(exc, DomainError):
            return exc
        error_code = code or ErrorCode.INTERNAL_ERROR
        # 安全提取异常信息(避免敏感数据泄露)
        safe_message = str(exc)[:500] if exc else ""
        merged_details = {"original_type": type(exc).__name__}
        if safe_message:
            merged_details["original_message"] = safe_message
        if details:
            merged_details.update(details)
        logger.warning(
            f"[DomainError] 异常降级 type={type(exc).__name__} "
            f"code={error_code.value} msg={safe_message[:100]}"
        )
        return cls(
            code=error_code,
            message=get_default_message(error_code),
            details=merged_details,
            cause=exc,
        )

    @classmethod
    def quota_exceeded(
        cls,
        quota_type: str = "decode",
        used: int = 0,
        limit: int = 0,
        locale: str = "zh-CN",
    ) -> "DomainError":
        """便捷构造:配额超限错误。

        Args:
            quota_type: 配额类型(decode/upload/external)
            used: 已用数量
            limit: 配额上限

        Returns:
            DomainError 实例
        """
        if quota_type == "upload":
            code = ErrorCode.QUOTA_UPLOAD_EXCEEDED
        elif quota_type == "external":
            code = ErrorCode.QUOTA_EXTERNAL_EXCEEDED
        else:
            code = ErrorCode.QUOTA_DECODE_EXCEEDED
        return cls(
            code=code,
            message=get_default_message(code, locale),
            details={"quota_type": quota_type, "used": used, "limit": limit},
            severity=ErrorSeverity.WARNING,
        )

    @classmethod
    def not_found(
        cls,
        resource: str = "file",
        resource_id: str = "",
        locale: str = "zh-CN",
    ) -> "DomainError":
        """便捷构造:资源不存在错误。

        Args:
            resource: 资源类型(file/user/approval)
            resource_id: 资源 ID

        Returns:
            DomainError 实例
        """
        if resource == "user":
            code = ErrorCode.USER_NOT_FOUND
        elif resource == "approval":
            code = ErrorCode.APPROVAL_NOT_FOUND
        else:
            code = ErrorCode.FILE_NOT_FOUND
        return cls(
            code=code,
            message=get_default_message(code, locale),
            details={"resource": resource, "id": resource_id},
            severity=ErrorSeverity.WARNING,
        )

    @classmethod
    def unauthorized(cls, locale: str = "zh-CN") -> "DomainError":
        """便捷构造:未授权错误。"""
        return cls(
            code=ErrorCode.USER_UNAUTHORIZED,
            message=get_default_message(ErrorCode.USER_UNAUTHORIZED, locale),
            severity=ErrorSeverity.ERROR,
        )

    @classmethod
    def forbidden(cls, locale: str = "zh-CN") -> "DomainError":
        """便捷构造:禁止访问错误。"""
        return cls(
            code=ErrorCode.USER_FORBIDDEN,
            message=get_default_message(ErrorCode.USER_FORBIDDEN, locale),
            severity=ErrorSeverity.ERROR,
        )

    @classmethod
    def validation_failed(
        cls,
        field_name: str = "",
        reason: str = "",
        locale: str = "zh-CN",
    ) -> "DomainError":
        """便捷构造:参数校验失败错误。

        Args:
            field_name: 字段名
            reason: 失败原因

        Returns:
            DomainError 实例
        """
        details = {}
        if field_name:
            details["field"] = field_name
        if reason:
            details["reason"] = reason
        return cls(
            code=ErrorCode.VALIDATION_FAILED,
            message=get_default_message(ErrorCode.VALIDATION_FAILED, locale),
            details=details,
            severity=ErrorSeverity.WARNING,
        )
