"""R76 O3: 本地 Provider 协议模拟器(CI/本地 secretless 测试用)。

整改背景(R76 终审报告 10.O-O3 / 10.C / P0-03 / P0-04):
    业务模块此前在测试中通过 ``docker compose exec ... python -c`` 直接调用
    ``bots.up_bot._dispatch_media``,并通过 monkey patch ``bot.get_file`` /
    ``bot.download_file`` 冒充真实 Provider,导致未证明 polling/webhook 入口、
    真实 provider 下载、生产消费者自然推进和最终 sendMessage/sendDocument 回执。

    本模拟器实现与 Telegram Bot API 协议兼容的本地 HTTP 服务,使:
        - 真实生产代码(up_bot / dsp_bot / web_adapter)通过 ``build_provider_client``
          连接本服务,不区分生产/测试入口;
        - 文件下载由本服务真实返回字节流,不再依赖 Update 内嵌内容;
        - 出站消息(sendMessage/sendDocument)持久化为签名 receipt,供验证脚本核对;
        - 故障注入(401/429/500/timeout/duplicate)在协议层确定性触发,业务层
          真实收到对应 HTTP 响应,不再以 ``print('OK')`` 为成功标准。

启动:
    # 单机/CI 直接启动
    python -m tests.support.provider_simulator \\
        --host 0.0.0.0 --port 8088 \\
        --contract-token "$PROVIDER_CONTRACT_TOKEN" \\
        --receipt-key "$PROVIDER_RECEIPT_KEY"

    # Docker Compose 中作为 provider-sim 服务启动(见 docker-compose.secretless.yml)

端点(R76 报告 10.O-O3 / 10.C):
    - ``POST /__fixtures/files``              上传测试文件,返回 ``file_id=sha256:<hex>``
    - ``POST /__fixtures/updates``            投递 Update 到应用公开入口(由调度器转发)
    - ``GET  /bot/{token}/getMe``             返回固定 ci-local-token 身份
    - ``GET  /bot/{token}/getFile?file_id=``  返回 file path/digest
    - ``GET  /files/{id}/content``            返回真实字节流
    - ``POST /bot/{token}/sendMessage``       持久化 outbound receipt
    - ``POST /bot/{token}/sendDocument``      持久化 outbound receipt
    - ``POST /__faults``                      设置按 operation 计数的故障计划
    - ``GET  /__receipts/{trace_id}``         返回该 trace 的全部回执
    - ``GET  /health``                        返回模拟器自身状态

隔离:
    - 仅出现在 secretless Compose profile;
    - 生产镜像 Dockerfile 不得 COPY tests/support/;
    - scanner 阻断 ``PROVIDER_BASE_URL`` 指向 simulator 的生产配置。

receipt 字段(R76 报告 10.O-O3):
    schema_version / trace_id / operation / request_digest / response_digest /
    message_id / file_id / file_sha256 / status / attempt / timestamp /
    previous_digest / signature
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any, Optional

# 使用项目已有 FastAPI/uvicorn 依赖(不引入大型框架)
from fastapi import FastAPI, Header, HTTPException, Request, Response
import uvicorn


# ════════════════════════════════════════════════════════════════
# 常量与类型
# ════════════════════════════════════════════════════════════════
SCHEMA_VERSION = "provider-receipt/v1"
DEFAULT_CI_TOKEN = "ci-local-token"
HEALTH_OK = {"status": "ok", "service": "provider-sim"}

# 故障支持的操作(R76 报告 10.O-O3)
FAULT_OPERATIONS = frozenset({
    "getMe", "getFile", "download", "sendMessage", "sendDocument",
})


class FaultPlan:
    """故障计划(确定性触发)。

    Attributes:
        operation: 故障对应的 provider 操作(如 ``getFile``)
        status: HTTP 状态码(401/429/500)
        delay_ms: 响应前 sleep 毫秒(用于触发 client timeout)
        retry_after: 仅 429 有效,Retry-After 头部秒数
        repeat: 故障重复次数(>0);超过后恢复正常响应
        body: 可选的响应体(JSON 字符串)
    """

    def __init__(
        self,
        operation: str,
        status: int,
        *,
        delay_ms: int = 0,
        retry_after: Optional[int] = None,
        repeat: int = 1,
        body: Optional[str] = None,
    ) -> None:
        if operation not in FAULT_OPERATIONS:
            raise ValueError(
                f"FaultPlan.operation={operation!r} 不在支持列表: "
                f"{sorted(FAULT_OPERATIONS)}"
            )
        self.operation = operation
        self.status = status
        self.delay_ms = max(0, delay_ms)
        self.retry_after = retry_after
        self.repeat = max(1, repeat)
        self.body = body
        # 已触发次数
        self.triggered = 0

    def consume(self) -> bool:
        """尝试触发一次故障;返回 True 表示仍应触发,False 表示已耗尽。"""
        if self.triggered < self.repeat:
            self.triggered += 1
            return True
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "status": self.status,
            "delay_ms": self.delay_ms,
            "retry_after": self.retry_after,
            "repeat": self.repeat,
            "triggered": self.triggered,
        }


# ════════════════════════════════════════════════════════════════
# ProviderSimulator 状态
# ════════════════════════════════════════════════════════════════
class ProviderSimulator:
    """Provider 模拟器内部状态。

    所有状态保存在内存中,不持久化;每次 CI run 重新生成。
    线程安全由 FastAPI/Starlette 的 async 单线程事件循环保证;同步操作
    使用 ``asyncio.Lock`` 保护(若有并发写入需求)。
    """

    def __init__(
        self,
        contract_token: str,
        receipt_key: str,
        ci_token: str = DEFAULT_CI_TOKEN,
    ) -> None:
        self.contract_token = contract_token
        self.receipt_key = receipt_key  # HMAC-SHA256 密钥(hex 编码)
        self.ci_token = ci_token

        # file_id (sha256:<hex>) -> bytes
        self.files: dict[str, bytes] = {}
        # trace_id -> list[receipt dict]
        self.receipts: dict[str, list[dict[str, Any]]] = {}
        # (operation) -> FaultPlan
        self.faults: dict[str, FaultPlan] = {}
        # 请求日志(便于调试,不进入 receipt)
        self.request_log: list[dict[str, Any]] = []
        # 出站消息计数器(用于生成 message_id)
        self._next_message_id = 1000
        # 出站消息 chat_id -> [(message_id, payload)]
        self.outbox: dict[int, list[dict[str, Any]]] = {}
        # 异步锁
        self._lock = asyncio.Lock()

    # ── 内部辅助 ────────────────────────────────────────────────
    def _now_iso(self) -> str:
        """RFC3339 UTC 时间戳。"""
        # 使用 time.gmtime 避免时区依赖;格式与 RFC3339 UTC 一致
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def _hmac_sign(self, payload: bytes) -> str:
        """HMAC-SHA256(receipt_key, payload) 返回 hex。"""
        if not self.receipt_key:
            return ""
        try:
            key = bytes.fromhex(self.receipt_key)
        except ValueError:
            # 非 hex,直接以 utf-8 编码使用
            key = self.receipt_key.encode("utf-8")
        return hmac.new(key, payload, hashlib.sha256).hexdigest()

    def _digest(self, data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _request_digest(self, body: bytes, headers: dict[str, Any]) -> str:
        """计算 request digest:method + path + body sha256。"""
        parts = [
            headers.get("x-trace-id", ""),
            str(headers.get("content-length", "0")),
            self._digest(body) if body else "",
        ]
        return self._digest("\n".join(parts).encode("utf-8"))

    async def _verify_contract_token(
        self,
        x_contract_token: Optional[str],
    ) -> None:
        """验证 X-Contract-Token 头部。"""
        if not x_contract_token:
            raise HTTPException(
                status_code=401,
                detail="missing X-Contract-Token header",
            )
        # 常量时间比较防止时序攻击
        if not hmac.compare_digest(x_contract_token, self.contract_token):
            raise HTTPException(
                status_code=401,
                detail="invalid X-Contract-Token",
            )

    def _append_receipt(
        self,
        trace_id: str,
        operation: str,
        *,
        request_digest: str,
        response_digest: str,
        message_id: Optional[int] = None,
        file_id: Optional[str] = None,
        file_sha256: Optional[str] = None,
        status: str = "ok",
        attempt: int = 1,
        previous_digest: Optional[str] = None,
    ) -> dict[str, Any]:
        """构造并持久化 receipt。"""
        # previous_digest 链:trace 内上一条 receipt 的 response_digest
        if previous_digest is None and trace_id in self.receipts:
            prev = self.receipts[trace_id][-1]
            previous_digest = prev.get("response_digest", "")

        receipt = {
            "schema_version": SCHEMA_VERSION,
            "trace_id": trace_id,
            "operation": operation,
            "request_digest": request_digest,
            "response_digest": response_digest,
            "message_id": message_id,
            "file_id": file_id,
            "file_sha256": file_sha256,
            "status": status,
            "attempt": attempt,
            "timestamp": self._now_iso(),
            "previous_digest": previous_digest or "",
        }
        # 签名:对 canonical JSON(receipt 排除 signature 字段)做 HMAC
        canonical = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8")
        receipt["signature"] = self._hmac_sign(canonical)

        self.receipts.setdefault(trace_id, []).append(receipt)
        return receipt

    async def _check_fault(
        self,
        operation: str,
        trace_id: str,
    ) -> Optional[Response]:
        """检查是否有针对该 operation 的故障计划;有则返回故障响应。"""
        plan = self.faults.get(operation)
        if plan is None:
            return None
        if not plan.consume():
            # 已耗尽,移除
            self.faults.pop(operation, None)
            return None

        # 触发故障
        if plan.delay_ms > 0:
            await asyncio.sleep(plan.delay_ms / 1000.0)

        # 记录故障 receipt(便于验证脚本核对)
        fault_receipt = {
            "schema_version": SCHEMA_VERSION,
            "trace_id": trace_id,
            "operation": operation,
            "status": f"fault_{plan.status}",
            "attempt": plan.triggered,
            "timestamp": self._now_iso(),
            "fault_plan": plan.to_dict(),
        }
        canonical = json.dumps(
            fault_receipt, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        fault_receipt["signature"] = self._hmac_sign(canonical)
        self.receipts.setdefault(trace_id, []).append(fault_receipt)

        # 构造故障响应
        headers: dict[str, str] = {}
        body = plan.body or json.dumps({"ok": False, "error_code": plan.status})
        if plan.status == 429 and plan.retry_after is not None:
            headers["Retry-After"] = str(plan.retry_after)
            body = json.dumps({
                "ok": False,
                "error_code": 429,
                "parameters": {"retry_after": plan.retry_after},
            })
        return Response(
            content=body,
            status_code=plan.status,
            headers=headers,
            media_type="application/json",
        )

    # ── 公开 API:fixture 上传 ────────────────────────────────────
    async def upload_fixture_file(
        self,
        content: bytes,
        trace_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """``POST /__fixtures/files``:上传测试文件,返回内容寻址 file_id。"""
        file_sha256 = self._digest(content)
        file_id = f"sha256:{file_sha256}"
        async with self._lock:
            self.files[file_id] = content
        return {
            "file_id": file_id,
            "file_unique_id": file_id,
            "file_size": len(content),
            "file_sha256": file_sha256,
        }

    async def get_receipts(self, trace_id: str) -> list[dict[str, Any]]:
        """``GET /__receipts/{trace_id}``:返回该 trace 的全部回执。"""
        return list(self.receipts.get(trace_id, []))

    async def set_fault(self, plan: FaultPlan) -> dict[str, Any]:
        """``POST /__faults``:设置故障计划。"""
        async with self._lock:
            self.faults[plan.operation] = plan
        return {"status": "ok", "plan": plan.to_dict()}

    async def clear_faults(self) -> dict[str, Any]:
        """``DELETE /__faults``:清除所有故障计划(便于测试间隔离)。"""
        async with self._lock:
            self.faults.clear()
        return {"status": "ok", "cleared": True}

    def health(self) -> dict[str, Any]:
        """``GET /health``:返回模拟器状态。"""
        return {
            **HEALTH_OK,
            "files_count": len(self.files),
            "receipts_count": sum(len(v) for v in self.receipts.values()),
            "active_faults": list(self.faults.keys()),
            "ci_token": self.ci_token,
        }


# ════════════════════════════════════════════════════════════════
# FastAPI 应用工厂
# ════════════════════════════════════════════════════════════════
def create_app(state: ProviderSimulator) -> FastAPI:
    """构造 FastAPI 应用(不全局依赖业务模块)。"""
    app = FastAPI(
        title="tgjiema Provider Simulator",
        version="r76-o3",
        docs_url=None,
        redoc_url=None,
    )

    # ── 健康检查 ─────────────────────────────────────────────
    @app.get("/health")
    async def health() -> dict[str, Any]:
        return state.health()

    # ── Fixture:上传测试文件 ─────────────────────────────────
    @app.post("/__fixtures/files")
    async def upload_fixture(
        request: Request,
        x_contract_token: Optional[str] = Header(None),
        x_trace_id: Optional[str] = Header(None),
    ) -> dict[str, Any]:
        await state._verify_contract_token(x_contract_token)
        content = await request.body()
        result = await state.upload_fixture_file(content, trace_id=x_trace_id)
        # 记录 receipt
        state._append_receipt(
            trace_id=x_trace_id or "",
            operation="upload_fixture",
            request_digest=state._request_digest(content, dict(request.headers)),
            response_digest=state._digest(json.dumps(result).encode("utf-8")),
            file_id=result["file_id"],
            file_sha256=result["file_sha256"],
            status="ok",
        )
        return result

    # ── Fixture:投递 Update 到应用入口 ──────────────────────
    @app.post("/__fixtures/updates")
    async def deliver_update(
        request: Request,
        x_contract_token: Optional[str] = Header(None),
        x_trace_id: Optional[str] = Header(None),
    ) -> dict[str, Any]:
        """接收测试 fixture 的 Update,转发到应用公开入口。

        本端点不直接调用 ``bots.*``;它把 Update 通过 HTTP 投递到
        ``APP_PUBLIC_URL/internal/contract/update``(由 O5 实现)。
        """
        await state._verify_contract_token(x_contract_token)
        payload = await request.json()
        # 必须包含 app_url 和 update 字段
        app_url = payload.get("app_url")
        update = payload.get("update")
        if not app_url or not update:
            raise HTTPException(
                status_code=400,
                detail="payload must contain app_url and update",
            )
        # 转发到应用公开入口
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                resp = await client.post(
                    f"{app_url.rstrip('/')}/internal/contract/update",
                    json={
                        "update": update,
                        "trace_id": x_trace_id or payload.get("trace_id", ""),
                    },
                    headers={"X-Contract-Token": state.contract_token},
                )
            except Exception as e:
                state._append_receipt(
                    trace_id=x_trace_id or "",
                    operation="deliver_update",
                    request_digest=state._request_digest(b"", dict(request.headers)),
                    response_digest=state._digest(str(e).encode("utf-8")),
                    status="deliver_failed",
                )
                raise HTTPException(
                    status_code=502,
                    detail=f"failed to deliver update to app: {e}",
                )

        state._append_receipt(
            trace_id=x_trace_id or "",
            operation="deliver_update",
            request_digest=state._request_digest(b"", dict(request.headers)),
            response_digest=state._digest(resp.content),
            status=f"app_{resp.status_code}",
        )
        return {
            "status": "delivered",
            "app_status": resp.status_code,
            "app_body": resp.text[:500] if resp.text else "",
        }

    # ── Telegram 协议兼容:GET /bot/{token}/getMe ────────────
    @app.get("/bot/{token}/getMe")
    async def bot_get_me(
        token: str,
        x_contract_token: Optional[str] = Header(None),
        x_trace_id: Optional[str] = Header(None),
    ) -> dict[str, Any]:
        await state._verify_contract_token(x_contract_token)
        # 验证 token 为 ci-local-token(不访问公网)
        if token != state.ci_token:
            raise HTTPException(
                status_code=401,
                detail=f"simulator only accepts ci-local-token, got: {token[:8]}...",
            )

        # 故障注入
        fault_resp = await state._check_fault("getMe", x_trace_id or "")
        if fault_resp is not None:
            return fault_resp

        result = {
            "ok": True,
            "result": {
                "id": 999999999,
                "is_bot": True,
                "first_name": "tgjiema-ci-bot",
                "username": "tgjiema_ci_bot",
            },
        }
        state._append_receipt(
            trace_id=x_trace_id or "",
            operation="getMe",
            request_digest=state._request_digest(b"", {"x-trace-id": x_trace_id or ""}),
            response_digest=state._digest(json.dumps(result).encode("utf-8")),
            status="ok",
        )
        return result

    # ── Telegram 协议兼容:GET /bot/{token}/getFile ──────────
    @app.get("/bot/{token}/getFile")
    async def bot_get_file(
        token: str,
        file_id: str,
        x_contract_token: Optional[str] = Header(None),
        x_trace_id: Optional[str] = Header(None),
    ) -> dict[str, Any]:
        await state._verify_contract_token(x_contract_token)
        if token != state.ci_token:
            raise HTTPException(
                status_code=401,
                detail=f"simulator only accepts ci-local-token, got: {token[:8]}...",
            )

        fault_resp = await state._check_fault("getFile", x_trace_id or "")
        if fault_resp is not None:
            return fault_resp

        if file_id not in state.files:
            raise HTTPException(
                status_code=404,
                detail=f"file_id not found: {file_id}",
            )
        content = state.files[file_id]
        file_sha256 = state._digest(content)
        result = {
            "ok": True,
            "result": {
                "file_id": file_id,
                "file_unique_id": file_id,
                "file_size": len(content),
                "file_path": file_id,  # download_file 使用 file_path
            },
        }
        state._append_receipt(
            trace_id=x_trace_id or "",
            operation="getFile",
            request_digest=state._request_digest(b"", {"x-trace-id": x_trace_id or ""}),
            response_digest=state._digest(json.dumps(result).encode("utf-8")),
            file_id=file_id,
            file_sha256=file_sha256,
            status="ok",
        )
        return result

    # ── Telegram 协议兼容:GET /files/{id}/content ───────────
    @app.get("/files/{file_id}/content")
    async def files_content(
        file_id: str,
        x_contract_token: Optional[str] = Header(None),
        x_trace_id: Optional[str] = Header(None),
    ) -> Response:
        await state._verify_contract_token(x_contract_token)

        # file_id 可能以 "sha256:" 开头;也可能为纯 hex
        normalized = file_id
        if not normalized.startswith("sha256:"):
            normalized = f"sha256:{file_id}"

        fault_resp = await state._check_fault("download", x_trace_id or "")
        if fault_resp is not None:
            return fault_resp

        content = state.files.get(normalized)
        if content is None:
            # 兼容裸 hex key
            content = state.files.get(file_id)
        if content is None:
            raise HTTPException(
                status_code=404,
                detail=f"file not found: {file_id}",
            )

        file_sha256 = state._digest(content)
        state._append_receipt(
            trace_id=x_trace_id or "",
            operation="download",
            request_digest=state._request_digest(b"", {"x-trace-id": x_trace_id or ""}),
            response_digest=state._digest(content),
            file_id=normalized,
            file_sha256=file_sha256,
            status="ok",
        )
        return Response(
            content=content,
            media_type="application/octet-stream",
            headers={"X-File-Sha256": file_sha256},
        )

    # ── Telegram 协议兼容:POST /bot/{token}/sendMessage ─────
    @app.post("/bot/{token}/sendMessage")
    async def bot_send_message(
        token: str,
        request: Request,
        x_contract_token: Optional[str] = Header(None),
        x_trace_id: Optional[str] = Header(None),
    ) -> dict[str, Any]:
        await state._verify_contract_token(x_contract_token)
        if token != state.ci_token:
            raise HTTPException(
                status_code=401,
                detail=f"simulator only accepts ci-local-token, got: {token[:8]}...",
            )

        body = await request.body()
        fault_resp = await state._check_fault("sendMessage", x_trace_id or "")
        if fault_resp is not None:
            return fault_resp

        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="invalid JSON body")

        chat_id = int(payload.get("chat_id", 0))
        message_id = state._next_message_id
        state._next_message_id += 1

        # 持久化到 outbox
        state.outbox.setdefault(chat_id, []).append({
            "message_id": message_id,
            "payload": payload,
            "kind": "sendMessage",
            "timestamp": state._now_iso(),
        })

        result = {
            "ok": True,
            "result": {
                "message_id": message_id,
                "date": int(time.time()),
                "chat": {"id": chat_id, "type": "private"},
                "text": payload.get("text", ""),
            },
        }
        state._append_receipt(
            trace_id=x_trace_id or "",
            operation="sendMessage",
            request_digest=state._request_digest(body, dict(request.headers)),
            response_digest=state._digest(json.dumps(result).encode("utf-8")),
            message_id=message_id,
            status="ok",
        )
        return result

    # ── Telegram 协议兼容:POST /bot/{token}/sendDocument ────
    @app.post("/bot/{token}/sendDocument")
    async def bot_send_document(
        token: str,
        request: Request,
        x_contract_token: Optional[str] = Header(None),
        x_trace_id: Optional[str] = Header(None),
    ) -> dict[str, Any]:
        await state._verify_contract_token(x_contract_token)
        if token != state.ci_token:
            raise HTTPException(
                status_code=401,
                detail=f"simulator only accepts ci-local-token, got: {token[:8]}...",
            )

        body = await request.body()
        fault_resp = await state._check_fault("sendDocument", x_trace_id or "")
        if fault_resp is not None:
            return fault_resp

        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="invalid JSON body")

        chat_id = int(payload.get("chat_id", 0))
        message_id = state._next_message_id
        state._next_message_id += 1

        state.outbox.setdefault(chat_id, []).append({
            "message_id": message_id,
            "payload": payload,
            "kind": "sendDocument",
            "timestamp": state._now_iso(),
        })

        result = {
            "ok": True,
            "result": {
                "message_id": message_id,
                "date": int(time.time()),
                "chat": {"id": chat_id, "type": "private"},
                "document": {
                    "file_id": str(payload.get("document", "")),
                    "file_unique_id": str(payload.get("document", "")),
                },
            },
        }
        state._append_receipt(
            trace_id=x_trace_id or "",
            operation="sendDocument",
            request_digest=state._request_digest(body, dict(request.headers)),
            response_digest=state._digest(json.dumps(result).encode("utf-8")),
            message_id=message_id,
            status="ok",
        )
        return result

    # ── 故障管理:POST /__faults ──────────────────────────────
    @app.post("/__faults")
    async def set_fault(
        request: Request,
        x_contract_token: Optional[str] = Header(None),
    ) -> dict[str, Any]:
        await state._verify_contract_token(x_contract_token)
        payload = await request.json()
        operation = payload.get("operation")
        status_code = int(payload.get("status", 500))
        if operation not in FAULT_OPERATIONS:
            raise HTTPException(
                status_code=400,
                detail=f"unsupported operation: {operation}",
            )
        plan = FaultPlan(
            operation=operation,
            status=status_code,
            delay_ms=int(payload.get("delay_ms", 0)),
            retry_after=payload.get("retry_after"),
            repeat=int(payload.get("repeat", 1)),
            body=payload.get("body"),
        )
        return await state.set_fault(plan)

    # ── 故障管理:DELETE /__faults ────────────────────────────
    @app.delete("/__faults")
    async def clear_faults(
        x_contract_token: Optional[str] = Header(None),
    ) -> dict[str, Any]:
        await state._verify_contract_token(x_contract_token)
        return await state.clear_faults()

    # ── Receipt 查询:GET /__receipts/{trace_id} ─────────────
    @app.get("/__receipts/{trace_id}")
    async def get_receipts(
        trace_id: str,
        x_contract_token: Optional[str] = Header(None),
    ) -> dict[str, Any]:
        await state._verify_contract_token(x_contract_token)
        receipts = await state.get_receipts(trace_id)
        return {
            "trace_id": trace_id,
            "count": len(receipts),
            "receipts": receipts,
        }

    # ── Outbox 查询(便于验证脚本核对最终输出)───────────────
    @app.get("/__outbox/{chat_id}")
    async def get_outbox(
        chat_id: int,
        x_contract_token: Optional[str] = Header(None),
    ) -> dict[str, Any]:
        await state._verify_contract_token(x_contract_token)
        return {
            "chat_id": chat_id,
            "messages": list(state.outbox.get(chat_id, [])),
        }

    return app


# ════════════════════════════════════════════════════════════════
# CLI 启动入口
# ════════════════════════════════════════════════════════════════
def _generate_random_key() -> str:
    """生成 32 字节随机 hex 密钥(用于 receipt HMAC 签名)。"""
    return secrets.token_hex(32)


def main() -> None:
    """CLI 启动入口(支持 ``python -m tests.support.provider_simulator``)。"""
    parser = argparse.ArgumentParser(
        description="tgjiema Provider Simulator (R76 O3)",
    )
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", type=int, default=8088, help="监听端口")
    parser.add_argument(
        "--contract-token",
        default=os.environ.get("PROVIDER_CONTRACT_TOKEN", ""),
        help="X-Contract-Token 验证令牌(必填,可通过环境变量提供)",
    )
    parser.add_argument(
        "--receipt-key",
        default=os.environ.get("PROVIDER_RECEIPT_KEY", ""),
        help="receipt HMAC 签名密钥(hex;可通过环境变量提供)",
    )
    parser.add_argument(
        "--ci-token",
        default=os.environ.get("PROVIDER_CI_TOKEN", DEFAULT_CI_TOKEN),
        help=f"模拟器接受的 bot token(默认: {DEFAULT_CI_TOKEN})",
    )
    parser.add_argument(
        "--auto-generate-key",
        action="store_true",
        help="若未提供 receipt-key,生成单次 run 临时密钥",
    )
    args = parser.parse_args()

    if not args.contract_token:
        # 自动生成(便于本地开发;CI 必须显式传入)
        args.contract_token = secrets.token_hex(16)
        print(
            f"[provider-sim] auto-generated contract token: "
            f"{args.contract_token}",
        )

    if not args.receipt_key and args.auto_generate_key:
        args.receipt_key = _generate_random_key()
        print(f"[provider-sim] auto-generated receipt key: {args.receipt_key}")

    state = ProviderSimulator(
        contract_token=args.contract_token,
        receipt_key=args.receipt_key,
        ci_token=args.ci_token,
    )
    app = create_app(state)

    print(f"[provider-sim] starting on {args.host}:{args.port}")
    print(f"[provider-sim] ci_token={args.ci_token}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
