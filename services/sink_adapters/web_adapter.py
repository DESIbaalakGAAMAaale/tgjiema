"""R64 P1-06: FastAPI/Web sink typed adapter + R76 O5: secretless contract 入口。

整改背景:
    - R64 终审报告 P1-06:业务模块禁止在 admin/ 路由中直接构造
      ``JSONResponse(content={...: str(e)})``,内部异常细节不应进入用户面响应体。
      本 adapter 强制 ``UserMessage | ErrorEnvelope`` 输入,内部异常仅记录到
      结构化日志,响应体只暴露 safe params + trace_id。
    - R76 终审报告 10.O-O5 / P0-03 / P0-04:secretless 测试需要一个公开 HTTP
      入口接收 contract Update,并调用与正常 webhook 相同的公开 dispatcher,
      而不是测试程序通过 ``docker compose exec ... python -c`` 直调私有 handler。
      本模块新增 ``create_contract_app()`` 工厂,仅在 ``SECRETLESS_MODE=true``
      时调用;production 启动若注册路由立即失败。

adapter 职责:
    1. 类型级强制 ``UserMessage | ErrorEnvelope`` 输入(拒绝裸 str)。
    2. 内部经 ``render_for_send`` 渲染为本地化字符串后构造 JSONResponse。
    3. 响应体只包含 ``message`` / ``error_code`` / ``trace_id`` / safe params;
       内部 exception 不进入响应体。
    4. R76 O5: ``create_contract_app()`` 暴露两个端点(仅 secretless):
       - ``POST /internal/contract/update``:校验 contract token 和 payload
         schema,异步调用 ``bots.up_bot._dispatch_media``(与 webhook 同入口)。
       - ``GET /internal/contract/transactions/{trace_id}``:只读聚合状态,
         不推动状态机。
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Union

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from services.error_codes import AppError, ErrorCodes
from services.user_message import (
    ErrorEnvelope,
    UserMessage,
    render_for_send,
)

# R76 10.M: 模块级 logger (上轮引入 logger.debug 但未定义 logger,本轮补齐)
logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# i18n manager 延迟解析(避免模块导入时的循环依赖)
# ════════════════════════════════════════════════════════════════
def _resolve_i18n_manager(i18n_manager: Optional[Any] = None) -> Any:
    """延迟解析 i18n manager。

    优先使用调用方显式传入的 ``i18n_manager``;未传入时回退到
    ``services.i18n.get_i18n_manager()`` 单例。
    """
    if i18n_manager is not None:
        return i18n_manager
    from services.i18n import get_i18n_manager
    return get_i18n_manager()


# R76 §10.M: Contract API 是机器到机器接口,响应文案使用 en-US locale
# (避免依赖 default_locale 兜底,符合 R65 P1-03 严格 fail-closed 模式)
_CONTRACT_API_LOCALE = "en-US"


def _t(msg_key: str, **kwargs: Any) -> str:
    """Contract API 错误文案 i18n 翻译(固定 en-US locale)。

    Args:
        msg_key: 翻译 key(如 "services.web_adapter.s1")
        **kwargs: 插值参数(如 key="sql", path="update")

    Returns:
        本地化字符串(en-US)
    """
    manager = _resolve_i18n_manager()
    return manager.format_message(msg_key, locale=_CONTRACT_API_LOCALE, **kwargs)


# ════════════════════════════════════════════════════════════════
# Typed payload 校验
# ════════════════════════════════════════════════════════════════
_MSG_REJECTS_STR = (
    "Web sink adapter 不接受裸 str,请使用 UserMessage.from_key(...) "
    "或 ErrorEnvelope(...) 包装用户可见消息(类型边界在 adapter 层强制)"
)
_MSG_REJECTS_TYPE = (
    "Web sink adapter 仅接受 UserMessage | ErrorEnvelope,实际类型 "
    "{type_name};请使用 UserMessage.from_key(...) 或 ErrorEnvelope(...) "
    "包装用户可见消息"
)


def _validate_payload(payload: Any) -> Union[UserMessage, ErrorEnvelope]:
    """类型级校验:payload 必须为 ``UserMessage | ErrorEnvelope``,拒绝裸 str。"""
    if isinstance(payload, str):
        raise TypeError(_MSG_REJECTS_STR)
    if isinstance(payload, (UserMessage, ErrorEnvelope)):
        return payload
    raise TypeError(_MSG_REJECTS_TYPE.format(type_name=type(payload).__name__))


# ════════════════════════════════════════════════════════════════
# Typed adapter API
# ════════════════════════════════════════════════════════════════
def json_response(
    payload: Union[UserMessage, ErrorEnvelope],
    status_code: int = 200,
    i18n_manager: Optional[Any] = None,
    **extra: Any,
) -> JSONResponse:
    """Typed adapter: ``fastapi.responses.JSONResponse``。

    只接受 ``UserMessage | ErrorEnvelope``,拒绝裸 str。内部经
    ``render_for_send`` 渲染为本地化字符串后构造 JSONResponse。

    响应体结构:
        {
            "message": "<本地化消息>",
            "error_code": "<错误码,仅 ErrorEnvelope>",
            "trace_id": "<追踪 ID,仅当 payload 含 trace_id>",
            ...safe params(已过滤敏感字段),
            ...extra(调用方追加的 safe 字段,如 ``{"status": "ok"}``)
        }

    内部 exception ``str(e)`` 不进入响应体(由调用方记录到结构化日志,
    通过 ``trace_id`` 让用户引用)。

    Args:
        payload: ``UserMessage`` 或 ``ErrorEnvelope`` 实例(禁止裸 str)
        status_code: HTTP 状态码(默认 200)
        i18n_manager: 可选的 I18nManager(未传入时使用 get_i18n_manager() 单例)
        **extra: 追加到响应体的 safe 字段(调用方负责确保不包含敏感信息)

    Returns:
        ``fastapi.responses.JSONResponse`` 实例

    Raises:
        TypeError: payload 为裸 str 或非 ``UserMessage | ErrorEnvelope`` 类型
    """
    validated = _validate_payload(payload)
    manager = _resolve_i18n_manager(i18n_manager)
    text = render_for_send(validated, manager)

    # 构造响应体:只暴露 safe 字段(内部 exception 不进入)
    body: dict[str, Any] = {"message": text}

    # 提取 UserMessage 字段(error_code / trace_id / safe params)
    if isinstance(validated, ErrorEnvelope):
        user_msg = validated.to_user_message()
    else:
        user_msg = validated

    if user_msg.error_code:
        body["error_code"] = user_msg.error_code
    if user_msg.trace_id:
        body["trace_id"] = user_msg.trace_id
    # safe params(params 已在 UserMessage 构造时经过 _sanitize_params 过滤)
    # 以扁平形式附加到响应体(便于前端按 key 引用)
    for k, v in user_msg.params.items():
        # 避免覆盖顶层字段(message/error_code/trace_id)
        if k not in body:
            body[k] = v

    # 追加调用方提供的 extra 字段
    for k, v in extra.items():
        if k not in body:
            body[k] = v

    return JSONResponse(status_code=status_code, content=body)


# ════════════════════════════════════════════════════════════════
# R76 O5: Secretless Contract 入口(仅 SECRETLESS_MODE=true 时启用)
# ════════════════════════════════════════════════════════════════
# 整改依据(R76 终审报告 10.O-O5):
#   - secretless 测试需要一个公开 HTTP 入口接收 contract Update;
#   - 该入口必须调用与正常 webhook 相同的公开 dispatcher;
#   - 禁止测试程序通过 docker compose exec / python -c 直调 bots.* 私有函数;
#   - production 启动若路由被注册则直接失败;
#   - 端点不能接受 SQL、stream key、handler 名或任意 method。

# 顶层 payload 白名单(只允许 update + trace_id)
_ALLOWED_CONTRACT_TOP_KEYS = frozenset({"update", "trace_id"})

# 禁止的 key(防止 SQL/stream key/handler/method 注入)
# 注:这些 key 出现在 update 任意层级均拒绝
_FORBIDDEN_CONTRACT_KEYS = frozenset({
    # SQL/数据库注入
    "sql", "query", "statement", "raw_sql", "db_query",
    # Redis stream key 注入
    "stream_key", "redis_key", "queue_key", "xadd_key",
    # Handler/method 注入
    "handler", "handler_name", "method", "command", "action",
    "exec", "eval", "import", "subprocess", "shell",
    # P0-03: Update 内嵌文件内容禁止
    "_e2e_file_content_b64", "file_content_b64", "file_content",
    # 内部私有字段
    "bot_override", "force_dispatch", "skip_validation",
})


@dataclass
class ContractTransactionState:
    """单笔 contract 交易的聚合状态(只读视图,不推动状态机)。

    Attributes:
        trace_id: 交易追踪 ID
        status: 当前状态(pending/accepted/delivered/failed)
        accepted_at: 接收时间(unix 时间戳)
        completed_at: 终态时间(delivered/failed 时设置)
        details: 详细子状态(各阶段进展)
        error: 失败时的错误描述(不包含敏感内部细节)
    """

    trace_id: str
    status: str = "pending"
    accepted_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    details: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


class ContractTransactionRegistry:
    """内存中的 contract 交易状态注册表(trace_id -> ContractTransactionState)。

    单进程内有效;secretless CI 单进程足够。多进程部署需要共享存储(CRDB),
    但本轮 secretless 测试范围不需要。
    """

    def __init__(self) -> None:
        self._states: dict[str, ContractTransactionState] = {}
        self._lock = asyncio.Lock()

    async def register(self, trace_id: str) -> ContractTransactionState:
        """注册新交易;若已存在则返回已有状态(幂等)。"""
        async with self._lock:
            if trace_id in self._states:
                return self._states[trace_id]
            state = ContractTransactionState(trace_id=trace_id)
            self._states[trace_id] = state
            return state

    async def mark_accepted(self, trace_id: str) -> None:
        """标记为 accepted(dispatcher 已调度但未完成)。"""
        async with self._lock:
            state = self._states.get(trace_id)
            if state is None:
                return
            state.status = "accepted"

    async def mark_terminal(
        self,
        trace_id: str,
        status: str,
        *,
        details: Optional[dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        """标记为终态(delivered/failed)。"""
        async with self._lock:
            state = self._states.get(trace_id)
            if state is None:
                return
            state.status = status
            if details:
                state.details.update(details)
            if error:
                state.error = error
            if status in ("delivered", "failed"):
                state.completed_at = time.time()

    async def get(self, trace_id: str) -> Optional[ContractTransactionState]:
        """获取交易状态(只读)。"""
        async with self._lock:
            return self._states.get(trace_id)


# 进程级单例(secretless CI 单进程足够;多进程需共享存储)
_contract_transaction_registry = ContractTransactionRegistry()


def _check_forbidden_keys(obj: Any, path: str) -> None:
    """递归检查字典/list 中是否包含禁止的 key。

    Args:
        obj: 待检查对象(dict / list / 标量)
        path: 当前路径(用于错误信息)

    Raises:
        HTTPException: 发现禁止 key 时 400
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in _FORBIDDEN_CONTRACT_KEYS:
                raise HTTPException(
                    status_code=400,
                    detail=_t("services.web_adapter.s1", field=key, path=path),
                )
            _check_forbidden_keys(value, f"{path}.{key}")
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            _check_forbidden_keys(item, f"{path}[{idx}]")


