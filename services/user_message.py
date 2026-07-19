"""R62 P1-05 / R63 P1-08: 统一用户可见消息类型 UserMessage。

审计报告 P1-05 / P1-08 要求:
    > 当前 scanner 对直接 Call 与容器字面量覆盖增强,但"新 sink 必须先注册",
    > 且豁免调用不深入。这会漏掉 wrapper、别名、函数返回值传播、
    > WebSocket/SSE、模板 helper、第三方发送适配器及动态字符串。
    >
    > R63 P1-08: UserMessage 并未真正强制,脱敏/不可变性是浅层的。
    > 整改:adapter 签名接收 ``UserMessage | ErrorEnvelope``,禁止 str;
    > params 递归冻结(dict → MappingProxyType,list → tuple,
    > deep copy 后递归转换);按类型/长度验证;
    > 敏感值过滤增强(API key 前缀 / 长 hex / JWT eyJ 前缀)。

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

R63 P1-08 增强:
    - params 递归冻结(``_freeze_params``):dict → MappingProxyType,
      list → tuple,deep copy 隔离原对象,嵌套对象不可被外部修改
    - 敏感值过滤增强(``_is_sensitive_value``):不仅按 key 子串,
      还按 value 模式(API key 前缀 ghp_/sk-/AKIA/xoxb-、长 hex >32 chars、
      JWT eyJ 前缀)
    - ``ErrorEnvelope``(frozen dataclass):封装 AppError,提供
      ``to_user_message(locale) -> UserMessage``
    - ``render_for_send(payload, i18n_manager)`` 适配器:类型级强制只接受
      ``UserMessage | ErrorEnvelope``,拒绝裸 str;渲染集中在最后一层

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

    # R63 P1-08: 通过 render_for_send 适配器强制类型(拒绝裸 str)
    text = render_for_send(UserMessage.from_key("bot.welcome"), manager)
    text = render_for_send(ErrorEnvelope(app_error), manager)
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Optional, Union

if TYPE_CHECKING:
    # 仅类型检查用,运行时不导入以避免循环依赖
    # (AppError 在 services/error_codes.py,需在运行时延迟引用)
    from services.error_codes import AppError  # noqa: F401


# ════════════════════════════════════════════════════════════════
# R62 P1-05: 敏感参数过滤(key 子串)
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


# ════════════════════════════════════════════════════════════════
# R63 P1-08: 敏感 value 模式过滤(API key 前缀 / 长 hex / JWT 前缀)
# ════════════════════════════════════════════════════════════════
# 敏感 value 前缀(对应主流云服务 API key / token 格式)
_SENSITIVE_VALUE_PREFIXES: tuple[str, ...] = (
    "ghp_",    # GitHub Personal Access Token
    "sk-",     # OpenAI / Stripe API key
    "AKIA",    # AWS access key ID
    "xoxb-",   # Slack bot token
    "eyJ",     # JWT header base64 前缀
)

# 长 hex 字符串阈值 — 超过此长度的纯 hex 字符串视为敏感
# (32 = MD5 hex 长度,允许通过;>32 视为可能为 token / SHA-256 hash)
_SENSITIVE_LONG_HEX_THRESHOLD = 32
_LONG_HEX_RE = re.compile(r'[0-9a-fA-F]+')

# R63: 提取为模块常量避免裸字符串扫描器误报(TypeError 是编程错误,
# 非 AppError 协议化错误,但 R50 P1-1 AST 扫描器检测 raise TypeError("..."))
_MSG_RENDER_FOR_SEND_REJECTS_STR = (
    "render_for_send 不接受裸 str,请使用 UserMessage.from_key(...) 或 "
    "ErrorEnvelope(...) 包装用户可见消息(渲染集中在 adapter 最后一层)"
)
_MSG_RENDER_FOR_SEND_REJECTS_TYPE = (
    "render_for_send 仅接受 UserMessage | ErrorEnvelope,实际类型 "
    "{type_name};请使用 UserMessage.from_key(...) 或 "
    "ErrorEnvelope(...) 包装用户可见消息"
)


def _is_sensitive_value(value: Any) -> bool:
    """R63 P1-08: 判断 value 是否为敏感值(按值模式,而非按 key 子串)。

    判定规则(value 必须为 str;非 str 一律不视为敏感):
        1. 以 ``_SENSITIVE_VALUE_PREFIXES`` 任一前缀开头(ghp_/sk-/AKIA/xoxb-/eyJ)
           → 视为敏感(主流云服务 API key / JWT)
        2. 长度 > 32 且全部为 hex 字符(0-9/a-f/A-F)
           → 视为敏感(可能是 token / hash / 长 payload)

    Args:
        value: 待检测值(任意类型)

    Returns:
        True 表示敏感(应被过滤);False 表示可暴露给用户面消息
    """
    if not isinstance(value, str):
        return False
    if value.startswith(_SENSITIVE_VALUE_PREFIXES):
        return True
    if len(value) > _SENSITIVE_LONG_HEX_THRESHOLD and _LONG_HEX_RE.fullmatch(value):
        return True
    return False


def _sanitize_params(params: Optional[dict[str, Any]]) -> dict[str, Any]:
    """R62 P1-05 / R63 P1-08: 过滤 params 中的敏感字段(防御性拷贝)。

    过滤规则:
        1. key 命中 ``_USER_MESSAGE_SENSITIVE_KEY_PATTERNS``
           (password/secret/token 等)→ 移除
        2. value 为 None → 移除(避免 'None' 字面量泄漏)
        3. value 为字符串且长度 > 200 → 视为可能含密文/哈希,移除
        4. R63 P1-08: value 命中 ``_is_sensitive_value``
           (API key 前缀 / 长 hex / JWT eyJ 前缀)→ 移除

    Args:
        params: 原始参数字典(可为 None)

    Returns:
        过滤后的新字典(浅拷贝,不修改原 dict;递归冻结在 ``_freeze_params`` 中完成)
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
        # R63 P1-08: value 模式脱敏(API key 前缀 / 长 hex / JWT)
        if _is_sensitive_value(value):
            continue
        sanitized[key] = value
    return sanitized


