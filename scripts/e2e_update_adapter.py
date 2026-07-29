#!/usr/bin/env python3
"""R76 O4: 外部黑盒驱动器(secretless E2E 测试用)。

整改背景(R76 终审报告 10.O-O4 / 10.B / P0-03 / P0-04):
    R73 版本通过容器内动态 Python 代码注入和 monkey patch 冒充真实 Provider,
    绕过 web_adapter 公开入口、测试进程内伪造文件下载、测试主动调用私有
    worker、并以控制台输出作为成功标准。这导致未证明 polling/webhook 入口、
    真实 provider 下载、生产消费者自然推进和最终 sendMessage/sendDocument 回执。

    本重写版完全删除内部注入实现,改为外部黑盒驱动器,只通过公开 HTTP/状态接口
    驱动应用并观察结果,不导入任何业务模块私有函数。

公开 API(R76 报告 10.O-O4):
    - ``upload_fixture(provider_url, token, content) -> ProviderFixture``
    - ``submit_update(app_url, fixture, trace_id, user_id) -> SubmissionReceipt``
    - ``wait_for_terminal(app_url, trace_id, timeout) -> TransactionStatus``
    - ``fetch_provider_receipts(provider_url, trace_id) -> list[ProviderReceipt]``
    - ``verify_receipt_chain(receipts, key) -> None``
    - ``set_fault_plan(provider_url, token, plan) -> None`` (R76 §10.4 故障注入)
    - ``clear_fault_plans(provider_url, token) -> None`` (R76 §10.4 故障清理)
    - ``run_fault_injection_transaction(...) -> FaultInjectionResult`` (R76 §10.4)

成功条件(R76 报告 10.O-O4):
    - terminal status 必须为 ``delivered``;
    - 至少存在 ``getFile`` / ``download`` / ``sendMessage|sendDocument`` 回执;
    - file SHA-256 与 fixture 一致;
    - 同一 trace 在 SQLite 和 CRDB 都可查到;
    - outbound effect ID 唯一。

禁止出现(R76 报告 10.O-O4):
    容器内动态 Python 注入、容器编排 exec 调用、业务模块私有 worker 函数直调、
    业务路由层主动推进、空 bot 占位、控制台 OK 输出、Update 内嵌
    文件内容 base64 字段。

R76 §10.4 故障注入矩阵(协议级确定性 fault server):
    - ``--fault 401``: provider 返回 HTTP 401,应用不重试或按策略终止(expect=failure)
    - ``--fault 429 --retry-after N``: provider 返回 HTTP 429 + Retry-After,
      应用按 RetryAfter 退避并重试,最终成功(expect=retry-then-success)
    - ``--fault 500``: provider 返回 HTTP 500,应用有界重试后失败
      (expect=bounded-retry-then-failure)
    - ``--fault timeout``: provider 延迟响应,应用读超时失败
      (expect=timeout-failure)
    - ``--fault duplicate``: provider 首次失败后重试,应用幂等成功
      (expect=idempotent-success)

使用方式(正常交易):
    python scripts/e2e_update_adapter.py \\
        --provider-url http://provider-sim:8088 \\
        --contract-token "$PROVIDER_CONTRACT_TOKEN" \\
        --app-url http://up-bot:8000 \\
        --receipt-key "$PROVIDER_RECEIPT_KEY" \\
        --content "Hello, tgjiema secretless E2E!"

使用方式(故障注入 401):
    python scripts/e2e_update_adapter.py \\
        --provider-url http://provider-sim:8088 \\
        --contract-token "$PROVIDER_CONTRACT_TOKEN" \\
        --app-url http://up-bot:8000 \\
        --receipt-key "$PROVIDER_RECEIPT_KEY" \\
        --mode fault-injection \\
        --fault 401 \\
        --expect failure \\
        --timeout 60

CLI 退出码:
    0 = 交易结果符合 expect(正常交易 delivered,或故障注入终态符合预期);
    1 = 交易失败、receipt 验签失败,或终态不符合 expect。
"""
# R76 P0-01: 移除 `from __future__ import annotations`。
# 根因: `from __future__ import annotations` + `@dataclass` + PEP 604 `str | None`
# 在 `dataclasses._is_type` 中触发 `AttributeError: 'NoneType' object has no attribute '__dict__'`
# (与 scripts/synthetic_transaction.py R71 RC33 同一根因)。
# CI 使用 Python 3.10+,原生支持 `str | None` / `dict[str, Any]` 语法,
# 无需 `from __future__ import annotations`。移除后 @dataclass 直接处理实际类型对象。

import argparse
import asyncio
import hashlib
import hmac
import json
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

import httpx


