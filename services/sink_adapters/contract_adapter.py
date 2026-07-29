"""R76 O2: Contract Provider Adapter(CI/本地 secretless 测试用)。

整改背景(R76 终审报告 10.O-O2 / 10.C):
    secretless CI/本地测试不能访问真实 Telegram Bot API,但需要完整验证:
        1. polling/webhook 公开入口(由 ``web_adapter`` 接收 contract update);
        2. 真实 provider 文件下载(由本 adapter 调用 ``GET /files/{id}/content``);
        3. 生产消费者自然推进(idx/dsp/db_writer 自然消费);
        4. 最终 sendMessage/sendDocument 回执(由本 adapter 调用 provider 出站接口)。

    本 adapter 实现 ``ProviderClient`` 协议,通过 ``httpx.AsyncClient`` 连接本地
    provider 模拟器(``tests/support/provider_simulator.py``):
        - ``PROVIDER_BASE_URL`` 指向 ``http://provider-sim:8088``;
        - 每次请求携带 ``X-Contract-Token``(CI 临时令牌)和 ``X-Trace-Id``;
        - 非 2xx 响应转换为 ``telegram.error`` 兼容异常,业务层 catch 不变。

    **生产边界**:
        - ``PROVIDER_BACKEND == "contract"`` 仅允许 ``APP_ENV in {test, ci, development}``
          (由 ``settings.validate_secretless_mode_constraints`` 强制);
        - 生产镜像不得包含 ``tests/support/provider_simulator.py``;
        - scanner 阻断 ``PROVIDER_BASE_URL`` 指向 simulator 的生产配置。

端点映射(对应 10.C):
    - ``GET  /bot/{token}/getMe``                 → get_me()
    - ``GET  /bot/{token}/getFile?file_id=...``   → get_file()
    - ``GET  /files/{id}/content``                → download_file()
    - ``POST /bot/{token}/sendMessage``           → send_message()
    - ``POST /bot/{token}/sendDocument``          → send_document()

错误转换:
    - HTTP 401            → BadRequest(无效 token)
    - HTTP 429 + Retry-After → RetryAfter(retry_after 秒)
    - HTTP 5xx            → NetworkError(provider 内部错误)
    - 其他非 2xx          → NetworkError(原始 status + body)
    - httpx.ConnectTimeout / ReadTimeout → TimedOut
    - httpx.ConnectError / NetworkError  → NetworkError
"""
from __future__ import annotations

from typing import Any, Optional

import httpx

# telegram.error 异常类型用于业务层 catch 兼容;本模块位于 sink_adapters/ 白名单,
# 允许直接导入 telegram 包。contract adapter 不导入 telegram.Bot,仅导入异常类。
from telegram.error import (
    BadRequest,
    NetworkError,
    RetryAfter,
    TimedOut,
)


# ════════════════════════════════════════════════════════════════
# ProviderFile / ProviderMessage 具体实现
# ════════════════════════════════════════════════════════════════
class ContractProviderFile:
    """Contract provider 文件描述符(对应 ``telegram.File``)。

    Attributes:
        file_id: Provider 返回的 ``file_id``(``sha256:<hex>`` 格式)
        file_unique_id: 文件唯一标识(与 ``file_id`` 相同,contract 模式无独立 unique_id)
        file_size: 文件字节数
        file_path: Provider 内部路径(等于 ``file_id``,``download_file`` 直接使用)
    """

    def __init__(
        self,
        file_id: str,
        file_unique_id: str,
        file_size: int,
        file_path: str,
    ) -> None:
        self.file_id = file_id
        self.file_unique_id = file_unique_id
        self.file_size = file_size
        self.file_path = file_path

    def __repr__(self) -> str:
        return (
            f"ContractProviderFile(file_id={self.file_id!r}, "
            f"file_size={self.file_size}, file_path={self.file_path!r})"
        )


class ContractProviderMessage:
    """Contract provider 出站消息回执(对应 ``telegram.Message``)。

    Attributes:
        message_id: Provider 分配的消息 ID
        chat_id: 目标会话 ID
    """

    def __init__(self, message_id: int, chat_id: int) -> None:
        self.message_id = message_id
        self.chat_id = chat_id

    def __repr__(self) -> str:
        return (
            f"ContractProviderMessage(message_id={self.message_id}, "
            f"chat_id={self.chat_id})"
        )