def _validate_contract_payload(
    payload: Any,
) -> tuple[dict[str, Any], str]:
    """校验 ``POST /internal/contract/update`` 的 payload schema。

    校验规则:
        1. 顶层必须为 dict,只允许 ``update`` 和 ``trace_id`` 两个 key;
        2. ``update`` 必须为 dict,必须包含 ``message`` 字段;
        3. ``trace_id`` 必须为非空字符串(长度 1-256);
        4. ``update.message`` 必须包含 ``from``/``chat``/``document`` 三字段;
        5. ``update.message.document`` 必须包含 ``file_id``(非空字符串)和
           ``file_size``(非负整数);
        6. ``update.update_id`` 和 ``update.message.message_id`` 若存在必须为 int;
        7. 任意层级不允许出现 ``_FORBIDDEN_CONTRACT_KEYS`` 中的 key;
        8. ``_e2e_file_content_b64`` 严格禁止(P0-03 整改要求)。

    Args:
        payload: 已解析的 JSON 对象

    Returns:
        ``(update_dict, trace_id)``

    Raises:
        HTTPException: 校验失败返回 400
    """
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400,
            detail=_t("services.web_adapter.s2"),
        )

    # 顶层 key 白名单
    extra_keys = set(payload.keys()) - _ALLOWED_CONTRACT_TOP_KEYS
    if extra_keys:
        raise HTTPException(
            status_code=400,
            detail=_t("services.web_adapter.s3", keys=sorted(extra_keys)),
        )

    update = payload.get("update")
    if not isinstance(update, dict):
        raise HTTPException(
            status_code=400,
            detail=_t("services.web_adapter.s4"),
        )

    trace_id = str(payload.get("trace_id", "")).strip()
    if not trace_id:
        raise HTTPException(
            status_code=400,
            detail=_t("services.web_adapter.s5"),
        )
    if len(trace_id) > 256:
        raise HTTPException(
            status_code=400,
            detail=_t("services.web_adapter.s6"),
        )

    # 递归检查禁止 key(顶层 update + trace_id 都检查)
    _check_forbidden_keys(update, "update")

    # update_id 若存在必须为 int
    if "update_id" in update and not isinstance(update["update_id"], int):
        raise HTTPException(
            status_code=400,
            detail=_t("services.web_adapter.s7"),
        )

    message = update.get("message")
    if not isinstance(message, dict):
        raise HTTPException(
            status_code=400,
            detail=_t("services.web_adapter.s8"),
        )

    # message 必填字段
    for required_key in ("from", "chat", "document", "message_id", "date"):
        if required_key not in message:
            raise HTTPException(
                status_code=400,
                detail=_t("services.web_adapter.s9", field=required_key),
            )

    # message_id / date 必须为 int
    for int_key in ("message_id", "date"):
        if not isinstance(message[int_key], int):
            raise HTTPException(
                status_code=400,
                detail=_t("services.web_adapter.s10", field=int_key),
            )

    # from / chat 必须为 dict
    if not isinstance(message["from"], dict):
        raise HTTPException(
            status_code=400,
            detail=_t("services.web_adapter.s11"),
        )
    if not isinstance(message["chat"], dict):
        raise HTTPException(
            status_code=400,
            detail=_t("services.web_adapter.s12"),
        )

    # from.id / chat.id 必须为 int
    from_id = message["from"].get("id")
    if not isinstance(from_id, int) or from_id <= 0:
        raise HTTPException(
            status_code=400,
            detail=_t("services.web_adapter.s13"),
        )
    chat_id = message["chat"].get("id")
    if not isinstance(chat_id, int):
        raise HTTPException(
            status_code=400,
            detail=_t("services.web_adapter.s14"),
        )

    # document 必须为 dict
    document = message["document"]
    if not isinstance(document, dict):
        raise HTTPException(
            status_code=400,
            detail=_t("services.web_adapter.s15"),
        )

    file_id = document.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        raise HTTPException(
            status_code=400,
            detail=_t("services.web_adapter.s16"),
        )
    file_size = document.get("file_size", 0)
    if not isinstance(file_size, int) or file_size < 0:
        raise HTTPException(
            status_code=400,
            detail=_t("services.web_adapter.s17"),
        )

    return update, trace_id


