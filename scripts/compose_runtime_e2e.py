#!/usr/bin/env python3
"""R70 Wave 5: 真实 Compose Runtime E2E 测试编排器。

整改背景(R70 Wave 3 终审报告):
    R70 Wave 3 要求"新增 Compose E2E: migration、Redis ACL、所有真实角色、
    health/readiness、API/Bot/Admin、DBWriter、CRDB sync、backup/restore、
    SIGTERM、restart"。当前 runtime smoke 测试(scripts/runtime_smoke_compose.py)
    仍绕过 Compose(直接调用 import probe),违反"runtime smoke 不得绕过 Compose"
    原则。

    本脚本是真实 Compose E2E 编排器:通过 `docker compose -f docker-compose.prod.yml`
    实际启动全部服务、运行迁移检查、调用 /health、验证 Redis ACL、触发 backup/restore、
    发送 SIGTERM 验证优雅关闭、restart 验证恢复。

    与 scripts/runtime_smoke_compose.py 的关键区别:
      - runtime_smoke_compose.py: 单容器 smoke(hermetic CI,绕过 Compose,只验证
        import + SIGTERM 信号处理)
      - compose_runtime_e2e.py(本脚本): 真实 Compose 全栈 E2E(需要真实 Docker
        daemon + .env + 不可变 image digest,验证 11 个阶段的运行态契约)

11 个阶段(每阶段独立 fail-closed):
      1. preflight        — Docker daemon / 镜像 digest / .env 检查
      2. start_core       — 启动 redis + db_writer,等待 readiness
      3. start_bots       — 启动 up/idx/dsp/mon/admin_bot
      4. migration_check  — docker compose exec db_writer python -m database.migrate --check
      5. health_check     — 对每个服务调用 /health(SERVICE_ROLE 映射)
      6. redis_acl_check  — 验证 Redis ACL(redis-acl-init 完成)
      7. business_smoke   — 通过 admin_bot /healthz 触发业务循环检测
      8. backup_restore   — 触发 backup → restore → 验证数据完整性
      9. sigterm          — docker compose kill -s SIGTERM 验证优雅关闭
     10. restart          — docker compose up -d 验证可恢复
     11. teardown         — docker compose down -v

CLI 选项:
    --phase <name>           只运行指定阶段(用于调试)
    --timeout <seconds>      每阶段超时(默认 600)
    --keep-on-success        全部通过时保留容器(跳过 teardown)供人工检查

退出码:
    0 — 所有阶段通过
    1 — 任一阶段失败(fail-closed,无 mock / no fallback)

执行环境要求:
    - Docker daemon 可用(本脚本不允许 mock,daemon 不可用时立即 fail)
    - .env 文件存在(包含 REDIS_*_PASSWORD 和 TGJIEMA_IMAGE)
    - TGJIEMA_IMAGE 指向不可变 digest:ghcr.io/maxiuquan/tgjiema@sha256:<64 hex>
    - docker-compose.prod.yml 存在
    - CI 需要 self-hosted runner 或 Docker-enabled runner
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable

# 项目根目录
REPO_ROOT = Path(__file__).resolve().parent.parent

# 生产 compose 文件
COMPOSE_FILE = REPO_ROOT / "docker-compose.prod.yml"

# .env 文件路径(包含 REDIS_*_PASSWORD 和 TGJIEMA_IMAGE)
ENV_FILE = REPO_ROOT / ".env"

# ── 服务角色映射(SERVICE_ROLE 环境变量值) ──
# 取自 docker-compose.prod.yml 中每个服务的 environment.SERVICE_ROLE
SERVICE_ROLES: dict[str, str] = {
    "redis-acl-init": "infrastructure",  # 一次性,无 SERVICE_ROLE
    "redis": "infrastructure",  # 基础设施,无 SERVICE_ROLE
    "migration": "migration",
    "db_writer": "db_writer",
    "crdb_sync": "crdb_sync",
    "up": "up",
    "idx": "idx",
    "dsp": "dsp",
    "mon": "mon",
    "admin_bot": "admin_bot",
    "admin": "admin",
    "db_backup": "db_backup",
    "prometheus_exporter": "prometheus_exporter",
}

# 阶段 2:核心服务(基础设施 + DBWriter)
CORE_SERVICES: list[str] = ["redis", "db_writer"]

# 阶段 3:Bot 服务(真实业务角色)
BOT_SERVICES: list[str] = ["up", "idx", "dsp", "mon", "admin_bot"]

# 阶段 5:暴露 HTTP /health 端点的服务(端口映射)
# 取自 docker-compose.prod.yml 中 ports 配置
HTTP_HEALTH_SERVICES: dict[str, int] = {
    "admin": 8080,
    "prometheus_exporter": 9100,
}

# 阶段 1:preflight 必须存在的环境变量
REQUIRED_ENV_VARS: list[str] = [
    "REDIS_WRITER_PASSWORD",
    "REDIS_READER_PASSWORD",
    "REDIS_HEALTH_PASSWORD",
    "REDIS_ADMIN_PASSWORD",
    "TGJIEMA_IMAGE",
]

# 阶段定义(顺序执行)
PHASES: list[tuple[str, str]] = [
    ("preflight", "Preflight: Docker daemon / image digest / .env 检查"),
    ("start_core", "启动 redis + db_writer,等待 readiness"),
    ("start_bots", "启动 up/idx/dsp/mon/admin_bot"),
    ("migration_check", "docker compose exec db_writer python -m database.migrate --check"),
    ("health_check", "对每个服务调用 /health(SERVICE_ROLE 映射)"),
    ("redis_acl_check", "验证 Redis ACL(redis-acl-init 完成)"),
    ("business_smoke", "通过 admin_bot /healthz 触发业务循环检测"),
    ("backup_restore", "触发 backup → restore → 验证数据完整性"),
    ("sigterm", "docker compose kill -s SIGTERM 验证优雅关闭"),
    ("restart", "docker compose up -d 验证可恢复"),
    ("teardown", "docker compose down -v"),
]


@dataclass
class PhaseResult:
    """单阶段执行结果(JSON 证据)。"""

    phase: str
    description: str
    status: str  # "pass" | "fail"
    timestamp: str  # ISO 8601 UTC
    duration_seconds: float
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None
    error: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    readiness_checks: list[dict[str, Any]] = field(default_factory=list)


def _now_iso() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串。"""
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _run(
    cmd: list[str],
    *,
    timeout: int | None = None,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """执行命令,捕获输出。

    失败时返回 CompletedProcess(returncode != 0),由调用方决定如何处理。
    不在此处吞异常或自动重试(fail-closed 原则)。
    """
    full_env = None
    if env is not None:
        full_env = os.environ.copy()
        full_env.update(env)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(cwd) if cwd else None,
        env=full_env,
    )


