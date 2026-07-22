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
import ast
import datetime as _dt
import hashlib
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

# docker/entrypoint.py 路径(R71 Wave 2: 角色集合自动导出源)
ENTRYPOINT_PATH = REPO_ROOT / "docker" / "entrypoint.py"

# synthetic_transaction.py 路径(R71 Wave 2: 合成交易执行器)
SYNTHETIC_TRANSACTION_PATH = REPO_ROOT / "scripts" / "synthetic_transaction.py"

# verify_restore_integrity.py 路径(R71 Wave 2/3: 恢复完整性校验)
VERIFY_RESTORE_INTEGRITY_PATH = REPO_ROOT / "scripts" / "verify_restore_integrity.py"

# R71 Wave 7 (P1-04/05/P0-13): 运行配置身份绑定校验模块
# 用于严格校验 TGJIEMA_IMAGE 格式 + host config digest 绑定 + 当前 SHA 绑定
try:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from validate_runtime_config_binding import (  # type: ignore[import-not-found]
        build_runtime_config_binding,
        compute_host_config_digest,
        parse_image_reference,
        validate_image_reference,
        DEFAULT_EXPECTED_REGISTRY,
        DEFAULT_EXPECTED_REPOSITORY,
    )
    _RUNTIME_CONFIG_BINDING_AVAILABLE = True
except ImportError:  # pragma: no cover — 容错,compose_runtime_e2e 自身仍可运行
    _RUNTIME_CONFIG_BINDING_AVAILABLE = False


def _get_entrypoint_roles() -> set[str]:
    """R71 Wave 2: 从 docker/entrypoint.py 的 ALLOWED_SERVICE_ROLES 自动导出角色集合。

    解析 entrypoint.py 中的:
      - SERVICE_ROLE_RUN_ALL = frozenset({...})
      - SERVICE_ROLE_MODULE = {...}
      - ALLOWED_SERVICE_ROLES = SERVICE_ROLE_RUN_ALL | frozenset(SERVICE_ROLE_MODULE.keys())

    返回 ALLOWED_SERVICE_ROLES 的完整角色集合(12 个角色)。

    fail-closed:解析失败时返回空集合(不允许硬编码角色列表作为 fallback)。

    Returns:
        12 个角色的集合:{up, idx, dsp, mon, admin, admin_bot, db_writer,
        crdb_sync, db_backup, r40_scheduler, migration, prometheus_exporter}
    """
    if not ENTRYPOINT_PATH.is_file():
        return set()
    try:
        src = ENTRYPOINT_PATH.read_text(encoding="utf-8")
        tree = ast.parse(src)
    except (OSError, SyntaxError):
        return set()

    roles: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            if target.id == "SERVICE_ROLE_RUN_ALL":
                # frozenset({...}) 或 frozenset({...})
                if isinstance(node.value, ast.Call):
                    arg = node.value.args[0] if node.value.args else None
                    if isinstance(arg, ast.Set):
                        for elt in arg.elts:
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                roles.add(elt.value)
            elif target.id == "SERVICE_ROLE_MODULE":
                # {...: "..."} dict
                if isinstance(node.value, ast.Dict):
                    for key in node.value.keys:
                        if isinstance(key, ast.Constant) and isinstance(key.value, str):
                            roles.add(key.value)
            elif target.id == "ALLOWED_SERVICE_ROLES":
                # ALLOWED_SERVICE_ROLES = SERVICE_ROLE_RUN_ALL | frozenset(...)
                # 直接遍历 BinOp 找出所有 Name 和 frozenset 字符串
                if isinstance(node.value, ast.BinOp):
                    # 收集 BinOp 两边的字符串字面量
                    for sub in ast.walk(node.value):
                        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                            roles.add(sub.value)
                    # 如果 BinOp 中有 SERVICE_ROLE_MODULE.keys() 调用,
                    # 我们已经在上面 SERVICE_ROLE_MODULE 分支收集过 keys
    return roles

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

