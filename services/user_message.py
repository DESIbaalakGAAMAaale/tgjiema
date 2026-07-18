"""R62 P1-05: 统一用户可见消息类型 UserMessage。

审计报告 P1-05 要求:
    > 当前 scanner 对直接 Call 与容器字面量覆盖增强,但"新 sink 必须先注册",
    > 且豁免调用不深入。这会漏掉 wrapper、别名、函数返回值传播、
    > WebSocket/SSE、模板 helper、第三方发送适配器及动态字符串。

整改方案:
    1. 引入 ``UserMessage`` — 统一用户可见消息类型
    2. 所有用户面出口(FastAPI response、Telegram、WebSocket、SSE、邮件、通知、
       模板 context)只接受结构化对象,而非裸字符串
    3. 强制所有用户面消息经过 i18n 本地化(``message_key`` + ``params``)
    4. params 经过 ``is_safe_param`` 二次过滤,防止敏感字段泄露

设计原则:
    - ``UserMessage`` 是 frozen dataclass(不可变),避免在传播过程中被篡改
    - ``render(i18n_manager)`` 惰性渲染,在真正写入用户面前才转为字符串
    - ``from_error(AppError)`` 从已结构化的 AppError 转换,保留 trace_id 全链路关联
    - params 在构造时即经过 ``_sanitize_params`` 过滤(防御性拷贝,避免外部修改)

用法示例:
    # 从 i18n key 直接构造
    msg = UserMessage(
        message_key="bot.upload_banned",
        locale="zh-CN",
        params={"file_code": "ABC123"},
    )
    text = msg.render(i18n_manager)  # 真正渲染为本地化字符串

    # 从 AppError 转换
    try:
        raise AppError(ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT,
                       params={"file_code": "abc"})
    except AppError as e:
        msg = UserMessage.from_error(e, locale="zh-CN")
        await safe_reply_text(update.message, msg.render(get_i18n_manager()))

R62 P1-05: 所有用户出口应接受 UserMessage 结构化对象,而非裸字符串。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    # 仅类型检查用,运行时不导入以避免循环依赖
    # (AppError 在 services/error_codes.py,需在运行时延迟引用)
    from services.error_codes import AppError  # noqa: F401


# ════════════════════════════════════════════════════════════════
# R62 P1-05: 敏感参数过滤
# ════════════════════════════════════════════════════════════════
# 内联敏感 key 子串(独立于 error_codes._SENSITIVE_KEY_PATTERNS,
# 避免对 services.error_codes 的强依赖;UserMessage 应可独立构造)
_USER_MESSAGE_SENSITIVE_KEY_PATTERNS: tuple[str, ...] = (
    "password",
    "secret",
    "token",
    "credential",
    "private_key",
    "api_key",
    "access_key",
    "session",
    "cookie",
    "hash",
    "salt",
    "otp",
    "mfa_code",
)


def _is_sensitive_key(key: str) -> bool:
    """R62 P1-05: 判断参数 key 是否为敏感字段(不应出现在用户面消息 params 中)。

    判定规则(大小写不敏感):
        key 包含 ``_USER_MESSAGE_SENSITIVE_KEY_PATTERNS`` 中任一子串 → 视为敏感

    Args:
        key: 参数名(如 "password" / "api_token" / "user_secret")

    Returns:
        True 表示敏感(应被过滤);False 表示可暴露给用户面消息
    """
    key_lower = str(key).lower()
    return any(pattern in key_lower for pattern in _USER_MESSAGE_SENSITIVE_KEY_PATTERNS)


def _sanitize_params(params: Optional[dict[str, Any]]) -> dict[str, Any]:
    """R62 P1-05: 过滤 params 中的敏感字段(防御性拷贝)。

    过滤规则:
        1. key 命中 ``_USER_MESSAGE_SENSITIVE_KEY_PATTERNS``(password/secret/token 等)→ 移除
        2. value 为字符串且长度 > 200 → 视为可能含密文/哈希,移除
        3. value 为 None → 移除(避免 'None' 字面量泄漏)

    Args:
        params: 原始参数字典(可为 None)

    Returns:
        过滤后的新字典(浅拷贝,不修改原 dict)
    """
    if not params:
        return {}
    sanitized: dict[str, Any] = {}
    for key, value in params.items():
        if _is_sensitive_key(key):
            continue
        if value is None:
            continue
        if isinstance(value, str) and len(value) > 200:
            # 长字符串可能含密文/哈希/长 payload,过滤
            continue
        sanitized[key] = value
    return sanitized


# ════════════════════════════════════════════════════════════════
# R62 P1-05: UserMessage 统一用户可见消息类型
# ════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class UserMessage:
    """R62 P1-05: 统一用户可见消息类型。

    所有用户出口(FastAPI response、Telegram、WebSocket、SSE、邮件、通知、模板)
    只接受 UserMessage 结构化对象,而非裸字符串。这强制所有用户面消息经过
    i18n 本地化和错误码协议化,防止裸字符串泄露到用户界面。

    Attributes:
        message_key: i18n 翻译键(如 "admin.errors.unauthorized")
        locale: 目标语言(如 "zh-CN", "en-US")
        params: ICU 插值参数(已脱敏,不含敏感字段)
        error_code: 关联的错误码(可选,用于协议化错误)
        trace_id: 追踪 ID(用于全链路日志关联)
    """
    message_key: str
    locale: str = "zh-CN"
    # R62 P1-05: params 在 __post_init__ 中经过 _sanitize_params 过滤(防御性拷贝)
    params: dict[str, Any] = field(default_factory=dict)
    error_code: Optional[str] = None
    trace_id: Optional[str] = None

    def __post_init__(self) -> None:
        """R62 P1-05: 构造时即过滤敏感字段(防御性拷贝,避免外部修改)。

        frozen dataclass 不能直接赋值,通过 object.__setattr__ 绕过冻结保护。
        """
        # 过滤 params(移除 password/secret/token 等敏感字段)
        sanitized = _sanitize_params(self.params)
        # frozen dataclass 通过 object.__setattr__ 更新字段
        object.__setattr__(self, "params", sanitized)

    def render(self, i18n_manager: Any) -> str:
        """通过 i18n manager 渲染为本地化字符串。

        Args:
            i18n_manager: I18nManager 实例(需实现 format_message(key, locale, **kwargs))

        Returns:
            本地化字符串(params 已展开为 {var} 占位符插值)

        Note:
            render 是惰性的 — UserMessage 在传播过程中保持结构化,
            仅在真正写入用户面前才转为字符串。
        """
        # R62 P1-05: params 已在 __post_init__ 中脱敏,可直接展开
        return i18n_manager.format_message(
            self.message_key, locale=self.locale, **self.params
        )

    @classmethod
    def from_error(cls, app_error: "AppError", locale: str = "zh-CN") -> "UserMessage":
        """从 AppError 构造 UserMessage。

        R62 P1-05: 桥接结构化错误协议(ErrorEnvelope / AppError)与用户面消息类型,
        保留 trace_id 全链路关联,error_code 供前端按协议渲染。

        Args:
            app_error: AppError 实例(已包含 envelope + trace_id + params)
            locale: 目标语言(默认 zh-CN)

        Returns:
            UserMessage 实例(message_key / params / error_code / trace_id 全部对齐)
        """
        # app_error.envelope 已经过 ErrorDefinition.safe_params 白名单过滤,
        # 这里再做一次 _sanitize_params 兜底(防止白名单配置失误)
        return cls(
            message_key=app_error.envelope.message_key,
            locale=locale,
            params=app_error.envelope.params,
            error_code=app_error.code,
            trace_id=app_error.trace_id,
        )

    @classmethod
    def from_key(
        cls,
        message_key: str,
        *,
        locale: str = "zh-CN",
        params: Optional[dict[str, Any]] = None,
        error_code: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> "UserMessage":
        """R62 P1-05: 从 i18n key 直接构造 UserMessage(语义化工厂方法)。

        与直接调用构造器相比,此方法明确语义:从 i18n key 出发构造用户消息,
        params / error_code / trace_id 均可选。便于在 bot handler 中替换裸字符串:

            # 旧代码(裸字符串):
            await reply_text(_t(user_id, "bot.upload_banned"))
            # 新代码(结构化 UserMessage):
            msg = UserMessage.from_key("bot.upload_banned", locale=locale)
            await reply_text(msg.render(get_i18n_manager()))
        """
        return cls(
            message_key=message_key,
            locale=locale,
            params=params if params is not None else {},
            error_code=error_code,
            trace_id=trace_id,
        )