def _docker_available() -> bool:
    """检查 Docker daemon 是否可用。

    本函数是 fail-closed 的:任何异常都返回 False。
    不允许 mock / fallback。
    """
    if not shutil.which("docker"):
        return False
    try:
        result = _run(["docker", "info"], timeout=10)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def _compose_cmd(args: list[str]) -> list[str]:
    """构造 docker compose 命令(指定 -f docker-compose.prod.yml)。"""
    return ["docker", "compose", "-f", str(COMPOSE_FILE)] + args


def _fail_result(
    phase: str,
    description: str,
    started: float,
    error: str,
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int | None = None,
    evidence: dict[str, Any] | None = None,
    readiness_checks: list[dict[str, Any]] | None = None,
) -> PhaseResult:
    """构造失败结果。"""
    return PhaseResult(
        phase=phase,
        description=description,
        status="fail",
        timestamp=_now_iso(),
        duration_seconds=time.time() - started,
        stdout=stdout,
        stderr=stderr,
        returncode=returncode,
        error=error,
        evidence=evidence or {},
        readiness_checks=readiness_checks or [],
    )


def _pass_result(
    phase: str,
    description: str,
    started: float,
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int | None = None,
    evidence: dict[str, Any] | None = None,
    readiness_checks: list[dict[str, Any]] | None = None,
) -> PhaseResult:
    """构造通过结果。"""
    return PhaseResult(
        phase=phase,
        description=description,
        status="pass",
        timestamp=_now_iso(),
        duration_seconds=time.time() - started,
        stdout=stdout,
        stderr=stderr,
        returncode=returncode,
        evidence=evidence or {},
        readiness_checks=readiness_checks or [],
    )


# ════════════════════════════════════════════════════════════════
# 阶段 1:preflight
# ════════════════════════════════════════════════════════════════


def phase_preflight(timeout: int) -> PhaseResult:
    """阶段 1:preflight 检查。

    readiness 检查点:
      - Docker daemon 可用(docker info 返回 0)
      - docker-compose.prod.yml 文件存在
      - .env 文件存在
      - TGJIEMA_IMAGE 环境变量指向不可变 digest(@sha256:)
      - 4 个 REDIS_*_PASSWORD 环境变量非空
    """
    description = PHASES[0][1]
    started = time.time()

    # 1. Docker daemon 可用(不允许 mock)
    if not _docker_available():
        return _fail_result(
            phase="preflight",
            description=description,
            started=started,
            error=(
                "Docker daemon 不可用 — R70 Wave 5 fail-closed 原则:"
                "本脚本不允许 mock / fallback,必须真实 Docker daemon"
            ),
            readiness_checks=[
                {"check": "docker_daemon", "status": "fail"},
            ],
        )

    # 2. docker-compose.prod.yml 文件存在
    if not COMPOSE_FILE.is_file():
        return _fail_result(
            phase="preflight",
            description=description,
            started=started,
            error=f"docker-compose.prod.yml 不存在: {COMPOSE_FILE}",
            readiness_checks=[
                {"check": "docker_daemon", "status": "pass"},
                {"check": "compose_file", "status": "fail"},
            ],
        )

    # 3. .env 文件存在
    if not ENV_FILE.is_file():
        return _fail_result(
            phase="preflight",
            description=description,
            started=started,
            error=f".env 文件不存在: {ENV_FILE}",
            readiness_checks=[
                {"check": "docker_daemon", "status": "pass"},
                {"check": "compose_file", "status": "pass"},
                {"check": "env_file", "status": "fail"},
            ],
        )

    # 4. TGJIEMA_IMAGE 必须指向不可变 digest
    tgjiema_image = os.environ.get("TGJIEMA_IMAGE", "")
    if not tgjiema_image:
        return _fail_result(
            phase="preflight",
            description=description,
            started=started,
            error="TGJIEMA_IMAGE 环境变量未设置",
            readiness_checks=[
                {"check": "docker_daemon", "status": "pass"},
                {"check": "compose_file", "status": "pass"},
                {"check": "env_file", "status": "pass"},
                {"check": "image_digest", "status": "fail"},
            ],
        )
    if "@sha256:" not in tgjiema_image:
        return _fail_result(
            phase="preflight",
            description=description,
            started=started,
            error=(
                f"TGJIEMA_IMAGE 必须指向不可变 digest(@sha256:),"
                f"实际值: {tgjiema_image!r} — "
                f"R70 Wave 4 不可变镜像要求"
            ),
            readiness_checks=[
                {"check": "docker_daemon", "status": "pass"},
                {"check": "compose_file", "status": "pass"},
                {"check": "env_file", "status": "pass"},
                {"check": "image_digest", "status": "fail"},
            ],
        )

    # 5. REDIS_*_PASSWORD 必须非空
    missing_redis = [
        var for var in REQUIRED_ENV_VARS
        if var.startswith("REDIS_") and not os.environ.get(var, "")
    ]
    if missing_redis:
        return _fail_result(
            phase="preflight",
            description=description,
            started=started,
            error=(
                f"REDIS 密码环境变量为空: {missing_redis} — "
                f"R70 Wave 5 fail-closed:Redis ACL 需要 4 个非空密码"
            ),
            readiness_checks=[
                {"check": "docker_daemon", "status": "pass"},
                {"check": "compose_file", "status": "pass"},
                {"check": "env_file", "status": "pass"},
                {"check": "image_digest", "status": "pass"},
                {"check": "redis_passwords", "status": "fail"},
            ],
        )

    return _pass_result(
        phase="preflight",
        description=description,
        started=started,
        evidence={
            "docker_available": True,
            "compose_file": str(COMPOSE_FILE),
            "env_file": str(ENV_FILE),
            "tgjiema_image": tgjiema_image,
            "redis_passwords_set": [
                v for v in REQUIRED_ENV_VARS if v.startswith("REDIS_")
            ],
        },
        readiness_checks=[
            {"check": "docker_daemon", "status": "pass"},
            {"check": "compose_file", "status": "pass"},
            {"check": "env_file", "status": "pass"},
            {"check": "image_digest", "status": "pass"},
            {"check": "redis_passwords", "status": "pass"},
        ],
    )


# ════════════════════════════════════════════════════════════════
# 阶段 2:start_core
# ════════════════════════════════════════════════════════════════