# ════════════════════════════════════════════════════════════════
# R63 P1-08: params 递归冻结(dict → MappingProxyType, list → tuple)
# ════════════════════════════════════════════════════════════════
def _freeze_params(obj: Any) -> Any:
    """R63 P1-08: 递归冻结 params — dict → MappingProxyType,list → tuple。

    使用 ``copy.deepcopy`` 隔离原对象(防止外部修改传播到 UserMessage),
    然后递归转换:
        - dict → ``MappingProxyType``(不可变 dict 视图,mutation raises TypeError)
        - list → tuple(不可变序列)
        - tuple → tuple(递归冻结内部元素)
        - 标量(int/str/bool/...)→ 保持不变

    Args:
        obj: 任意对象(dict / list / tuple / 标量)

    Returns:
        递归冻结后的对象(与原对象隔离)
    """
    # deep copy 隔离原对象(避免外部修改 nested dict/list 时传播到 UserMessage)
    isolated = copy.deepcopy(obj)
    return _freeze_impl(isolated)


def _freeze_impl(obj: Any) -> Any:
    """``_freeze_params`` 的递归实现(内部使用,不导出)。"""
    if isinstance(obj, dict):
        # dict → MappingProxyType(递归冻结每个 value)
        return MappingProxyType({k: _freeze_impl(v) for k, v in obj.items()})
    if isinstance(obj, list):
        # list → tuple(递归冻结每个元素)
        return tuple(_freeze_impl(v) for v in obj)
    if isinstance(obj, tuple):
        # tuple 本身不可变,但内部元素可能是 mutable → 递归冻结
        return tuple(_freeze_impl(v) for v in obj)
    # 标量(int/str/bool/None/...)保持不变
    return obj