def _verify_contract_token(provided: Optional[str], expected: str) -> None:
    """常量时间比较 X-Contract-Token。

    Args:
        provided: 请求头提供的 token
        expected: 配置的期望 token

    Raises:
        HTTPException: 缺失或不匹配返回 401
    """
    if not provided:
        raise HTTPException(
            status_code=401,
            detail=_t("services.web_adapter.s18"),
        )
    if not expected:
        # 配置错误:contract_token 未设置;拒绝所有请求
        raise HTTPException(
            status_code=503,
            detail=_t("services.web_adapter.s19"),
        )
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=401,
            detail=_t("services.web_adapter.s20"),
        )


class _ContractContext:
    """最小 Context 对象,模拟 ``telegram.ext.ContextTypes.DEFAULT_TYPE``。

    业务层 dispatcher 调用 ``context.bot`` / ``context.user_data`` 即可;
    其他属性返回 None / 空容器,不阻塞 dispatcher 主路径。

    Attributes:
        bot: ProviderClient 实例(ContractProviderClient 或 telegram.Bot)
        user_data: 用户级状态字典(对应 telegram.ext 的 user_data)
        chat_data: 会话级状态字典
        bot_data: bot 级状态字典
        args: 命令参数列表(contract 模式通常为空)
    """

    def __init__(self, *, bot: Any, trace_id: str) -> None:
        self.bot = bot
        self.user_data: dict[str, Any] = {}
        self.chat_data: dict[str, Any] = {}
        self.bot_data: dict[str, Any] = {}
        self.args: list[str] = []
        self.trace_id = trace_id

    def __getattr__(self, name: str) -> Any:
        # 未知属性返回 None,避免 AttributeError 阻塞 dispatcher
        # (telegram.ext.ContextTypes.DEFAULT_TYPE 的属性集较大,这里做最小化兼容)
        return None


