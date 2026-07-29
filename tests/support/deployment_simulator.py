"""R76 O10: Deployment Simulator for CI/Local Secretless Testing.

整改背景(R76 终审报告 O10 / P0-08 / P0-09):
    ``scripts/deployment_state_machine.py`` 在 CI 中需要真实 ``DEPLOY_HOOK_URL``
    和 ``DEPLOY_PROBE_URL``,否则状态机无法跨过 ``deploying`` → ``deployed``
    阶段。生产环境之外(secretless CI / 本地演练)没有真实部署目标,
    但仍需验证状态机的:
        - 状态转换合法性(init → deploying → deployed → verified / failed)
        - 探针超时 / RepoDigest drift / 业务探针失败 等故障分支
        - 状态持久化与重试语义(终态不可再转换)

    本模拟器提供与生产部署 webhook/health probe 协议兼容的本地 HTTP 服务:
        - ``POST /deploy-hook``       接受部署触发,可选延迟/故障注入
        - ``GET  /health``            返回 image_repo_digest + runtime_config_digest
        - ``GET  /api/v1/status``     业务探针响应
        - ``POST /__scenario``        设置下一次 deploy/probe 的故障场景
        - ``GET  /__receipts``        返回所有 deploy-hook 调用记录(审计)

隔离:
    - 仅出现在 secretless Compose profile / CI 测试环境
    - 生产镜像 Dockerfile 不得 COPY tests/support/
    - scanner 阻断 ``DEPLOY_HOOK_URL`` / ``DEPLOY_PROBE_URL`` 指向 simulator 的生产配置

场景(通过 ``POST /__scenario`` 设置):
    - ``success``                  默认 — 全程 2xx,digest 匹配
    - ``hook_timeout``             deploy-hook 延迟 > timeout,触发状态机失败
    - ``hook_http_500``            deploy-hook 返回 500
    - ``probe_timeout``            /health 持续返回 503,探针超时
    - ``digest_drift``             /health 返回不匹配的 image_repo_digest
    - ``runtime_config_drift``     /health 返回不匹配的 runtime_config_digest
    - ``business_probe_failure``   /api/v1/status 返回 status=error
    - ``business_probe_http_500``  /api/v1/status 返回 500

启动:
    # 单机/CI 直接启动
    python -m tests.support.deployment_simulator \\
        --host 0.0.0.0 --port 8099 \\
        --expected-image-repo-digest "ghcr.io/owner/repo@sha256:abc..." \\
        --expected-runtime-config-digest "sha256:def..."

    # Docker Compose 中作为 deploy-sim 服务启动(见 docker-compose.secretless.yml)

协议契约:
    与 ``scripts/deployment_state_machine.py`` 严格对齐:
        - deploy hook 接受 JSON: {production_tag, source_sha, image_repo_digest,
          runtime_config_digest, deployment_id}
        - /health 返回 JSON: {status, image_repo_digest, runtime_config_digest}
        - /api/v1/status 返回 JSON: {status, ready}
"""
from __future__ import annotations

import argparse
import asyncio
import json
import threading
from dataclasses import dataclass, field
from typing import Any

import uvicorn

# 使用项目已有 FastAPI/uvicorn 依赖(与 provider_simulator 一致)
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# ════════════════════════════════════════════════════════════════
# 常量
# ════════════════════════════════════════════════════════════════

SCHEMA_VERSION = "deployment-sim/v1"

# 支持的故障场景
SCENARIOS: frozenset[str] = frozenset({
    "success",
    "hook_timeout",
    "hook_http_500",
    "probe_timeout",
    "digest_drift",
    "runtime_config_drift",
    "business_probe_failure",
    "business_probe_http_500",
})

DEFAULT_SCENARIO = "success"

# hook_timeout 场景下 sleep 秒数(> state machine 的 DEFAULT_HOOK_TIMEOUT_SECONDS=30)
HOOK_TIMEOUT_SLEEP_SECONDS = 35