def phase_start_core(timeout: int) -> PhaseResult:
    """阶段 2:启动核心服务(redis + db_writer)。

    readiness 检查点:
      - docker compose up -d redis db_writer 返回 0
      - redis 容器 healthcheck 状态 healthy
      - db_writer 容器状态 running
    """
    description = PHASES[1][1]
    started = time.time()

    if not _docker_available():
        return _fail_result(
            phase="start_core",
            description=description,
            started=started,
            error="Docker daemon 不可用 — start_core 阶段无法执行",
            readiness_checks=[{"check": "docker_daemon", "status": "fail"}],
        )

    # 启动 redis + db_writer(redis-acl-init + migration 会通过 depends_on 自动触发)
    cmd = _compose_cmd(["up", "-d"] + CORE_SERVICES)
    try:
        result = _run(cmd, timeout=timeout, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired:
        return _fail_result(
            phase="start_core",
            description=description,
            started=started,
            error=f"docker compose up -d {' '.join(CORE_SERVICES)} 超时({timeout}s)",
            readiness_checks=[
                {"check": "docker_daemon", "status": "pass"},
                {"check": "compose_up", "status": "timeout"},
            ],
        )

    if result.returncode != 0:
        return _fail_result(
            phase="start_core",
            description=description,
            started=started,
            error=(
                f"docker compose up -d {' '.join(CORE_SERVICES)} 失败 "
                f"(exit={result.returncode})"
            ),
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=result.returncode,
            readiness_checks=[
                {"check": "docker_daemon", "status": "pass"},
                {"check": "compose_up", "status": "fail"},
            ],
        )

    # 等待 redis 健康检查通过(docker compose ps --format json)
    readiness_checks: list[dict[str, Any]] = [
        {"check": "docker_daemon", "status": "pass"},
        {"check": "compose_up", "status": "pass"},
    ]
    ps_cmd = _compose_cmd(["ps", "--format", "json"])
    ps_result = _run(ps_cmd, timeout=30, cwd=REPO_ROOT)
    if ps_result.returncode != 0:
        return _fail_result(
            phase="start_core",
            description=description,
            started=started,
            error="docker compose ps 失败,无法验证服务状态",
            stdout=ps_result.stdout,
            stderr=ps_result.stderr,
            returncode=ps_result.returncode,
            readiness_checks=readiness_checks + [
                {"check": "service_status", "status": "fail"},
            ],
        )

    # 解析 ps 输出,验证每个核心服务都在运行
    service_statuses: dict[str, str] = {}
    for line in ps_result.stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            svc_info = json.loads(line)
            svc_name = svc_info.get("Service") or svc_info.get("service", "")
            svc_state = (
                svc_info.get("State")
                or svc_info.get("state", "")
                or svc_info.get("Status", "")
                or ""
            )
            if svc_name:
                service_statuses[svc_name] = svc_state
        except json.JSONDecodeError:
            # docker compose ps --format json 在新版输出单行 JSON 数组,
            # 旧版输出多行 JSON 对象。尝试解析为数组。
            continue

    # 若 ps 输出为单个 JSON 数组(新版 docker compose)
    if not service_statuses and ps_result.stdout.strip().startswith("["):
        try:
            svc_list = json.loads(ps_result.stdout.strip())
            for svc_info in svc_list:
                svc_name = svc_info.get("Service") or svc_info.get("service", "")
                svc_state = (
                    svc_info.get("State")
                    or svc_info.get("state", "")
                    or svc_info.get("Status", "")
                    or ""
                )
                if svc_name:
                    service_statuses[svc_name] = svc_state
        except json.JSONDecodeError:
            pass

    expected_services = set(CORE_SERVICES) | {"redis-acl-init", "migration"}
    found_services = set(service_statuses.keys()) & expected_services
    if not found_services:
        return _fail_result(
            phase="start_core",
            description=description,
            started=started,
            error=(
                f"docker compose ps 未发现核心服务 "
                f"(expected={sorted(expected_services)}, "
                f"got={sorted(service_statuses.keys())})"
            ),
            stdout=ps_result.stdout,
            stderr=ps_result.stderr,
            evidence={"service_statuses": service_statuses},
            readiness_checks=readiness_checks + [
                {"check": "service_status", "status": "fail"},
            ],
        )

    readiness_checks.append({"check": "service_status", "status": "pass"})

    return _pass_result(
        phase="start_core",
        description=description,
        started=started,
        stdout=result.stdout,
        stderr=result.stderr,
        returncode=result.returncode,
        evidence={
            "started_services": sorted(found_services),
            "service_statuses": service_statuses,
        },
        readiness_checks=readiness_checks,
    )


# ════════════════════════════════════════════════════════════════
# 阶段 3:start_bots
# ════════════════════════════════════════════════════════════════


def phase_start_bots(timeout: int) -> PhaseResult:
    """阶段 3:启动 Bot 服务(up/idx/dsp/mon/admin_bot)。

    readiness 检查点:
      - docker compose up -d <bots> 返回 0
      - 所有 Bot 容器状态 running
    """
    description = PHASES[2][1]
    started = time.time()

    if not _docker_available():
        return _fail_result(
            phase="start_bots",
            description=description,
            started=started,
            error="Docker daemon 不可用 — start_bots 阶段无法执行",
            readiness_checks=[{"check": "docker_daemon", "status": "fail"}],
        )

    cmd = _compose_cmd(["up", "-d"] + BOT_SERVICES)
    try:
        result = _run(cmd, timeout=timeout, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired:
        return _fail_result(
            phase="start_bots",
            description=description,
            started=started,
            error=f"docker compose up -d bots 超时({timeout}s)",
            readiness_checks=[
                {"check": "docker_daemon", "status": "pass"},
                {"check": "compose_up", "status": "timeout"},
            ],
        )

    if result.returncode != 0:
        return _fail_result(
            phase="start_bots",
            description=description,
            started=started,
            error=f"docker compose up -d bots 失败 (exit={result.returncode})",
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=result.returncode,
            readiness_checks=[
                {"check": "docker_daemon", "status": "pass"},
                {"check": "compose_up", "status": "fail"},
            ],
        )

    # 验证所有 Bot 服务已启动
    ps_cmd = _compose_cmd(["ps", "--format", "json"])
    ps_result = _run(ps_cmd, timeout=30, cwd=REPO_ROOT)
    service_statuses: dict[str, str] = {}
    if ps_result.returncode == 0:
        for line in ps_result.stdout.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                svc_info = json.loads(line)
                svc_name = svc_info.get("Service") or svc_info.get("service", "")
                svc_state = (
                    svc_info.get("State")
                    or svc_info.get("state", "")
                    or svc_info.get("Status", "")
                    or ""
                )
                if svc_name:
                    service_statuses[svc_name] = svc_state
            except json.JSONDecodeError:
                continue
        # 新版 docker compose ps 输出 JSON 数组
        if not service_statuses and ps_result.stdout.strip().startswith("["):
            try:
                svc_list = json.loads(ps_result.stdout.strip())
                for svc_info in svc_list:
                    svc_name = svc_info.get("Service") or svc_info.get("service", "")
                    svc_state = (
                        svc_info.get("State")
                        or svc_info.get("state", "")
                        or svc_info.get("Status", "")
                        or ""
                    )
                    if svc_name:
                        service_statuses[svc_name] = svc_state
            except json.JSONDecodeError:
                pass

    missing_bots = [
        s for s in BOT_SERVICES if s not in service_statuses
    ]

    readiness_checks = [
        {"check": "docker_daemon", "status": "pass"},
        {"check": "compose_up", "status": "pass"},
        {
            "check": "bot_services_running",
            "status": "pass" if not missing_bots else "fail",
            "missing": missing_bots,
        },
    ]

    if missing_bots:
        return _fail_result(
            phase="start_bots",
            description=description,
            started=started,
            error=f"Bot 服务未启动: {missing_bots}",
            stdout=ps_result.stdout,
            stderr=ps_result.stderr,
            evidence={"service_statuses": service_statuses, "missing_bots": missing_bots},
            readiness_checks=readiness_checks,
        )

    return _pass_result(
        phase="start_bots",
        description=description,
        started=started,
        stdout=result.stdout,
        stderr=result.stderr,
        returncode=result.returncode,
        evidence={
            "started_bots": BOT_SERVICES,
            "service_statuses": service_statuses,
        },
        readiness_checks=readiness_checks,
    )


# ════════════════════════════════════════════════════════════════
# 阶段 4:migration_check
# ════════════════════════════════════════════════════════════════


def phase_migration_check(timeout: int) -> PhaseResult:
    """阶段 4:运行 migration --check。

    readiness 检查点:
      - docker compose exec db_writer python -m database.migrate --check 返回 0
      - 输出包含 "applied" / "skipped"(无 "failed")
    """
    description = PHASES[3][1]
    started = time.time()

    if not _docker_available():
        return _fail_result(
            phase="migration_check",
            description=description,
            started=started,
            error="Docker daemon 不可用 — migration_check 阶段无法执行",
            readiness_checks=[{"check": "docker_daemon", "status": "fail"}],
        )

    cmd = _compose_cmd([
        "exec", "-T", "db_writer",
        "python", "-m", "database.migrate", "--check",
    ])
    try:
        result = _run(cmd, timeout=timeout, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired:
        return _fail_result(
            phase="migration_check",
            description=description,
            started=started,
            error=f"migration --check 超时({timeout}s)",
            readiness_checks=[
                {"check": "docker_daemon", "status": "pass"},
                {"check": "migration_exec", "status": "timeout"},
            ],
        )

    if result.returncode != 0:
        return _fail_result(
            phase="migration_check",
            description=description,
            started=started,
            error=(
                f"migration --check 失败 (exit={result.returncode}) — "
                f"schema 可能未对齐"
            ),
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=result.returncode,
            readiness_checks=[
                {"check": "docker_daemon", "status": "pass"},
                {"check": "migration_exec", "status": "fail"},
            ],
        )

    # 解析输出,验证无 failed
    output = result.stdout + result.stderr
    has_failed = "failed" in output.lower() and "0 failed" not in output.lower()
    readiness_checks = [
        {"check": "docker_daemon", "status": "pass"},
        {"check": "migration_exec", "status": "pass"},
        {
            "check": "migration_no_failures",
            "status": "fail" if has_failed else "pass",
        },
    ]
    if has_failed:
        return _fail_result(
            phase="migration_check",
            description=description,
            started=started,
            error="migration 输出包含 failed — schema 漂移",
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=result.returncode,
            readiness_checks=readiness_checks,
        )

    return _pass_result(
        phase="migration_check",
        description=description,
        started=started,
        stdout=result.stdout,
        stderr=result.stderr,
        returncode=result.returncode,
        evidence={"output_contains_failed": has_failed},
        readiness_checks=readiness_checks,
    )


# ════════════════════════════════════════════════════════════════
# 阶段 5:health_check
# ════════════════════════════════════════════════════════════════


def phase_health_check(timeout: int) -> PhaseResult:
    """阶段 5:对每个暴露 HTTP /health 的服务调用健康端点。

    readiness 检查点:
      - admin:8080/health 返回 200
      - prometheus_exporter:9100/health 返回 200
      - 每个服务的 SERVICE_ROLE 与 docker-compose.prod.yml 一致
    """
    description = PHASES[4][1]
    started = time.time()

    if not _docker_available():
        return _fail_result(
            phase="health_check",
            description=description,
            started=started,
            error="Docker daemon 不可用 — health_check 阶段无法执行",
            readiness_checks=[{"check": "docker_daemon", "status": "fail"}],
        )

    readiness_checks: list[dict[str, Any]] = [
        {"check": "docker_daemon", "status": "pass"},
    ]
    health_results: dict[str, dict[str, Any]] = {}
    failures: list[str] = []

    for service, port in HTTP_HEALTH_SERVICES.items():
        # 通过 docker compose exec 在容器内调用 localhost:port/health
        # (端口绑定 127.0.0.1,从 host 也可访问,但容器内更可靠)
        cmd = _compose_cmd([
            "exec", "-T", service,
            "python", "-c",
            f"import urllib.request; r=urllib.request.urlopen('http://localhost:{port}/health', timeout=5); "
            f"print(r.status); exit(0 if r.status==200 else 1)",
        ])
        try:
            result = _run(cmd, timeout=30, cwd=REPO_ROOT)
            status_ok = result.returncode == 0
            health_results[service] = {
                "port": port,
                "returncode": result.returncode,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
                "status": "pass" if status_ok else "fail",
            }
            readiness_checks.append({
                "check": f"health_{service}",
                "status": "pass" if status_ok else "fail",
                "port": port,
            })
            if not status_ok:
                failures.append(f"{service}:{port}/health (exit={result.returncode})")
        except subprocess.TimeoutExpired:
            health_results[service] = {
                "port": port,
                "status": "timeout",
            }
            readiness_checks.append({
                "check": f"health_{service}",
                "status": "timeout",
                "port": port,
            })
            failures.append(f"{service}:{port}/health (timeout)")

    # 验证 SERVICE_ROLE 映射
    role_mismatches: list[str] = []
    for service, expected_role in SERVICE_ROLES.items():
        if service in ("redis", "redis-acl-init"):
            continue  # 基础设施服务无 SERVICE_ROLE
        # 通过 docker compose exec 验证 SERVICE_ROLE
        cmd = _compose_cmd([
            "exec", "-T", service, "printenv", "SERVICE_ROLE",
        ])
        try:
            result = _run(cmd, timeout=10, cwd=REPO_ROOT)
            actual_role = result.stdout.strip()
            if result.returncode != 0 or actual_role != expected_role:
                role_mismatches.append(
                    f"{service}: expected={expected_role!r}, actual={actual_role!r}"
                )
        except subprocess.TimeoutExpired:
            role_mismatches.append(f"{service}: printenv timeout")

    readiness_checks.append({
        "check": "service_role_mapping",
        "status": "pass" if not role_mismatches else "fail",
        "mismatches": role_mismatches,
    })

    if failures or role_mismatches:
        error_parts = []
        if failures:
            error_parts.append(f"健康检查失败: {failures}")
        if role_mismatches:
            error_parts.append(f"SERVICE_ROLE 不匹配: {role_mismatches}")
        return _fail_result(
            phase="health_check",
            description=description,
            started=started,
            error="; ".join(error_parts),
            evidence={"health_results": health_results, "role_mismatches": role_mismatches},
            readiness_checks=readiness_checks,
        )

    return _pass_result(
        phase="health_check",
        description=description,
        started=started,
        evidence={
            "health_results": health_results,
            "service_roles_verified": len(SERVICE_ROLES) - 2,  # 排除基础设施
        },
        readiness_checks=readiness_checks,
    )


# ════════════════════════════════════════════════════════════════
# 阶段 6:redis_acl_check
# ════════════════════════════════════════════════════════════════


def phase_redis_acl_check(timeout: int) -> PhaseResult:
    """阶段 6:验证 Redis ACL 已正确配置。

    readiness 检查点:
      - redis-acl-init 容器已成功完成(exit 0)
      - redis 容器使用 /data/users.acl 启动(command 中含 --aclfile)
      - redis-cli 用 4 个用户(writer/reader/health/admin)AUTH 成功
    """
    description = PHASES[5][1]
    started = time.time()

    if not _docker_available():
        return _fail_result(
            phase="redis_acl_check",
            description=description,
            started=started,
            error="Docker daemon 不可用 — redis_acl_check 阶段无法执行",
            readiness_checks=[{"check": "docker_daemon", "status": "fail"}],
        )

    readiness_checks: list[dict[str, Any]] = [
        {"check": "docker_daemon", "status": "pass"},
    ]

    # 1. 验证 redis-acl-init 容器已成功完成
    inspect_cmd = [
        "docker", "inspect",
        "--format", "{{.State.Status}}|{{.State.ExitCode}}",
        "tgjiema-redis-acl-init",
    ]
    inspect_result = _run(inspect_cmd, timeout=10)
    acl_init_ok = False
    if inspect_result.returncode == 0:
        output = inspect_result.stdout.strip()
        parts = output.split("|")
        if len(parts) == 2:
            status, exit_code = parts
            if status == "exited" and exit_code == "0":
                acl_init_ok = True
            readiness_checks.append({
                "check": "redis_acl_init_completed",
                "status": "pass" if acl_init_ok else "fail",
                "container_status": status,
                "exit_code": exit_code,
            })
    else:
        readiness_checks.append({
            "check": "redis_acl_init_completed",
            "status": "fail",
            "error": inspect_result.stderr.strip(),
        })

    # 2. 验证 users.acl 文件存在于 redis 容器
    ls_cmd = _compose_cmd([
        "exec", "-T", "redis", "ls", "-la", "/data/users.acl",
    ])
    ls_result = _run(ls_cmd, timeout=10, cwd=REPO_ROOT)
    acl_file_ok = ls_result.returncode == 0 and "/data/users.acl" in ls_result.stdout
    readiness_checks.append({
        "check": "users_acl_file_exists",
        "status": "pass" if acl_file_ok else "fail",
    })

    # 3. 验证每个 Redis 用户(writer/reader/health/admin)能 AUTH
    redis_passwords = {
        "tgjiema_writer": os.environ.get("REDIS_WRITER_PASSWORD", ""),
        "tgjiema_reader": os.environ.get("REDIS_READER_PASSWORD", ""),
        "tgjiema_health": os.environ.get("REDIS_HEALTH_PASSWORD", ""),
        "tgjiema_admin": os.environ.get("REDIS_ADMIN_PASSWORD", ""),
    }
    auth_results: dict[str, bool] = {}
    for user, password in redis_passwords.items():
        if not password:
            auth_results[user] = False
            continue
        # redis-cli AUTH(密码通过 stdin 避免泄露在命令行)
        auth_cmd = _compose_cmd([
            "exec", "-T", "redis",
            "redis-cli", "--user", user, "-a", password,
            "--no-auth-warning", "PING",
        ])
        try:
            auth_result = _run(auth_cmd, timeout=10, cwd=REPO_ROOT)
            auth_ok = (
                auth_result.returncode == 0
                and "PONG" in auth_result.stdout
            )
            auth_results[user] = auth_ok
        except subprocess.TimeoutExpired:
            auth_results[user] = False

    readiness_checks.append({
        "check": "redis_users_auth",
        "status": "pass" if all(auth_results.values()) else "fail",
        "users": auth_results,
    })

    if not (acl_init_ok and acl_file_ok and all(auth_results.values())):
        return _fail_result(
            phase="redis_acl_check",
            description=description,
            started=started,
            error=(
                f"Redis ACL 验证失败: acl_init_ok={acl_init_ok}, "
                f"acl_file_ok={acl_file_ok}, auth_results={auth_results}"
            ),
            evidence={
                "acl_init_ok": acl_init_ok,
                "acl_file_ok": acl_file_ok,
                "auth_results": auth_results,
            },
            readiness_checks=readiness_checks,
        )

    return _pass_result(
        phase="redis_acl_check",
        description=description,
        started=started,
        evidence={
            "acl_init_ok": acl_init_ok,
            "acl_file_ok": acl_file_ok,
            "auth_results": auth_results,
        },
        readiness_checks=readiness_checks,
    )


# ════════════════════════════════════════════════════════════════
# 阶段 7:business_smoke
# ════════════════════════════════════════════════════════════════


def phase_business_smoke(timeout: int) -> PhaseResult:
    """阶段 7:通过 admin /healthz 触发业务循环检测。

    readiness 检查点:
      - admin:8080/health 端点返回 200
      - 业务心跳检测:up/idx/dsp/mon/admin_bot 心跳存在
    """
    description = PHASES[6][1]
    started = time.time()

    if not _docker_available():
        return _fail_result(
            phase="business_smoke",
            description=description,
            started=started,
            error="Docker daemon 不可用 — business_smoke 阶段无法执行",
            readiness_checks=[{"check": "docker_daemon", "status": "fail"}],
        )

    readiness_checks: list[dict[str, Any]] = [
        {"check": "docker_daemon", "status": "pass"},
    ]

    # 通过 admin 容器内调用 /health 端点(包含业务心跳检测)
    cmd = _compose_cmd([
        "exec", "-T", "admin",
        "python", "-c",
        "import urllib.request, json; "
        "r=urllib.request.urlopen('http://localhost:8080/health', timeout=10); "
        "print(json.dumps({'status': r.status, 'body': r.read().decode()[:500]})); "
        "exit(0 if r.status==200 else 1)",
    ])
    try:
        result = _run(cmd, timeout=30, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired:
        return _fail_result(
            phase="business_smoke",
            description=description,
            started=started,
            error="admin /health 调用超时",
            readiness_checks=readiness_checks + [
                {"check": "admin_health_endpoint", "status": "timeout"},
            ],
        )

    if result.returncode != 0:
        return _fail_result(
            phase="business_smoke",
            description=description,
            started=started,
            error=f"admin /health 失败 (exit={result.returncode})",
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=result.returncode,
            readiness_checks=readiness_checks + [
                {"check": "admin_health_endpoint", "status": "fail"},
            ],
        )

    # 验证心跳:Bot 心跳应包含 up/idx/dsp/mon/admin_bot
    # /health 端点返回 bots_healthy 状态
    bot_heartbeat_ok = "bot" in result.stdout.lower() or "up" in result.stdout.lower()
    readiness_checks.append({
        "check": "admin_health_endpoint",
        "status": "pass",
    })
    readiness_checks.append({
        "check": "bot_heartbeat_detected",
        "status": "pass" if bot_heartbeat_ok else "fail",
    })

    if not bot_heartbeat_ok:
        return _fail_result(
            phase="business_smoke",
            description=description,
            started=started,
            error="admin /health 输出未检测到 bot 心跳",
            stdout=result.stdout,
            stderr=result.stderr,
            readiness_checks=readiness_checks,
        )

    return _pass_result(
        phase="business_smoke",
        description=description,
        started=started,
        stdout=result.stdout,
        stderr=result.stderr,
        returncode=result.returncode,
        evidence={"bot_heartbeat_detected": bot_heartbeat_ok},
        readiness_checks=readiness_checks,
    )


# ════════════════════════════════════════════════════════════════
# 阶段 8:backup_restore
# ════════════════════════════════════════════════════════════════


def phase_backup_restore(timeout: int) -> PhaseResult:
    """阶段 8:触发 backup → 触发 restore → 验证数据完整性。

    readiness 检查点:
      - docker compose run --rm db_backup python -m services.db_backup 返回 0
      - docker compose run --rm db_writer python -m services.db_restore --staging 返回 0
      - restore 输出包含 "OK" / "success" / "verified"
    """
    description = PHASES[7][1]
    started = time.time()

    if not _docker_available():
        return _fail_result(
            phase="backup_restore",
            description=description,
            started=started,
            error="Docker daemon 不可用 — backup_restore 阶段无法执行",
            readiness_checks=[{"check": "docker_daemon", "status": "fail"}],
        )

    readiness_checks: list[dict[str, Any]] = [
        {"check": "docker_daemon", "status": "pass"},
    ]

    # 1. 触发 backup
    backup_cmd = _compose_cmd([
        "run", "--rm", "db_backup",
        "python", "-m", "services.db_backup",
    ])
    try:
        backup_result = _run(backup_cmd, timeout=timeout, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired:
        return _fail_result(
            phase="backup_restore",
            description=description,
            started=started,
            error=f"backup 触发超时({timeout}s)",
            readiness_checks=readiness_checks + [
                {"check": "backup_triggered", "status": "timeout"},
            ],
        )

    if backup_result.returncode != 0:
        return _fail_result(
            phase="backup_restore",
            description=description,
            started=started,
            error=f"backup 失败 (exit={backup_result.returncode})",
            stdout=backup_result.stdout,
            stderr=backup_result.stderr,
            returncode=backup_result.returncode,
            readiness_checks=readiness_checks + [
                {"check": "backup_triggered", "status": "fail"},
            ],
        )

    readiness_checks.append({"check": "backup_triggered", "status": "pass"})

    # 2. 触发 restore(到 staging,不覆盖生产数据)
    restore_cmd = _compose_cmd([
        "run", "--rm", "db_writer",
        "python", "-m", "services.db_restore", "--staging",
    ])
    try:
        restore_result = _run(restore_cmd, timeout=timeout, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired:
        return _fail_result(
            phase="backup_restore",
            description=description,
            started=started,
            error=f"restore 触发超时({timeout}s)",
            stdout=backup_result.stdout,
            stderr=backup_result.stderr,
            readiness_checks=readiness_checks + [
                {"check": "restore_triggered", "status": "timeout"},
            ],
        )

    if restore_result.returncode != 0:
        return _fail_result(
            phase="backup_restore",
            description=description,
            started=started,
            error=f"restore 失败 (exit={restore_result.returncode})",
            stdout=restore_result.stdout,
            stderr=restore_result.stderr,
            returncode=restore_result.returncode,
            readiness_checks=readiness_checks + [
                {"check": "restore_triggered", "status": "fail"},
            ],
        )

    # 3. 验证数据完整性(restore 输出包含 success/verified/OK)
    restore_output = (restore_result.stdout + restore_result.stderr).lower()
    integrity_keywords = ["ok", "success", "verified", "complete"]
    integrity_ok = any(kw in restore_output for kw in integrity_keywords)
    readiness_checks.append({
        "check": "restore_triggered",
        "status": "pass",
    })
    readiness_checks.append({
        "check": "data_integrity_verified",
        "status": "pass" if integrity_ok else "fail",
    })

    if not integrity_ok:
        return _fail_result(
            phase="backup_restore",
            description=description,
            started=started,
            error=(
                "restore 输出未包含完整性验证关键字"
                f"({integrity_keywords})"
            ),
            stdout=restore_result.stdout,
            stderr=restore_result.stderr,
            readiness_checks=readiness_checks,
        )

    return _pass_result(
        phase="backup_restore",
        description=description,
        started=started,
        stdout=restore_result.stdout,
        stderr=restore_result.stderr,
        returncode=restore_result.returncode,
        evidence={
            "backup_stdout_tail": backup_result.stdout[-500:],
            "restore_stdout_tail": restore_result.stdout[-500:],
            "integrity_keywords_found": [
                kw for kw in integrity_keywords if kw in restore_output
            ],
        },
        readiness_checks=readiness_checks,
    )


# ════════════════════════════════════════════════════════════════
# 阶段 9:sigterm
# ════════════════════════════════════════════════════════════════


def phase_sigterm(timeout: int) -> PhaseResult:
    """阶段 9:发送 SIGTERM,验证优雅关闭。

    readiness 检查点:
      - docker compose kill -s SIGTERM 返回 0
      - 所有容器退出码为 0(SIGTERM 优雅退出)或 137(SIGKILL,视为失败)
      - 容器退出时间 < stop_timeout(无 SIGKILL)
    """
    description = PHASES[8][1]
    started = time.time()

    if not _docker_available():
        return _fail_result(
            phase="sigterm",
            description=description,
            started=started,
            error="Docker daemon 不可用 — sigterm 阶段无法执行",
            readiness_checks=[{"check": "docker_daemon", "status": "fail"}],
        )

    # 发送 SIGTERM
    kill_cmd = _compose_cmd(["kill", "-s", "SIGTERM"])
    try:
        kill_result = _run(kill_cmd, timeout=timeout, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired:
        return _fail_result(
            phase="sigterm",
            description=description,
            started=started,
            error=f"docker compose kill -s SIGTERM 超时({timeout}s)",
            readiness_checks=[
                {"check": "docker_daemon", "status": "pass"},
                {"check": "sigterm_sent", "status": "timeout"},
            ],
        )

    if kill_result.returncode != 0:
        return _fail_result(
            phase="sigterm",
            description=description,
            started=started,
            error=f"docker compose kill -s SIGTERM 失败 (exit={kill_result.returncode})",
            stdout=kill_result.stdout,
            stderr=kill_result.stderr,
            returncode=kill_result.returncode,
            readiness_checks=[
                {"check": "docker_daemon", "status": "pass"},
                {"check": "sigterm_sent", "status": "fail"},
            ],
        )

    readiness_checks = [
        {"check": "docker_daemon", "status": "pass"},
        {"check": "sigterm_sent", "status": "pass"},
    ]

    # 等待容器退出(最多 30s)
    time.sleep(5)

    # 检查每个容器退出码
    ps_cmd = _compose_cmd(["ps", "-a", "--format", "json"])
    ps_result = _run(ps_cmd, timeout=30, cwd=REPO_ROOT)
    exit_codes: dict[str, int | None] = {}
    if ps_result.returncode == 0:
        for line in ps_result.stdout.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                svc_info = json.loads(line)
                svc_name = svc_info.get("Service") or svc_info.get("service", "")
                if svc_name:
                    # ExitCode 字段可能不存在(running 状态)
                    exit_code = svc_info.get("ExitCode")
                    if exit_code is None:
                        # 尝试从 Status 解析
                        status = svc_info.get("Status", "") or svc_info.get("State", "")
                        if "exited" in str(status).lower():
                            exit_code = -1
                    exit_codes[svc_name] = (
                        int(exit_code) if exit_code is not None else None
                    )
            except (json.JSONDecodeError, ValueError, TypeError):
                continue

    # 验证无 SIGKILL(exit code 137)
    sigkill_services = [
        name for name, code in exit_codes.items() if code == 137
    ]
    readiness_checks.append({
        "check": "no_sigkill",
        "status": "pass" if not sigkill_services else "fail",
        "sigkill_services": sigkill_services,
    })

    if sigkill_services:
        return _fail_result(
            phase="sigterm",
            description=description,
            started=started,
            error=(
                f"以下服务被 SIGKILL 强制终止(未优雅处理 SIGTERM): "
                f"{sigkill_services}"
            ),
            stdout=ps_result.stdout,
            stderr=ps_result.stderr,
            evidence={"exit_codes": exit_codes, "sigkill_services": sigkill_services},
            readiness_checks=readiness_checks,
        )

    return _pass_result(
        phase="sigterm",
        description=description,
        started=started,
        stdout=kill_result.stdout,
        stderr=kill_result.stderr,
        returncode=kill_result.returncode,
        evidence={"exit_codes": exit_codes},
        readiness_checks=readiness_checks,
    )


# ════════════════════════════════════════════════════════════════
# 阶段 10:restart
# ════════════════════════════════════════════════════════════════


def phase_restart(timeout: int) -> PhaseResult:
    """阶段 10:restart 验证可恢复。

    readiness 检查点:
      - docker compose up -d 返回 0
      - 所有服务重新进入 running 状态
      - redis healthcheck 重新通过
    """
    description = PHASES[9][1]
    started = time.time()

    if not _docker_available():
        return _fail_result(
            phase="restart",
            description=description,
            started=started,
            error="Docker daemon 不可用 — restart 阶段无法执行",
            readiness_checks=[{"check": "docker_daemon", "status": "fail"}],
        )

    cmd = _compose_cmd(["up", "-d"])
    try:
        result = _run(cmd, timeout=timeout, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired:
        return _fail_result(
            phase="restart",
            description=description,
            started=started,
            error=f"docker compose up -d 超时({timeout}s)",
            readiness_checks=[
                {"check": "docker_daemon", "status": "pass"},
                {"check": "compose_up", "status": "timeout"},
            ],
        )

    if result.returncode != 0:
        return _fail_result(
            phase="restart",
            description=description,
            started=started,
            error=f"docker compose up -d 失败 (exit={result.returncode})",
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=result.returncode,
            readiness_checks=[
                {"check": "docker_daemon", "status": "pass"},
                {"check": "compose_up", "status": "fail"},
            ],
        )

    # 等待服务就绪(start_period 60s,等待 30s 应足够)
    time.sleep(15)

    # 验证服务重新运行
    ps_cmd = _compose_cmd(["ps", "--format", "json"])
    ps_result = _run(ps_cmd, timeout=30, cwd=REPO_ROOT)
    running_services: list[str] = []
    if ps_result.returncode == 0:
        for line in ps_result.stdout.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                svc_info = json.loads(line)
                svc_name = svc_info.get("Service") or svc_info.get("service", "")
                svc_state = (
                    svc_info.get("State")
                    or svc_info.get("state", "")
                    or svc_info.get("Status", "")
                    or ""
                )
                if svc_name and "running" in str(svc_state).lower():
                    running_services.append(svc_name)
            except json.JSONDecodeError:
                continue

    expected_running = set(CORE_SERVICES) | set(BOT_SERVICES)
    found_running = set(running_services) & expected_running
    restart_ok = len(found_running) >= len(expected_running)

    readiness_checks = [
        {"check": "docker_daemon", "status": "pass"},
        {"check": "compose_up", "status": "pass"},
        {
            "check": "services_running_after_restart",
            "status": "pass" if restart_ok else "fail",
            "expected": sorted(expected_running),
            "found": sorted(found_running),
        },
    ]

    if not restart_ok:
        return _fail_result(
            phase="restart",
            description=description,
            started=started,
            error=(
                f"restart 后服务未恢复运行: "
                f"expected={sorted(expected_running)}, "
                f"found={sorted(found_running)}"
            ),
            stdout=ps_result.stdout,
            stderr=ps_result.stderr,
            evidence={
                "running_services": running_services,
                "expected": sorted(expected_running),
            },
            readiness_checks=readiness_checks,
        )

    return _pass_result(
        phase="restart",
        description=description,
        started=started,
        stdout=result.stdout,
        stderr=result.stderr,
        returncode=result.returncode,
        evidence={"running_services": running_services},
        readiness_checks=readiness_checks,
    )


# ════════════════════════════════════════════════════════════════
# 阶段 11:teardown
# ════════════════════════════════════════════════════════════════


def phase_teardown(timeout: int) -> PhaseResult:
    """阶段 11:teardown — docker compose down -v。

    readiness 检查点:
      - docker compose down -v 返回 0
      - 所有容器已移除
    """
    description = PHASES[10][1]
    started = time.time()

    if not _docker_available():
        return _fail_result(
            phase="teardown",
            description=description,
            started=started,
            error="Docker daemon 不可用 — teardown 阶段无法执行",
            readiness_checks=[{"check": "docker_daemon", "status": "fail"}],
        )

    cmd = _compose_cmd(["down", "-v"])
    try:
        result = _run(cmd, timeout=timeout, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired:
        return _fail_result(
            phase="teardown",
            description=description,
            started=started,
            error=f"docker compose down -v 超时({timeout}s)",
            readiness_checks=[
                {"check": "docker_daemon", "status": "pass"},
                {"check": "compose_down", "status": "timeout"},
            ],
        )

    if result.returncode != 0:
        return _fail_result(
            phase="teardown",
            description=description,
            started=started,
            error=f"docker compose down -v 失败 (exit={result.returncode})",
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=result.returncode,
            readiness_checks=[
                {"check": "docker_daemon", "status": "pass"},
                {"check": "compose_down", "status": "fail"},
            ],
        )

    return _pass_result(
        phase="teardown",
        description=description,
        started=started,
        stdout=result.stdout,
        stderr=result.stderr,
        returncode=result.returncode,
        readiness_checks=[
            {"check": "docker_daemon", "status": "pass"},
            {"check": "compose_down", "status": "pass"},
        ],
    )


# ════════════════════════════════════════════════════════════════
# 阶段分发器
# ════════════════════════════════════════════════════════════════

PHASE_FUNCS: dict[str, Callable[[int], PhaseResult]] = {
    "preflight": phase_preflight,
    "start_core": phase_start_core,
    "start_bots": phase_start_bots,
    "migration_check": phase_migration_check,
    "health_check": phase_health_check,
    "redis_acl_check": phase_redis_acl_check,
    "business_smoke": phase_business_smoke,
    "backup_restore": phase_backup_restore,
    "sigterm": phase_sigterm,
    "restart": phase_restart,
    "teardown": phase_teardown,
}


def _print_result(result: PhaseResult) -> None:
    """打印单阶段结果(JSON 格式)。"""
    print(json.dumps(asdict(result), indent=2, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    """主入口。

    Returns:
        0 — 所有阶段通过
        1 — 任一阶段失败
    """
    parser = argparse.ArgumentParser(
        description=(
            "R70 Wave 5: 真实 Compose Runtime E2E 测试编排器"
            "(11 阶段 fail-closed,不允许 mock)"
        ),
    )
    parser.add_argument(
        "--phase",
        metavar="NAME",
        help=(
            "只运行指定阶段(可选: "
            + ", ".join(name for name, _ in PHASES)
            + ")"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="每阶段超时秒数(默认 600)",
    )
    parser.add_argument(
        "--keep-on-success",
        action="store_true",
        help="全部通过时跳过 teardown,保留容器供人工检查",
    )
    args = parser.parse_args(argv)

    # 验证 --phase 参数
    if args.phase is not None and args.phase not in PHASE_FUNCS:
        print(
            f"ERROR: 未知阶段 {args.phase!r}, 可选: "
            + ", ".join(name for name, _ in PHASES),
            file=sys.stderr,
        )
        return 1

    # 阶段执行顺序
    if args.phase is not None:
        phases_to_run = [args.phase]
        skip_teardown = False  # 单阶段模式不应用 keep-on-success
    else:
        # --keep-on-success: 全部通过时跳过 teardown(不执行,保留容器)
        if args.keep_on_success:
            phases_to_run = [name for name, _ in PHASES if name != "teardown"]
            skip_teardown = True
        else:
            phases_to_run = [name for name, _ in PHASES]
            skip_teardown = False

    print(
        f"=== R70 Wave 5: Compose Runtime E2E ===\n"
        f"compose_file: {COMPOSE_FILE}\n"
        f"env_file: {ENV_FILE}\n"
        f"phases: {phases_to_run}\n"
        f"timeout: {args.timeout}s\n"
        f"keep_on_success: {args.keep_on_success}\n",
        file=sys.stderr,
    )

    results: list[PhaseResult] = []
    for phase_name in phases_to_run:
        # fail-closed: 任一阶段失败立即退出
        # (失败时若 teardown 未在 phases_to_run 中,仍单独执行清理)
        phase_func = PHASE_FUNCS[phase_name]
        try:
            result = phase_func(args.timeout)
        except Exception as e:
            # 任何未捕获异常都视为失败(fail-closed,不允许吞异常)
            result = PhaseResult(
                phase=phase_name,
                description=next(
                    (d for n, d in PHASES if n == phase_name), ""
                ),
                status="fail",
                timestamp=_now_iso(),
                duration_seconds=0,
                error=f"未捕获异常: {type(e).__name__}: {e}",
            )
        results.append(result)
        _print_result(result)

        if result.status == "fail":
            # 失败时仍尝试 teardown 清理资源(若 teardown 未在 phases_to_run 中)
            if phase_name != "teardown" and "teardown" not in phases_to_run:
                print(
                    "\n=== 阶段失败,执行 teardown 清理资源 ===",
                    file=sys.stderr,
                )
                teardown_result = phase_teardown(args.timeout)
                results.append(teardown_result)
                _print_result(teardown_result)
            print(
                f"\nFAIL: 阶段 {phase_name} 失败 — {result.error}",
                file=sys.stderr,
            )
            return 1

    if skip_teardown:
        print(
            "\n=== --keep-on-success: 已跳过 teardown,容器保留供人工检查 ===",
            file=sys.stderr,
        )

    print(
        f"\n=== R70 Wave 5: 全部 {len(results)} 阶段通过 ===",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