async def _dispatch_contract_update(
    *,
    update_dict: dict[str, Any],
    trace_id: str,
    public_dispatcher: Optional[Callable[..., Awaitable[Any]]] = None,
    bot: Optional[Any] = None,
) -> None:
    """异步调度 contract update 到公开 dispatcher。

    成功路径:
        1. 从 ``bots.up_bot`` 读取 bot 实例(若未显式传入);
        2. 用 ``telegram.Update.de_json`` 构造 Update 对象;
        3. 构造最小 Context;
        4. 调用 ``public_dispatcher(update, context)``(默认 ``_dispatch_media``);
        5. 标记交易为 delivered。

    失败路径:
        - 任何异常都标记 failed,记录 error 类型 + 消息(不暴露堆栈到响应体);
        - 通过 loguru 记录完整异常供运维排查。
    """
    from loguru import logger

    try:
        # 延迟导入 telegram 和 bots.up_bot,避免模块加载时副作用
        from telegram import Bot as _TelegramBot
        from telegram import Update as _TelegramUpdate

        # 获取 bot 实例(优先使用显式传入的,否则从 up_bot 模块全局 _bot 读取)
        actual_bot = bot
        if actual_bot is None:
            from bots import up_bot as _up_bot
            actual_bot = getattr(_up_bot, "_bot", None)
        if actual_bot is None:
            raise AppError(
                ErrorCodes.UPLOAD_OUTBOX_BOT_UNINITIALIZED,
                params={
                    "reason": "bot instance not available; ensure "
                              "bots.up_bot._async_main has initialized _bot "
                              "before dispatching contract updates"
                },
            )

        # R80 P0-02 fix: python-telegram-bot 21.6 的 set_bot() 强制
        # isinstance(bot, Bot) 类型检查。ContractProviderClient 不继承 Bot,
        # 直接传入 de_json 会抛 TypeError。解决方案:用真实 Bot(dummy token)
        # 完成反序列化,再将 bot 引用替换为 ContractProviderClient,
        # 使 message.reply_text 等快捷方法路由到 provider-sim。
        # R81 fix: telegram 包未安装时(conftest 注入 MagicMock),
        # _TelegramBot 不是 type,isinstance() 会抛 TypeError。
        # 此时跳过类型检查,直接走 mock 路径。
        _is_real_bot = (
            isinstance(_TelegramBot, type)
            and isinstance(actual_bot, _TelegramBot)
        )
        if _is_real_bot:
            _de_json_bot = actual_bot
        else:
            _de_json_bot = _TelegramBot(token="123456:contract-deserialization-shim")

        update = _TelegramUpdate.de_json(update_dict, _de_json_bot)
        if update is None or update.message is None:
            raise AppError(
                ErrorCodes.VALIDATION_FAILED,
                params={
                    "reason": "failed to construct telegram.Update from "
                              "payload or update.message is None"
                },
            )

        # 将 Update 树中所有 TelegramObject 的 bot 引用替换为 actual_bot,
        # 使 reply_text / copy_message 等快捷方法路由到 ContractProviderClient。
        # python-telegram-bot 21.6 无 walk() 方法,手动遍历已知子对象。
        if not _is_real_bot:
            _objs_to_patch = [update]
            if update.message:
                _objs_to_patch.append(update.message)
                for _attr in ("from_user", "chat", "document", "photo",
                              "video", "audio", "voice", "sticker",
                              "animation", "reply_to_message"):
                    _child = getattr(update.message, _attr, None)
                    if _child is not None:
                        if isinstance(_child, (list, tuple)):
                            _objs_to_patch.extend(_child)
                        else:
                            _objs_to_patch.append(_child)
            for _obj in _objs_to_patch:
                if hasattr(_obj, "_bot"):
                    _obj._bot = actual_bot  # noqa: SLF001

        # 注入 trace_id 到 update 自定义属性(便于业务层关联 receipt)
        # telegram.Update 是 dataclass,允许设置额外属性
        try:
            setattr(update, "_contract_trace_id", trace_id)
        except AttributeError:
            # R76 10.M: 删除 except Exception: pass — 某些 telegram 版本限制属性设置;
            # 不影响主路径,但记录到日志供审计
            logger.debug(
                f"[contract_dispatch] trace_id={trace_id} "
                "cannot set _contract_trace_id on update "
                "(telegram version restriction)"
            )

        # 构造最小 Context
        context = _ContractContext(bot=actual_bot, trace_id=trace_id)

        # R80: 将 trace_id 注入 ContractProviderClient,使后续所有
        # provider-sim 请求携带 X-Trace-Id(receipt 关联必需)
        if hasattr(actual_bot, "_trace_id"):
            actual_bot._trace_id = trace_id

        # R80 P0-02: secretless 模式无 /start 注册流程,预注册用户以确保
        # check_upload_permission 通过(生产模式由 Telegram handler 链注册)
        _user_id = update.effective_user.id if update.effective_user else None
        if _user_id:
            from services.permission import get_or_create_user
            await get_or_create_user(
                _user_id,
                username=update.effective_user.username,
                first_name=update.effective_user.first_name,
            )

        # 选择 dispatcher(默认 _dispatch_media,与 webhook 入口一致)
        if public_dispatcher is None:
            from bots.up_bot import _dispatch_media as public_dispatcher

        # 标记 accepted(dispatcher 即将执行)
        await _contract_transaction_registry.mark_accepted(trace_id)

        # 调度
        await public_dispatcher(update, context)

        # 标记成功
        await _contract_transaction_registry.mark_terminal(
            trace_id=trace_id,
            status="delivered",
            details={
                "dispatcher": getattr(public_dispatcher, "__name__", "dispatcher"),
                "message_id": update.message.message_id,
                "file_id": getattr(update.message.document, "file_id", ""),
            },
        )
    except Exception as e:
        logger.exception(f"[contract_dispatch] trace_id={trace_id} failed")
        # 不暴露内部堆栈到响应体,只记录类型 + 消息
        await _contract_transaction_registry.mark_terminal(
            trace_id=trace_id,
            status="failed",
            error=f"{type(e).__name__}: {e}",
        )