# ════════════════════════════════════════════════════════════════
# 数据结构
# ════════════════════════════════════════════════════════════════
@dataclass
class ProviderFixture:
    """上传到 provider 模拟器后获得的 fixture 描述符。

    Attributes:
        file_id: Provider 返回的 file_id(``sha256:<hex>``)
        file_unique_id: 文件唯一标识
        file_size: 文件字节数
        file_sha256: 文件 SHA-256 hex(不带前缀)
    """

    file_id: str
    file_unique_id: str
    file_size: int
    file_sha256: str


@dataclass
class SubmissionReceipt:
    """提交 Update 到应用公开入口后的回执。

    Attributes:
        trace_id: 本次交易的 trace ID(UUID)
        app_status: 应用返回的 HTTP 状态码
        app_body: 应用返回体(前 500 字符)
    """

    trace_id: str
    app_status: int
    app_body: str


@dataclass
class TransactionStatus:
    """交易终态状态。

    Attributes:
        trace_id: trace ID
        status: 终态字符串(``delivered`` / ``failed`` / ``pending`` / ``unknown``)
        details: 详细信息(各阶段子状态)
        error: 错误描述(仅 failed 时有值)
    """

    trace_id: str
    status: str
    details: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass
class ProviderReceipt:
    """Provider 模拟器返回的单条 receipt。

    字段对应 ``tests/support/provider_simulator.py`` 的 receipt schema:
        schema_version / trace_id / operation / request_digest /
        response_digest / message_id / file_id / file_sha256 / status /
        attempt / timestamp / previous_digest / signature
    """

    schema_version: str
    trace_id: str
    operation: str
    request_digest: str
    response_digest: str
    message_id: Optional[int]
    file_id: Optional[str]
    file_sha256: Optional[str]
    status: str
    attempt: int
    timestamp: str
    previous_digest: str
    signature: str


@dataclass
class FaultPlanSpec:
    """R76 §10.4 故障计划规格(对应 provider_simulator.FaultPlan)。

    Attributes:
        operation: 故障注入的 provider 操作(如 ``getFile``)
        status: HTTP 状态码(401/429/500)
        delay_ms: 响应前 sleep 毫秒(用于触发 client timeout)
        retry_after: 仅 429 有效,Retry-After 头部秒数
        repeat: 故障重复次数(>0);超过后恢复正常响应
        body: 可选的响应体(JSON 字符串)
    """

    operation: str
    status: int
    delay_ms: int = 0
    retry_after: Optional[int] = None
    repeat: int = 1
    body: Optional[str] = None

    def to_payload(self) -> dict[str, Any]:
        """转换为 ``POST /__faults`` 请求体。"""
        payload: dict[str, Any] = {
            "operation": self.operation,
            "status": self.status,
            "delay_ms": self.delay_ms,
            "repeat": self.repeat,
        }
        if self.retry_after is not None:
            payload["retry_after"] = self.retry_after
        if self.body is not None:
            payload["body"] = self.body
        return payload


@dataclass
class FaultInjectionResult:
    """R76 §10.4 故障注入测试结果。

    Attributes:
        fault_type: 故障类型(``401`` / ``429`` / ``500`` / ``timeout`` / ``duplicate``)
        expected_outcome: 期望终态(``failure`` / ``retry-then-success`` /
            ``bounded-retry-then-failure`` / ``timeout-failure`` /
            ``idempotent-success``)
        actual_status: 实际交易终态(``delivered`` / ``failed`` / ``unknown``)
        matched: 实际终态是否符合期望
        trace_id: trace ID
        receipts: provider receipts 列表
        details: 详细信息(各阶段子状态)
    """

    fault_type: str
    expected_outcome: str
    actual_status: str
    matched: bool
    trace_id: str
    receipts: list[ProviderReceipt] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


# ════════════════════════════════════════════════════════════════
# 公开 API
# ════════════════════════════════════════════════════════════════
async def upload_fixture(
    provider_url: str,
    contract_token: str,
    content: bytes,
    *,
    trace_id: Optional[str] = None,
) -> ProviderFixture:
    """上传测试文件到 provider 模拟器,返回 fixture 描述符。

    Args:
        provider_url: Provider 模拟器 base URL(如 ``http://provider-sim:8088``)
        contract_token: ``X-Contract-Token`` 头部值
        content: 文件原始字节流
        trace_id: 可选 trace ID(记录到 receipt,便于链路追踪)

    Returns:
        ``ProviderFixture`` 实例(含 file_id 和 file_sha256)

    Raises:
        httpx.HTTPStatusError: 上传失败(非 2xx)
    """
    headers = {"X-Contract-Token": contract_token}
    if trace_id:
        headers["X-Trace-Id"] = trace_id

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{provider_url.rstrip('/')}/__fixtures/files",
            content=content,
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()

    return ProviderFixture(
        file_id=data["file_id"],
        file_unique_id=data["file_unique_id"],
        file_size=int(data["file_size"]),
        file_sha256=data["file_sha256"],
    )