# ════════════════════════════════════════════════════════════════
# ContractProviderClient
# ════════════════════════════════════════════════════════════════
class ContractProviderClient:
    """连接本地 provider 协议模拟器的 ``ProviderClient`` 实现。

    所有方法均为 ``async``,与 ``telegram.Bot`` 接口签名一致;业务层无需修改 catch 逻辑。

    生命周期:
        - 通过 ``build_provider_client(settings)`` 工厂创建;
        - 内部 ``httpx.AsyncClient`` 在 ``aclose()`` 时关闭;
        - 业务层通常不主动调用 ``aclose()``,由进程退出回收。

    安全:
        - 不读写真实 Telegram Bot Token;
        - ``contract_token`` 为 CI 单次 run 临时令牌(``openssl rand -hex 16``);
        - 所有请求需携带 ``X-Contract-Token``,simulator 验证失败返回 401。
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        contract_token: str,
        *,
        timeout: float = 30.0,
        trace_id: Optional[str] = None,
    ) -> None:
        """初始化 Contract Provider Client。

        Args:
            base_url: Provider 模拟器 base URL(如 ``http://provider-sim:8088``)
            token: Bot token(对应 ``/bot/{token}/...`` 路径),secretless CI 用 ``ci-local-token``
            contract_token: ``X-Contract-Token`` 头部值(CI 单次 run 临时令牌)
            timeout: HTTP 请求超时(秒),默认 30.0
            trace_id: 可选的初始 trace ID(每次请求会更新 ``X-Trace-Id`` 头)
        """
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._contract_token = contract_token
        self._trace_id = trace_id
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
        )

    # ── 内部辅助 ────────────────────────────────────────────────
    def _headers(self, trace_id: Optional[str] = None) -> dict[str, str]:
        """构造请求头(携带 contract token 和 trace ID)。"""
        headers = {"X-Contract-Token": self._contract_token}
        tid = trace_id or self._trace_id
        if tid:
            headers["X-Trace-Id"] = tid
        return headers

    def _bot_url(self, method: str) -> str:
        """构造 ``/bot/{token}/{method}`` 路径。"""
        return f"/bot/{self._token}/{method}"

    def _raise_for_status(self, response: httpx.Response) -> None:
        """把非 2xx HTTP 响应转换为 ``telegram.error`` 兼容异常。

        Args:
            response: httpx 响应对象

        Raises:
            RetryAfter: HTTP 429 + ``Retry-After`` 头部
            BadRequest: HTTP 401/400/403/404(客户端错误)
            NetworkError: HTTP 5xx 或其他非 2xx
        """
        status = response.status_code
        if 200 <= status < 300:
            return

        body_text = response.text
        # 429 + Retry-After
        if status == 429:
            retry_after_raw = response.headers.get("Retry-After", "1")
            try:
                retry_after = float(retry_after_raw)
            except (ValueError, TypeError):
                retry_after = 1.0
            raise RetryAfter(retry_after=retry_after)

        # 4xx 客户端错误
        if status in (400, 401, 403, 404):
            raise BadRequest(
                f"contract provider returned {status}: {body_text[:200]}"
            )

        # 5xx 或其他
        raise NetworkError(
            f"contract provider returned {status}: {body_text[:200]}"
        )

    # ── ProviderClient 协议实现 ─────────────────────────────────
    async def get_me(self) -> dict[str, Any]:
        """``GET /bot/{token}/getMe``。

        Returns:
            Provider 身份字典(``{"id": ..., "username": ..., "first_name": ...}``)
        """
        try:
            response = await self._client.get(
                self._bot_url("getMe"),
                headers=self._headers(),
            )
        except httpx.ConnectTimeout as e:
            raise TimedOut(f"contract provider connect timeout: {e}") from e
        except httpx.ReadTimeout as e:
            raise TimedOut(f"contract provider read timeout: {e}") from e
        except (httpx.ConnectError, httpx.NetworkError) as e:
            raise NetworkError(f"contract provider connect error: {e}") from e

        self._raise_for_status(response)
        data = response.json()
        if not data.get("ok", False):
            raise NetworkError(
                f"contract provider returned non-ok payload: {data}"
            )
        return data.get("result", {})

    async def get_file(self, file_id: str) -> ContractProviderFile:
        """``GET /bot/{token}/getFile?file_id=...``。

        Args:
            file_id: Provider 返回的 file_id(``sha256:<hex>``)

        Returns:
            ``ContractProviderFile`` 实例,``file_path`` 等于 ``file_id``
        """
        try:
            response = await self._client.get(
                self._bot_url("getFile"),
                params={"file_id": file_id},
                headers=self._headers(),
            )
        except httpx.ConnectTimeout as e:
            raise TimedOut(f"contract provider connect timeout: {e}") from e
        except httpx.ReadTimeout as e:
            raise TimedOut(f"contract provider read timeout: {e}") from e
        except (httpx.ConnectError, httpx.NetworkError) as e:
            raise NetworkError(f"contract provider connect error: {e}") from e

        self._raise_for_status(response)
        data = response.json()
        if not data.get("ok", False):
            raise NetworkError(
                f"contract provider returned non-ok payload: {data}"
            )
        result = data.get("result", {})
        # contract simulator 返回 file_id / file_unique_id / file_size / file_path
        # file_path 用于后续 download_file,在 contract 模式下等于 file_id
        file_path = result.get("file_path") or file_id
        return ContractProviderFile(
            file_id=result.get("file_id", file_id),
            file_unique_id=result.get("file_unique_id", file_id),
            file_size=int(result.get("file_size", 0)),
            file_path=file_path,
        )

    async def download_file(self, file_path: str) -> bytes:
        """``GET /files/{id}/content``。

        Args:
            file_path: ``get_file()`` 返回的 ``file_path``(contract 模式下等于 file_id)

        Returns:
            文件原始字节流
        """
        # file_path 在 contract 模式下等于 ``sha256:<hex>``,直接作为 path 段
        # 若 file_path 含 ``/`` 截取最后一段避免路径注入
        safe_id = file_path.rsplit("/", 1)[-1]
        url = f"/files/{safe_id}/content"
        try:
            response = await self._client.get(url, headers=self._headers())
        except httpx.ConnectTimeout as e:
            raise TimedOut(f"contract provider connect timeout: {e}") from e
        except httpx.ReadTimeout as e:
            raise TimedOut(f"contract provider read timeout: {e}") from e
        except (httpx.ConnectError, httpx.NetworkError) as e:
            raise NetworkError(f"contract provider connect error: {e}") from e

        self._raise_for_status(response)
        return response.content

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        **kwargs: Any,
    ) -> ContractProviderMessage:
        """``POST /bot/{token}/sendMessage``。

        Args:
            chat_id: 目标会话 ID
            text: 消息文本
            **kwargs: 透传给 provider 的额外参数(``reply_markup`` / ``parse_mode``)

        Returns:
            ``ContractProviderMessage`` 出站回执
        """
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
        }
        # 透传 reply_markup / parse_mode 等
        # R80: 过滤 None 和 PTB DefaultValue 哨兵(Message.reply_text 透传)
        for k, v in kwargs.items():
            if v is not None and type(v).__name__ != "DefaultValue":
                # telegram 对象(InlineKeyboardMarkup 等)需 to_dict() 序列化
                payload[k] = v.to_dict() if hasattr(v, "to_dict") else v

        try:
            response = await self._client.post(
                self._bot_url("sendMessage"),
                json=payload,
                headers=self._headers(),
            )
        except httpx.ConnectTimeout as e:
            raise TimedOut(f"contract provider connect timeout: {e}") from e
        except httpx.ReadTimeout as e:
            raise TimedOut(f"contract provider read timeout: {e}") from e
        except (httpx.ConnectError, httpx.NetworkError) as e:
            raise NetworkError(f"contract provider connect error: {e}") from e

        self._raise_for_status(response)
        data = response.json()
        if not data.get("ok", False):
            raise NetworkError(
                f"contract provider returned non-ok payload: {data}"
            )
        result = data.get("result", {})
        return ContractProviderMessage(
            message_id=int(result.get("message_id", 0)),
            chat_id=int(result.get("chat", {}).get("id", chat_id)),
        )

    async def send_document(
        self,
        *,
        chat_id: int,
        document: Any,
        **kwargs: Any,
    ) -> ContractProviderMessage:
        """``POST /bot/{token}/sendDocument``。

        Args:
            chat_id: 目标会话 ID
            document: 文档对象(file_id 字符串 / file stream / InputFile)
            **kwargs: 透传给 provider 的额外参数(``caption`` / ``parse_mode``)

        Returns:
            ``ContractProviderMessage`` 出站回执
        """
        # document 可能是 file_id 字符串、bytes 或 InputFile;统一序列化
        # contract simulator 接受 JSON,document 字段以字符串形式传递
        if isinstance(document, (bytes, bytearray)):
            # 真实 bytes 不直接传输,需上传到 /files 后以 file_id 引用
            # 此处仅在测试中接收 file_id 字符串;bytes 上传由 upload_fixture 完成
            raise BadRequest(
                "send_document does not accept raw bytes; "
                "upload via /files first and pass file_id"
            )

        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "document": document,
        }
        for k, v in kwargs.items():
            if v is not None and type(v).__name__ != "DefaultValue":
                payload[k] = v.to_dict() if hasattr(v, "to_dict") else v

        try:
            response = await self._client.post(
                self._bot_url("sendDocument"),
                json=payload,
                headers=self._headers(),
            )
        except httpx.ConnectTimeout as e:
            raise TimedOut(f"contract provider connect timeout: {e}") from e
        except httpx.ReadTimeout as e:
            raise TimedOut(f"contract provider read timeout: {e}") from e
        except (httpx.ConnectError, httpx.NetworkError) as e:
            raise NetworkError(f"contract provider connect error: {e}") from e

        self._raise_for_status(response)
        data = response.json()
        if not data.get("ok", False):
            raise NetworkError(
                f"contract provider returned non-ok payload: {data}"
            )
        result = data.get("result", {})
        return ContractProviderMessage(
            message_id=int(result.get("message_id", 0)),
            chat_id=int(result.get("chat", {}).get("id", chat_id)),
        )

    # ── 资源管理 ────────────────────────────────────────────────
    async def aclose(self) -> None:
        """关闭内部 httpx client。"""
        await self._client.aclose()

    async def __aenter__(self) -> "ContractProviderClient":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.aclose()


__all__ = [
    "ContractProviderFile",
    "ContractProviderMessage",
    "ContractProviderClient",
]