def create_contract_app(
    *,
    contract_token: str,
    public_dispatcher: Optional[Callable[..., Awaitable[Any]]] = None,
    bot: Optional[Any] = None,
) -> FastAPI:
    """构造 secretless contract 入口 FastAPI 应用。

    **仅在 ``SECRETLESS_MODE=true`` 时调用**;production 启动时若调用本函数,
    会抛 ``RuntimeError`` 阻止路由注册(防御性检查)。

    Args:
        contract_token: X-Contract-Token 验证令牌(CI 单次 run 临时令牌)
        public_dispatcher: 可选的 dispatcher 函数(默认 ``bots.up_bot._dispatch_media``)
        bot: 可选的 bot 实例(默认从 ``bots.up_bot._bot`` 读取)

    Returns:
        FastAPI 应用,包含两个端点:
            - ``POST /internal/contract/update``
            - ``GET /internal/contract/transactions/{trace_id}``

    Raises:
        AppError: 在非 SECRETLESS_MODE 环境调用时 (VALIDATION_FAILED)
    """
    # 防御性检查:production 不允许注册 contract 路由
    # 即使调用方错误地在 production 启动流程中调用本函数,也要立即失败
    from config import settings
    if not getattr(settings, "SECRETLESS_MODE", False):
        raise AppError(
            ErrorCodes.VALIDATION_FAILED,
            params={
                "reason": "create_contract_app() cannot be called outside "
                          "SECRETLESS_MODE; production startup must refuse "
                          "to mount contract routes "
                          "(R76 O5 security boundary)"
            },
        )

    if not contract_token:
        raise AppError(
            ErrorCodes.VALIDATION_FAILED,
            params={
                "reason": "contract_token must be a non-empty string for "
                          "create_contract_app()"
            },
        )

    app = FastAPI(
        title="tgjiema Contract Adapter (secretless)",
        version="r76-o5",
        docs_url=None,
        redoc_url=None,
    )

    @app.post("/internal/contract/update")
    async def contract_update(
        request: Request,
        x_contract_token: Optional[str] = Header(None),
    ) -> dict[str, Any]:
        """接收 contract Update,异步调度到公开 dispatcher。

        流程:
            1. 验证 ``X-Contract-Token``;
            2. 校验 payload schema(禁止 SQL/stream key/handler/method 注入);
            3. 注册交易状态(幂等:同 trace_id 重复提交返回当前状态);
            4. 异步调用 ``bots.up_bot._dispatch_media(update, context)``;
            5. 立即返回 ``accepted`` 和 ``trace_id``(不阻塞等待终态)。

        终态通过 ``GET /internal/contract/transactions/{trace_id}`` 查询。
        """
        _verify_contract_token(x_contract_token, contract_token)

        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(
                status_code=400,
                detail=_t("services.web_adapter.s21"),
            )

        update_dict, trace_id = _validate_contract_payload(payload)

        # 注册交易(幂等:已存在则返回当前状态)
        state = await _contract_transaction_registry.register(trace_id)
        if state.status not in ("pending",):
            # 同 trace_id 已被处理过,返回当前状态
            return {
                "trace_id": trace_id,
                "status": state.status,
                "accepted_at": state.accepted_at,
                "completed_at": state.completed_at,
                "details": state.details,
            }

        # 异步调度 dispatcher(不阻塞 HTTP 响应)
        # 注:asyncio.create_task 在 FastAPI/Starlette 事件循环中调度
        asyncio.create_task(
            _dispatch_contract_update(
                update_dict=update_dict,
                trace_id=trace_id,
                public_dispatcher=public_dispatcher,
                bot=bot,
            )
        )

        return {
            "trace_id": trace_id,
            "status": "accepted",
            "accepted_at": state.accepted_at,
        }

    @app.get("/internal/contract/transactions/{trace_id}")
    async def contract_transaction_status(
        trace_id: str,
        x_contract_token: Optional[str] = Header(None),
    ) -> dict[str, Any]:
        """只读聚合状态查询(不推动状态机)。

        返回当前交易状态、各阶段进展和错误描述(若有)。
        本端点不调用任何 worker 函数,不修改交易状态。
        """
        _verify_contract_token(x_contract_token, contract_token)

        state = await _contract_transaction_registry.get(trace_id)
        if state is None:
            raise HTTPException(
                status_code=404,
                detail=_t("services.web_adapter.s22", trace_id=trace_id),
            )
        return {
            "trace_id": state.trace_id,
            "status": state.status,
            "accepted_at": state.accepted_at,
            "completed_at": state.completed_at,
            "details": state.details,
            "error": state.error,
        }

    @app.get("/health")
    async def contract_health() -> dict[str, Any]:
        """secretless contract adapter 健康检查(独立于业务 /health)。"""
        return {
            "status": "ok",
            "service": "contract-adapter",
            "version": "r76-o5",
            "secretless_mode": True,
        }

    return app


def get_contract_transaction_registry() -> ContractTransactionRegistry:
    """获取进程级 contract 交易注册表单例(用于测试断言)。"""
    return _contract_transaction_registry


__all__ = [
    # R64 P1-06: typed JSON response adapter
    "json_response",
    # R76 O5: secretless contract 入口
    "ContractTransactionState",
    "ContractTransactionRegistry",
    "create_contract_app",
    "get_contract_transaction_registry",
]