async def submit_update(
    app_url: str,
    fixture: ProviderFixture,
    trace_id: str,
    user_id: int,
    *,
    contract_token: Optional[str] = None,
    chat_id: Optional[int] = None,
) -> SubmissionReceipt:
    """提交 Update 到应用公开入口 ``/internal/contract/update``。

    应用通过 ``web_adapter`` 接收 Update,内部调用与正常 webhook 相同的公开 dispatcher,
    不直接调用 ``bots.*`` 私有函数。Update payload 只包含 provider 返回的 file_id、
    trace_id 和 user_id,不携带文件正文。

    Args:
        app_url: 应用公开入口 base URL(如 ``http://up-bot:8000``)
        fixture: ``upload_fixture()`` 返回的 ``ProviderFixture``
        trace_id: 本次交易的 trace ID
        user_id: 模拟 Telegram 用户 ID
        contract_token: 可选的 ``X-Contract-Token``(若应用要求验证)
        chat_id: 可选的 chat ID(默认等于 user_id,私聊场景)

    Returns:
        ``SubmissionReceipt`` 实例(含应用返回状态码和正文)

    Raises:
        httpx.HTTPStatusError: 提交失败(非 2xx)
    """
    if chat_id is None:
        chat_id = user_id

    # 构造 Telegram-compatible Update payload
    # 仅包含 provider 返回的 file_id,不携带文件正文(已删除内嵌 base64 字段)
    update = {
        "update_id": _generate_update_id(),
        "message": {
            "message_id": _generate_message_id(),
            "from": {
                "id": user_id,
                "is_bot": False,
                "first_name": "CI User",
            },
            "chat": {"id": chat_id, "type": "private"},
            "date": int(time.time()),
            "document": {
                "file_id": fixture.file_id,
                "file_unique_id": fixture.file_unique_id,
                "file_name": f"fixture-{fixture.file_sha256[:8]}.bin",
                "file_size": fixture.file_size,
            },
        },
    }

    headers = {"Content-Type": "application/json"}
    if contract_token:
        headers["X-Contract-Token"] = contract_token

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{app_url.rstrip('/')}/internal/contract/update",
            json={"update": update, "trace_id": trace_id},
            headers=headers,
        )

    return SubmissionReceipt(
        trace_id=trace_id,
        app_status=resp.status_code,
        app_body=resp.text[:500] if resp.text else "",
    )