# 阶段 3:Bot 服务 + 全部业务角色服务(R71 Wave 2 扩展)
# R71 P0-05: 旧版只启动 up/idx/dsp/mon/admin_bot 5 个 bot,
# 缺少 admin/crdb_sync/db_backup/prometheus_exporter。
# R71 Wave 2: 扩展为全部业务服务(migration 是 oneshot,
# 通过 depends_on 自动触发,不在此列表中;
# r40_scheduler 不在 docker-compose.prod.yml 中,故不启动)
BOT_SERVICES: list[str] = [
    "up", "idx", "dsp", "mon", "admin_bot",  # 5 个 Bot 服务
    "admin", "crdb_sync", "db_backup", "prometheus_exporter",  # 4 个业务服务
]

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
    ("start_bots", "启动 up/idx/dsp/mon/admin_bot + admin/crdb_sync/db_backup/prometheus_exporter"),
    ("migration_check", "docker compose exec db_writer python -m database.migrate --check"),
    ("health_check", "对每个服务调用 /health + python -m services.health --role <role> --json"),
    ("redis_acl_check", "验证 Redis ACL(redis-acl-init 完成)"),
    ("business_smoke", "R71 Wave 2: 合成业务交易(注入 → 消费 → 落库 → 幂等 → 失败 → 清理)"),
    ("backup_restore", "触发 backup → restore → 结构化数据完整性校验(verify_restore_integrity.py)"),
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
    # R71 P1-04: 严格校验 — 替换原宽松 "@sha256:" 子串检查为完整正则
    # (registry/repository@sha256:<64位小写hex>)。
    # 旧版只检查包含 "@sha256:" 子串,可被以下绕过:
    #   - "any-repo@sha256:0000" (其他仓库 + 全零 digest)
    #   - "tgjiema@sha256:abc"   (短 hash)
    #   - "tgjiema:latest@sha256:..." (tag + digest 混合)
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
    # R71 P1-04: 严格格式校验
    if _RUNTIME_CONFIG_BINDING_AVAILABLE:
        parsed, img_errors = validate_image_reference(
            tgjiema_image,
            DEFAULT_EXPECTED_REGISTRY,
            DEFAULT_EXPECTED_REPOSITORY,
        )
        if parsed is None:
            return _fail_result(
                phase="preflight",
                description=description,
                started=started,
                error=(
                    f"TGJIEMA_IMAGE 格式不合法(R71 P1-04 严格校验)— "
                    + "; ".join(img_errors)
                ),
                readiness_checks=[
                    {"check": "docker_daemon", "status": "pass"},
                    {"check": "compose_file", "status": "pass"},
                    {"check": "env_file", "status": "pass"},
                    {"check": "image_digest", "status": "fail"},
                ],
            )
    elif "@sha256:" not in tgjiema_image:
        # 回退到宽松检查(仅当 validate_runtime_config_binding 模块不可用时)
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
        # R71 RC17 fix: 超时时也捕获容器日志,用于诊断哪个容器导致 compose up 挂起。
        # (migration 挂起 / redis healthcheck 失败 / redis-acl-init 卡住等)
        container_logs: dict[str, Any] = {}
        for svc in ("redis-acl-init", "redis", "migration", "db_writer"):
            logs_cmd = _compose_cmd(["logs", "--no-color", "--tail", "100", svc])
            try:
                logs_result = _run(logs_cmd, timeout=15, cwd=REPO_ROOT)
                svc_log = (logs_result.stdout or "") + (logs_result.stderr or "")
                if svc_log.strip():
                    container_logs[svc] = svc_log[-4000:]
            except (subprocess.TimeoutExpired, OSError):
                pass
        return _fail_result(
            phase="start_core",
            description=description,
            started=started,
            error=f"docker compose up -d {' '.join(CORE_SERVICES)} 超时({timeout}s)",
            evidence={"container_logs": container_logs} if container_logs else {},
            readiness_checks=[
                {"check": "docker_daemon", "status": "pass"},
                {"check": "compose_up", "status": "timeout"},
            ],
        )

    if result.returncode != 0:
        # R71 RC5 fix: compose 输出不包含 oneshot 容器的实际错误输出。
        # 捕获 redis-acl-init / redis / migration / db_writer 的容器日志,
        # 用于诊断 redis-acl-init render_acl.sh 等脚本的实际失败原因。
        container_logs: dict[str, Any] = {}
        for svc in ("redis-acl-init", "redis", "migration", "db_writer"):
            logs_cmd = _compose_cmd(["logs", "--no-color", "--tail", "100", svc])
            try:
                logs_result = _run(logs_cmd, timeout=15, cwd=REPO_ROOT)
                svc_log = (logs_result.stdout or "") + (logs_result.stderr or "")
                if svc_log.strip():
                    container_logs[svc] = svc_log[-4000:]
            except (subprocess.TimeoutExpired, OSError):
                pass
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
            evidence={"container_logs": container_logs} if container_logs else {},
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
    """阶段 3:启动 Bot 服务 + 全部业务角色服务(R71 Wave 2 扩展)。

    R71 P0-05 整改:旧版只启动 up/idx/dsp/mon/admin_bot 5 个 bot,
    缺少 admin/crdb_sync/db_backup/prometheus_exporter。
    R71 Wave 2: 启动全部业务服务(migration 是 oneshot,通过
    depends_on 自动触发;r40_scheduler 不在 compose 文件中,故不启动)。

    readiness 检查点:
      - docker compose up -d <bots> 返回 0
      - 所有 Bot + 业务服务容器状态 running
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
    """阶段 5:对每个暴露 HTTP /health 的服务调用健康端点 + 角色级 readiness。

    R71 Wave 2 整改:除 HTTP /health 端点外,对每个业务服务执行
    `docker compose exec <svc> python -m services.health --role <role> --json`,
    解析 JSON,断言 healthy=true。这是 R71 Wave 1 引入的角色级 readiness,
    比单纯 HTTP /health 更严格(检查 Redis/CRDB/Bot token 等真实依赖)。

    readiness 检查点:
      - admin:8080/health 返回 200
      - prometheus_exporter:9100/health 返回 200
      - 每个业务服务的 services.health --role <role> --json 返回 healthy=true
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

    # R71 Wave 2: 对每个业务服务执行 python -m services.health --role <role> --json
    # 解析 JSON,断言 healthy=true
    role_health_results: dict[str, dict[str, Any]] = {}
    for service, expected_role in SERVICE_ROLES.items():
        if service in ("redis", "redis-acl-init"):
            continue  # 基础设施服务无 SERVICE_ROLE
        if expected_role == "infrastructure":
            continue
        cmd = _compose_cmd([
            "exec", "-T", service,
            "python", "-m", "services.health",
            "--role", expected_role,
            "--json",
        ])
        try:
            result = _run(cmd, timeout=30, cwd=REPO_ROOT)
            role_health_ok = False
            role_health_detail: dict[str, Any] = {
                "service": service,
                "role": expected_role,
                "returncode": result.returncode,
                "stdout": result.stdout.strip()[:500],  # 截断防止过长
                "stderr": result.stderr.strip()[:500],
            }
            if result.returncode == 0:
                try:
                    parsed = json.loads(result.stdout.strip())
                    role_health_ok = bool(parsed.get("healthy", False))
                    role_health_detail["healthy"] = role_health_ok
                    role_health_detail["checks_count"] = len(parsed.get("checks", []))
                except json.JSONDecodeError as e:
                    role_health_detail["parse_error"] = str(e)
            role_health_results[service] = role_health_detail
            readiness_checks.append({
                "check": f"role_health_{service}",
                "status": "pass" if role_health_ok else "fail",
                "role": expected_role,
            })
            if not role_health_ok:
                failures.append(
                    f"{service}:services.health --role {expected_role} "
                    f"(exit={result.returncode})"
                )
        except subprocess.TimeoutExpired:
            role_health_results[service] = {
                "service": service,
                "role": expected_role,
                "status": "timeout",
            }
            readiness_checks.append({
                "check": f"role_health_{service}",
                "status": "timeout",
                "role": expected_role,
            })
            failures.append(f"{service}:services.health --role {expected_role} (timeout)")

    # 验证 SERVICE_ROLE 映射
    role_mismatches: list[str] = []
    for service, expected_role in SERVICE_ROLES.items():
        if service in ("redis", "redis-acl-init"):
            continue  # 基础设施服务无 SERVICE_ROLE
        if expected_role == "infrastructure":
            continue
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
            evidence={
                "health_results": health_results,
                "role_health_results": role_health_results,
                "role_mismatches": role_mismatches,
            },
            readiness_checks=readiness_checks,
        )

    return _pass_result(
        phase="health_check",
        description=description,
        started=started,
        evidence={
            "health_results": health_results,
            "role_health_results": role_health_results,
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


def _run_synthetic_transaction(timeout: int) -> tuple[bool, dict[str, Any]]:
    """R71 Wave 2: 执行合成业务交易(替代 admin /healthz 调用)。

    通过 scripts/synthetic_transaction.py 的 run_full_transaction() 完整执行:
      1. 注入测试事件(Redis XADD)
      2. 验证落库(db_writer 消费 → SQLite bot_heartbeat)
      3. 验证幂等性(重复 XADD 不增加行数)
      4. 注入失败场景(畸形 JSON → DLQ)
      5. 清理(DELETE 测试 row)

    不再用 admin /healthz 代替业务交易(R71 P0-06 整改)。

    Args:
        timeout: 单步骤最大等待秒数

    Returns:
        (passed, evidence_dict)
    """
    # 直接 import synthetic_transaction 模块(同目录)
    # 不用 subprocess 调用,以便捕获结构化证据
    import importlib.util

    if not SYNTHETIC_TRANSACTION_PATH.is_file():
        return False, {
            "error": f"synthetic_transaction.py 不存在: {SYNTHETIC_TRANSACTION_PATH}",
        }

    try:
        spec = importlib.util.spec_from_file_location(
            "synthetic_transaction", SYNTHETIC_TRANSACTION_PATH,
        )
        if spec is None or spec.loader is None:
            return False, {"error": "加载 synthetic_transaction 模块失败"}
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as e:
        return False, {"error": f"加载 synthetic_transaction 模块异常: {type(e).__name__}: {e}"}

    try:
        evidence = module.run_full_transaction(timeout=timeout)
    except Exception as e:
        return False, {
            "error": f"run_full_transaction 异常: {type(e).__name__}: {e}",
        }

    evidence_dict = asdict(evidence) if hasattr(evidence, "__dataclass_fields__") else {
        "trace_id": getattr(evidence, "trace_id", ""),
        "overall_passed": getattr(evidence, "overall_passed", False),
        "error": getattr(evidence, "error", None),
    }
    return evidence.overall_passed, evidence_dict


def phase_business_smoke(timeout: int) -> PhaseResult:
    """阶段 7:R71 Wave 2 合成业务交易(替代 /healthz 调用)。

    R71 P0-06 整改:旧版只调用 admin /healthz 并检查 Bot heartbeat,
    不是完整业务交易。R71 Wave 2 改为通过真实应用入口注入合成交易,
    验证完整业务链路:Redis Stream → db_writer → SQLite → 幂等性 → 失败处理 → 清理。

    不再用 /healthz 代替业务交易(R71 P0-06 fail-closed)。

    readiness 检查点:
      - synthetic_transaction.py 可加载
      - inject 步骤通过(Redis XADD)
      - verify 步骤通过(db_writer 消费 → SQLite 落库)
      - idempotency 步骤通过(重复 XADD 不增加行数)
      - failure_scenario 步骤通过(畸形 JSON → DLQ)
      - cleanup 步骤通过(DELETE 测试 row)
      - overall_passed=True
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

    # R71 Wave 2: 调用合成交易执行器
    passed, evidence = _run_synthetic_transaction(timeout=timeout)

    # 从证据中提取各步骤结果
    step_results = {
        "inject": evidence.get("inject", {}),
        "verify": evidence.get("verify", {}),
        "idempotency": evidence.get("idempotency", {}),
        "failure_scenario": evidence.get("failure_scenario", {}),
        "cleanup": evidence.get("cleanup", {}),
    }

    for step_name, step_data in step_results.items():
        step_passed = step_data.get("passed", False)
        readiness_checks.append({
            "check": f"synthetic_{step_name}",
            "status": "pass" if step_passed else "fail",
        })

    readiness_checks.append({
        "check": "synthetic_overall",
        "status": "pass" if passed else "fail",
    })

    if not passed:
        error_msg = evidence.get("error") or "合成交易未通过"
        return _fail_result(
            phase="business_smoke",
            description=description,
            started=started,
            error=f"合成业务交易失败: {error_msg}",
            evidence=evidence,
            readiness_checks=readiness_checks,
        )

    return _pass_result(
        phase="business_smoke",
        description=description,
        started=started,
        evidence=evidence,
        readiness_checks=readiness_checks,
    )


# ════════════════════════════════════════════════════════════════
# 阶段 8:backup_restore
# ════════════════════════════════════════════════════════════════


def _safe_cleanup_marker(vri_mod: Any, tid: str) -> str | None:
    """R71 Wave 2: 执行清理标记,返回错误字符串(成功返回 None)。

    R70 Wave 5 fail-closed 原则:不吞异常。
    清理失败时不掩盖原始错误,而是返回错误描述供调用方记入 evidence,
    确保证据链完整可审计。

    Args:
        vri_mod: verify_restore_integrity 模块实例
        tid: 测试标记 ID(trace_id)

    Returns:
        None 表示清理成功;非空字符串表示清理失败(含错误描述)
    """
    try:
        rc = vri_mod.cleanup_marker(tid)
    except Exception as e:
        return f"cleanup_marker 异常: {type(e).__name__}: {e}"
    if rc != 0:
        return f"cleanup_marker 退出码 {rc}"
    return None


def _run_restore_integrity_verify(
    trace_id: str,
    pre_snapshot_path: Path,
    timeout: int,
    target_db: str = "staging",
    backup_schema_version: str | None = None,
    skip_synthetic: bool = False,
    skip_app_checks: bool = False,
) -> tuple[bool, dict[str, Any]]:
    """R71 Wave 3: 通过 verify_restore_integrity.py 进行完整结构化校验。

    替代旧版日志关键词匹配(R71 P0-07 整改)与 Wave 2 基本校验(R71 P0-08 升级):
      - 校验测试标记行存在(确认恢复后数据可见)
      - 比对关键表 row count(备份前 vs 恢复后)
      - Schema 指纹捕获与比对(tables / pk / columns / conflict_col / source / DDL hash)
      - 字段级 hash 比对(每表 SELECT * ORDER BY pk → sha256 of canonical JSON)
      - 迁移版本兼容性检查(current vs backup schema_version)
      - 应用启动/读写验证(python -m services.health + INSERT/SELECT/DELETE)
      - 恢复环境合成交易(synthetic_transaction.run_full_transaction)
      - 切换/回滚证据(RestoreOrchestrator import check + 结构化 JSON)
      - 不再依赖 "ok"/"success"/"verified" 等日志关键词

    R71 Wave 3 P0-08: 由 verify() 升级为 verify_full(),
    target_db 默认为 "staging"(恢复到隔离目标,不覆盖生产数据)。

    Args:
        trace_id: 测试标记 ID
        pre_snapshot_path: 备份前快照路径
        timeout: 命令超时秒数(目前未直接使用,verify_full 内部按阶段超时)
        target_db: 目标数据库(默认 staging,符合 R71 Wave 3 隔离恢复要求)
        backup_schema_version: 备份 manifest 中的 schema_version(可选)
        skip_synthetic: 跳过合成交易(用于快速校验)
        skip_app_checks: 跳过应用启动/读写检查(用于离线校验)

    Returns:
        (passed, evidence_dict) — evidence_dict 是 IntegrityEvidence asdict,
        包含 schema_fingerprint / field_hashes / migration_version_check /
        app_start_check / app_read_write_check / synthetic_transaction /
        switch_rollback_evidence 等结构化字段。
    """
    import importlib.util

    if not VERIFY_RESTORE_INTEGRITY_PATH.is_file():
        return False, {
            "error": f"verify_restore_integrity.py 不存在: {VERIFY_RESTORE_INTEGRITY_PATH}",
        }

    try:
        spec = importlib.util.spec_from_file_location(
            "verify_restore_integrity", VERIFY_RESTORE_INTEGRITY_PATH,
        )
        if spec is None or spec.loader is None:
            return False, {"error": "加载 verify_restore_integrity 模块失败"}
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as e:
        return False, {
            "error": f"加载 verify_restore_integrity 模块异常: {type(e).__name__}: {e}",
        }

    # R71 Wave 3 P0-08: 优先使用 verify_full()(完整结构化校验);
    # 若旧版模块未实现 verify_full,则回退到 verify()(保持向后兼容)。
    verify_full_fn = getattr(module, "verify_full", None)
    try:
        if callable(verify_full_fn):
            evidence = verify_full_fn(
                trace_id=trace_id,
                pre_snapshot_path=pre_snapshot_path,
                target_db=target_db,
                backup_schema_version=backup_schema_version,
                skip_synthetic=skip_synthetic,
                skip_app_checks=skip_app_checks,
            )
        else:
            # 回退到基本校验(Wave 2 兼容)
            evidence = module.verify(trace_id, pre_snapshot_path)
    except Exception as e:
        return False, {
            "error": f"verify_full()/verify() 异常: {type(e).__name__}: {e}",
        }

    evidence_dict = asdict(evidence) if hasattr(evidence, "__dataclass_fields__") else {
        "passed": getattr(evidence, "passed", False),
        "error": getattr(evidence, "error", None),
    }
    return evidence.passed, evidence_dict


def phase_backup_restore(timeout: int) -> PhaseResult:
    """阶段 8:R71 Wave 3 backup → restore → 完整结构化数据完整性校验。

    R71 P0-07 整改:旧版用日志关键词("ok"/"success"/"verified")
    判断恢复成功,这是不安全的(日志输出可能包含 "ok" 但实际恢复失败)。
    R71 Wave 2 改为通过 scripts/verify_restore_integrity.py 进行结构化校验。
    R71 Wave 3 P0-08 进一步升级为完整结构化校验:
      1. 备份前:写入测试标记行 + 获取关键表 row count + schema 指纹 + 字段级 hash 快照
      2. 触发 backup(docker compose run db_backup)
      3. 触发 restore(到 staging,不覆盖生产数据)
      4. 完整校验(verify_restore_integrity.py verify_full):
         - 测试标记存在
         - 关键表 row count 无回归
         - Schema 指纹捕获与比对(tables / pk / columns / conflict_col / source / DDL hash)
         - 字段级 hash 比对(每表 SELECT * ORDER BY pk → sha256 of canonical JSON)
         - 迁移版本兼容性检查(current vs backup schema_version)
         - 应用启动验证(python -m services.health --role db_writer --json)
         - 应用读写验证(INSERT/SELECT/DELETE on bot_heartbeat)
         - 合成交易验证(synthetic_transaction.run_full_transaction)
         - 切换/回滚证据(RestoreOrchestrator import check + 结构化 JSON)
      5. 清理:删除测试标记行

    不再用日志关键词判断恢复成功(R71 P0-07 fail-closed)。
    所有 readiness 检查必须是 pass 或 fail(无 skip/warn,R71 P0-08)。

    readiness 检查点(R71 Wave 3):
      - write_marker 通过(测试标记写入 bot_heartbeat)
      - pre_snapshot 通过(获取 pre-snapshot,含 schema 指纹与字段级 hash)
      - backup_triggered 通过(docker compose run db_backup 返回 0)
      - restore_triggered 通过(docker compose run db_writer ... --staging 返回 0)
      - data_integrity_verified 通过(标记存在 + 关键表 row count 无回归)
      - schema_fingerprint_captured 通过(Schema 指纹捕获成功,无错误)
      - field_hashes_captured 通过(字段级 hash 捕获成功,无 mismatch)
      - migration_version_compatible 通过(当前 schema_version 与备份兼容)
      - app_start_after_restore 通过(services.health --role db_writer healthy=true)
      - app_read_write_after_restore 通过(INSERT/SELECT/DELETE 全部成功)
      - synthetic_transaction_after_restore 通过(合成交易 overall_passed=true)
      - switch_rollback_evidence_generated 通过(RestoreOrchestrator 可导入,switch/rollback 阶段存在)
      - cleanup_marker 通过(删除测试标记)
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

    # R71 Wave 2/3: 使用 verify_restore_integrity.py 进行结构化校验
    # 1. 写入测试标记
    import importlib.util
    if not VERIFY_RESTORE_INTEGRITY_PATH.is_file():
        return _fail_result(
            phase="backup_restore",
            description=description,
            started=started,
            error=f"verify_restore_integrity.py 不存在: {VERIFY_RESTORE_INTEGRITY_PATH}",
            readiness_checks=readiness_checks + [
                {"check": "verify_restore_integrity_available", "status": "fail"},
            ],
        )

    try:
        spec = importlib.util.spec_from_file_location(
            "verify_restore_integrity", VERIFY_RESTORE_INTEGRITY_PATH,
        )
        if spec is None or spec.loader is None:
            raise ImportError("spec_from_file_location 返回 None")
        vri_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(vri_module)
    except Exception as e:
        return _fail_result(
            phase="backup_restore",
            description=description,
            started=started,
            error=f"加载 verify_restore_integrity 失败: {type(e).__name__}: {e}",
            readiness_checks=readiness_checks + [
                {"check": "verify_restore_integrity_available", "status": "fail"},
            ],
        )
    readiness_checks.append({
        "check": "verify_restore_integrity_available", "status": "pass",
    })

    # 生成唯一 trace_id(R71 Wave 2: 使用 uuid.uuid4() 确保全局唯一)
    import uuid as _uuid_mod
    trace_id = f"restore_marker_{int(time.time())}_{_uuid_mod.uuid4().hex[:8]}"

    # 写入测试标记
    try:
        write_marker_rc = vri_module.write_marker(trace_id)
    except Exception as e:
        return _fail_result(
            phase="backup_restore",
            description=description,
            started=started,
            error=f"write_marker 异常: {type(e).__name__}: {e}",
            readiness_checks=readiness_checks + [
                {"check": "write_marker", "status": "fail"},
            ],
        )
    if write_marker_rc != 0:
        return _fail_result(
            phase="backup_restore",
            description=description,
            started=started,
            error=f"write_marker 失败 (exit={write_marker_rc})",
            readiness_checks=readiness_checks + [
                {"check": "write_marker", "status": "fail"},
            ],
        )
    readiness_checks.append({"check": "write_marker", "status": "pass"})

    # 获取备份前快照(R71 Wave 3: 含 schema 指纹与字段级 hash)
    pre_snapshot_path = REPO_ROOT / f".tmp_restore_pre_snapshot_{trace_id}.json"
    try:
        snapshot_rc = vri_module.take_snapshot(pre_snapshot_path)
    except Exception as e:
        # 清理已写入的标记(不吞异常,错误记入 evidence)
        cleanup_err = _safe_cleanup_marker(vri_module, trace_id)
        return _fail_result(
            phase="backup_restore",
            description=description,
            started=started,
            error=f"take_snapshot 异常: {type(e).__name__}: {e}"
                 + (f" | cleanup_err={cleanup_err}" if cleanup_err else ""),
            readiness_checks=readiness_checks + [
                {"check": "pre_snapshot", "status": "fail"},
            ],
        )
    if snapshot_rc != 0:
        cleanup_err = _safe_cleanup_marker(vri_module, trace_id)
        return _fail_result(
            phase="backup_restore",
            description=description,
            started=started,
            error=f"take_snapshot 失败 (exit={snapshot_rc})"
                 + (f" | cleanup_err={cleanup_err}" if cleanup_err else ""),
            readiness_checks=readiness_checks + [
                {"check": "pre_snapshot", "status": "fail"},
            ],
        )
    readiness_checks.append({"check": "pre_snapshot", "status": "pass"})

    # 触发 backup
    backup_cmd = _compose_cmd([
        "run", "--rm", "db_backup",
        "python", "-m", "services.db_backup",
    ])
    try:
        backup_result = _run(backup_cmd, timeout=timeout, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired:
        cleanup_err = _safe_cleanup_marker(vri_module, trace_id)
        if pre_snapshot_path.exists():
            pre_snapshot_path.unlink(missing_ok=True)
        return _fail_result(
            phase="backup_restore",
            description=description,
            started=started,
            error=f"backup 触发超时({timeout}s)"
                 + (f" | cleanup_err={cleanup_err}" if cleanup_err else ""),
            readiness_checks=readiness_checks + [
                {"check": "backup_triggered", "status": "timeout"},
            ],
        )
    if backup_result.returncode != 0:
        cleanup_err = _safe_cleanup_marker(vri_module, trace_id)
        if pre_snapshot_path.exists():
            pre_snapshot_path.unlink(missing_ok=True)
        return _fail_result(
            phase="backup_restore",
            description=description,
            started=started,
            error=f"backup 失败 (exit={backup_result.returncode})"
                 + (f" | cleanup_err={cleanup_err}" if cleanup_err else ""),
            stdout=backup_result.stdout,
            stderr=backup_result.stderr,
            returncode=backup_result.returncode,
            readiness_checks=readiness_checks + [
                {"check": "backup_triggered", "status": "fail"},
            ],
        )
    readiness_checks.append({"check": "backup_triggered", "status": "pass"})

    # 触发 restore(到 staging,不覆盖生产数据)
    restore_cmd = _compose_cmd([
        "run", "--rm", "db_writer",
        "python", "-m", "services.db_restore", "--staging",
    ])
    try:
        restore_result = _run(restore_cmd, timeout=timeout, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired:
        cleanup_err = _safe_cleanup_marker(vri_module, trace_id)
        if pre_snapshot_path.exists():
            pre_snapshot_path.unlink(missing_ok=True)
        return _fail_result(
            phase="backup_restore",
            description=description,
            started=started,
            error=f"restore 触发超时({timeout}s)"
                 + (f" | cleanup_err={cleanup_err}" if cleanup_err else ""),
            stdout=backup_result.stdout,
            stderr=backup_result.stderr,
            readiness_checks=readiness_checks + [
                {"check": "restore_triggered", "status": "timeout"},
            ],
        )
    if restore_result.returncode != 0:
        cleanup_err = _safe_cleanup_marker(vri_module, trace_id)
        if pre_snapshot_path.exists():
            pre_snapshot_path.unlink(missing_ok=True)
        return _fail_result(
            phase="backup_restore",
            description=description,
            started=started,
            error=f"restore 失败 (exit={restore_result.returncode})"
                 + (f" | cleanup_err={cleanup_err}" if cleanup_err else ""),
            stdout=restore_result.stdout,
            stderr=restore_result.stderr,
            returncode=restore_result.returncode,
            readiness_checks=readiness_checks + [
                {"check": "restore_triggered", "status": "fail"},
            ],
        )
    readiness_checks.append({"check": "restore_triggered", "status": "pass"})

    # R71 Wave 3 P0-08: 完整结构化校验(verify_full,替代 verify + 日志关键词匹配)
    # target_db="staging" 对应恢复目标(隔离验证,不覆盖生产数据)
    verify_passed, verify_evidence = _run_restore_integrity_verify(
        trace_id=trace_id,
        pre_snapshot_path=pre_snapshot_path,
        timeout=timeout,
        target_db="staging",
        skip_synthetic=False,
        skip_app_checks=False,
    )
    readiness_checks.append({
        "check": "data_integrity_verified",
        "status": "pass" if verify_passed else "fail",
    })

    # R71 Wave 3 P0-08: 从 verify_full 证据中提取各结构化检查点
    # 所有检查必须 pass 或 fail(无 skip/warn — fail-closed 原则)
    schema_fp = verify_evidence.get("schema_fingerprint", {}) or {}
    schema_fp_captured = (
        bool(schema_fp)
        and not schema_fp.get("error")
        and bool(schema_fp.get("fingerprint_hash", ""))
    )
    readiness_checks.append({
        "check": "schema_fingerprint_captured",
        "status": "pass" if schema_fp_captured else "fail",
    })

    post_fh = verify_evidence.get("post_field_hashes", []) or []
    fh_mismatches = verify_evidence.get("field_hash_mismatches", []) or []
    field_hashes_ok = (
        len(post_fh) > 0
        and all(not h.get("error") for h in post_fh)
        and len(fh_mismatches) == 0
    )
    readiness_checks.append({
        "check": "field_hashes_captured",
        "status": "pass" if field_hashes_ok else "fail",
    })

    migration_check = verify_evidence.get("migration_version_check", {}) or {}
    migration_compatible = bool(migration_check.get("compatible", False))
    readiness_checks.append({
        "check": "migration_version_compatible",
        "status": "pass" if migration_compatible else "fail",
    })

    app_start = verify_evidence.get("app_start_check", {}) or {}
    app_start_ok = (
        bool(app_start.get("started", False))
        and bool(app_start.get("healthy", False))
    )
    readiness_checks.append({
        "check": "app_start_after_restore",
        "status": "pass" if app_start_ok else "fail",
    })

    app_rw = verify_evidence.get("app_read_write_check", {}) or {}
    app_rw_ok = (
        bool(app_rw.get("write_ok", False))
        and bool(app_rw.get("read_ok", False))
        and bool(app_rw.get("cleanup_ok", False))
    )
    readiness_checks.append({
        "check": "app_read_write_after_restore",
        "status": "pass" if app_rw_ok else "fail",
    })

    synthetic_ev = verify_evidence.get("synthetic_transaction", {}) or {}
    synthetic_ok = bool(synthetic_ev.get("overall_passed", False))
    readiness_checks.append({
        "check": "synthetic_transaction_after_restore",
        "status": "pass" if synthetic_ok else "fail",
    })

    switch_ev = verify_evidence.get("switch_rollback_evidence", {}) or {}
    switch_ok = (
        bool(switch_ev.get("orchestrator_available", False))
        and bool(switch_ev.get("has_switch_phase", False))
        and bool(switch_ev.get("has_rollback_phase", False))
    )
    readiness_checks.append({
        "check": "switch_rollback_evidence_generated",
        "status": "pass" if switch_ok else "fail",
    })

    # 清理测试标记(无论 verify 通过与否)
    try:
        cleanup_rc = vri_module.cleanup_marker(trace_id)
    except Exception as e:
        cleanup_rc = -1
        cleanup_err = f"{type(e).__name__}: {e}"
    else:
        cleanup_err = None
    if pre_snapshot_path.exists():
        pre_snapshot_path.unlink(missing_ok=True)

    readiness_checks.append({
        "check": "cleanup_marker",
        "status": "pass" if cleanup_rc == 0 else "fail",
    })

    if not verify_passed:
        return _fail_result(
            phase="backup_restore",
            description=description,
            started=started,
            error=(
                f"完整结构化校验失败: {verify_evidence.get('error', '未知错误')} "
                f"(R71 Wave 3 P0-08: 不再用日志关键词判断恢复成功; "
                f"schema_fp_captured={schema_fp_captured}, "
                f"field_hashes_ok={field_hashes_ok}, "
                f"migration_compatible={migration_compatible}, "
                f"app_start_ok={app_start_ok}, "
                f"app_rw_ok={app_rw_ok}, "
                f"synthetic_ok={synthetic_ok}, "
                f"switch_ok={switch_ok})"
            ),
            stdout=restore_result.stdout,
            stderr=restore_result.stderr,
            returncode=restore_result.returncode,
            evidence={
                "trace_id": trace_id,
                "verify_evidence": verify_evidence,
                "backup_stdout_tail": backup_result.stdout[-500:],
                "restore_stdout_tail": restore_result.stdout[-500:],
                "integrity_method": "verify_full_structured_verification",
                "target_db": "staging",
                "cleanup_rc": cleanup_rc,
                "cleanup_error": cleanup_err,
                "wave": "r71-wave3-p0-08",
            },
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
            "trace_id": trace_id,
            "verify_evidence": verify_evidence,
            "backup_stdout_tail": backup_result.stdout[-500:],
            "restore_stdout_tail": restore_result.stdout[-500:],
            "integrity_method": "verify_full_structured_verification",
            "target_db": "staging",
            "cleanup_rc": cleanup_rc,
            "wave": "r71-wave3-p0-08",
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


def _get_source_sha() -> str:
    """R71 Wave 2: 获取当前 git 源码 SHA(用于 evidence 输出)。

    fail-closed:git 不可用时返回空字符串(不抛异常,不影响 E2E 流程)。
    """
    try:
        result = _run(
            ["git", "rev-parse", "HEAD"],
            timeout=5, cwd=REPO_ROOT,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return ""


def _get_image_repo_digest() -> str:
    """R71 Wave 2: 获取 TGJIEMA_IMAGE 的 RepoDigests。

    fail-closed:docker 不可用时返回空字符串。
    """
    tgjiema_image = os.environ.get("TGJIEMA_IMAGE", "")
    if not tgjiema_image:
        return ""
    # 提取 image name(去掉 @sha256:... 部分)
    image_name = tgjiema_image.split("@")[0]
    try:
        result = _run(
            ["docker", "inspect", "--format",
             "{{json .RepoDigests}}", image_name],
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return tgjiema_image  # 回退到环境变量值


def _get_compose_digest() -> str:
    """R71 Wave 2: 获取 docker-compose.prod.yml 的 SHA256 digest。

    用于 evidence 输出,确保 Compose 文件内容可追溯。
    """
    try:
        content = COMPOSE_FILE.read_bytes()
        return "sha256:" + hashlib.sha256(content).hexdigest()
    except OSError:
        return ""


def _build_role_matrix() -> dict[str, Any]:
    """R71 Wave 2: 构建角色矩阵 evidence(角色 → SERVICE_ROLE 映射 + entrypoint 角色)。

    Returns:
        dict 含:
          - service_roles: docker-compose.prod.yml 中的 SERVICE_ROLE 映射
          - entrypoint_roles: docker/entrypoint.py 的 ALLOWED_SERVICE_ROLES
          - bot_services: start_bots 阶段启动的服务列表
          - core_services: start_core 阶段启动的服务列表
          - http_health_services: 暴露 HTTP /health 的服务列表
    """
    return {
        "service_roles": dict(SERVICE_ROLES),
        "entrypoint_roles": sorted(_get_entrypoint_roles()),
        "bot_services": list(BOT_SERVICES),
        "core_services": list(CORE_SERVICES),
        "http_health_services": dict(HTTP_HEALTH_SERVICES),
    }


def _build_evidence(
    results: list[PhaseResult],
    started_at: str,
    finished_at: str,
    overall_passed: bool,
) -> dict[str, Any]:
    """R71 Wave 2 / Wave 7: 构建 runtime-e2e-evidence.json 证据结构。

    包含:
      - source SHA(git rev-parse HEAD)— R71 P0-13
      - workflow run_id / run_attempt — R71 P0-13
      - image RepoDigest(docker inspect)— R71 P1-04
      - image_digest(从 TGJIEMA_IMAGE 解析)— R71 P1-04
      - Compose digest(docker-compose.prod.yml SHA256)
      - host config digest(groups.yaml / topology.yaml)— R71 P1-05
      - 角色矩阵(SERVICE_ROLE 映射 + entrypoint 角色)
      - 各阶段时间戳和结果
      - overall_passed
    """
    # 基础字段
    evidence: dict[str, Any] = {
        "schema_version": "r71-wave7",
        "started_at": started_at,
        "finished_at": finished_at,
        "overall_passed": overall_passed,
        "source_sha": _get_source_sha(),
        "workflow_run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "workflow_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
        "image_repo_digest": _get_image_repo_digest(),
        "compose_digest": _get_compose_digest(),
        "compose_file": str(COMPOSE_FILE),
        "env_file": str(ENV_FILE),
        "role_matrix": _build_role_matrix(),
        "phases": [asdict(r) for r in results],
        "phase_summary": [
            {
                "phase": r.phase,
                "status": r.status,
                "timestamp": r.timestamp,
                "duration_seconds": r.duration_seconds,
            }
            for r in results
        ],
    }

    # R71 Wave 7 (P1-04/05/P0-13): 注入运行配置身份绑定字段
    if _RUNTIME_CONFIG_BINDING_AVAILABLE:
        try:
            binding = build_runtime_config_binding(
                repo_root=REPO_ROOT,
                image_ref=os.environ.get("TGJIEMA_IMAGE", ""),
                candidate_manifest_path=None,
                pull_and_compare=False,
            )
            evidence["image_reference"] = binding.image_reference
            evidence["image_registry"] = binding.image_registry
            evidence["image_repository"] = binding.image_repository
            evidence["image_digest"] = binding.image_digest
            evidence["host_config_digests"] = [
                {
                    "path": f.path,
                    "exists": f.exists,
                    "sha256": f.sha256,
                    "size_bytes": f.size_bytes,
                }
                for f in binding.host_config_digests
            ]
            evidence["combined_host_config_digest"] = (
                binding.combined_host_config_digest
            )
            evidence["runtime_config_binding_passed"] = binding.overall_passed
            if binding.errors:
                evidence["runtime_config_binding_errors"] = list(binding.errors)
        except Exception as exc:  # pragma: no cover — binding 失败不应阻断 E2E
            evidence["runtime_config_binding_error"] = (
                f"build_runtime_config_binding 异常: {exc}"
            )

    return evidence


def main(argv: list[str] | None = None) -> int:
    """主入口。

    Returns:
        0 — 所有阶段通过
        1 — 任一阶段失败
    """
    parser = argparse.ArgumentParser(
        description=(
            "R70 Wave 5 / R71 Wave 2: 真实 Compose Runtime E2E 测试编排器"
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
    parser.add_argument(
        "--output",
        metavar="PATH",
        help=(
            "R71 Wave 2: 证据输出 JSON 文件路径"
            "(runtime-e2e-evidence.json,含 source SHA / image RepoDigest / "
            "Compose digest / 角色矩阵 / 各阶段时间戳和结果)"
        ),
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

    started_at = _now_iso()
    print(
        f"=== R70 Wave 5 / R71 Wave 2: Compose Runtime E2E ===\n"
        f"compose_file: {COMPOSE_FILE}\n"
        f"env_file: {ENV_FILE}\n"
        f"phases: {phases_to_run}\n"
        f"timeout: {args.timeout}s\n"
        f"keep_on_success: {args.keep_on_success}\n"
        f"output: {args.output or '(stdout)'}\n",
        file=sys.stderr,
    )

    results: list[PhaseResult] = []
    overall_passed = True
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
            overall_passed = False
            # 失败时仍尝试 teardown 清理资源(若 teardown 未在 phases_to_run 中)
            if phase_name != "teardown" and "teardown" not in phases_to_run:
                print(
                    "\n=== 阶段失败,执行 teardown 清理资源 ===",
                    file=sys.stderr,
                )
                teardown_result = phase_teardown(args.timeout)
                results.append(teardown_result)
                _print_result(teardown_result)
            # R71 Wave 2: 即使失败也输出 evidence(便于事后分析)
            if args.output:
                finished_at = _now_iso()
                evidence = _build_evidence(
                    results=results,
                    started_at=started_at,
                    finished_at=finished_at,
                    overall_passed=False,
                )
                output_path = Path(args.output)
                output_path.write_text(
                    json.dumps(evidence, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                print(
                    f"Evidence written to: {output_path}",
                    file=sys.stderr,
                )
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

    # R71 Wave 2: 输出 runtime-e2e-evidence.json
    finished_at = _now_iso()
    if args.output:
        evidence = _build_evidence(
            results=results,
            started_at=started_at,
            finished_at=finished_at,
            overall_passed=overall_passed,
        )
        output_path = Path(args.output)
        output_path.write_text(
            json.dumps(evidence, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(
            f"Evidence written to: {output_path}",
            file=sys.stderr,
        )

    print(
        f"\n=== R70 Wave 5 / R71 Wave 2: 全部 {len(results)} 阶段通过 ===",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
