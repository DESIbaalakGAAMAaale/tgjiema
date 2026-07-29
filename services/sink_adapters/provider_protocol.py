"""R76 O2: Provider 抽象协议(可替换 Telegram 依赖)。

整改背景(R76 终审报告 10.O-O2 / P0-03):
    业务模块(bots/、services/)此前直接依赖 ``telegram.Bot`` / ``Application.builder()``,
    在测试中通过 monkey patch ``bot.get_file`` / ``bot.download_file`` 冒充真实 Provider,
    导致:
        1. 未证明 polling/webhook 入口、真实 provider 下载、生产消费者自然推进;
        2. 测试代码通过 ``from bots.up_bot import _dispatch_media`` 私有 handler 直调,
           绕过公开入口;
        3. 文件内容直接来自 Update 中的 ``_e2e_file_content_b64``,而非 provider 真实下载。

    本模块定义与具体实现无关的 Provider 协议(``ProviderClient``),业务层通过协议
    依赖 Provider,运行时由 ``build_provider_client(settings)`` 工厂注入:
        - ``telegram`` 分支: 真实 Telegram Bot 包装器(生产);
        - ``contract`` 分支: 本地协议模拟器 adapter(CI/本地 secretless 测试)。

    协议设计原则:
        - 仅定义业务真正调用的方法子集(get_me / get_file / download_file /
          send_message / send_document),不强行复刻 telegram.Bot 全部 API;
        - 文件下载分两步: ``get_file(file_id)`` 返回 ``ProviderFile`` 描述符,
          ``download_file(file_path)`` 返回原始 bytes,与 Telegram 官方 API 一致;
        - 出错时抛 ``telegram.error`` 兼容异常(``RetryAfter`` / ``NetworkError``
          / ``TimedOut``),保持业务层 catch 不变;contract adapter 内部把 HTTP
          非 2xx 转换为等价异常。

使用示例:
    from services.sink_adapters.provider_protocol import ProviderClient
    from services.sink_adapters.telegram_adapter import build_provider_client

    client: ProviderClient = build_provider_client(settings)
    provider_file = await client.get_file(file_id)
    content = await client.download_file(provider_file.file_path)

禁止:
    - 业务层自行 ``Application.builder().token(...).build()`` 选择测试实现;
    - 业务层直接 ``from telegram import Bot`` 构造 client;
    - 把 contract adapter 包装为 ``telegram.Bot`` 子类伪装真实 Telegram。
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


# ════════════════════════════════════════════════════════════════
# Provider 协议定义
# ════════════════════════════════════════════════════════════════
@runtime_checkable
class ProviderFile(Protocol):
    """Provider 文件描述符(对应 telegram.File)。

    Attributes:
        file_id: Provider 返回的文件 ID( Telegram file_id 或 contract ``sha256:<hex>``)
        file_unique_id: 文件唯一标识(用于去重,同一文件不同 file_id 共享 file_unique_id)
        file_size: 文件字节数(可能为 0,Provider 不一定返回)
        file_path: Provider 内部相对路径,``download_file(file_path)`` 用于下载
    """

    file_id: str
    file_unique_id: str
    file_size: int
    file_path: str


@runtime_checkable
class ProviderMessage(Protocol):
    """Provider 出站消息回执(对应 telegram.Message)。

    Attributes:
        message_id: Provider 分配的消息 ID
        chat_id: 目标会话 ID
    """

    message_id: int
    chat_id: int


@runtime_checkable
class ProviderClient(Protocol):
    """Provider 客户端协议(对应 telegram.Bot 子集)。

    业务层依赖此协议而非具体 ``telegram.Bot``;运行时由
    ``build_provider_client(settings)`` 注入:

        - ``settings.PROVIDER_BACKEND == "telegram"`` → TelegramProviderClient
          (内部包装 ``telegram.Bot``,生产真实使用)
        - ``settings.PROVIDER_BACKEND == "contract"`` → ContractProviderClient
          (连接 ``PROVIDER_BASE_URL`` 协议模拟器,仅 CI/本地测试)

    所有方法必须为 ``async``;非 2xx 响应、连接错误、超时由具体实现转换为
    ``telegram.error`` 兼容异常(``RetryAfter`` / ``NetworkError`` / ``TimedOut``),
    业务层 catch 保持不变。
    """

    async def get_me(self) -> Any:
        """返回 Provider Bot 身份(对应 ``bot.get_me()``)。

        Returns:
            Provider 身份对象(包含 ``id`` / ``username`` / ``first_name`` 等字段)
        """
        ...

    async def get_file(self, file_id: str) -> ProviderFile:
        """获取文件描述符(对应 ``bot.get_file(file_id)``)。

        Args:
            file_id: Provider 返回的 file_id

        Returns:
            ``ProviderFile`` 实例,``file_path`` 用于后续 ``download_file``
        """
        ...

    async def download_file(self, file_path: str) -> bytes:
        """下载文件内容(对应 ``bot.download_file(file_path)``)。

        Args:
            file_path: ``get_file()`` 返回的 ``file_path``

        Returns:
            文件原始字节流
        """
        ...

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        **kwargs: Any,
    ) -> ProviderMessage:
        """发送文本消息(对应 ``bot.send_message(chat_id=..., text=...)``)。

        Args:
            chat_id: 目标会话 ID
            text: 消息文本
            **kwargs: 透传给 Provider 的额外参数(如 ``reply_markup`` / ``parse_mode``)

        Returns:
            ``ProviderMessage`` 出站回执(包含 ``message_id``)
        """
        ...

    async def send_document(
        self,
        *,
        chat_id: int,
        document: Any,
        **kwargs: Any,
    ) -> ProviderMessage:
        """发送文档(对应 ``bot.send_document(chat_id=..., document=...)``)。

        Args:
            chat_id: 目标会话 ID
            document: 文档(file_id / file stream / InputFile)
            **kwargs: 透传给 Provider 的额外参数(如 ``caption`` / ``parse_mode``)

        Returns:
            ``ProviderMessage`` 出站回执(包含 ``message_id``)
        """
        ...


# ════════════════════════════════════════════════════════════════
# 后端标识常量
# ════════════════════════════════════════════════════════════════
# 与 settings.PROVIDER_BACKEND 字段值对应
PROVIDER_BACKEND_TELEGRAM = "telegram"
PROVIDER_BACKEND_CONTRACT = "contract"

# 合法后端白名单(用于 build_provider_client 校验)
VALID_PROVIDER_BACKENDS = frozenset({
    PROVIDER_BACKEND_TELEGRAM,
    PROVIDER_BACKEND_CONTRACT,
})


__all__ = [
    "ProviderFile",
    "ProviderMessage",
    "ProviderClient",
    "PROVIDER_BACKEND_TELEGRAM",
    "PROVIDER_BACKEND_CONTRACT",
    "VALID_PROVIDER_BACKENDS",
]