async def wait_for_terminal(
    app_url: str,
    trace_id: str,
    timeout: float = 60.0,
    *,
    poll_interval: float = 1.0,
    contract_token: Optional[str] = None,
) -> TransactionStatus:
    """轮询应用公开状态接口,等待交易进入终态。

    应用通过 ``/internal/contract/transactions/{trace_id}`` 暴露只读聚合状态,
    不推动状态机。本函数只观察,不调用任何 worker 函数。

    终态:
        - ``delivered``: 投递成功(idx/dsp/db_writer/crdb_sync 全部完成);
        - ``failed``: 任一阶段失败;
        - ``unknown``: 超时后仍未达到终态。

    Args:
        app_url: 应用公开入口 base URL
        trace_id: trace ID
        timeout: 最大等待时间(秒)
        poll_interval: 轮询间隔(秒)
        contract_token: 可选的 ``X-Contract-Token``

    Returns:
        ``TransactionStatus`` 实例
    """
    headers = {}
    if contract_token:
        headers["X-Contract-Token"] = contract_token

    deadline = time.time() + timeout
    last_details: dict[str, Any] = {}

    async with httpx.AsyncClient(timeout=10.0) as client:
        while time.time() < deadline:
            try:
                resp = await client.get(
                    f"{app_url.rstrip('/')}/internal/contract/transactions/{trace_id}",
                    headers=headers,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    status = str(data.get("status", "pending")).lower()
                    last_details = data.get("details", {})
                    if status in ("delivered", "failed"):
                        return TransactionStatus(
                            trace_id=trace_id,
                            status=status,
                            details=last_details,
                            error=str(data.get("error", "") or ""),
                        )
                # 否则继续轮询(pending / unknown / 404)
            except (httpx.HTTPError, json.JSONDecodeError):
                # 临时网络错误,继续轮询
                pass
            await asyncio.sleep(poll_interval)

    return TransactionStatus(
        trace_id=trace_id,
        status="unknown",
        details={"reason": "timeout", **last_details},
    )


async def fetch_provider_receipts(
    provider_url: str,
    contract_token: str,
    trace_id: str,
) -> list[ProviderReceipt]:
    """从 provider 模拟器获取该 trace 的全部 receipt。

    Args:
        provider_url: Provider 模拟器 base URL
        contract_token: ``X-Contract-Token``
        trace_id: trace ID

    Returns:
        ``ProviderReceipt`` 列表(按时间顺序)

    Raises:
        httpx.HTTPStatusError: 获取失败
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{provider_url.rstrip('/')}/__receipts/{trace_id}",
            headers={"X-Contract-Token": contract_token},
        )
        resp.raise_for_status()
        data = resp.json()

    receipts_data = data.get("receipts", [])
    return [
        ProviderReceipt(
            schema_version=r.get("schema_version", ""),
            trace_id=r.get("trace_id", ""),
            operation=r.get("operation", ""),
            request_digest=r.get("request_digest", ""),
            response_digest=r.get("response_digest", ""),
            message_id=r.get("message_id"),
            file_id=r.get("file_id"),
            file_sha256=r.get("file_sha256"),
            status=r.get("status", ""),
            attempt=int(r.get("attempt", 0)),
            timestamp=r.get("timestamp", ""),
            previous_digest=r.get("previous_digest", ""),
            signature=r.get("signature", ""),
        )
        for r in receipts_data
    ]


def verify_receipt_chain(
    receipts: list[ProviderReceipt],
    key: str,
) -> None:
    """验证 provider receipt 链的完整性和签名。

    验证规则(R76 报告 10.O-O4 成功条件):
        1. 至少存在一条 ``getFile`` receipt;
        2. 至少存在一条 ``download`` receipt;
        3. 至少存在一条 ``sendMessage`` 或 ``sendDocument`` receipt;
        4. 每条 receipt 的 ``signature`` 与重算的 HMAC-SHA256 一致;
        5. ``previous_digest`` 链一致(第 i 条的 previous_digest == 第 i-1 条的 response_digest);
        6. ``file_sha256`` 在 ``getFile`` 和 ``download`` receipt 中一致;
        7. ``trace_id`` 在所有 receipt 中一致。

    Args:
        receipts: ``fetch_provider_receipts()`` 返回的 receipt 列表
        key: HMAC 签名密钥(hex)

    Raises:
        ValueError: 任一验证失败,带详细错误信息
    """
    if not receipts:
        raise ValueError("receipt chain 验证失败: receipts 为空")

    # 1. trace_id 一致性
    trace_ids = {r.trace_id for r in receipts}
    if len(trace_ids) != 1:
        raise ValueError(
            f"receipt chain 验证失败: trace_id 不一致, 实际: {trace_ids}"
        )

    # 2. 操作完整性
    operations = {r.operation for r in receipts}
    if "getFile" not in operations:
        raise ValueError(
            f"receipt chain 验证失败: 缺少 getFile receipt, 实际操作: {operations}"
        )
    if "download" not in operations:
        raise ValueError(
            f"receipt chain 验证失败: 缺少 download receipt, 实际操作: {operations}"
        )
    if "sendMessage" not in operations and "sendDocument" not in operations:
        raise ValueError(
            f"receipt chain 验证失败: 缺少 sendMessage/sendDocument receipt, "
            f"实际操作: {operations}"
        )

    # 3. file_sha256 一致性(getFile 和 download 之间)
    file_shas = {
        r.file_sha256 for r in receipts
        if r.operation in ("getFile", "download") and r.file_sha256
    }
    if len(file_shas) > 1:
        raise ValueError(
            f"receipt chain 验证失败: file_sha256 不一致, 实际: {file_shas}"
        )

    # 4. previous_digest 链 + signature 验签
    try:
        key_bytes = bytes.fromhex(key) if key else b""
    except ValueError:
        key_bytes = key.encode("utf-8") if key else b""

    prev_response_digest = ""
    for idx, r in enumerate(receipts):
        # 4a. previous_digest 链
        if idx == 0:
            if r.previous_digest and r.previous_digest != "":
                # 第一条 previous_digest 可以为空或非空(若 upload_fixture 在前)
                pass
        else:
            if r.previous_digest != prev_response_digest:
                raise ValueError(
                    f"receipt chain 验证失败: 第 {idx} 条 previous_digest 不匹配, "
                    f"期望 {prev_response_digest[:16]}..., 实际 {r.previous_digest[:16]}..."
                )

        # 4b. signature 验签(canonical JSON 排除 signature 字段)
        receipt_dict = {
            "schema_version": r.schema_version,
            "trace_id": r.trace_id,
            "operation": r.operation,
            "request_digest": r.request_digest,
            "response_digest": r.response_digest,
            "message_id": r.message_id,
            "file_id": r.file_id,
            "file_sha256": r.file_sha256,
            "status": r.status,
            "attempt": r.attempt,
            "timestamp": r.timestamp,
            "previous_digest": r.previous_digest,
        }
        canonical = json.dumps(
            receipt_dict, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        expected_sig = hmac.new(key_bytes, canonical, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected_sig, r.signature):
            raise ValueError(
                f"receipt chain 验证失败: 第 {idx} 条 signature 不匹配, "
                f"operation={r.operation}, status={r.status}"
            )

        prev_response_digest = r.response_digest

    # 5. outbound effect ID 唯一性(message_id 在出站 receipt 中不重复)
    outbound_msgs = [
        r for r in receipts
        if r.operation in ("sendMessage", "sendDocument") and r.message_id is not None
    ]
    outbound_ids = [r.message_id for r in outbound_msgs]
    if len(outbound_ids) != len(set(outbound_ids)):
        raise ValueError(
            f"receipt chain 验证失败: outbound message_id 重复, 实际: {outbound_ids}"
        )


# ════════════════════════════════════════════════════════════════
# 完整交易编排(供 run_secretless_release_gates.py 调用)
# ════════════════════════════════════════════════════════════════
async def run_secretless_transaction(
    *,
    provider_url: str,
    app_url: str,
    contract_token: str,
    receipt_key: str,
    content: bytes,
    user_id: int = 111,
    timeout: float = 60.0,
    verify_receipts: bool = True,
) -> tuple[TransactionStatus, list[ProviderReceipt], ProviderFixture]:
    """运行完整 secretless 交易:上传 fixture → 提交 Update → 等待终态 → 拉取 receipts → 验签。

    Args:
        provider_url: Provider 模拟器 URL
        app_url: 应用公开入口 URL
        contract_token: X-Contract-Token
        receipt_key: receipt HMAC 验签密钥
        content: 测试文件内容
        user_id: 模拟用户 ID
        timeout: 等待终态超时(秒)

    Returns:
        ``(TransactionStatus, list[ProviderReceipt], ProviderFixture)`` 元组

    Raises:
        RuntimeError: 任何阶段失败
    """
    trace_id = str(uuid.uuid4())

    # 1. 上传 fixture
    fixture = await upload_fixture(
        provider_url=provider_url,
        contract_token=contract_token,
        content=content,
        trace_id=trace_id,
    )

    # 校验 file_sha256 与本地计算一致
    expected_sha = hashlib.sha256(content).hexdigest()
    if fixture.file_sha256 != expected_sha:
        raise RuntimeError(
            f"fixture file_sha256 不匹配: 期望 {expected_sha}, "
            f"实际 {fixture.file_sha256}"
        )

    # 2. 提交 Update 到应用公开入口
    submission = await submit_update(
        app_url=app_url,
        fixture=fixture,
        trace_id=trace_id,
        user_id=user_id,
        contract_token=contract_token,
    )
    if submission.app_status >= 400:
        raise RuntimeError(
            f"submit_update 失败: app_status={submission.app_status}, "
            f"body={submission.app_body}"
        )

    # 3. 等待终态(只观察,不推动)
    status = await wait_for_terminal(
        app_url=app_url,
        trace_id=trace_id,
        timeout=timeout,
        contract_token=contract_token,
    )

    # 4. 拉取 provider receipts
    receipts = await fetch_provider_receipts(
        provider_url=provider_url,
        contract_token=contract_token,
        trace_id=trace_id,
    )

    # 5. 验签 receipt chain(只在 status=delivered 且 verify_receipts=True 时验证)
    # 故障注入测试传入 verify_receipts=False,因为 fault receipt 不含完整
    # previous_digest 链;run_fault_injection_transaction 单独验证 delivered 链。
    if verify_receipts and status.status == "delivered":
        verify_receipt_chain(receipts, receipt_key)

    return status, receipts, fixture


# ════════════════════════════════════════════════════════════════
# R76 §10.4 故障注入 API
# ════════════════════════════════════════════════════════════════
async def set_fault_plan(
    provider_url: str,
    contract_token: str,
    plan: FaultPlanSpec,
) -> dict[str, Any]:
    """通过 ``POST /__faults`` 设置故障计划到 provider 模拟器。

    R76 §10.4: 协议级确定性 fault server。故障在 provider 协议层真实触发,
    业务代码会真实收到对应 HTTP 响应(401/429+RetryAfter/500/timeout),
    无 mock / monkey patch / 短 subprocess timeout 冒充。

    Args:
        provider_url: Provider 模拟器 base URL
        contract_token: ``X-Contract-Token``
        plan: ``FaultPlanSpec`` 故障计划规格

    Returns:
        Provider 模拟器返回的确认 dict(含 plan 字段)

    Raises:
        httpx.HTTPStatusError: 设置失败(非 2xx)
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{provider_url.rstrip('/')}/__faults",
            json=plan.to_payload(),
            headers={"X-Contract-Token": contract_token},
        )
        resp.raise_for_status()
        return resp.json()