# ════════════════════════════════════════════════════════════════
# 状态
# ════════════════════════════════════════════════════════════════


@dataclass
class SimulatorState:
    """模拟器运行时状态(线程安全 — 通过 _lock 保护)。"""

    expected_image_repo_digest: str = ""
    expected_runtime_config_digest: str = ""
    current_scenario: str = DEFAULT_SCENARIO
    # 审计:所有 deploy-hook 调用记录
    hook_receipts: list[dict[str, Any]] = field(default_factory=list)
    # 探针调用计数(用于 digest_drift / probe_timeout 在多次重试后切换行为)
    probe_call_count: int = 0
    business_probe_call_count: int = 0


# 全局状态(单进程模拟器)
_state = SimulatorState()
_lock = threading.Lock()


def _set_scenario(scenario: str) -> None:
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario: {scenario!r} (allowed: {sorted(SCENARIOS)})")
    with _lock:
        _state.current_scenario = scenario
        _state.probe_call_count = 0
        _state.business_probe_call_count = 0


def _get_scenario() -> str:
    with _lock:
        return _state.current_scenario


def _increment_probe_count() -> int:
    with _lock:
        _state.probe_call_count += 1
        return _state.probe_call_count


def _increment_business_probe_count() -> int:
    with _lock:
        _state.business_probe_call_count += 1
        return _state.business_probe_call_count


def _append_receipt(receipt: dict[str, Any]) -> None:
    with _lock:
        _state.hook_receipts.append(receipt)


def _get_receipts() -> list[dict[str, Any]]:
    with _lock:
        return list(_state.hook_receipts)


# ════════════════════════════════════════════════════════════════
# FastAPI 应用
# ════════════════════════════════════════════════════════════════


app = FastAPI(
    title="R76 O10 Deployment Simulator",
    description="Local deployment target for secretless CI / local testing",
    version=SCHEMA_VERSION,
)


@app.get("/health")
def health() -> JSONResponse:
    """模拟生产实例的 /health 端点。

    返回 ``image_repo_digest`` 和 ``runtime_config_digest``,供状态机的
    ``wait_for_deployed`` 阶段校验。

    故障注入:
        - ``probe_timeout``        — 持续返回 503,触发状态机超时
        - ``digest_drift``         — 返回不匹配的 image digest
        - ``runtime_config_drift`` — 返回不匹配的 runtime config digest
    """
    call_count = _increment_probe_count()
    scenario = _get_scenario()

    if scenario == "probe_timeout":
        return JSONResponse(
            status_code=503,
            content={
                "status": "starting",
                "detail": f"probe_timeout scenario (call #{call_count})",
            },
        )

    expected_digest = _state.expected_image_repo_digest
    expected_config = _state.expected_runtime_config_digest

    if scenario == "digest_drift":
        # 返回不匹配的 image digest(供非 Secretless 平台身份路径测试)。
        return JSONResponse(
            status_code=200,
            content={
                "status": "ok",
                "image_repo_digest": "ghcr.io/wrong/repo@sha256:" + "0" * 64,
                "runtime_config_digest": expected_config,
                "scenario": "digest_drift",
                "call": call_count,
            },
        )

    if scenario == "runtime_config_drift":
        # Secretless 模式的 image identity 来自已签名 Step 14 manifest；配置漂移
        # 仍必须通过独立 health response 被状态机识别并 fail-closed。
        return JSONResponse(
            status_code=200,
            content={
                "status": "ok",
                "image_repo_digest": expected_digest,
                "runtime_config_digest": "sha256:" + "1" * 64,
                "scenario": "runtime_config_drift",
                "call": call_count,
            },
        )

    # success / hook_timeout / hook_http_500 / business_probe_* — 正常返回 /health
    # R80: wildcard 模式 — expected 为空时回显 deploy-hook 收到的 digest
    resp_digest = expected_digest
    resp_config = expected_config
    if not resp_digest or not resp_config:
        with _lock:
            if _state.hook_receipts:
                last_payload = _state.hook_receipts[-1].get("payload", {})
                if not resp_digest:
                    resp_digest = last_payload.get("image_repo_digest", "")
                if not resp_config:
                    resp_config = last_payload.get("runtime_config_digest", "")
    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "image_repo_digest": resp_digest,
            "runtime_config_digest": resp_config,
            "scenario": scenario,
            "call": call_count,
        },
    )