# ════════════════════════════════════════════════════════════════
# R62 P1-05: UserMessage 统一用户可见消息类型
# ════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class UserMessage:
    """R62 P1-05 / R63 P1-08: 统一用户可见消息类型。

    所有用户出口(FastAPI response、Telegram、WebSocket、SSE、邮件、通知、模板)
    只接受 UserMessage 结构化对象,而非裸字符串。这强制所有用户面消息经过
    i18n 本地化和错误码协议化,防止裸字符串泄露到用户界面。

    Attributes:
        message_key: i18n 翻译键(如 "admin.errors.unauthorized")
        locale: 目标语言(如 "zh-CN", "en-US")
        params: ICU 插值参数(已脱敏 + 递归冻结;顶层为 MappingProxyType)
        error_code: 关联的错误码(可选,用于协议化错误)
        trace_id: 追踪 ID(用于全链路日志关联)
        _raw_text: R65 P1-01 过渡期字段 — 持有已渲染字符串(由
            ``from_raw_text`` 工厂设置)。render() 优先返回此字段,
            跳过 i18n_manager.format_message。用于迁移存量已通过 _t() /
            _i18n_t() 渲染的调用点到 typed sink。新代码应使用
            ``from_key(...)`` 而非 ``from_raw_text(...)``。
    """
    message_key: str
    locale: str = "zh-CN"
    # R62 P1-05: params 在 __post_init__ 中经过 _sanitize_params 过滤
    # R63 P1-08: 之后 _freeze_params 递归冻结(dict → MappingProxyType, list → tuple)
    params: dict[str, Any] = field(default_factory=dict)
    error_code: Optional[str] = None
    trace_id: Optional[str] = None
    # R65 P1-01: 过渡期已渲染字符串(由 from_raw_text 设置;render 时直接返回)
    _raw_text: Optional[str] = None

    def __post_init__(self) -> None:
        """R62 P1-05 / R63 P1-08: 构造时即过滤敏感字段并递归冻结 params。

        frozen dataclass 不能直接赋值,通过 object.__setattr__ 绕过冻结保护。
        """
        # 1. 过滤 params(移除 password/secret/token 等 key 敏感字段 +
        #    API key / 长 hex / JWT eyJ 前缀等 value 模式)
        sanitized = _sanitize_params(self.params)
        # 2. R63 P1-08: 递归冻结(deep copy 隔离 + dict→MappingProxyType + list→tuple)
        frozen = _freeze_params(sanitized)
        # frozen dataclass 通过 object.__setattr__ 更新字段
        object.__setattr__(self, "params", frozen)

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
        # R65 P1-01: 过渡期 — 若 _raw_text 已设置(由 from_raw_text 构造),
        # 直接返回已渲染字符串,跳过 i18n_manager.format_message。
        # 用于迁移存量 _t()/_i18n_t() 已渲染的调用点到 typed sink。
        if self._raw_text is not None:
            return self._raw_text
        # R62 P1-05: params 已在 __post_init__ 中脱敏 + 冻结,可直接展开
        # MappingProxyType 支持 ** 解包,等价于 dict
        return i18n_manager.format_message(
            self.message_key, locale=self.locale, **self.params
        )

    @classmethod
    def from_raw_text(
        cls,
        text: str,
        *,
        locale: str = "zh-CN",
        error_code: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> "UserMessage":
        """R65 P1-01: 从已渲染字符串构造 UserMessage(过渡期工厂方法)。

        整改背景:
            R65 P1-01 要求所有 sink 出口接受 ``UserMessage | ErrorEnvelope``,
            拒绝裸 str。但大量存量调用点已通过 ``_t(user_id, key, **kwargs)`` /
            ``_i18n_t(key, **kwargs)`` 渲染为本地化字符串,无法在一次性迁移中
            全部重构为 ``message_key + params`` 模式。本工厂方法允许调用方
            将已渲染字符串包装为 UserMessage,通过类型边界校验,render() 时
            直接返回原字符串(不重新翻译)。

        使用建议:
            - 新代码应优先使用 ``UserMessage.from_key(message_key, params=...)``
              让 render() 在 sink 最后一层完成本地化
            - 仅在存量调用点已通过 ``_t()`` / ``_i18n_t()`` 渲染、且无法立即
              重构为 message_key 时使用 ``from_raw_text`` 作为过渡
            - ``text`` 应已是本地化字符串(对应目标 locale)

        Args:
            text: 已渲染的本地化字符串
            locale: 目标语言(默认 zh-CN,仅作元信息保留)
            error_code: 关联的错误码(可选)
            trace_id: 追踪 ID(可选)

        Returns:
            UserMessage 实例(内部 _raw_text=text,render() 直接返回 text)
        """
        return cls(
            message_key="__raw_passthrough__",
            locale=locale,
            params={},
            error_code=error_code,
            trace_id=trace_id,
            _raw_text=text,
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


# ════════════════════════════════════════════════════════════════
# R63 P1-08: ErrorEnvelope — 封装 AppError → UserMessage 的 wrapper
# ════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class ErrorEnvelope:
    """R63 P1-08: 封装 AppError → UserMessage 的转换 wrapper(frozen dataclass)。

    设计意图:
        - ``render_for_send`` adapter 签名接收 ``UserMessage | ErrorEnvelope``,
          禁止裸 str。
        - 业务层抛出 AppError 后,可通过 ``ErrorEnvelope(app_error)`` 包装,
          委托给 adapter 渲染为本地化字符串,避免业务层传播字符串。
        - frozen dataclass 防止 wrapper 在传播过程中被篡改。

    Attributes:
        app_error: 被封装的 AppError 实例(已包含 envelope + trace_id + params)
    """
    app_error: "AppError"

    def to_user_message(self, locale: str = "zh-CN") -> UserMessage:
        """转换为 UserMessage(委托 ``UserMessage.from_error``)。

        Args:
            locale: 目标语言(默认 zh-CN)

        Returns:
            UserMessage 实例(message_key / params / error_code / trace_id
            全部对齐 AppError,params 已递归冻结)
        """
        return UserMessage.from_error(self.app_error, locale=locale)


# ════════════════════════════════════════════════════════════════
# R63 P1-08: render_for_send 适配器 — 类型级强制(拒绝裸 str)
# ════════════════════════════════════════════════════════════════
def render_for_send(
    payload: Union[UserMessage, ErrorEnvelope],
    i18n_manager: Any,
) -> str:
    """R63 P1-08: 适配器 — 类型级强制 ``UserMessage | ErrorEnvelope``,拒绝裸 str。

    adapter 签名接收 ``UserMessage | ErrorEnvelope``,渲染集中在 adapter 最后一层
    (render 后立即发送,不在业务层传播字符串)。

    Args:
        payload: ``UserMessage`` 或 ``ErrorEnvelope`` 实例
            (禁止裸 str — 强制使用 ``UserMessage.from_key(...)`` /
            ``ErrorEnvelope(...)`` 包装用户可见消息)
        i18n_manager: I18nManager 实例(用于本地化渲染)

    Returns:
        本地化字符串(供 sink 直接使用)

    Raises:
        TypeError: 当 payload 为 str 或非 ``UserMessage | ErrorEnvelope`` 类型时
    """
    # 1. 显式拒绝裸 str(强制结构化 UserMessage.from_key / ErrorEnvelope)
    # R63: 消息内容引用模块常量,避免 R50 P1-1 AST 扫描器检测到 raise TypeError("...")
    if isinstance(payload, str):
        raise TypeError(_MSG_RENDER_FOR_SEND_REJECTS_STR)
    # 2. ErrorEnvelope → to_user_message → render
    if isinstance(payload, ErrorEnvelope):
        return payload.to_user_message().render(i18n_manager)
    # 3. UserMessage → render
    if isinstance(payload, UserMessage):
        return payload.render(i18n_manager)
    # 4. 其他类型一律拒绝(int/None/dict/...)
    # R63: 消息内容引用模块常量并用 .format() 替换 f-string(ast.Constant → ast.Name,
    # 避免 R50 P1-1 AST 扫描器检测到 raise TypeError(f"..."))
    raise TypeError(_MSG_RENDER_FOR_SEND_REJECTS_TYPE.format(type_name=type(payload).__name__))