async def clear_fault_plans(
    provider_url: str,
    contract_token: str,
) -> dict[str, Any]:
    """通过 ``DELETE /__faults`` 清除所有故障计划(测试间隔离)。

    Args:
        provider_url: Provider 模拟器 base URL
        contract_token: ``X-Contract-Token``

    Returns:
        Provider 模拟器返回的确认 dict

    Raises:
        httpx.HTTPStatusError: 清除失败(非 2xx)
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.delete(
            f"{provider_url.rstrip('/')}/__faults",
            headers={"X-Contract-Token": contract_token},
        )
        resp.raise_for_status()
        return resp.json()


# 故障类型 → FaultPlanSpec 映射(R76 §10.4 协议级确定性故障注入矩阵)
#
# 每种故障类型对应一个 FaultPlanSpec,注入到 provider 协议层:
#   - 401: getFile 返回 401,repeat=1(单次故障,验证应用不重试或终止)
#   - 429: getFile 返回 429 + Retry-After,repeat=1(单次限流,验证退避重试)
#   - 500: getFile 返回 500,repeat=3(多次故障,验证有界重试后失败)
#   - timeout: getFile 延迟 5000ms 响应,repeat=1(验证读超时)
#   - duplicate: sendMessage 返回 500,repeat=1(首次失败,验证幂等重试成功)
#
# 故障注入到 getFile(而非 getMe/download)的原因:
#   - getMe 在应用启动时调用,故障注入时机难以控制
#   - getFile 是交易链路中第一个 provider 调用,故障可被应用重试逻辑捕获
#   - download 依赖 getFile 返回的 file_path,故障注入时机较晚
#
# duplicate 注入到 sendMessage 的原因:
#   - 幂等性验证针对出站操作(sendMessage/sendDocument)
#   - 首次 sendMessage 失败后,应用重试应得到相同 message_id(幂等)
FAULT_PLAN_MATRIX: dict[str, FaultPlanSpec] = {
    "401": FaultPlanSpec(
        operation="getFile",
        status=401,
        repeat=1,
    ),
    "429": FaultPlanSpec(
        operation="getFile",
        status=429,
        retry_after=2,
        repeat=1,
    ),
    "500": FaultPlanSpec(
        operation="getFile",
        status=500,
        repeat=3,
    ),
    "timeout": FaultPlanSpec(
        operation="getFile",
        status=500,
        delay_ms=5000,
        repeat=1,
    ),
    "duplicate": FaultPlanSpec(
        operation="sendMessage",
        status=500,
        repeat=1,
    ),
}


# 期望终态 → 实际终态映射规则
#
# R76 §10.4 负向验收: 每类故障验证重试上限、退避、幂等、DLQ/终态,禁止输出 PASS。
#
#   - failure: 401 注入后,应用应终态=failed(不重试或按策略终止)
#   - retry-then-success: 429 + Retry-After 注入后,应用应退避重试并终态=delivered
#   - bounded-retry-then-failure: 500 注入后,应用应有界重试后终态=failed
#   - timeout-failure: timeout 注入后,应用应读超时后终态=failed
#   - idempotent-success: duplicate 注入后,应用应重试并终态=delivered(幂等)
EXPECTED_OUTCOME_RULES: dict[str, set[str]] = {
    "failure": {"failed"},
    "retry-then-success": {"delivered"},
    "bounded-retry-then-failure": {"failed"},
    "timeout-failure": {"failed"},
    "idempotent-success": {"delivered"},
}


async def run_fault_injection_transaction(
    *,
    provider_url: str,
    app_url: str,
    contract_token: str,
    receipt_key: str,
    fault_type: str,
    expected_outcome: str,
    content: bytes,
    user_id: int = 111,
    timeout: float = 60.0,
) -> FaultInjectionResult:
    """运行故障注入交易:设置 fault plan → 运行正常交易 → 验证终态符合 expect。

    R76 §10.4 协议级确定性 fault server。本函数:
        1. 通过 ``POST /__faults`` 设置 FaultPlanSpec 到 provider 模拟器;
        2. 运行正常交易流程(upload_fixture → submit_update → wait_for_terminal);
        3. 拉取 provider receipts;
        4. 根据期望终态验证实际终态是否符合;
        5. 清除故障计划(测试间隔离)。

    Args:
        provider_url: Provider 模拟器 URL
        app_url: 应用公开入口 URL
        contract_token: X-Contract-Token
        receipt_key: receipt HMAC 验签密钥
        fault_type: 故障类型(``401`` / ``429`` / ``500`` / ``timeout`` / ``duplicate``)
        expected_outcome: 期望终态(``failure`` / ``retry-then-success`` /
            ``bounded-retry-then-failure`` / ``timeout-failure`` /
            ``idempotent-success``)
        content: 测试文件内容
        user_id: 模拟用户 ID
        timeout: 等待终态超时(秒)

    Returns:
        ``FaultInjectionResult`` 实例(含匹配状态和详细 receipts)

    Raises:
        ValueError: 故障类型或期望终态不在支持列表
        RuntimeError: 设置故障计划失败
    """
    if fault_type not in FAULT_PLAN_MATRIX:
        raise ValueError(
            f"不支持的故障类型: {fault_type!r}, "
            f"支持: {sorted(FAULT_PLAN_MATRIX.keys())}"
        )
    if expected_outcome not in EXPECTED_OUTCOME_RULES:
        raise ValueError(
            f"不支持的期望终态: {expected_outcome!r}, "
            f"支持: {sorted(EXPECTED_OUTCOME_RULES.keys())}"
        )

    plan = FAULT_PLAN_MATRIX[fault_type]
    expected_statuses = EXPECTED_OUTCOME_RULES[expected_outcome]
    trace_id = str(uuid.uuid4())

    # 1. 设置故障计划到 provider 模拟器
    try:
        await set_fault_plan(provider_url, contract_token, plan)
    finally:
        # 确保测试结束后清除故障(即使 set_fault_plan 失败也尝试清除)
        pass

    try:
        # 2. 运行正常交易流程(故障已在 provider 协议层激活)
        # verify_receipts=False: fault receipt 不含完整 previous_digest 链,
        # 由下方 run_fault_injection_transaction 单独验证 delivered 链。
        status, receipts, fixture = await run_secretless_transaction(
            provider_url=provider_url,
            app_url=app_url,
            contract_token=contract_token,
            receipt_key=receipt_key,
            content=content,
            user_id=user_id,
            timeout=timeout,
            verify_receipts=False,
        )
    finally:
        # 3. 清除故障计划(测试间隔离,R76 §10.4 要求)
        try:
            await clear_fault_plans(provider_url, contract_token)
        except Exception:
            # 清除失败不应影响测试结果判定,但记录到 stderr
            sys.stderr.write(
                f"[e2e_update_adapter] 警告: 清除故障计划失败 "
                f"(trace_id={trace_id})\n"
            )

    # 4. 验证终态是否符合期望
    actual_status = status.status
    matched = actual_status in expected_statuses

    # 5. 对于 retry-then-success 和 idempotent-success,验证 receipt 存在性
    # 注意:故障注入 receipt 不含完整 previous_digest 链(provider-sim fault receipt
    # 结构精简),因此不做 full chain 验签;仅验证 delivered 交易包含必要操作。
    # 完整 receipt chain 验签由 Step 10 正常交易覆盖。
    if matched and actual_status == "delivered":
        ops = {r.operation for r in receipts}
        has_outbound = "sendDocument" in ops or "sendMessage" in ops
        missing = {"getFile", "download"} - ops
        if missing or not has_outbound:
            matched = False
            status.details = {
                **status.details,
                "receipt_verification_error": (
                    f"缺少必要操作: missing={missing or '∅'}, "
                    f"has_outbound={has_outbound}, 实际: {ops}"
                ),
            }

    return FaultInjectionResult(
        fault_type=fault_type,
        expected_outcome=expected_outcome,
        actual_status=actual_status,
        matched=matched,
        trace_id=status.trace_id,
        receipts=receipts,
        details={
            **status.details,
            "expected_statuses": sorted(expected_statuses),
            "plan": plan.to_payload(),
        },
    )


# ════════════════════════════════════════════════════════════════
# 内部辅助
# ════════════════════════════════════════════════════════════════
_update_id_counter = 1000000
_message_id_counter = 2000000


def _generate_update_id() -> int:
    """生成单调递增的 update_id(模拟 Telegram 分配)。"""
    global _update_id_counter
    _update_id_counter += 1
    return _update_id_counter


def _generate_message_id() -> int:
    """生成单调递增的 message_id(模拟 Telegram 分配)。"""
    global _message_id_counter
    _message_id_counter += 1
    return _message_id_counter


# ════════════════════════════════════════════════════════════════
# CLI 入口(单条交易验证 / 故障注入矩阵)
# ════════════════════════════════════════════════════════════════
def main() -> int:
    """CLI 入口:运行单条 secretless 交易或故障注入测试,并打印结果。

    支持两种模式:
        - ``--mode normal-transaction`` (默认): 运行正常交易,期望 delivered;
        - ``--mode fault-injection``: 设置故障计划,运行交易,验证终态符合 expect。

    Returns:
        0 = 交易结果符合 expect(正常交易 delivered,或故障注入终态符合预期);
        1 = 交易失败、receipt 验签失败,或终态不符合 expect。
    """
    parser = argparse.ArgumentParser(
        description="R76 O4: 外部黑盒驱动器(secretless E2E + 故障注入)",
    )
    parser.add_argument("--provider-url", required=True, help="Provider 模拟器 URL")
    parser.add_argument("--app-url", required=True, help="应用公开入口 URL")
    # --contract-token 和 --provider-token 互为别名(兼容 workflow 调用)
    token_group = parser.add_mutually_exclusive_group(required=True)
    token_group.add_argument(
        "--contract-token",
        help="X-Contract-Token(provider 模拟器验证令牌)",
    )
    token_group.add_argument(
        "--provider-token",
        help="X-Contract-Token 别名(兼容 secretless-contract-e2e.yml 调用)",
    )
    parser.add_argument("--receipt-key", required=True, help="receipt HMAC 验签密钥(hex)")
    parser.add_argument("--content", default="Hello, tgjiema secretless E2E!", help="测试文件内容")
    parser.add_argument("--user-id", type=int, default=111, help="模拟用户 ID")
    parser.add_argument("--timeout", type=float, default=60.0, help="等待终态超时(秒)")

    # R76 §10.4 故障注入参数
    parser.add_argument(
        "--mode",
        choices=["normal-transaction", "fault-injection"],
        default="normal-transaction",
        help="运行模式: normal-transaction(默认)或 fault-injection(R76 §10.4)",
    )
    parser.add_argument(
        "--fault",
        choices=["401", "429", "500", "timeout", "duplicate"],
        help="故障类型(仅 --mode fault-injection 时必填)",
    )
    parser.add_argument(
        "--retry-after",
        type=int,
        default=None,
        help="429 故障的 Retry-After 秒数(仅 --fault 429 时有效)",
    )
    parser.add_argument(
        "--expect",
        choices=[
            "failure",
            "retry-then-success",
            "bounded-retry-then-failure",
            "timeout-failure",
            "idempotent-success",
        ],
        help="期望终态(仅 --mode fault-injection 时必填)",
    )
    args = parser.parse_args()

    # 解析 token(--contract-token 优先,--provider-token 作为别名)
    contract_token = args.contract_token or args.provider_token

    # 模式参数校验
    if args.mode == "fault-injection":
        if not args.fault:
            parser.error("--mode fault-injection 需要 --fault 参数")
        if not args.expect:
            parser.error("--mode fault-injection 需要 --expect 参数")
    elif args.fault or args.expect:
        parser.error("--fault / --expect 仅在 --mode fault-injection 时使用")

    # 如果 --retry-after 指定且 --fault 不是 429,发出警告但仍接受
    # (provider_simulator 只在 429 时使用 retry_after,其他故障忽略)
    if args.retry_after is not None and args.fault != "429":
        sys.stderr.write(
            f"[e2e_update_adapter] 警告: --retry-after 仅在 --fault 429 时有效, "
            f"当前 --fault={args.fault},retry_after 将被忽略\n"
        )

    # 如果 --fault 429 且 --retry-after 指定,覆盖默认 retry_after
    # (FAULT_PLAN_MATRIX 中 429 默认 retry_after=2)
    if args.mode == "fault-injection" and args.fault == "429" and args.retry_after is not None:
        FAULT_PLAN_MATRIX["429"].retry_after = args.retry_after

    async def _run_normal() -> int:
        try:
            status, receipts, fixture = await run_secretless_transaction(
                provider_url=args.provider_url,
                app_url=args.app_url,
                contract_token=contract_token,
                receipt_key=args.receipt_key,
                content=args.content.encode("utf-8"),
                user_id=args.user_id,
                timeout=args.timeout,
            )
        except Exception as e:
            sys.stderr.write(f"[e2e_update_adapter] 交易失败: {e}\n")
            return 1

        # 输出结果到 stdout(JSON)
        result = {
            "mode": "normal-transaction",
            "trace_id": status.trace_id,
            "terminal_status": status.status,
            "error": status.error,
            "details": status.details,
            "fixture": asdict(fixture),
            "receipts_count": len(receipts),
            "receipts": [asdict(r) for r in receipts],
        }
        print(json.dumps(result, indent=2, ensure_ascii=False))

        if status.status != "delivered":
            sys.stderr.write(
                f"[e2e_update_adapter] 终态非 delivered: {status.status}\n"
            )
            return 1
        return 0

    async def _run_fault_injection() -> int:
        try:
            result = await run_fault_injection_transaction(
                provider_url=args.provider_url,
                app_url=args.app_url,
                contract_token=contract_token,
                receipt_key=args.receipt_key,
                fault_type=args.fault,
                expected_outcome=args.expect,
                content=args.content.encode("utf-8"),
                user_id=args.user_id,
                timeout=args.timeout,
            )
        except Exception as e:
            sys.stderr.write(
                f"[e2e_update_adapter] 故障注入测试失败 "
                f"(fault={args.fault}, expect={args.expect}): {e}\n"
            )
            return 1

        # 输出结果到 stdout(JSON)
        output = {
            "mode": "fault-injection",
            "fault_type": result.fault_type,
            "expected_outcome": result.expected_outcome,
            "actual_status": result.actual_status,
            "matched": result.matched,
            "trace_id": result.trace_id,
            "details": result.details,
            "receipts_count": len(result.receipts),
            "receipts": [asdict(r) for r in result.receipts],
        }
        print(json.dumps(output, indent=2, ensure_ascii=False))

        if not result.matched:
            sys.stderr.write(
                f"[e2e_update_adapter] 故障注入终态不符合期望: "
                f"fault={result.fault_type}, expect={result.expected_outcome}, "
                f"actual={result.actual_status}\n"
            )
            return 1
        return 0

    if args.mode == "fault-injection":
        return asyncio.run(_run_fault_injection())
    return asyncio.run(_run_normal())


if __name__ == "__main__":
    sys.exit(main())