@app.get("/api/v1/status")
def api_status() -> JSONResponse:
    """模拟生产实例的业务探针端点。

    返回 ``{status, ready}`` 供状态机的 ``verify_business_probe`` 校验。

    故障注入:
        - ``business_probe_failure``   — 返回 200 但 status=error
        - ``business_probe_http_500``  — 返回 500
    """
    call_count = _increment_business_probe_count()
    scenario = _get_scenario()

    if scenario == "business_probe_http_500":
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "detail": f"business_probe_http_500 scenario (call #{call_count})",
            },
        )

    if scenario == "business_probe_failure":
        return JSONResponse(
            status_code=200,
            content={
                "status": "error",
                "ready": False,
                "detail": f"business_probe_failure scenario (call #{call_count})",
            },
        )

    # success / 其他场景 — 业务探针通过
    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "ready": True,
            "scenario": scenario,
            "call": call_count,
        },
    )


@app.post("/deploy-hook")
async def deploy_hook(request: Request) -> JSONResponse:
    """模拟生产部署 webhook。

    接受 ``{production_tag, source_sha, image_repo_digest,
    runtime_config_digest, deployment_id}`` 并记录到 receipt。

    故障注入:
        - ``hook_timeout``    — sleep 35s(超过状态机 30s timeout)
        - ``hook_http_500``   — 返回 500
    """
    scenario = _get_scenario()

    # 解析请求体
    try:
        body_bytes = await request.body()
        body_text = body_bytes.decode("utf-8", errors="replace")
        payload: dict[str, Any] = json.loads(body_text) if body_text else {}
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return JSONResponse(
            status_code=400,
            content={"error": f"invalid request body: {e}"},
        )

    receipt = {
        "received_at": _now_iso(),
        "scenario": scenario,
        "payload": payload,
    }
    _append_receipt(receipt)

    if scenario == "hook_timeout":
        # sleep 超过状态机的 DEFAULT_HOOK_TIMEOUT_SECONDS=30
        await asyncio.sleep(HOOK_TIMEOUT_SLEEP_SECONDS)
        return JSONResponse(
            status_code=200,
            content={"status": "accepted", "detail": "hook_timeout scenario (delayed response)"},
        )

    if scenario == "hook_http_500":
        return JSONResponse(
            status_code=500,
            content={"status": "error", "detail": "hook_http_500 scenario"},
        )

    # success / 其他场景 — 接受部署
    return JSONResponse(
        status_code=202,
        content={
            "status": "accepted",
            "deployment_id": payload.get("deployment_id", ""),
            "production_tag": payload.get("production_tag", ""),
            "scenario": scenario,
        },
    )


@app.post("/__scenario")
async def set_scenario(request: Request) -> JSONResponse:
    """设置下一次 deploy/probe 的故障场景。

    请求体: ``{"scenario": "success|hook_timeout|hook_http_500|probe_timeout|digest_drift|runtime_config_drift|business_probe_failure|business_probe_http_500"}``
    """
    try:
        body_bytes = await request.body()
        body_text = body_bytes.decode("utf-8", errors="replace")
        payload = json.loads(body_text) if body_text else {}
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return JSONResponse(
            status_code=400,
            content={"error": f"invalid request body: {e}"},
        )
    scenario = str(payload.get("scenario", ""))
    if scenario not in SCENARIOS:
        return JSONResponse(
            status_code=400,
            content={
                "error": f"unknown scenario: {scenario!r}",
                "allowed": sorted(SCENARIOS),
            },
        )
    _set_scenario(scenario)
    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "scenario": scenario,
            "message": f"scenario set to {scenario!r}",
        },
    )


@app.get("/__receipts")
def get_receipts() -> JSONResponse:
    """返回所有 deploy-hook 调用记录(审计用)。"""
    return JSONResponse(
        status_code=200,
        content={
            "schema_version": SCHEMA_VERSION,
            "receipts": _get_receipts(),
            "count": len(_get_receipts()),
        },
    )


@app.get("/__state")
def get_state() -> JSONResponse:
    """返回模拟器当前状态(调试用)。"""
    with _lock:
        return JSONResponse(
            status_code=200,
            content={
                "schema_version": SCHEMA_VERSION,
                "current_scenario": _state.current_scenario,
                "expected_image_repo_digest": _state.expected_image_repo_digest,
                "expected_runtime_config_digest": _state.expected_runtime_config_digest,
                "probe_call_count": _state.probe_call_count,
                "business_probe_call_count": _state.business_probe_call_count,
                "hook_receipts_count": len(_state.hook_receipts),
            },
        )


@app.post("/__reset")
def reset_state() -> JSONResponse:
    """重置模拟器状态到默认(用于测试间隔离)。"""
    with _lock:
        _state.current_scenario = DEFAULT_SCENARIO
        _state.probe_call_count = 0
        _state.business_probe_call_count = 0
        _state.hook_receipts.clear()
    return JSONResponse(
        status_code=200,
        content={"status": "ok", "message": "simulator state reset to defaults"},
    )


# ════════════════════════════════════════════════════════════════
# 工具
# ════════════════════════════════════════════════════════════════


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


# ════════════════════════════════════════════════════════════════
# CLI 入口
# ════════════════════════════════════════════════════════════════


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deployment_simulator",
        description="R76 O10: Deployment Simulator (CI/local secretless testing)",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8099, help="Bind port (default: 8099)")
    parser.add_argument(
        "--expected-image-repo-digest",
        default="",
        help="Expected image_repo_digest (ghcr.io/owner/repo@sha256:<64-hex>). Empty=wildcard (accept any)",
    )
    parser.add_argument(
        "--expected-runtime-config-digest",
        default="",
        help="Expected runtime_config_digest (sha256:<64-hex>). Empty=wildcard (accept any)",
    )
    parser.add_argument(
        "--initial-scenario",
        default=DEFAULT_SCENARIO,
        choices=sorted(SCENARIOS),
        help=f"Initial scenario (default: {DEFAULT_SCENARIO})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # 初始化全局状态
    with _lock:
        _state.expected_image_repo_digest = args.expected_image_repo_digest
        _state.expected_runtime_config_digest = args.expected_runtime_config_digest
        _state.current_scenario = args.initial_scenario
        _state.probe_call_count = 0
        _state.business_probe_call_count = 0
        _state.hook_receipts.clear()

    print("=== R76 O10 Deployment Simulator ===")
    print(f"  bind:                          {args.host}:{args.port}")
    print(f"  expected_image_repo_digest:    {_state.expected_image_repo_digest}")
    print(f"  expected_runtime_config_digest:{_state.expected_runtime_config_digest}")
    print(f"  initial_scenario:              {_state.current_scenario}")
    print()
    print("Endpoints:")
    print("  POST /deploy-hook         模拟部署 webhook")
    print("  GET  /health               模拟 /health 探针")
    print("  GET  /api/v1/status        模拟业务探针")
    print("  POST /__scenario           设置故障场景")
    print("  GET  /__receipts           查看 deploy-hook 调用记录")
    print("  GET  /__state              查看模拟器状态")
    print("  POST /__reset              重置状态")
    print()
    print(f"Scenarios: {sorted(SCENARIOS)}")
    print()

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
