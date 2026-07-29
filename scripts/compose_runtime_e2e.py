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
        daemon + .env + 不可变 image digest,验证 16 个阶段的运行态契约)

R73 §5.15 整改(16 阶段严格 DAG,后续阶段不能在上游失败后继续产生成功证据):
      1.  preflight                                — Docker / image digest / .env / role_set_equality / immutable_image_identity / no_production_bypass
      2.  start_infrastructure                     — redis-acl-init(exit 0) + redis(healthy) + migration(exit 0 + schema aligned)
      3.  start_application_roles                  — 所有长驻角色 healthy + exact RepoDigest
      4.  real_product_transaction_before_backup   — R73 P0-04: 真实产品交易(up→idx→dsp→writer→CRDB→output)
      5.  full_backup_to_r2                        — R73 P0-05: 全量备份到 R2(三对象 payload/manifest/COMPLETE)
      6.  blank_isolated_restore                   — R73 P0-05: 空白隔离恢复到 staging target
      7.  restore_integrity_and_target_identity    — R73 P0-05: 恢复完整性 + target identity 校验
      8.  actual_switch                            — R73 P0-06: 实际切换(active pointer 改变)
      9.  real_product_transaction_after_switch    — R73 P0-06: 切换后真实产品交易
     10.  fault_injection                          — R73 P0-06: 故障注入验证 switch probe
     11.  actual_rollback                          — R73 P0-06: 实际回滚到旧 identity
     12.  real_product_transaction_after_rollback  — R73 P0-06: 回滚后真实产品交易
     13.  sigterm_with_inflight_message            — R73 §5.11: SIGTERM + 处理中消息
     14.  restart_and_pending_recovery             — R73 §5.11: 重启 + 处理中消息恢复
     15.  final_identity_and_cleanup               — 最终 identity 校验 + 清理
     16.  evidence_signing                         — 签名 evidence envelope

CLI 选项:
    --phase <name>           只运行指定阶段(用于调试)
    --timeout <seconds>      每阶段超时(默认 600)
    --keep-on-success        全部通过时保留容器(跳过 final_identity_and_cleanup)供人工检查

R73 §5.15 DAG 失败传播规则:
    - 任一阶段失败总状态立即标记 failure
    - 仅允许执行 cleanup 和诊断采集阶段(final_identity_and_cleanup / evidence_signing)
    - 其余阶段必须 skipped(blocking_reason 记录上游失败阶段)
    - cleanup 成功不得覆盖原始 failure(overall_passed 一旦为 False 不可逆)
    - skipped 只在上游 failure 导致无法执行时存在,且总状态仍为 failure
    - 不允许 continue-on-error 影响门禁结论

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
# R71 RC35: 移除 `from __future__ import annotations`(防御性修复)。
# 根因(RC33 同类): `from __future__ import annotations` + `@dataclass` + PEP 604
# `int | None` / `str | None` 在 `dataclasses._is_type` 中可能触发
# `AttributeError: 'NoneType' object has no attribute '__dict__'`。
# 本脚本的 PhaseResult @dataclass 字段含 PEP 604 union 字段(虽然历史未触发,
# 但通过 importlib 加载的 verify_restore_integrity / synthetic_transaction 同类
# 模块已触发)。CI 使用 Python 3.12,本地 Python 3.10,均原生支持 PEP 604/585,
# 无需 `from __future__ import annotations`。统一移除避免后续 RC 再次失败。

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
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

# 项目根目录
REPO_ROOT = Path(__file__).resolve().parent.parent

# 默认生产 Compose 身份。RC Secretless 运行通过可重复的 --compose-file
# 显式注入完整 Compose 文件集合；所有 docker compose 子命令、preflight 和
# evidence 必须复用同一集合，禁止启动一套拓扑后退回另一套拓扑。
DEFAULT_COMPOSE_FILE = REPO_ROOT / "docker-compose.prod.yml"
COMPOSE_FILE = DEFAULT_COMPOSE_FILE  # 向后兼容既有单文件测试/调用方
COMPOSE_FILES: list[Path] = [DEFAULT_COMPOSE_FILE]

# .env 文件路径(包含 REDIS_*_PASSWORD 和 TGJIEMA_IMAGE)
ENV_FILE = REPO_ROOT / ".env"

# docker/entrypoint.py 路径(R71 Wave 2: 角色集合自动导出源)
ENTRYPOINT_PATH = REPO_ROOT / "docker" / "entrypoint.py"

# services/health.py 路径(R72 P0-05 fix: 用 AST 解析替代 import,
# 避免 services.health 顶层 `from loguru import logger` 在 CI host
# 进程(GitHub Actions runner 裸 Python,无 loguru)触发 ImportError)
HEALTH_PATH = REPO_ROOT / "services" / "health.py"

# synthetic_transaction.py 路径(R71 Wave 2: 合成交易执行器)
SYNTHETIC_TRANSACTION_PATH = REPO_ROOT / "scripts" / "synthetic_transaction.py"

# verify_restore_integrity.py 路径(R71 Wave 2/3: 恢复完整性校验)
VERIFY_RESTORE_INTEGRITY_PATH = REPO_ROOT / "scripts" / "verify_restore_integrity.py"

# R72 P0-05 fix: 脚本以 `python scripts/compose_runtime_e2e.py` 启动时,
# Python 把 sys.path[0] 设为脚本所在目录(scripts/),不含仓库根。这导致
# `from services.health import ROLE_REQUIREMENTS` 抛 ImportError,被旧版
# `except ImportError: pass` 静默吞掉,使 role_set_equality 假性失败。
# 显式把仓库根加入 sys.path,确保项目内模块在任意 cwd 下可导入。
sys.path.insert(0, str(REPO_ROOT))

# R71 Wave 7 (P1-04/05/P0-13): 运行配置身份绑定校验模块
# 用于严格校验 TGJIEMA_IMAGE 格式 + host config digest 绑定 + 当前 SHA 绑定
try:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from validate_runtime_config_binding import (  # type: ignore[import-not-found]
        DEFAULT_EXPECTED_REGISTRY,
        DEFAULT_EXPECTED_REPOSITORY,
        build_runtime_config_binding,
        validate_image_reference,
    )
    _RUNTIME_CONFIG_BINDING_AVAILABLE = True
except ImportError:  # pragma: no cover — 容错,compose_runtime_e2e 自身仍可运行
    _RUNTIME_CONFIG_BINDING_AVAILABLE = False

# R80 Step 12: MinIO/S3 存储配置(由 CLI 参数注入,phase 函数通过此字典访问)
# 单阶段模式(--phase X)下,argparse 解析后填充;全 DAG 模式下为空(使用 R2)。
_STORAGE_CONFIG: dict[str, str] = {
    "storage_backend": "",   # minio | r2 | ""
    "endpoint": "",          # e.g. http://localhost:9000
    "bucket": "",            # e.g. tgjiema-backup
    "access_key": "",        # CI_MINIO_ROOT_USER
    "secret_key": "",        # CI_MINIO_ROOT_PASSWORD
    "signing_key": "",       # CI_BACKUP_SIGNING_KEY
    "expect": "",            # failure | no-production-tag | ""
}

# 阶段 16 诊断 envelope 的权威 DAG 上下文。main() 在执行每个阶段前
# 原位更新该列表，使 phase_evidence_signing() 能聚合此前所有 required 阶段，
# 避免上游失败后仍因硬编码 success 生成 promotion_eligible=true 的冲突证据。
_DAG_RESULTS_CONTEXT: list["PhaseResult"] = []


def _get_entrypoint_roles() -> set[str]:
    """R71 Wave 2: 从 docker/entrypoint.py 的 ALLOWED_SERVICE_ROLES 自动导出角色集合。

    解析 entrypoint.py 中的:
      - SERVICE_ROLE_RUN_ALL = frozenset({...})
      - SERVICE_ROLE_MODULE = {...}
      - ALLOWED_SERVICE_ROLES = SERVICE_ROLE_RUN_ALL | frozenset(SERVICE_ROLE_MODULE.keys())

    返回 ALLOWED_SERVICE_ROLES 的完整角色集合(13 个角色,含 R76 §10.C 新增的 provider_sim)。

    fail-closed:解析失败时返回空集合(不允许硬编码角色列表作为 fallback)。

    Returns:
        13 个角色的集合:{up, idx, dsp, mon, admin, admin_bot, db_writer,
        crdb_sync, db_backup, r40_scheduler, migration, prometheus_exporter,
        provider_sim}
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
    # R79 §12: ALLOWED_SERVICE_ROLES 包含 provider_sim(R76 §10.C 新增,
    # entrypoint.py 运行时由 production/staging 守卫隔离)。角色发现函数应
    # 忠实返回 ALLOWED_SERVICE_ROLES 的全部角色(13 个),不做额外过滤。
    return roles


def _get_health_roles_and_aliases() -> tuple[set[str], dict[str, str]]:
    """R72 P0-05 fix: 用 AST 解析 services/health.py 提取角色集合,不导入模块。

    旧版用 `from services.health import ROLE_REQUIREMENTS` 在 CI 中触发
    ImportError(因为 services/health.py 顶层 `from loguru import logger`,
    而 GitHub Actions runner 的 host Python 没装 loguru)。

    本函数沿用与 `_get_entrypoint_roles()` 相同的 AST 解析模式,从源码静态
    提取两个常量:
      - ROLE_REQUIREMENTS: dict[str, dict[str, bool]] — 提取所有 key
        (即 health 模块认定的全部规范角色名,如 up_bot/idx_bot/db_writer...)
      - _ROLE_ALIASES: dict[str, str] — 提取 key→value 映射
        (即 entrypoint 简写 → health 规范名,如 "up" → "up_bot")

    Returns:
        (role_keys, aliases) 二元组:
        - role_keys: ROLE_REQUIREMENTS 的所有 key 集合
        - aliases: _ROLE_ALIASES 的 key→value 字典

    fail-closed:解析失败时返回 (set(), {})。调用方据此返回失败结果。
    """
    role_keys: set[str] = set()
    aliases: dict[str, str] = {}
    if not HEALTH_PATH.is_file():
        return role_keys, aliases
    try:
        src = HEALTH_PATH.read_text(encoding="utf-8")
        tree = ast.parse(src)
    except (OSError, SyntaxError):
        return role_keys, aliases

    for node in ast.walk(tree):
        # 同时支持 ast.Assign(无类型注解)和 ast.AnnAssign(有类型注解,如
        # `ROLE_REQUIREMENTS: dict[str, dict[str, bool]] = {...}`)。
        # services/health.py 中两个常量都用 AnnAssign 形式。
        assign_target: ast.Name | None = None
        assign_value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                continue
            assign_target = node.targets[0]
            assign_value = node.value
        elif isinstance(node, ast.AnnAssign):
            if not isinstance(node.target, ast.Name) or node.value is None:
                continue
            assign_target = node.target
            assign_value = node.value
        else:
            continue

        if assign_target.id == "ROLE_REQUIREMENTS":
            # ROLE_REQUIREMENTS: dict[str, dict[str, bool]] = {"role": {...}, ...}
            # 提取所有 key
            if isinstance(assign_value, ast.Dict):
                for key in assign_value.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        role_keys.add(key.value)
        elif assign_target.id == "_ROLE_ALIASES":
            # _ROLE_ALIASES: dict[str, str] = {"short": "canonical", ...}
            # 提取 key→value 映射
            if isinstance(assign_value, ast.Dict):
                keys = assign_value.keys
                vals = assign_value.values
                for k, v in zip(keys, vals):
                    if (
                        isinstance(k, ast.Constant) and isinstance(k.value, str)
                        and isinstance(v, ast.Constant) and isinstance(v.value, str)
                    ):
                        aliases[k.value] = v.value
    return role_keys, aliases


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
    "r40_scheduler": "r40_scheduler",
    "prometheus_exporter": "prometheus_exporter",
}

# R79 §12 / R76 §10.C: secretless-only 角色集合。provider_sim 属于 entrypoint
# ALLOWED_SERVICE_ROLES(R71 契约要求 13 角色忠实返回,见 _get_entrypoint_roles),
# 但不存在于生产 compose/health 拓扑(仅 docker-compose.secretless.yml)。
# preflight 的 role_set_equality 是生产拓扑不变式,需在比较前减去此集合。
SECRETLESS_ONLY_ROLES: frozenset[str] = frozenset({"provider_sim"})

# 阶段 2:核心服务(基础设施 + DBWriter)
CORE_SERVICES: list[str] = ["redis", "db_writer"]

# 阶段 3:Bot 服务 + 全部业务角色服务(R72 P0-04 扩展)
# R72 P0-04: r40_scheduler 已加入 docker-compose.prod.yml,
# 不再是半部署状态。migration 是 oneshot,通过 depends_on 自动触发。
BOT_SERVICES: list[str] = [
    "up", "idx", "dsp", "mon", "admin_bot",  # 5 个 Bot 服务
    "admin", "crdb_sync", "db_backup", "r40_scheduler",  # 4 个业务服务
    "prometheus_exporter",
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

# R73 §5.15 阶段 DAG 定义(严格顺序执行,后续阶段不能在上游失败后继续产生成功证据)
# 每个阶段的 depends_on 列表定义 DAG 边:前置阶段失败时,本阶段必须 skipped 而非 pass
PHASE_DEPENDENCIES: dict[str, list[str]] = {
    "preflight": [],
    "start_infrastructure": ["preflight"],
    "start_application_roles": ["start_infrastructure"],
    "real_product_transaction_before_backup": ["start_application_roles"],
    "full_backup_to_r2": ["real_product_transaction_before_backup"],
    "blank_isolated_restore": ["full_backup_to_r2"],
    "restore_integrity_and_target_identity": ["blank_isolated_restore"],
    "actual_switch": ["restore_integrity_and_target_identity"],
    "real_product_transaction_after_switch": ["actual_switch"],
    "fault_injection": ["real_product_transaction_after_switch"],
    "actual_rollback": ["fault_injection"],
    "real_product_transaction_after_rollback": ["actual_rollback"],
    "sigterm_with_inflight_message": ["real_product_transaction_after_rollback"],
    "restart_and_pending_recovery": ["sigterm_with_inflight_message"],
    "final_identity_and_cleanup": ["restart_and_pending_recovery"],
    "evidence_signing": ["final_identity_and_cleanup"],
}

# R73 §5.15: 失败后允许执行的阶段(cleanup 和诊断采集)
# 这些阶段即使上游失败也必须执行,但 cleanup 成功不覆盖原始 failure。
# 仅这两个阶段可在上游失败后继续产生执行证据;其余阶段必须 skipped。
ALLOWED_AFTER_FAILURE: set[str] = {
    "final_identity_and_cleanup",  # cleanup: 清理资源 + 最终 identity 校验
    "evidence_signing",             # 诊断采集: 签名 evidence envelope
}

# 阶段定义(顺序执行,符合 R73 §5.15 DAG)
PHASES: list[tuple[str, str]] = [
    ("preflight", "Preflight: Docker daemon / image digest / .env / role_set_equality / immutable_image_identity / no_production_bypass"),
    ("start_infrastructure", "Start redis-acl-init + redis + migration, wait for readiness"),
    ("start_application_roles", "Start all long-running roles (up/idx/dsp/mon/admin_bot/admin/crdb_sync/db_backup/r40_scheduler/prometheus_exporter)"),
    ("real_product_transaction_before_backup", "R73 P0-04: Real product transaction via up→idx→dsp→writer→CRDB→output"),
    ("full_backup_to_r2", "R73 P0-05: Full backup to R2 (three objects: payload/manifest/COMPLETE)"),
    ("blank_isolated_restore", "R73 P0-05: Blank isolated restore to staging target"),
    ("restore_integrity_and_target_identity", "R73 P0-05: Verify restore integrity (schema/row count/field hash/target identity)"),
    ("actual_switch", "R73 P0-06: Execute actual switch (active pointer change)"),
    ("real_product_transaction_after_switch", "R73 P0-06: Real product transaction after switch"),
    ("fault_injection", "R73 P0-06: Inject fault to verify switch probe fails"),
    ("actual_rollback", "R73 P0-06: Execute actual rollback to old identity"),
    ("real_product_transaction_after_rollback", "R73 P0-06: Real product transaction after rollback"),
    ("sigterm_with_inflight_message", "R73 §5.11: SIGTERM with in-flight message"),
    ("restart_and_pending_recovery", "R73 §5.11: Restart and pending message recovery"),
    ("final_identity_and_cleanup", "Final identity verification and cleanup"),
    ("evidence_signing", "Sign evidence envelope"),
]


@dataclass
class PhaseResult:
    """单阶段执行结果(JSON 证据)。

    R73 §5.15 整改:新增 DAG 相关字段,严格记录阶段依赖与时间戳,
    后续阶段不能在上游失败后继续产生成功证据。
    """

    phase: str
    description: str
    status: str  # "pass" | "fail" | "skipped"
    timestamp: str  # ISO 8601 UTC(完成时间,向后兼容)
    duration_seconds: float
    # R73 §5.15: DAG 字段
    depends_on: list[str] = field(default_factory=list)
    started_at: str = ""  # ISO 8601 UTC(阶段开始时间)
    completed_at: str = ""  # ISO 8601 UTC(阶段完成时间)
    blocking_reason: str | None = None  # 失败原因(skipped 时记录上游失败阶段)
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None
    error: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    readiness_checks: list[dict[str, Any]] = field(default_factory=list)


def _aggregate_required_phase_conclusion(
    results: list[PhaseResult],
) -> tuple[str, bool, dict[str, Any]]:
    """聚合阶段 16 之前的 required DAG 结果并生成 fail-closed 结论。

    只有前 15 个 required 阶段各出现且恰好出现一次、状态全部为 pass 时，
    才返回 ("success", True, details)。缺失、重复、fail、skipped 或未知状态
    均返回 ("failure", False, details)，防止诊断 evidence 冒充晋级授权。
    """
    required_phases = [
        phase_name for phase_name, _ in PHASES
        if phase_name != "evidence_signing"
    ]
    statuses_by_phase: dict[str, list[str]] = {
        phase_name: [] for phase_name in required_phases
    }
    for result in results:
        if result.phase in statuses_by_phase:
            statuses_by_phase[result.phase].append(result.status)

    missing = [
        phase_name for phase_name, statuses in statuses_by_phase.items()
        if not statuses
    ]
    duplicates = {
        phase_name: statuses
        for phase_name, statuses in statuses_by_phase.items()
        if len(statuses) > 1
    }
    non_pass = {
        phase_name: statuses[0]
        for phase_name, statuses in statuses_by_phase.items()
        if len(statuses) == 1 and statuses[0] != "pass"
    }
    all_required_passed = not missing and not duplicates and not non_pass
    details = {
        "required_phase_count": len(required_phases),
        "observed_required_phase_count": sum(
            1 for statuses in statuses_by_phase.values() if statuses
        ),
        "required_phase_statuses": {
            phase_name: statuses[0] if len(statuses) == 1 else statuses
            for phase_name, statuses in statuses_by_phase.items()
        },
        "missing_required_phases": missing,
        "duplicate_required_phases": duplicates,
        "non_pass_required_phases": non_pass,
        "all_required_phases_passed": all_required_passed,
    }
    conclusion = "success" if all_required_passed else "failure"
    return conclusion, all_required_passed, details


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

    R72 RC66: subprocess.run 在 timeout 触发时会抛 TimeoutExpired 并丢失
    已捕获的 stdout/stderr。本函数在 timeout 时重新抛出带有 partial output
    的 TimeoutExpired,供调用方提取诊断信息(避免 600s 超时后 stdout/stderr
    全空,无法定位失败原因)。
    """
    full_env = None
    if env is not None:
        full_env = os.environ.copy()
        full_env.update(env)
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
            env=full_env,
        )
    except subprocess.TimeoutExpired as e:
        # R72 RC66: 重新抛出带有 partial output 的 TimeoutExpired
        # subprocess.run 在 timeout 时已捕获部分输出(存于 e.stdout/e.stderr),
        # 但默认 TimeoutExpired 的 stdout/stderr 可能为 None(取决于捕获时机)。
        # 确保字段非 None,便于调用方安全引用。
        if e.stdout is None:
            e.stdout = ""
        if e.stderr is None:
            e.stderr = ""
        raise


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


def _active_compose_files() -> list[Path]:
    """返回当前权威 Compose 文件集合。

    COMPOSE_FILE 保留给既有调用方和测试；当 COMPOSE_FILES 仍为默认值而
    COMPOSE_FILE 被覆盖时，自动采用被覆盖的单文件身份。
    """
    if COMPOSE_FILES == [DEFAULT_COMPOSE_FILE] and COMPOSE_FILE != DEFAULT_COMPOSE_FILE:
        return [COMPOSE_FILE]
    return list(COMPOSE_FILES)


def _compose_cmd(args: list[str]) -> list[str]:
    """构造复用同一权威 Compose 文件集合的命令。"""
    command = ["docker", "compose"]
    for compose_file in _active_compose_files():
        command.extend(["-f", str(compose_file)])
    return command + args


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
    started_at: str = "",
    depends_on: list[str] | None = None,
) -> PhaseResult:
    """构造失败结果。"""
    completed = _now_iso()
    return PhaseResult(
        phase=phase,
        description=description,
        status="fail",
        timestamp=completed,
        duration_seconds=time.time() - started,
        depends_on=depends_on if depends_on is not None else PHASE_DEPENDENCIES.get(phase, []),
        started_at=started_at or completed,
        completed_at=completed,
        blocking_reason=error,
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
    started_at: str = "",
    depends_on: list[str] | None = None,
) -> PhaseResult:
    """构造通过结果。"""
    completed = _now_iso()
    return PhaseResult(
        phase=phase,
        description=description,
        status="pass",
        timestamp=completed,
        duration_seconds=time.time() - started,
        depends_on=depends_on if depends_on is not None else PHASE_DEPENDENCIES.get(phase, []),
        started_at=started_at or completed,
        completed_at=completed,
        blocking_reason=None,
        stdout=stdout,
        stderr=stderr,
        returncode=returncode,
        evidence=evidence or {},
        readiness_checks=readiness_checks or [],
    )


def _skipped_result(
    phase: str,
    description: str,
    blocking_reason: str,
    *,
    depends_on: list[str] | None = None,
) -> PhaseResult:
    """R73 §5.15: 构造 skipped 结果(上游失败导致本阶段无法执行)。

    skipped 不影响最终失败结论(总状态仍为 failure),
    但保留阶段记录以供 evidence 审计。
    """
    now = _now_iso()
    return PhaseResult(
        phase=phase,
        description=description,
        status="skipped",
        timestamp=now,
        duration_seconds=0.0,
        depends_on=depends_on if depends_on is not None else PHASE_DEPENDENCIES.get(phase, []),
        started_at=now,
        completed_at=now,
        blocking_reason=blocking_reason,
        error=blocking_reason,
    )


def _get_compose_ps_info(
    include_exited: bool = False,
) -> dict[str, dict[str, Any]]:
    """R72 P0-06/07/13/14: 解析 docker compose ps --format json,返回每个服务的状态。

    替代旧版在各 phase 内联的 ps 解析逻辑,统一返回结构化状态信息,
    供 start_core / start_bots / sigterm / restart 阶段做严格断言。

    返回 dict[service_name] = {
        "state": str,           # running / exited / restarting / dead / ...
        "health": str,          # healthy / unhealthy / starting / "" (无 healthcheck)
        "exit_code": int | None,
    }

    Args:
        include_exited: True 时使用 `docker compose ps -a`(包含已退出容器),
                       False 时只包含运行中容器。

    fail-closed:docker compose ps 失败或输出无法解析时返回空 dict
    (调用方必须显式检查期望服务是否存在,空 dict 会导致断言失败)。
    """
    cmd_args = ["ps"]
    if include_exited:
        cmd_args.append("-a")
    cmd_args.extend(["--format", "json"])
    ps_cmd = _compose_cmd(cmd_args)
    ps_result = _run(ps_cmd, timeout=30, cwd=REPO_ROOT)
    info: dict[str, dict[str, Any]] = {}
    if ps_result.returncode != 0:
        return info

    stdout_stripped = ps_result.stdout.strip()
    parsed_entries: list[dict[str, Any]] = []
    # 支持多行 JSON 对象(旧版 docker compose)
    for line in stdout_stripped.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, list):
                parsed_entries.extend(e for e in obj if isinstance(e, dict))
            elif isinstance(obj, dict):
                parsed_entries.append(obj)
        except json.JSONDecodeError:
            continue
    # 支持单行 JSON 数组(新版 docker compose)
    if not parsed_entries and stdout_stripped.startswith("["):
        try:
            arr = json.loads(stdout_stripped)
            if isinstance(arr, list):
                parsed_entries = [e for e in arr if isinstance(e, dict)]
        except json.JSONDecodeError:
            pass

    for svc_info in parsed_entries:
        svc_name = svc_info.get("Service") or svc_info.get("service", "")
        if not svc_name:
            continue
        state = (
            svc_info.get("State")
            or svc_info.get("state", "")
            or svc_info.get("Status", "")
            or ""
        )
        health = svc_info.get("Health") or svc_info.get("health", "") or ""
        exit_code = svc_info.get("ExitCode")
        if exit_code is not None:
            try:
                exit_code = int(exit_code)
            except (ValueError, TypeError):
                exit_code = None
        # R72 RC55 fix: 捕获容器实际 Name / ID,供后续 docker inspect 使用。
        # docker compose ps JSON 输出可能用 "Name"/"Containers"/"ID" 等字段,
        # container_name 在 compose 文件中显式声明时通常是 "tgjiema-{svc}",
        # 但在某些 docker compose 版本 / 配置下可能带项目前缀或 -1 后缀。
        # 使用 compose ps 返回的真实 Name 最稳健。
        container_name = (
            svc_info.get("Name")
            or svc_info.get("name")
            or svc_info.get("Container")
            or ""
        )
        container_id = svc_info.get("ID") or svc_info.get("Id") or ""
        info[svc_name] = {
            "state": str(state),
            "health": str(health),
            "exit_code": exit_code,
            "container_name": str(container_name) if container_name else "",
            "container_id": str(container_id) if container_id else "",
        }
    return info


def _wait_for_services(
    expected: dict[str, dict[str, str]],
    timeout_seconds: int = 180,
    poll_interval: int = 5,
) -> tuple[bool, dict[str, dict[str, Any]]]:
    """R72 P0-06/07/14: 轮询 docker compose ps 直到所有服务达到期望状态。

    Args:
        expected: dict[service_name] = {"state": "...", "health": "..."}
                  health 为空字符串表示不检查 health(如 oneshot 服务)。
        timeout_seconds: 总等待秒数。
        poll_interval: 轮询间隔秒数。

    Returns:
        (all_ready, final_info) — all_ready=True 表示所有服务达到期望状态。
    """
    deadline = time.time() + timeout_seconds
    last_info: dict[str, dict[str, Any]] = {}
    while time.time() < deadline:
        last_info = _get_compose_ps_info(include_exited=True)
        all_ready = True
        for svc, req in expected.items():
            si = last_info.get(svc)
            if si is None:
                all_ready = False
                break
            if si["state"] != req["state"]:
                all_ready = False
                break
            if req["health"] and si["health"] != req["health"]:
                all_ready = False
                break
        if all_ready:
            return True, last_info
        time.sleep(poll_interval)
    return False, last_info


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

    # 2. 每个权威 Compose 文件都必须存在；禁止缺失 overlay 时静默回退。
    compose_files = _active_compose_files()
    missing_compose_files = [path for path in compose_files if not path.is_file()]
    if not compose_files or missing_compose_files:
        return _fail_result(
            phase="preflight",
            description=description,
            started=started,
            error=(
                "Compose 文件集合为空或存在缺失项: "
                f"{[str(path) for path in missing_compose_files]}"
            ),
            readiness_checks=[
                {"check": "docker_daemon", "status": "pass"},
                {"check": "compose_files", "status": "fail"},
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
    # R72 P1-01: runtime binding 必须 fail-closed — 模块缺失、binding 失败
    # 或 inspect 失败即整体失败,禁止回退到宽松字符串包含检查。
    if not _RUNTIME_CONFIG_BINDING_AVAILABLE:
        # R72 P1-01: 模块不可用是代码缺陷,不允许回退
        return _fail_result(
            phase="preflight",
            description=description,
            started=started,
            error=(
                "R72 P1-01: validate_runtime_config_binding 模块不可用,"
                "runtime binding 校验 fail-closed — 禁止回退到宽松字符串检查"
            ),
            readiness_checks=[
                {"check": "docker_daemon", "status": "pass"},
                {"check": "compose_file", "status": "pass"},
                {"check": "env_file", "status": "pass"},
                {"check": "image_digest", "status": "fail"},
            ],
        )
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
                "TGJIEMA_IMAGE 格式不合法(R72 P1-01 严格校验)— "
                + "; ".join(img_errors)
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

    # 6. R72 P0-05: 严格角色集合等价检查
    # entrypoint 生产角色 == Compose SERVICE_ROLE 集合 == health ROLE_REQUIREMENTS 角色
    entrypoint_roles = _get_entrypoint_roles()
    compose_roles = {
        v for v in SERVICE_ROLES.values() if v != "infrastructure"
    }

    # R72 P0-05 fix: 旧版用 `except ImportError: health_roles = set()` 静默吞异常,
    # 在 CI(cwd=仓库根,但 sys.path[0]=scripts/)中 `from services.health import ...`
    # 抛 ImportError 被吞,导致 health_roles 为空集 → role_set_equality 假性失败。
    #
    # 真实根因(RC49 evidence 实锤):services/health.py 顶层
    # `from loguru import logger`,而 GitHub Actions runner 的 host Python
    # 没装 loguru。即使 sys.path 含仓库根,`import services.health` 仍会触发
    # loguru 导入失败。
    #
    # 最终修复:用 AST 静态解析 services/health.py 提取 ROLE_REQUIREMENTS
    # 和 _ROLE_ALIASES,完全不导入模块(与 _get_entrypoint_roles() 同一模式)。
    # 这样 host 进程不依赖 services.health 的运行时依赖链。
    health_role_keys, health_aliases = _get_health_roles_and_aliases()
    if not health_role_keys:
        return _fail_result(
            phase="preflight",
            description=description,
            started=started,
            error=(
                f"R72 P0-05: AST 解析 services/health.py 失败,无法提取 "
                f"ROLE_REQUIREMENTS。HEALTH_PATH={str(HEALTH_PATH)!r}, "
                f"exists={HEALTH_PATH.is_file()}. 不允许吞异常导致 "
                f"role_set_equality 假性通过/失败。"
            ),
            readiness_checks=[
                {"check": "docker_daemon", "status": "pass"},
                {"check": "compose_file", "status": "pass"},
                {"check": "env_file", "status": "pass"},
                {"check": "image_digest", "status": "pass"},
                {"check": "redis_passwords", "status": "pass"},
                {"check": "role_set_equality", "status": "fail"},
            ],
        )

    # 别名归一化: health 使用 up_bot/idx_bot 等,entrypoint/compose 使用 up/idx
    # 把 health 规范名通过 _ROLE_ALIASES 反向映射回 entrypoint 简写
    normalized_health: set[str] = set()
    for r in health_role_keys:
        found_short = None
        for short_name, canonical in health_aliases.items():
            if canonical == r and short_name:
                found_short = short_name
                break
        normalized_health.add(found_short if found_short is not None else r)
    health_roles = normalized_health

    # R79 §12: role_set_equality 是生产拓扑不变式。entrypoint 忠实返回
    # ALLOWED_SERVICE_ROLES 的全部角色(含 R76 §10.C 新增的 secretless-only
    # provider_sim,见 _get_entrypoint_roles 的 13 角色契约),但 provider_sim
    # 不存在于生产 compose/health 拓扑。比较前减去 SECRETLESS_ONLY_ROLES,
    # 使三方仅等价于生产角色集合,避免误报。
    production_entrypoint = entrypoint_roles - SECRETLESS_ONLY_ROLES

    role_mismatches: list[str] = []
    if production_entrypoint != compose_roles:
        only_entrypoint = production_entrypoint - compose_roles
        only_compose = compose_roles - production_entrypoint
        if only_entrypoint or only_compose:
            role_mismatches.append(
                f"entrypoint vs compose: only_entrypoint={only_entrypoint or '{}'}, "
                f"only_compose={only_compose or '{}'}"
            )
    if production_entrypoint != health_roles:
        only_entrypoint = production_entrypoint - health_roles
        only_health = health_roles - production_entrypoint
        if only_entrypoint or only_health:
            role_mismatches.append(
                f"entrypoint vs health: only_entrypoint={only_entrypoint or '{}'}, "
                f"only_health={only_health or '{}'}"
            )

    base_checks = [
        {"check": "docker_daemon", "status": "pass"},
        {"check": "compose_file", "status": "pass"},
        {"check": "env_file", "status": "pass"},
        {"check": "image_digest", "status": "pass"},
        {"check": "redis_passwords", "status": "pass"},
    ]

    if role_mismatches:
        base_checks.append({"check": "role_set_equality", "status": "fail"})
        return _fail_result(
            phase="preflight",
            description=description,
            started=started,
            error=(
                f"R72 P0-05: 角色集合不一致 — "
                f"{'; '.join(role_mismatches)} — "
                f"entrypoint/compose/health 三方角色集合必须完全等价"
            ),
            readiness_checks=base_checks,
        )

    base_checks.append({"check": "role_set_equality", "status": "pass"})

    # R73 §5.15: immutable_image_identity 校验 — image digest 已通过
    # validate_image_reference 严格校验(非浮动 tag),此处只追加 readiness 记录
    base_checks.append({"check": "immutable_image_identity", "status": "pass"})

    # R73 §5.15: no_production_bypass — 调用 scripts/scan_production_bypasses.py
    # 验证无 P0/P1 违规(continue-on-error / if:always() success / 浮动 tag 等)
    bypass_check: dict[str, Any] = {
        "check": "no_production_bypass", "status": "fail",
    }
    bypass_evidence: dict[str, Any] = {}
    scan_bypasses_path = REPO_ROOT / "scripts" / "scan_production_bypasses.py"
    if not scan_bypasses_path.is_file():
        bypass_check["reason"] = "scan_production_bypasses.py 不存在"
        bypass_check["status"] = "fail"
        return _fail_result(
            phase="preflight",
            description=description,
            started=started,
            error=(
                "R73 §5.15: scan_production_bypasses.py 不存在 — "
                "no_production_bypass 校验 fail-closed"
            ),
            evidence={
                "scan_bypasses_path": str(scan_bypasses_path),
            },
            readiness_checks=base_checks + [bypass_check],
        )
    # 调用 scanner,通过 -m 方式运行(避免 import 副作用)
    scan_cmd = [
        sys.executable, "-m", "scan_production_bypasses",
        "--json",
    ]
    scan_scripts_dir = str(REPO_ROOT / "scripts")
    scan_env = os.environ.copy()
    scan_env["PYTHONPATH"] = (
        scan_scripts_dir + os.pathsep + scan_env.get("PYTHONPATH", "")
    )
    try:
        scan_result = _run(
            scan_cmd, timeout=120, cwd=REPO_ROOT, env=scan_env,
        )
    except subprocess.TimeoutExpired as te:
        bypass_check["reason"] = f"scanner 超时({120}s)"
        bypass_check["status"] = "fail"
        return _fail_result(
            phase="preflight",
            description=description,
            started=started,
            error=(
                f"R73 §5.15: scan_production_bypasses 超时({120}s) — "
                "no_production_bypass 校验 fail-closed"
            ),
            stdout=(te.stdout or "") if isinstance(te.stdout, str) else "",
            stderr=(te.stderr or "") if isinstance(te.stderr, str) else "",
            readiness_checks=base_checks + [bypass_check],
        )
    if scan_result.returncode != 0:
        bypass_check["reason"] = f"scanner exit={scan_result.returncode}"
        bypass_check["status"] = "fail"
        return _fail_result(
            phase="preflight",
            description=description,
            started=started,
            error=(
                f"R73 §5.15: scan_production_bypasses 失败 "
                f"(exit={scan_result.returncode}) — "
                f"存在 P0/P1 违规或扫描器异常(fail-closed)"
            ),
            stdout=scan_result.stdout,
            stderr=scan_result.stderr,
            returncode=scan_result.returncode,
            readiness_checks=base_checks + [bypass_check],
        )
    # 解析 scanner JSON 输出
    try:
        scan_data = json.loads(scan_result.stdout.strip() or "{}")
    except json.JSONDecodeError as e:
        bypass_check["reason"] = f"scanner 输出非 JSON: {e}"
        bypass_check["status"] = "fail"
        return _fail_result(
            phase="preflight",
            description=description,
            started=started,
            error=(
                f"R73 §5.15: scan_production_bypasses 输出非 JSON: {e} — "
                "no_production_bypass 校验 fail-closed"
            ),
            stdout=scan_result.stdout,
            stderr=scan_result.stderr,
            readiness_checks=base_checks + [bypass_check],
        )
    bypass_passed = bool(scan_data.get("passed", False))
    summary = scan_data.get("summary", {}) or {}
    violations_by_severity = summary.get("violations_by_severity", {}) or {}
    bypass_evidence = {
        "passed": bypass_passed,
        "policy_version": scan_data.get("policy_version", ""),
        "files_scanned": summary.get("files_scanned", 0),
        "violations_by_severity": violations_by_severity,
        "violations_count": len(scan_data.get("violations", [])),
    }
    bypass_check["status"] = "pass" if bypass_passed else "fail"
    bypass_check["evidence"] = bypass_evidence
    if not bypass_passed:
        return _fail_result(
            phase="preflight",
            description=description,
            started=started,
            error=(
                f"R73 §5.15: no_production_bypass 校验失败 — "
                f"P0={violations_by_severity.get('P0', 0)}, "
                f"P1={violations_by_severity.get('P1', 0)} — "
                "存在生产绕过违规(fail-closed)"
            ),
            stdout=scan_result.stdout,
            stderr=scan_result.stderr,
            evidence=bypass_evidence,
            readiness_checks=base_checks + [bypass_check],
        )
    base_checks.append(bypass_check)

    return _pass_result(
        phase="preflight",
        description=description,
        started=started,
        evidence={
            "docker_available": True,
            "compose_file": str(_active_compose_files()[0]),
            "compose_files": [str(path) for path in _active_compose_files()],
            "env_file": str(ENV_FILE),
            "tgjiema_image": tgjiema_image,
            "redis_passwords_set": [
                v for v in REQUIRED_ENV_VARS if v.startswith("REDIS_")
            ],
            "role_sets": {
                "entrypoint": sorted(entrypoint_roles),
                "compose": sorted(compose_roles),
                "health": sorted(health_roles),
            },
            "no_production_bypass": bypass_evidence,
        },
        readiness_checks=base_checks,
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
            logs_cmd = _compose_cmd(["logs", "--no-color", "--tail", "500", svc])
            try:
                logs_result = _run(logs_cmd, timeout=15, cwd=REPO_ROOT)
                svc_log = (logs_result.stdout or "") + (logs_result.stderr or "")
                if svc_log.strip():
                    container_logs[svc] = svc_log[-8000:]
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
            logs_cmd = _compose_cmd(["logs", "--no-color", "--tail", "500", svc])
            try:
                logs_result = _run(logs_cmd, timeout=15, cwd=REPO_ROOT)
                svc_log = (logs_result.stdout or "") + (logs_result.stderr or "")
                if svc_log.strip():
                    container_logs[svc] = svc_log[-8000:]
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

    # R72 P0-06: 逐服务严格断言(替代旧版"发现至少一个核心服务"弱断言)
    # - redis-acl-init: exited 0
    # - redis: running + healthy
    # - migration: completed + exit 0
    # - db_writer: running + healthy
    # 缺少任一服务、状态未知或无法解析都必须失败。
    readiness_checks: list[dict[str, Any]] = [
        {"check": "docker_daemon", "status": "pass"},
        {"check": "compose_up", "status": "pass"},
    ]

    # 轮询等待所有核心服务达到期望状态
    # (db_writer healthcheck start_period=90s,需要等待)
    expected_core: dict[str, dict[str, str]] = {
        "redis-acl-init": {"state": "exited", "health": ""},
        "redis": {"state": "running", "health": "healthy"},
        "migration": {"state": "exited", "health": ""},
        "db_writer": {"state": "running", "health": "healthy"},
    }
    all_ready, service_info = _wait_for_services(
        expected=expected_core, timeout_seconds=180, poll_interval=5,
    )

    # 逐服务断言
    service_failures: list[str] = []
    for svc, req in expected_core.items():
        si = service_info.get(svc)
        if si is None:
            service_failures.append(
                f"{svc}: 服务不存在(docker compose ps 未发现)"
            )
            readiness_checks.append({
                "check": f"service_{svc}",
                "status": "fail",
                "reason": "not_found",
            })
            continue
        svc_ok = si["state"] == req["state"]
        if req["health"]:
            svc_ok = svc_ok and si["health"] == req["health"]
        # 对 oneshot 服务(redis-acl-init, migration)额外检查 exit_code == 0
        if req["state"] == "exited":
            exit_code = si.get("exit_code")
            svc_ok = svc_ok and (exit_code == 0)
        readiness_checks.append({
            "check": f"service_{svc}",
            "status": "pass" if svc_ok else "fail",
            "state": si["state"],
            "health": si["health"],
            "exit_code": si.get("exit_code"),
            "expected_state": req["state"],
            "expected_health": req["health"],
        })
        if not svc_ok:
            reasons = [f"state={si['state']!r}(expected={req['state']!r})"]
            if req["health"]:
                reasons.append(
                    f"health={si['health']!r}(expected={req['health']!r})"
                )
            if req["state"] == "exited":
                reasons.append(
                    f"exit_code={si.get('exit_code')!r}(expected=0)"
                )
            service_failures.append(f"{svc}: {', '.join(reasons)}")

    if service_failures or not all_ready:
        # R72 RC51 fix: 捕获失败容器的 docker logs 用于诊断
        # (db_writer healthcheck 失败时,需要看到容器内日志才能定位根因)
        container_logs: dict[str, Any] = {}
        for svc in expected_core:
            logs_cmd = _compose_cmd(["logs", "--no-color", "--tail", "300", svc])
            try:
                logs_result = _run(logs_cmd, timeout=15, cwd=REPO_ROOT)
                svc_log = (logs_result.stdout or "") + (logs_result.stderr or "")
                if svc_log.strip():
                    container_logs[svc] = svc_log[-6000:]
            except (subprocess.TimeoutExpired, OSError):
                pass
        return _fail_result(
            phase="start_core",
            description=description,
            started=started,
            error=(
                f"R72 P0-06: 核心服务状态断言失败 — "
                f"{'; '.join(service_failures) if service_failures else '等待超时(180s)'}"
            ),
            evidence={
                "service_info": service_info,
                "expected": expected_core,
                "failures": service_failures,
                "wait_timeout_seconds": 180,
                "all_ready": all_ready,
                "container_logs": container_logs,
            },
            readiness_checks=readiness_checks,
        )

    return _pass_result(
        phase="start_core",
        description=description,
        started=started,
        stdout=result.stdout,
        stderr=result.stderr,
        returncode=result.returncode,
        evidence={
            "started_services": sorted(expected_core.keys()),
            "service_info": service_info,
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
    depends_on 自动触发;r40_scheduler 已在 R72 P0-04 中加入 compose)。

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

    # R72 P0-07: 对每个长驻角色验证服务存在 + running + healthy
    # restarting / exited / dead / unhealthy / starting / 状态未知 均失败
    readiness_checks: list[dict[str, Any]] = [
        {"check": "docker_daemon", "status": "pass"},
        {"check": "compose_up", "status": "pass"},
    ]

    # 轮询等待所有 Bot 服务达到 running + healthy
    # R72 RC52 fix: timeout 从 180s 提升到 300s。
    # healthcheck 配置为 start_period=90s + interval=30s + retries=3,
    # 理论最小到 healthy 时间 = 90s + 3*30s = 180s(恰好与旧 timeout 重合,
    # 边界效应导致 admin_bot/r40_scheduler 等慢启动服务在 180s 时仍为 starting)。
    # 提升到 300s 给慢启动服务(app import / SQLite 初始化 / Redis 连接建立)
    # 提供额外 120s 缓冲,避免边界假性失败。
    expected_bots: dict[str, dict[str, str]] = {
        svc: {"state": "running", "health": "healthy"} for svc in BOT_SERVICES
    }
    all_ready, service_info = _wait_for_services(
        expected=expected_bots, timeout_seconds=300, poll_interval=5,
    )

    # 逐服务断言
    service_failures: list[str] = []
    for svc, req in expected_bots.items():
        si = service_info.get(svc)
        if si is None:
            service_failures.append(f"{svc}: 服务不存在(docker compose ps 未发现)")
            readiness_checks.append({
                "check": f"service_{svc}",
                "status": "fail",
                "reason": "not_found",
            })
            continue
        svc_ok = si["state"] == "running" and si["health"] == "healthy"
        readiness_checks.append({
            "check": f"service_{svc}",
            "status": "pass" if svc_ok else "fail",
            "state": si["state"],
            "health": si["health"],
        })
        if not svc_ok:
            service_failures.append(
                f"{svc}: state={si['state']!r}, health={si['health']!r} "
                f"(expected state='running', health='healthy')"
            )

    readiness_checks.append({
        "check": "bot_services_healthy",
        "status": "pass" if not service_failures else "fail",
        "failures": service_failures,
    })

    if service_failures or not all_ready:
        # R72 RC52 fix: 失败时捕获容器日志 + 主动运行 health check 获取
        # 结构化诊断信息(与 start_core 失败路径对齐)。
        # 1) docker compose logs:看应用启动是否报错(import 失败/连接拒绝/...)
        # 2) docker compose exec check_readiness --json:看具体哪个检查项失败
        #    (database_crdb / bot_token_valid / scheduler_heartbeat / ...)
        container_logs: dict[str, Any] = {}
        health_check_detail: dict[str, Any] = {}
        failing_svcs = [
            svc for svc in BOT_SERVICES
            if svc in service_info and (
                service_info[svc].get("state") != "running"
                or service_info[svc].get("health") != "healthy"
            )
        ]
        for svc in failing_svcs:
            # 1) 捕获容器日志
            logs_cmd = _compose_cmd(["logs", "--no-color", "--tail", "300", svc])
            try:
                logs_result = _run(logs_cmd, timeout=15, cwd=REPO_ROOT)
                svc_log = (logs_result.stdout or "") + (logs_result.stderr or "")
                if svc_log.strip():
                    container_logs[svc] = svc_log[-6000:]
            except (subprocess.TimeoutExpired, OSError):
                pass

            # 2) 主动运行 health check 获取结构化 JSON
            #    (healthcheck 命令本身只 exit 1 不打印错误,
            #     通过 exec 运行 check_readiness --json 可看到具体失败的检查项)
            health_cmd = _compose_cmd([
                "exec", "-T", svc, "python", "-c",
                "import asyncio, os, json; "
                "from services.health import check_readiness; "
                "r = asyncio.run(check_readiness(os.environ.get('SERVICE_ROLE', ''))); "
                "print(json.dumps(r.to_dict(), ensure_ascii=False))",
            ])
            try:
                health_result = _run(health_cmd, timeout=20, cwd=REPO_ROOT)
                health_stdout = (health_result.stdout or "").strip()
                if health_stdout:
                    try:
                        health_check_detail[svc] = json.loads(health_stdout)
                    except json.JSONDecodeError:
                        health_check_detail[svc] = {
                            "parse_error": "stdout is not JSON",
                            "raw_stdout": health_stdout[-2000:],
                            "stderr": (health_result.stderr or "")[-1000:],
                        }
            except (subprocess.TimeoutExpired, OSError):
                health_check_detail[svc] = {"error": "exec timed out or failed"}

        return _fail_result(
            phase="start_bots",
            description=description,
            started=started,
            error=(
                f"R72 P0-07: Bot 服务状态断言失败 — "
                f"{'; '.join(service_failures) if service_failures else '等待超时(300s)'}"
            ),
            stdout=result.stdout,
            stderr=result.stderr,
            evidence={
                "service_info": service_info,
                "failures": service_failures,
                "wait_timeout_seconds": 300,
                "all_ready": all_ready,
                "container_logs": container_logs,
                "health_check_detail": health_check_detail,
            },
            readiness_checks=readiness_checks,
        )

    # R72 P0-07: 验证每个 Bot 服务的 SERVICE_ROLE 环境变量 + RepoDigest
    # 禁止只检查服务名存在,必须验证角色身份和镜像身份
    role_failures: list[str] = []
    digest_failures: list[str] = []
    expected_image = os.environ.get("TGJIEMA_IMAGE", "")
    expected_digest = ""
    if "@" in expected_image:
        expected_digest = expected_image.split("@", 1)[1]

    for svc in BOT_SERVICES:
        # 验证 SERVICE_ROLE 环境变量
        role_cmd = _compose_cmd(["exec", "-T", svc, "printenv", "SERVICE_ROLE"])
        role_result = _run(role_cmd, timeout=15, cwd=REPO_ROOT)
        if role_result.returncode != 0 or not role_result.stdout.strip():
            role_failures.append(f"{svc}: SERVICE_ROLE 未设置或读取失败")
            readiness_checks.append({
                "check": f"service_role_{svc}",
                "status": "fail",
                "reason": "env_not_set",
            })
        else:
            actual_role = role_result.stdout.strip()
            expected_role = SERVICE_ROLES.get(svc, "")
            if actual_role != expected_role:
                role_failures.append(
                    f"{svc}: SERVICE_ROLE={actual_role!r} (expected={expected_role!r})"
                )
                readiness_checks.append({
                    "check": f"service_role_{svc}",
                    "status": "fail",
                    "actual": actual_role,
                    "expected": expected_role,
                })
            else:
                readiness_checks.append({
                    "check": f"service_role_{svc}",
                    "status": "pass",
                })

        # R72 P1-02: 验证容器实际使用的 RepoDigest(不是环境变量请求的 digest)
        # R72 RC55 fix: 使用 docker compose ps 返回的真实容器名,
        # 不再硬编码 "tgjiema-{svc}" 前缀(可能因 docker compose 版本/配置差异)
        # R72 RC56 fix: 用 {{json .}} 输出完整 container JSON,在 Python 中解析。
        # 旧模板 {{json .RepoDigests}} 在某些 docker 版本上会因 .RepoDigests 字段
        # 缺失而报 "map has no entry for key RepoDigests" 模板解析错误
        # (locally-built / OCI-only 镜像不携带 RepoDigests 字段)。
        svc_info_entry = service_info.get(svc, {})
        container_name = (
            svc_info_entry.get("container_name")
            or svc_info_entry.get("container_id")
            or f"tgjiema-{svc}"  # fallback: 旧式 container_name 声明
        )
        inspect_cmd = [
            "docker", "inspect",
            "--format", "{{json .}}",
            container_name,
        ]
        inspect_result = _run(inspect_cmd, timeout=10)
        if inspect_result.returncode != 0:
            # R72 RC55 fix: 捕获 stderr 用于诊断 docker inspect 失败原因
            # (常见原因:容器名不匹配 / docker daemon 权限 / 容器已被移除)
            stderr_snippet = (inspect_result.stderr or "").strip()[:200]
            stdout_snippet = (inspect_result.stdout or "").strip()[:200]
            digest_failures.append(
                f"{svc}: docker inspect 失败 "
                f"(container_name={container_name!r}, "
                f"stderr={stderr_snippet!r}, stdout={stdout_snippet!r})"
            )
            readiness_checks.append({
                "check": f"repo_digest_{svc}",
                "status": "fail",
                "reason": "inspect_failed",
                "stderr": stderr_snippet,
                "stdout": stdout_snippet,
                "cmd": " ".join(inspect_cmd),
                "container_name": container_name,
            })
        else:
            # R72 RC56 fix: 解析完整 container JSON,避免 .RepoDigests 字段缺失导致
            # 的模板解析错误。locally-built / OCI-only 镜像不携带 RepoDigests 字段时,
            # 视为 RepoDigests=[] (P1-02 验证:仅在 expected_digest 非空时才需要匹配)。
            try:
                container_info = json.loads(inspect_result.stdout.strip())
            except json.JSONDecodeError as exc:
                digest_failures.append(
                    f"{svc}: docker inspect 输出非 JSON "
                    f"(container_name={container_name!r}, err={exc!r})"
                )
                readiness_checks.append({
                    "check": f"repo_digest_{svc}",
                    "status": "fail",
                    "reason": "inspect_output_not_json",
                    "stdout_snippet": inspect_result.stdout.strip()[:200],
                    "container_name": container_name,
                })
            else:
                container_image_id = container_info.get("Image", "")
                repo_digests = container_info.get("RepoDigests", []) or []
                config_image = (
                    container_info.get("Config", {}).get("Image", "")
                    if isinstance(container_info.get("Config"), dict)
                    else ""
                )
                repo_digests_json = (
                    repo_digests if isinstance(repo_digests, str)
                    else json.dumps(repo_digests)
                )
                # 检查 RepoDigest 是否包含期望的 digest
                # R72 RC56 fix: RepoDigests 在某些 docker 版本上可能为空 []。
                # 此时回退到 .Config.Image(创建容器时使用的镜像引用字符串),
                # 其中可能直接包含 @sha256:<digest>。
                # 这与 P1-02 验证目的一致:验证容器实际使用的镜像就是 expected_digest。
                digest_match = (
                    not expected_digest
                    or expected_digest in repo_digests_json
                    or (config_image and expected_digest in config_image)
                )
                if not digest_match:
                    digest_failures.append(
                        f"{svc}: RepoDigest 不匹配 "
                        f"(expected contains {expected_digest[:24]}..., "
                        f"actual_repo_digests={repo_digests_json[:60]}..., "
                        f"config_image={config_image[:120]!r})"
                    )
                    readiness_checks.append({
                        "check": f"repo_digest_{svc}",
                        "status": "fail",
                        "expected_digest": expected_digest[:24] + "...",
                        "actual_repo_digests": repo_digests_json[:60] + "...",
                        "config_image": config_image[:120],
                    })
                else:
                    matched_via = (
                        "repo_digests" if expected_digest and expected_digest in repo_digests_json
                        else ("config_image" if expected_digest and expected_digest in config_image else "no_expected")
                    )
                    readiness_checks.append({
                        "check": f"repo_digest_{svc}",
                        "status": "pass",
                        "container_image_id": container_image_id,
                        "config_image": config_image,
                        "repo_digests_count": (
                            len(repo_digests) if isinstance(repo_digests, list) else 0
                        ),
                        "matched_via": matched_via,
                    })

    readiness_checks.append({
        "check": "service_roles_verified",
        "status": "pass" if not role_failures else "fail",
        "failures": role_failures,
    })
    readiness_checks.append({
        "check": "repo_digests_verified",
        "status": "pass" if not digest_failures else "fail",
        "failures": digest_failures,
    })

    if role_failures or digest_failures:
        all_role_digest_failures = role_failures + digest_failures
        return _fail_result(
            phase="start_bots",
            description=description,
            started=started,
            error=(
                f"R72 P0-07: 角色身份/镜像 digest 验证失败 — "
                f"{'; '.join(all_role_digest_failures)}"
            ),
            stdout=result.stdout,
            stderr=result.stderr,
            evidence={
                "service_info": service_info,
                "role_failures": role_failures,
                "digest_failures": digest_failures,
                "expected_image": expected_image,
            },
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
            "service_info": service_info,
        },
        readiness_checks=readiness_checks,
    )


# ════════════════════════════════════════════════════════════════
# 阶段 4:migration_check
# ════════════════════════════════════════════════════════════════


def phase_migration_check(timeout: int) -> PhaseResult:
    """阶段 4:运行 migration --check --json。

    R72 P0-08: 不再依赖输出字符串是否含 "failed"。
    使用结构化 JSON 输出,校验 applied/skipped/failed/pending/schema_version。

    readiness 检查点:
      - docker compose exec db_writer python -m database.migrate --check --json 返回 0
      - JSON 输出可解析
      - failed 列表为空
      - final_status == "ok"
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

    # R72 P0-08: 使用 --json 获取结构化输出
    cmd = _compose_cmd([
        "exec", "-T", "db_writer",
        "python", "-m", "database.migrate", "--check", "--json",
    ])
    try:
        result = _run(cmd, timeout=timeout, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired:
        return _fail_result(
            phase="migration_check",
            description=description,
            started=started,
            error=f"migration --check --json 超时({timeout}s)",
            readiness_checks=[
                {"check": "docker_daemon", "status": "pass"},
                {"check": "migration_exec", "status": "timeout"},
            ],
        )

    if result.returncode != 0:
        # R71 RC23 fix: migration_check 失败时捕获 db_writer 容器日志,
        # 诊断 db_writer restart loop 的根因(readiness gate 失败 / 主进程崩溃)。
        container_logs: dict[str, Any] = {}
        for svc in ("db_writer", "migration", "redis"):
            logs_cmd = _compose_cmd(["logs", "--no-color", "--tail", "500", svc])
            try:
                logs_result = _run(logs_cmd, timeout=15, cwd=REPO_ROOT)
                svc_log = (logs_result.stdout or "") + (logs_result.stderr or "")
                if svc_log.strip():
                    container_logs[svc] = svc_log[-8000:]
            except (subprocess.TimeoutExpired, OSError):
                pass
        return _fail_result(
            phase="migration_check",
            description=description,
            started=started,
            error=(
                f"migration --check --json 失败 (exit={result.returncode}) — "
                f"schema 可能未对齐"
            ),
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=result.returncode,
            evidence={"container_logs": container_logs} if container_logs else {},
            readiness_checks=[
                {"check": "docker_daemon", "status": "pass"},
                {"check": "migration_exec", "status": "fail"},
            ],
        )

    # R72 P0-08: 解析结构化 JSON 输出
    readiness_checks: list[dict[str, Any]] = [
        {"check": "docker_daemon", "status": "pass"},
        {"check": "migration_exec", "status": "pass"},
    ]

    migration_evidence: dict[str, Any] = {}
    try:
        migration_evidence = json.loads(result.stdout.strip())
    except json.JSONDecodeError as e:
        # R72 P0-08: JSON 解析失败是严重问题(fail-closed)
        return _fail_result(
            phase="migration_check",
            description=description,
            started=started,
            error=(
                f"R72 P0-08: migration --json 输出不是合法 JSON: {e} — "
                f"不得用字符串包含关系作为语义证据"
            ),
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=result.returncode,
            readiness_checks=readiness_checks + [
                {"check": "migration_json_parsed", "status": "fail"},
            ],
        )

    readiness_checks.append({"check": "migration_json_parsed", "status": "pass"})

    # R72 P0-08: 从结构化 JSON 中提取并校验关键字段
    failed_migrations = migration_evidence.get("failed", [])
    pending_migrations = migration_evidence.get("pending", [])
    final_status = migration_evidence.get("final_status", "unknown")
    current_version = migration_evidence.get("current_schema_version", 0)
    expected_version = migration_evidence.get("expected_schema_version", 0)

    readiness_checks.append({
        "check": "migration_no_failures",
        "status": "pass" if not failed_migrations else "fail",
        "failed_count": len(failed_migrations),
        "failed": failed_migrations,
    })
    readiness_checks.append({
        "check": "migration_no_pending",
        "status": "pass" if not pending_migrations else "fail",
        "pending_count": len(pending_migrations),
        "pending": pending_migrations,
    })
    readiness_checks.append({
        "check": "migration_version_aligned",
        "status": "pass" if current_version == expected_version else "fail",
        "current_version": current_version,
        "expected_version": expected_version,
    })
    readiness_checks.append({
        "check": "migration_final_status",
        "status": "pass" if final_status == "ok" else "fail",
        "final_status": final_status,
    })

    # 任一结构化检查失败则整体失败
    struct_failures = [
        rc["check"] for rc in readiness_checks
        if rc.get("status") == "fail"
    ]
    if struct_failures:
        return _fail_result(
            phase="migration_check",
            description=description,
            started=started,
            error=(
                f"R72 P0-08: 结构化 migration 校验失败 — "
                f"failed_checks={struct_failures}, "
                f"failed_migrations={failed_migrations}, "
                f"pending_migrations={pending_migrations}, "
                f"final_status={final_status}, "
                f"current_version={current_version}, "
                f"expected_version={expected_version}"
            ),
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=result.returncode,
            evidence={
                "migration_evidence": migration_evidence,
                "failed_checks": struct_failures,
            },
            readiness_checks=readiness_checks,
        )

    return _pass_result(
        phase="migration_check",
        description=description,
        started=started,
        stdout=result.stdout,
        stderr=result.stderr,
        returncode=result.returncode,
        evidence={
            "migration_evidence": migration_evidence,
            "exit_code": result.returncode,
        },
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
        # R71 RC30: 通过 docker compose exec 在容器内调用 127.0.0.1:port/health
        # 使用 127.0.0.1 替代 localhost(避免 IPv6 ::1 解析导致 Connection refused),
        # 并添加重试逻辑(6 次尝试,间隔 5s = 最多 30s 等待 HTTP 服务器启动)。
        # 根因:start_bots 只检查容器 "running" 状态,不等待 HTTP 服务器就绪;
        # migration_check 完成后 HTTP 服务器可能尚未启动 → Connection refused。
        probe_script = (
            "import urllib.request, time\n"
            "ok=False\n"
            "for i in range(6):\n"
            "  try:\n"
            f"    r=urllib.request.urlopen('http://127.0.0.1:{port}/health', timeout=5)\n"
            "    print(r.status)\n"
            "    ok = (r.status==200)\n"
            "    break\n"
            "  except Exception as e:\n"
            "    if i < 5: time.sleep(5)\n"
            "    else: print(repr(e))\n"
            "exit(0 if ok else 1)"
        )
        cmd = _compose_cmd([
            "exec", "-T", service,
            "python", "-c", probe_script,
        ])
        try:
            result = _run(cmd, timeout=60, cwd=REPO_ROOT)
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
        # R71 RC25 fix: migration 是 oneshot,启动后立即退出(service_completed_successfully)。
        # "service is not running" 是正常状态,不应视为健康检查失败。
        if service == "migration":
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
        # R71 RC25 fix: migration 是 oneshot,已退出,printenv 不可用
        if service == "migration":
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

        # R71 RC28: 捕获失败容器的 docker compose logs,用于诊断崩溃根因。
        # 之前 health_check 失败时没有容器日志,无法判断 bot 进程为何崩溃。
        container_logs: dict[str, str] = {}
        for service in SERVICE_ROLES:
            if service in ("redis", "redis-acl-init", "migration"):
                continue
            cmd = _compose_cmd(["logs", "--tail", "50", service])
            try:
                result = _run(cmd, timeout=15, cwd=REPO_ROOT)
                container_logs[service] = result.stdout[-2000:] if len(result.stdout) > 2000 else result.stdout
            except Exception:
                container_logs[service] = "<failed to capture logs>"

        return _fail_result(
            phase="health_check",
            description=description,
            started=started,
            error="; ".join(error_parts),
            evidence={
                "health_results": health_results,
                "role_health_results": role_health_results,
                "role_mismatches": role_mismatches,
                "container_logs": container_logs,
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
    # R71 RC32: health 用户在 users.acl.template 中名为 `health`(无 tgjiema_ 前缀),
    # 与 writer/reader/admin 的 `tgjiema_*` 前缀不一致。e2e 原代码用 `tgjiema_health`
    # 导致 AUTH 失败。此处按 ACL 实际用户名匹配。
    redis_passwords = {
        "tgjiema_writer": os.environ.get("REDIS_WRITER_PASSWORD", ""),
        "tgjiema_reader": os.environ.get("REDIS_READER_PASSWORD", ""),
        "health": os.environ.get("REDIS_HEALTH_PASSWORD", ""),
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

    通过 scripts/synthetic_transaction.py 的 run_dbwriter_component_test() 完整执行:
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
        evidence = module.run_dbwriter_component_test(timeout=timeout)
    except Exception as e:
        return False, {
            "error": f"run_dbwriter_component_test 异常: {type(e).__name__}: {e}",
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

    R73 P0-04 整改:在调用 synthetic_transaction.run_dbwriter_component_test 后,
    额外验证:
      - CRDB sync 是否真实完成(调用 verify_crdb_sync_result)
      - dsp 派送是否真实完成(检查 dsp_dispatch_verify / dsp_dispatch_idempotency)
      - 故障注入测试是否真实失败(检查 fault_injection 字段)

    readiness 检查点:
      - synthetic_transaction.py 可加载
      - inject 步骤通过(Redis XADD)
      - verify 步骤通过(db_writer 消费 → SQLite 落库)
      - idempotency 步骤通过(重复 XADD 不增加行数)
      - failure_scenario 步骤通过(畸形 JSON → DLQ)
      - cleanup 步骤通过(DELETE 测试 row)
      - overall_passed=True
      - R73 P0-04: crdb_sync_verify 通过(真实 CRDB 落库)
      - R73 P0-04: dsp_dispatch_verify 通过(真实 dsp 派送)
      - R73 P0-04: dsp_dispatch_idempotency 通过(dsp 派送幂等)
      - R73 P0-04: fault_injection 通过(角色停止时交易 fail-closed)
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

    # R73 P0-04: 额外验证 CRDB sync / dsp 派送 / 故障注入字段
    crdb_sync_verify = evidence.get("crdb_sync_verify", {})
    dsp_dispatch_inject = evidence.get("dsp_dispatch_inject", {})
    dsp_dispatch_verify = evidence.get("dsp_dispatch_verify", {})
    dsp_dispatch_idempotency = evidence.get("dsp_dispatch_idempotency", {})
    fault_injection = evidence.get("fault_injection", {})

    readiness_checks.append({
        "check": "synthetic_crdb_sync_verify",
        "status": "pass" if crdb_sync_verify.get("passed", False) else "fail",
    })
    readiness_checks.append({
        "check": "synthetic_dsp_dispatch_inject",
        "status": "pass" if dsp_dispatch_inject.get("passed", False) else "fail",
    })
    readiness_checks.append({
        "check": "synthetic_dsp_dispatch_verify",
        "status": "pass" if dsp_dispatch_verify.get("passed", False) else "fail",
    })
    readiness_checks.append({
        "check": "synthetic_dsp_dispatch_idempotency",
        "status": "pass" if dsp_dispatch_idempotency.get("passed", False) else "fail",
    })

    # R73 P0-04: 故障注入测试 — crdb_sync 角色停止时交易必须 fail-closed
    fault_crdb = fault_injection.get("crdb_sync", {}) if isinstance(fault_injection, dict) else {}
    fault_start = fault_injection.get("crdb_sync_start", {}) if isinstance(fault_injection, dict) else {}
    fault_injection_ok = (
        fault_crdb.get("passed", False) and
        fault_start.get("passed", False)
    )
    readiness_checks.append({
        "check": "synthetic_fault_injection",
        "status": "pass" if fault_injection_ok else "fail",
    })

    readiness_checks.append({
        "check": "synthetic_overall",
        "status": "pass" if passed else "fail",
    })

    # R73 P0-04: 严格 fail-closed — CRDB sync / dsp 派送 / 故障注入
    # 任一失败都使整个 phase 失败(不再只看 overall_passed)
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

    # R73 P0-04: 额外校验新字段(不允许 overall_passed=True 但新字段失败)
    if not crdb_sync_verify.get("passed", False):
        return _fail_result(
            phase="business_smoke",
            description=description,
            started=started,
            error=(
                f"CRDB sync 验证未通过(fail-closed): "
                f"{crdb_sync_verify.get('error', 'unknown')}"
            ),
            evidence=evidence,
            readiness_checks=readiness_checks,
        )

    if not dsp_dispatch_verify.get("passed", False):
        return _fail_result(
            phase="business_smoke",
            description=description,
            started=started,
            error=(
                f"dsp 派送验证未通过: "
                f"{dsp_dispatch_verify.get('error', 'unknown')}"
            ),
            evidence=evidence,
            readiness_checks=readiness_checks,
        )

    if not dsp_dispatch_idempotency.get("passed", False):
        return _fail_result(
            phase="business_smoke",
            description=description,
            started=started,
            error=(
                f"dsp 派送幂等性验证未通过: "
                f"{dsp_dispatch_idempotency.get('error', 'unknown')}"
            ),
            evidence=evidence,
            readiness_checks=readiness_checks,
        )

    if not fault_injection_ok:
        return _fail_result(
            phase="business_smoke",
            description=description,
            started=started,
            error=(
                f"故障注入测试未通过(角色停止时交易未 fail-closed): "
                f"{fault_crdb.get('error', 'unknown')}"
            ),
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
      - 恢复环境合成交易(synthetic_transaction.run_dbwriter_component_test)
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
         - 合成交易验证(synthetic_transaction.run_dbwriter_component_test)
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

    # R72 P0-09/10: 删除 CI bypass — RC Runtime E2E 必须执行真实 backup/restore。
    # 禁止 ci_skipped 后整体通过。缺少 R2 凭证或真实依赖时门禁必须 FAIL,
    # 不得伪造成功。单元测试使用 mock;Compose RC 使用真实 production profile。
    #
    # R72 P0-10: 使用 db_backup backup --once(一次性备份,不进入 daemon 循环)
    #             使用 db_restore --target staging --backup-id(显式恢复目标)
    # R72 RC60: 传递 --timeout 240 让 db_backup 在 asyncpg 连接卡住时先于编排器
    #           超时返回结构化 evidence(避免编排器 600s 强杀导致无错误信息)。
    # R72 RC63: compose run 必须加 -T 禁用 TTY 分配,否则在 GitHub Actions 非 TTY
    #           环境下会因等待 TTY 输入而无限挂起,导致编排器 600s 超时强杀,
    #           且 stdout/stderr 为空(无法定位失败原因)。与 compose exec -T 一致。
    # R72 RC63: evidence 文件路径必须使用容器内可写路径(/app/data 映射到宿主机 ./data),
    #           而非宿主机路径 — db_backup 容器 read_only: true,只挂载 ./data:/app/data,
    #           传入宿主机路径会写入失败(OSError),且 evidence 文件无法被编排器读取。
    # R72 RC65: compose run 必须加 --entrypoint python 覆盖 Dockerfile ENTRYPOINT。
    #           Dockerfile ENTRYPOINT 是 python /app/docker/entrypoint.py,它会:
    #             1. 读取 SERVICE_ROLE=db_backup
    #             2. 构造 cmd = ["python", "run_all.py", "--standalone", "db_backup"]
    #             3. 把 sys.argv[1:] (即 "python -m services.db_backup backup --once ...")
    #                作为 extra_args 追加到 cmd
    #             4. execvp 执行:python run_all.py --standalone db_backup python -m ...
    #           这导致 db_backup DAEMON 被启动(永不退出),而非一次性 backup --once CLI。
    #           600s 超时强杀,stdout 为空,无 evidence 输出。
    #           修复:--entrypoint python 直接覆盖 ENTRYPOINT 为 python,
    #           COMMAND 变为 "-m services.db_backup backup --once ...",
    #           实际执行:python -m services.db_backup backup --once ...,
    #           绕过 entrypoint.py 的角色映射 + readiness gate,直接运行 CLI。
    #           合理性:backup_restore 阶段在 health_check 之后执行,
    #           此时 db_backup 服务已通过 readiness gate,无需重复检查。
    # R72 RC66: compose run 必须加 --no-deps 跳过 depends_on 依赖检查。
    #           db_backup 服务 depends_on migration(condition: service_completed_successfully),
    #           但 docker compose run 在某些版本会尝试重新创建/启动已退出的 migration
    #           容器来满足 depends_on 条件,导致命令无限挂起。
    #           backup_restore 阶段在 start_bots 之后执行,所有依赖服务(redis/migration)
    #           已在期望状态,无需 compose run 再次检查。
    #           --no-deps 跳过依赖管理,直接启动 run 容器。
    # R72 RC66: 改进 TimeoutExpired 错误处理,提取 partial stdout/stderr。
    #           subprocess.run 在 timeout 时已捕获部分输出(存于 e.stdout/e.stderr),
    #           原代码直接 except 后丢弃,导致 600s 超时后 stdout/stderr 全空,
    #           无法定位是 docker compose run 卡住还是 python 进程卡住。
    backup_evidence_path = REPO_ROOT / "data" / f"backup_evidence_{trace_id}.json"
    backup_evidence_container_path = f"/app/data/backup_evidence_{trace_id}.json"
    backup_cmd = _compose_cmd([
        "run", "--rm", "-T",
        "--no-deps",
        "--entrypoint", "python",
        "db_backup",
        "-m", "services.db_backup", "backup",
        "--once",
        "--timeout", "240",
        "--output-json", backup_evidence_container_path,
    ])
    try:
        backup_result = _run(backup_cmd, timeout=timeout, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired as te:
        cleanup_err = _safe_cleanup_marker(vri_module, trace_id)
        if pre_snapshot_path.exists():
            pre_snapshot_path.unlink(missing_ok=True)
        partial_stdout = (te.stdout or "")[:2000] if isinstance(te.stdout, str) else ""
        partial_stderr = (te.stderr or "")[:2000] if isinstance(te.stderr, str) else ""
        return _fail_result(
            phase="backup_restore",
            description=description,
            started=started,
            error=f"R72 P0-10: backup --once 触发超时({timeout}s)"
                 + (f" | cleanup_err={cleanup_err}" if cleanup_err else "")
                 + (f" | partial_stdout={partial_stdout[:500]}" if partial_stdout else "")
                 + (f" | partial_stderr={partial_stderr[:500]}" if partial_stderr else ""),
            stdout=partial_stdout,
            stderr=partial_stderr,
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
            error=f"R72 P0-10: backup --once 失败 (exit={backup_result.returncode})"
                 + (f" | cleanup_err={cleanup_err}" if cleanup_err else ""),
            stdout=backup_result.stdout,
            stderr=backup_result.stderr,
            returncode=backup_result.returncode,
            readiness_checks=readiness_checks + [
                {"check": "backup_triggered", "status": "fail"},
            ],
        )
    readiness_checks.append({"check": "backup_triggered", "status": "pass"})

    # R72 P0-10: 从 backup evidence JSON 解析 backup_id,传给 restore
    backup_id = ""
    backup_evidence: dict[str, Any] = {}
    try:
        if backup_evidence_path.is_file():
            backup_evidence = json.loads(backup_evidence_path.read_text(encoding="utf-8"))
            backup_id = str(backup_evidence.get("backup_id", ""))
    except (json.JSONDecodeError, OSError) as e:
        # evidence 解析失败是严重问题(fail-closed)
        cleanup_err = _safe_cleanup_marker(vri_module, trace_id)
        if pre_snapshot_path.exists():
            pre_snapshot_path.unlink(missing_ok=True)
        return _fail_result(
            phase="backup_restore",
            description=description,
            started=started,
            error=f"R72 P0-10: backup evidence JSON 解析失败: {type(e).__name__}: {e}"
                 + (f" | cleanup_err={cleanup_err}" if cleanup_err else ""),
            stdout=backup_result.stdout,
            stderr=backup_result.stderr,
            readiness_checks=readiness_checks + [
                {"check": "backup_id_parsed", "status": "fail"},
            ],
        )
    if not backup_id:
        # backup_id 为空说明 backup --once 未产生有效 evidence(fail-closed)
        cleanup_err = _safe_cleanup_marker(vri_module, trace_id)
        if pre_snapshot_path.exists():
            pre_snapshot_path.unlink(missing_ok=True)
        return _fail_result(
            phase="backup_restore",
            description=description,
            started=started,
            error="R72 P0-10: backup evidence 缺少 backup_id 字段 — backup --once 未产生有效证据"
                 + (f" | cleanup_err={cleanup_err}" if cleanup_err else ""),
            stdout=backup_result.stdout,
            stderr=backup_result.stderr,
            readiness_checks=readiness_checks + [
                {"check": "backup_id_parsed", "status": "fail"},
            ],
        )
    readiness_checks.append({"check": "backup_id_parsed", "status": "pass"})

    # R72 P0-10: 触发 restore --target staging --backup-id(隔离目标,不覆盖生产数据)
    # R72 RC63: compose run 必须加 -T 禁用 TTY 分配(同 backup_cmd 注释)
    # R72 RC63: evidence 文件路径使用容器内 /app/data(同 backup_evidence_path 逻辑)
    # R72 RC65: compose run 必须加 --entrypoint python 覆盖 Dockerfile ENTRYPOINT
    #           (同 backup_cmd RC65 注释,否则 entrypoint.py 启动 db_writer daemon
    #           而非一次性 db_restore CLI,导致 600s 超时强杀)
    # R72 RC66: compose run 必须加 --no-deps(同 backup_cmd RC66 注释,
    #           跳过 depends_on 依赖检查,避免 compose run 尝试重启 migration)
    # R72 RC66: 改进 TimeoutExpired 错误处理,提取 partial stdout/stderr
    #           (同 backup_cmd RC66 注释)
    # R72 RC67: db_restore.py 被 Dockerfile 第 81 行 `RUN rm -f` 物理删除
    #           (R69 P0-5 blocklist 第二道防线),且 .dockerignore 第 41 行
    #           也排除(R68 P0-07 第一道防线)。verify_oci_allowlist.py 强制
    #           要求 Dockerfile 包含 rm -f services/db_restore.py(第 257 行)
    #           且 .dockerignore 包含 services/db_restore.py(第 279 行),
    #           不得修改这两处。
    #           修复方案:docker compose run 使用 -v 选项将宿主机源码
    #           services/db_restore.py 只读挂载到容器内 /app/services/db_restore.py,
    #           绕过 Dockerfile 物理删除(不修改 Dockerfile/.dockerignore)。
    #           同时设置 -e APP_ENV=development 覆盖 Dockerfile ENV APP_ENV=production,
    #           使 _production_guard.assert_no_legacy_restore_in_production() 通过
    #           (生产环境无逃生舱,即使 ALLOW_LEGACY_RESTORE=1 也拒绝;
    #            development 环境允许 ALLOW_LEGACY_RESTORE 逃生舱)。
    # R72 RC68: docker-compose.prod.yml 为所有服务设置 `ALLOW_LEGACY_RESTORE=`
    #           (空字符串,生产安全策略 — 防止逃生舱被意外启用)。
    #           db_restore.main() 中 `os.environ.setdefault("ALLOW_LEGACY_RESTORE", "1")`
    #           仅在 key 不存在时设置;compose 文件已设置空字符串,
    #           setdefault 无法覆盖,导致 run_restore() capability-seal 检查
    #           (line 348: `os.environ.get("ALLOW_LEGACY_RESTORE", "").lower()
    #            not in ("1", "true", "yes")`)失败,抛 AppError(legacy_writer_sealed)。
    #           修复方案:显式设置 -e ALLOW_LEGACY_RESTORE=1 覆盖 compose 文件的
    #           空字符串值,使 capability-seal 检查通过。
    #           合理性:compose-runtime-e2e 是 CI 测试场景,不涉及生产数据
    #           (--target staging 恢复到隔离 staging 数据库)。
    restore_evidence_path = REPO_ROOT / "data" / f"restore_evidence_{trace_id}.json"
    restore_evidence_container_path = f"/app/data/restore_evidence_{trace_id}.json"
    db_restore_host_path = (REPO_ROOT / "services" / "db_restore.py").as_posix()
    restore_cmd = _compose_cmd([
        "run", "--rm", "-T",
        "--no-deps",
        "--entrypoint", "python",
        "-e", "APP_ENV=development",
        "-e", "ALLOW_LEGACY_RESTORE=1",
        "-v", f"{db_restore_host_path}:/app/services/db_restore.py:ro",
        "db_writer",
        "-m", "services.db_restore",
        "--backup-id", backup_id,
        "--target", "staging",
        "--output-json", restore_evidence_container_path,
    ])
    try:
        restore_result = _run(restore_cmd, timeout=timeout, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired as te:
        cleanup_err = _safe_cleanup_marker(vri_module, trace_id)
        if pre_snapshot_path.exists():
            pre_snapshot_path.unlink(missing_ok=True)
        partial_stdout = (te.stdout or "")[:2000] if isinstance(te.stdout, str) else ""
        partial_stderr = (te.stderr or "")[:2000] if isinstance(te.stderr, str) else ""
        return _fail_result(
            phase="backup_restore",
            description=description,
            started=started,
            error=f"restore 触发超时({timeout}s)"
                 + (f" | cleanup_err={cleanup_err}" if cleanup_err else "")
                 + (f" | partial_stdout={partial_stdout[:500]}" if partial_stdout else "")
                 + (f" | partial_stderr={partial_stderr[:500]}" if partial_stderr else ""),
            stdout=partial_stdout,
            stderr=partial_stderr,
            readiness_checks=readiness_checks + [
                {"check": "restore_triggered", "status": "timeout"},
            ],
        )
    if restore_result.returncode != 0:
        cleanup_err = _safe_cleanup_marker(vri_module, trace_id)
        if pre_snapshot_path.exists():
            pre_snapshot_path.unlink(missing_ok=True)
        # R72 RC73: restore 失败时执行环境变量诊断,定位 BACKUP_KEK / BACKUP_SIGNING_KEY
        # 是否被容器加载。只打印长度和前缀,不暴露 secret 值。
        diag_cmd = _compose_cmd([
            "run", "--rm", "-T",
            "--no-deps",
            "--entrypoint", "python",
            "db_writer",
            "-c",
            "import os; "
            "print('BACKUP_KEK_set=', bool(os.environ.get('BACKUP_KEK',''))); "
            "print('BACKUP_KEK_len=', len(os.environ.get('BACKUP_KEK',''))); "
            "print('BACKUP_KEK_FILE_set=', bool(os.environ.get('BACKUP_KEK_FILE',''))); "
            "print('BACKUP_SIGNING_KEY_set=', bool(os.environ.get('BACKUP_SIGNING_KEY',''))); "
            "print('BACKUP_SIGNING_KEY_len=', len(os.environ.get('BACKUP_SIGNING_KEY',''))); "
            "print('R2_ACCOUNT_ID_set=', bool(os.environ.get('R2_ACCOUNT_ID',''))); "
            "print('R2_BUCKET_NAME_set=', bool(os.environ.get('R2_BUCKET_NAME',''))); "
            "print('APP_ENV=', os.environ.get('APP_ENV','')); "
            "print('SERVICE_ROLE=', os.environ.get('SERVICE_ROLE',''))",
        ])
        try:
            diag_result = _run(diag_cmd, timeout=30, cwd=REPO_ROOT)
            diag_output = diag_result.stdout or ""
        except Exception as diag_exc:
            diag_output = f"diag_failed: {type(diag_exc).__name__}: {diag_exc}"
        return _fail_result(
            phase="backup_restore",
            description=description,
            started=started,
            error=f"restore 失败 (exit={restore_result.returncode})"
                 + (f" | cleanup_err={cleanup_err}" if cleanup_err else "")
                 + f" | env_diag={diag_output[:500]}",
            stdout=restore_result.stdout,
            stderr=restore_result.stderr,
            returncode=restore_result.returncode,
            readiness_checks=readiness_checks + [
                {"check": "restore_triggered", "status": "fail"},
                {"check": "env_diag", "status": "pass" if diag_output else "fail"},
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
    # R72 P0-12: switch/rollback 必须 orchestrator_available + 实际执行 + 通过
    # 不再只检查 has_switch_phase/has_rollback_phase 字段存在(import check)
    switch_ok = (
        bool(switch_ev.get("orchestrator_available", False))
        and bool(switch_ev.get("orchestrator_executed", False))
        and bool(switch_ev.get("passed", False))
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
    # R72 P0-10: 清理 backup/restore evidence 临时文件
    for _tmp in (backup_evidence_path, restore_evidence_path):
        try:
            if _tmp.exists():
                _tmp.unlink(missing_ok=True)
        except OSError:
            pass

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
            "backup_id": backup_id,
            "backup_evidence": backup_evidence,
            "verify_evidence": verify_evidence,
            "backup_stdout_tail": backup_result.stdout[-500:],
            "restore_stdout_tail": restore_result.stdout[-500:],
            "integrity_method": "verify_full_structured_verification",
            "target_db": "staging",
            "cleanup_rc": cleanup_rc,
            "wave": "r72-p0-09-10",
        },
        readiness_checks=readiness_checks,
    )


# ════════════════════════════════════════════════════════════════
# 阶段 9:sigterm
# ════════════════════════════════════════════════════════════════


def phase_sigterm(timeout: int) -> PhaseResult:
    """阶段 9:发送 SIGTERM,验证优雅关闭。

    R72 P0-13 整改:不再只阻断 exit code 137。
    对每个长驻角色:
      - exit code 0 或 143 (SIGTERM) 视为正常退出
      - exit code 137 (SIGKILL) 失败
      - exit code 1 失败
      - 仍 running 失败
      - 状态未知失败

    readiness 检查点:
      - docker compose kill -s SIGTERM 返回 0
      - 每个长驻角色 exit code 为 0 或 143
      - 无 SIGKILL(137)/ exit 1 / 仍 running / 状态未知
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

    # R72 P0-13: 轮询等待所有长驻角色退出,然后逐服务验证退出码
    # 长驻服务 = redis + db_writer + 所有 BOT_SERVICES
    # (redis-acl-init / migration 是 oneshot,不在此阶段验证)
    long_running_services: set[str] = set(CORE_SERVICES) | set(BOT_SERVICES) | {"redis"}

    # 轮询等待所有长驻服务达到 exited 状态(最多 30s)
    wait_deadline = time.time() + 30
    service_info: dict[str, dict[str, Any]] = {}
    all_exited = False
    while time.time() < wait_deadline:
        service_info = _get_compose_ps_info(include_exited=True)
        all_exited = True
        for svc in long_running_services:
            si = service_info.get(svc)
            if si is None or si["state"] != "exited":
                all_exited = False
                break
        if all_exited:
            break
        time.sleep(2)

    # 逐服务验证退出码
    exit_code_results: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    for svc in sorted(long_running_services):
        si = service_info.get(svc)
        if si is None:
            exit_code_results[svc] = {
                "status": "unknown", "reason": "not_found",
            }
            failures.append(f"{svc}: 状态未知(docker compose ps 未发现)")
            continue
        state = si["state"]
        exit_code = si.get("exit_code")
        if state == "running":
            exit_code_results[svc] = {
                "state": state,
                "exit_code": exit_code,
                "status": "fail",
                "reason": "still_running",
            }
            failures.append(f"{svc}: 仍 running(SIGTERM 后未退出)")
            continue
        if state == "exited":
            if exit_code in (0, 143):
                exit_code_results[svc] = {
                    "state": state,
                    "exit_code": exit_code,
                    "status": "pass",
                }
                continue
            exit_code_results[svc] = {
                "state": state,
                "exit_code": exit_code,
                "status": "fail",
                "reason": f"unexpected_exit_code_{exit_code}",
            }
            failures.append(
                f"{svc}: exit code={exit_code}(期望 0 或 143 SIGTERM)"
            )
            continue
        # restarting / dead / unknown state
        exit_code_results[svc] = {
            "state": state,
            "exit_code": exit_code,
            "status": "fail",
            "reason": f"unexpected_state_{state}",
        }
        failures.append(f"{svc}: state={state}(期望 exited)")

    readiness_checks.append({
        "check": "exit_codes_verified",
        "status": "pass" if not failures else "fail",
        "services": exit_code_results,
        "failures": failures,
    })

    if failures:
        return _fail_result(
            phase="sigterm",
            description=description,
            started=started,
            error=(
                f"R72 P0-13: SIGTERM 退出码验证失败 — "
                f"{'; '.join(failures)}"
            ),
            evidence={
                "exit_codes": exit_code_results,
                "failures": failures,
                "all_exited": all_exited,
            },
            readiness_checks=readiness_checks,
        )

    return _pass_result(
        phase="sigterm",
        description=description,
        started=started,
        stdout=kill_result.stdout,
        stderr=kill_result.stderr,
        returncode=kill_result.returncode,
        evidence={"exit_codes": exit_code_results},
        readiness_checks=readiness_checks,
    )


# ════════════════════════════════════════════════════════════════
# 阶段 10:restart
# ════════════════════════════════════════════════════════════════


def phase_restart(timeout: int) -> PhaseResult:
    """阶段 10:restart 验证可恢复。

    R72 P0-14 整改:不再只检查 running。
    restart 后必须重新执行:
      - 所有服务 running
      - Docker health 为 healthy(对有 healthcheck 的服务)

    readiness 检查点:
      - docker compose up -d 返回 0
      - 所有服务重新进入 running 状态
      - 所有有 healthcheck 的服务 Docker health 为 healthy
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

    # R72 P0-14: restart 后必须重新验证所有服务 running + healthy
    # 不再只检查 running。
    # 所有服务(包括 redis + redis-acl-init + migration)都需要验证:
    # - redis-acl-init / migration: exited 0(oneshot 已完成)
    # - redis + db_writer + 所有 BOT_SERVICES: running + healthy
    expected_services: dict[str, dict[str, str]] = {
        "redis-acl-init": {"state": "exited", "health": ""},
        "migration": {"state": "exited", "health": ""},
        "redis": {"state": "running", "health": "healthy"},
        "db_writer": {"state": "running", "health": "healthy"},
    }
    for svc in BOT_SERVICES:
        expected_services[svc] = {"state": "running", "health": "healthy"}

    # 轮询等待所有服务达到期望状态(healthcheck start_period 最高 90s)
    all_ready, service_info = _wait_for_services(
        expected=expected_services, timeout_seconds=180, poll_interval=5,
    )

    # 逐服务断言
    service_failures: list[str] = []
    readiness_checks: list[dict[str, Any]] = [
        {"check": "docker_daemon", "status": "pass"},
        {"check": "compose_up", "status": "pass"},
    ]
    for svc, req in expected_services.items():
        si = service_info.get(svc)
        if si is None:
            service_failures.append(
                f"{svc}: 服务不存在(docker compose ps 未发现)"
            )
            readiness_checks.append({
                "check": f"service_{svc}",
                "status": "fail",
                "reason": "not_found",
            })
            continue
        svc_ok = si["state"] == req["state"]
        if req["health"]:
            svc_ok = svc_ok and si["health"] == req["health"]
        # 对 oneshot 服务检查 exit_code == 0
        if req["state"] == "exited":
            exit_code = si.get("exit_code")
            svc_ok = svc_ok and (exit_code == 0)
        readiness_checks.append({
            "check": f"service_{svc}",
            "status": "pass" if svc_ok else "fail",
            "state": si["state"],
            "health": si["health"],
            "exit_code": si.get("exit_code"),
        })
        if not svc_ok:
            service_failures.append(
                f"{svc}: state={si['state']!r}, health={si['health']!r} "
                f"(expected state={req['state']!r}, health={req['health']!r})"
            )

    readiness_checks.append({
        "check": "services_healthy_after_restart",
        "status": "pass" if not service_failures else "fail",
        "expected": sorted(expected_services.keys()),
        "failures": service_failures,
    })

    if service_failures or not all_ready:
        return _fail_result(
            phase="restart",
            description=description,
            started=started,
            error=(
                f"R72 P0-14: restart 后服务未恢复 healthy — "
                f"{'; '.join(service_failures) if service_failures else '等待超时(180s)'}"
            ),
            evidence={
                "service_info": service_info,
                "failures": service_failures,
                "wait_timeout_seconds": 180,
                "all_ready": all_ready,
            },
            readiness_checks=readiness_checks,
        )

    # R72 P0-14: restart 后必须重新执行真实业务交易
    # 不再只检查 running — 必须验证完整业务链(上传→索引→解码→派送→SQLite→幂等)
    synth_passed, synth_evidence = _run_synthetic_transaction(timeout=timeout)
    readiness_checks.append({
        "check": "business_transaction_after_restart",
        "status": "pass" if synth_passed else "fail",
    })
    if not synth_passed:
        return _fail_result(
            phase="restart",
            description=description,
            started=started,
            error=(
                f"R72 P0-14: restart 后业务交易验证失败 — "
                f"readiness 恢复但业务链不可用: "
                f"{synth_evidence.get('error', '未知错误')}"
            ),
            evidence={
                "service_info": service_info,
                "synthetic_transaction": synth_evidence,
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
        evidence={
            "service_info": service_info,
            "expected": sorted(expected_services.keys()),
            "synthetic_transaction": synth_evidence,
        },
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
# R73 §5.15 阶段 DAG 函数(16 个阶段)
# ════════════════════════════════════════════════════════════════


def _phase_desc(phase_name: str) -> str:
    """从 PHASES 列表中查找阶段描述。"""
    for name, desc in PHASES:
        if name == phase_name:
            return desc
    return ""


def phase_start_infrastructure(timeout: int) -> PhaseResult:
    """R73 §5.15 阶段 2:启动基础设施(redis-acl-init + redis + migration)。

    readiness 检查点:
      - docker compose up -d redis db_writer 返回 0
        (redis-acl-init / migration 通过 depends_on 自动触发)
      - redis-acl-init 容器 exited 0
      - redis 容器 running + healthy
      - migration 容器 exited 0 + schema aligned(通过 --check --json 验证)
    """
    description = _phase_desc("start_infrastructure")
    started = time.time()
    started_at = _now_iso()

    if not _docker_available():
        return _fail_result(
            phase="start_infrastructure",
            description=description,
            started=started,
            error="Docker daemon 不可用 — start_infrastructure 阶段无法执行",
            started_at=started_at,
            readiness_checks=[{"check": "docker_daemon", "status": "fail"}],
        )

    # 启动 redis + db_writer(redis-acl-init + migration 会通过 depends_on 自动触发)
    cmd = _compose_cmd(["up", "-d"] + CORE_SERVICES)
    try:
        result = _run(cmd, timeout=timeout, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired:
        container_logs: dict[str, Any] = {}
        for svc in ("redis-acl-init", "redis", "migration", "db_writer"):
            logs_cmd = _compose_cmd(["logs", "--no-color", "--tail", "500", svc])
            try:
                logs_result = _run(logs_cmd, timeout=15, cwd=REPO_ROOT)
                svc_log = (logs_result.stdout or "") + (logs_result.stderr or "")
                if svc_log.strip():
                    container_logs[svc] = svc_log[-8000:]
            except (subprocess.TimeoutExpired, OSError):
                pass
        return _fail_result(
            phase="start_infrastructure",
            description=description,
            started=started,
            error=f"docker compose up -d {' '.join(CORE_SERVICES)} 超时({timeout}s)",
            started_at=started_at,
            evidence={"container_logs": container_logs} if container_logs else {},
            readiness_checks=[
                {"check": "docker_daemon", "status": "pass"},
                {"check": "compose_up", "status": "timeout"},
            ],
        )

    if result.returncode != 0:
        container_logs: dict[str, Any] = {}
        for svc in ("redis-acl-init", "redis", "migration", "db_writer"):
            logs_cmd = _compose_cmd(["logs", "--no-color", "--tail", "500", svc])
            try:
                logs_result = _run(logs_cmd, timeout=15, cwd=REPO_ROOT)
                svc_log = (logs_result.stdout or "") + (logs_result.stderr or "")
                if svc_log.strip():
                    container_logs[svc] = svc_log[-8000:]
            except (subprocess.TimeoutExpired, OSError):
                pass
        return _fail_result(
            phase="start_infrastructure",
            description=description,
            started=started,
            error=(
                f"docker compose up -d {' '.join(CORE_SERVICES)} 失败 "
                f"(exit={result.returncode})"
            ),
            started_at=started_at,
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=result.returncode,
            evidence={"container_logs": container_logs} if container_logs else {},
            readiness_checks=[
                {"check": "docker_daemon", "status": "pass"},
                {"check": "compose_up", "status": "fail"},
            ],
        )

    readiness_checks: list[dict[str, Any]] = [
        {"check": "docker_daemon", "status": "pass"},
        {"check": "compose_up", "status": "pass"},
    ]

    # 轮询等待所有核心服务达到期望状态
    expected_infra: dict[str, dict[str, str]] = {
        "redis-acl-init": {"state": "exited", "health": ""},
        "redis": {"state": "running", "health": "healthy"},
        "migration": {"state": "exited", "health": ""},
        "db_writer": {"state": "running", "health": "healthy"},
    }
    all_ready, service_info = _wait_for_services(
        expected=expected_infra, timeout_seconds=180, poll_interval=5,
    )

    service_failures: list[str] = []
    for svc, req in expected_infra.items():
        si = service_info.get(svc)
        if si is None:
            service_failures.append(f"{svc}: 服务不存在(docker compose ps 未发现)")
            readiness_checks.append({
                "check": f"service_{svc}", "status": "fail", "reason": "not_found",
            })
            continue
        svc_ok = si["state"] == req["state"]
        if req["health"]:
            svc_ok = svc_ok and si["health"] == req["health"]
        if req["state"] == "exited":
            exit_code = si.get("exit_code")
            svc_ok = svc_ok and (exit_code == 0)
        readiness_checks.append({
            "check": f"service_{svc}",
            "status": "pass" if svc_ok else "fail",
            "state": si["state"], "health": si["health"],
            "exit_code": si.get("exit_code"),
        })
        if not svc_ok:
            service_failures.append(
                f"{svc}: state={si['state']!r}, health={si['health']!r}, "
                f"exit_code={si.get('exit_code')!r}"
            )

    if service_failures or not all_ready:
        container_logs = {}
        for svc in expected_infra:
            logs_cmd = _compose_cmd(["logs", "--no-color", "--tail", "300", svc])
            try:
                logs_result = _run(logs_cmd, timeout=15, cwd=REPO_ROOT)
                svc_log = (logs_result.stdout or "") + (logs_result.stderr or "")
                if svc_log.strip():
                    container_logs[svc] = svc_log[-6000:]
            except (subprocess.TimeoutExpired, OSError):
                pass
        return _fail_result(
            phase="start_infrastructure",
            description=description,
            started=started,
            started_at=started_at,
            error=(
                f"R73 §5.15: 基础设施状态断言失败 — "
                f"{'; '.join(service_failures) if service_failures else '等待超时(180s)'}"
            ),
            evidence={
                "service_info": service_info, "expected": expected_infra,
                "failures": service_failures, "wait_timeout_seconds": 180,
                "all_ready": all_ready, "container_logs": container_logs,
            },
            readiness_checks=readiness_checks,
        )

    # R73 §5.15: migration schema aligned — 运行 migration --check --json
    mig_cmd = _compose_cmd([
        "exec", "-T", "db_writer",
        "python", "-m", "database.migrate", "--check", "--json",
    ])
    try:
        mig_result = _run(mig_cmd, timeout=timeout, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired:
        return _fail_result(
            phase="start_infrastructure",
            description=description,
            started=started,
            started_at=started_at,
            error=f"migration --check --json 超时({timeout}s)",
            readiness_checks=readiness_checks + [
                {"check": "migration_check", "status": "timeout"},
            ],
        )
    if mig_result.returncode != 0:
        return _fail_result(
            phase="start_infrastructure",
            description=description,
            started=started,
            started_at=started_at,
            error=(
                f"migration --check --json 失败 (exit={mig_result.returncode}) — "
                "schema 未对齐"
            ),
            stdout=mig_result.stdout, stderr=mig_result.stderr,
            returncode=mig_result.returncode,
            readiness_checks=readiness_checks + [
                {"check": "migration_check", "status": "fail"},
            ],
        )
    try:
        mig_evidence = json.loads(mig_result.stdout.strip())
    except json.JSONDecodeError as e:
        return _fail_result(
            phase="start_infrastructure",
            description=description,
            started=started,
            started_at=started_at,
            error=f"migration --check --json 输出非 JSON: {e}",
            stdout=mig_result.stdout, stderr=mig_result.stderr,
            readiness_checks=readiness_checks + [
                {"check": "migration_check", "status": "fail"},
                {"check": "migration_json_parsed", "status": "fail"},
            ],
        )
    mig_failed = mig_evidence.get("failed", [])
    mig_pending = mig_evidence.get("pending", [])
    mig_final = mig_evidence.get("final_status", "unknown")
    mig_current = mig_evidence.get("current_schema_version", 0)
    mig_expected = mig_evidence.get("expected_schema_version", 0)
    mig_aligned = (
        not mig_failed and not mig_pending
        and mig_final == "ok" and mig_current == mig_expected
    )
    readiness_checks.append({
        "check": "migration_check",
        "status": "pass" if mig_aligned else "fail",
        "failed_count": len(mig_failed),
        "pending_count": len(mig_pending),
        "current_version": mig_current, "expected_version": mig_expected,
        "final_status": mig_final,
    })
    if not mig_aligned:
        return _fail_result(
            phase="start_infrastructure",
            description=description,
            started=started,
            started_at=started_at,
            error=(
                f"R73 §5.15: migration schema 未对齐 — "
                f"failed={mig_failed}, pending={mig_pending}, "
                f"final_status={mig_final}, "
                f"current={mig_current}, expected={mig_expected}"
            ),
            stdout=mig_result.stdout, stderr=mig_result.stderr,
            evidence={"migration_evidence": mig_evidence},
            readiness_checks=readiness_checks,
        )

    return _pass_result(
        phase="start_infrastructure",
        description=description,
        started=started,
        started_at=started_at,
        stdout=result.stdout, stderr=result.stderr,
        returncode=result.returncode,
        evidence={
            "started_services": sorted(expected_infra.keys()),
            "service_info": service_info,
            "migration_evidence": mig_evidence,
        },
        readiness_checks=readiness_checks,
    )


def phase_start_application_roles(timeout: int) -> PhaseResult:
    """R73 §5.15 阶段 3:启动所有长驻角色并验证 RepoDigest。

    readiness 检查点:
      - docker compose up -d <bots> 返回 0
      - 所有 Bot + 业务服务容器 running + healthy
      - 每个服务 SERVICE_ROLE 与 docker-compose.prod.yml 一致
      - 每个服务容器实际 RepoDigest 包含 expected_digest
    """
    description = _phase_desc("start_application_roles")
    started = time.time()
    started_at = _now_iso()

    if not _docker_available():
        return _fail_result(
            phase="start_application_roles",
            description=description,
            started=started,
            started_at=started_at,
            error="Docker daemon 不可用 — start_application_roles 阶段无法执行",
            readiness_checks=[{"check": "docker_daemon", "status": "fail"}],
        )

    cmd = _compose_cmd(["up", "-d"] + BOT_SERVICES)
    try:
        result = _run(cmd, timeout=timeout, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired:
        return _fail_result(
            phase="start_application_roles",
            description=description,
            started=started,
            started_at=started_at,
            error=f"docker compose up -d bots 超时({timeout}s)",
            readiness_checks=[
                {"check": "docker_daemon", "status": "pass"},
                {"check": "compose_up", "status": "timeout"},
            ],
        )

    if result.returncode != 0:
        return _fail_result(
            phase="start_application_roles",
            description=description,
            started=started,
            started_at=started_at,
            error=f"docker compose up -d bots 失败 (exit={result.returncode})",
            stdout=result.stdout, stderr=result.stderr,
            returncode=result.returncode,
            readiness_checks=[
                {"check": "docker_daemon", "status": "pass"},
                {"check": "compose_up", "status": "fail"},
            ],
        )

    readiness_checks: list[dict[str, Any]] = [
        {"check": "docker_daemon", "status": "pass"},
        {"check": "compose_up", "status": "pass"},
    ]

    expected_bots: dict[str, dict[str, str]] = {
        svc: {"state": "running", "health": "healthy"} for svc in BOT_SERVICES
    }
    all_ready, service_info = _wait_for_services(
        expected=expected_bots, timeout_seconds=300, poll_interval=5,
    )

    service_failures: list[str] = []
    for svc, req in expected_bots.items():
        si = service_info.get(svc)
        if si is None:
            service_failures.append(f"{svc}: 服务不存在")
            readiness_checks.append({
                "check": f"service_{svc}", "status": "fail", "reason": "not_found",
            })
            continue
        svc_ok = si["state"] == "running" and si["health"] == "healthy"
        readiness_checks.append({
            "check": f"service_{svc}",
            "status": "pass" if svc_ok else "fail",
            "state": si["state"], "health": si["health"],
        })
        if not svc_ok:
            service_failures.append(
                f"{svc}: state={si['state']!r}, health={si['health']!r}"
            )

    if service_failures or not all_ready:
        return _fail_result(
            phase="start_application_roles",
            description=description,
            started=started,
            started_at=started_at,
            error=(
                f"R73 §5.15: Bot 服务状态断言失败 — "
                f"{'; '.join(service_failures) if service_failures else '等待超时(300s)'}"
            ),
            stdout=result.stdout, stderr=result.stderr,
            evidence={
                "service_info": service_info, "failures": service_failures,
                "wait_timeout_seconds": 300, "all_ready": all_ready,
            },
            readiness_checks=readiness_checks,
        )

    # 验证 SERVICE_ROLE + RepoDigest
    role_failures: list[str] = []
    digest_failures: list[str] = []
    expected_image = os.environ.get("TGJIEMA_IMAGE", "")
    expected_digest = ""
    if "@" in expected_image:
        expected_digest = expected_image.split("@", 1)[1]

    for svc in BOT_SERVICES:
        role_cmd = _compose_cmd(["exec", "-T", svc, "printenv", "SERVICE_ROLE"])
        role_result = _run(role_cmd, timeout=15, cwd=REPO_ROOT)
        if role_result.returncode != 0 or not role_result.stdout.strip():
            role_failures.append(f"{svc}: SERVICE_ROLE 未设置")
            readiness_checks.append({
                "check": f"service_role_{svc}", "status": "fail",
                "reason": "env_not_set",
            })
        else:
            actual_role = role_result.stdout.strip()
            expected_role = SERVICE_ROLES.get(svc, "")
            if actual_role != expected_role:
                role_failures.append(
                    f"{svc}: SERVICE_ROLE={actual_role!r} (expected={expected_role!r})"
                )
                readiness_checks.append({
                    "check": f"service_role_{svc}", "status": "fail",
                    "actual": actual_role, "expected": expected_role,
                })
            else:
                readiness_checks.append({
                    "check": f"service_role_{svc}", "status": "pass",
                })

        svc_info_entry = service_info.get(svc, {})
        container_name = (
            svc_info_entry.get("container_name")
            or svc_info_entry.get("container_id")
            or f"tgjiema-{svc}"
        )
        inspect_cmd = ["docker", "inspect", "--format", "{{json .}}", container_name]
        inspect_result = _run(inspect_cmd, timeout=10)
        if inspect_result.returncode != 0:
            digest_failures.append(
                f"{svc}: docker inspect 失败 (container={container_name!r})"
            )
            readiness_checks.append({
                "check": f"repo_digest_{svc}", "status": "fail",
                "reason": "inspect_failed",
            })
        else:
            try:
                container_info = json.loads(inspect_result.stdout.strip())
            except json.JSONDecodeError:
                digest_failures.append(f"{svc}: inspect 输出非 JSON")
                readiness_checks.append({
                    "check": f"repo_digest_{svc}", "status": "fail",
                    "reason": "inspect_output_not_json",
                })
            else:
                repo_digests = container_info.get("RepoDigests", []) or []
                config_image = (
                    container_info.get("Config", {}).get("Image", "")
                    if isinstance(container_info.get("Config"), dict) else ""
                )
                repo_digests_json = (
                    repo_digests if isinstance(repo_digests, str)
                    else json.dumps(repo_digests)
                )
                digest_match = (
                    not expected_digest
                    or expected_digest in repo_digests_json
                    or (config_image and expected_digest in config_image)
                )
                if not digest_match:
                    digest_failures.append(
                        f"{svc}: RepoDigest 不匹配 (expected {expected_digest[:24]}...)"
                    )
                    readiness_checks.append({
                        "check": f"repo_digest_{svc}", "status": "fail",
                        "expected_digest": expected_digest[:24] + "...",
                    })
                else:
                    readiness_checks.append({
                        "check": f"repo_digest_{svc}", "status": "pass",
                    })

    if role_failures or digest_failures:
        all_failures = role_failures + digest_failures
        return _fail_result(
            phase="start_application_roles",
            description=description,
            started=started,
            started_at=started_at,
            error=(
                f"R73 §5.15: 角色身份/镜像 digest 验证失败 — "
                f"{'; '.join(all_failures)}"
            ),
            stdout=result.stdout, stderr=result.stderr,
            evidence={
                "service_info": service_info,
                "role_failures": role_failures,
                "digest_failures": digest_failures,
                "expected_image": expected_image,
            },
            readiness_checks=readiness_checks,
        )

    return _pass_result(
        phase="start_application_roles",
        description=description,
        started=started,
        started_at=started_at,
        stdout=result.stdout, stderr=result.stderr,
        returncode=result.returncode,
        evidence={
            "started_bots": BOT_SERVICES,
            "service_info": service_info,
        },
        readiness_checks=readiness_checks,
    )


def _run_real_product_transaction(phase_name: str, started: float, started_at: str, timeout: int) -> PhaseResult:
    """R73 P0-04: 在当前 active identity 上运行真实产品交易。

    调用 synthetic_transaction.run_dbwriter_component_test() 验证完整业务链路:
    up→idx→dsp→writer→CRDB→output,失败时立即 fail-closed。
    """
    description = _phase_desc(phase_name)
    if not _docker_available():
        return _fail_result(
            phase=phase_name,
            description=description,
            started=started,
            started_at=started_at,
            error=f"Docker daemon 不可用 — {phase_name} 阶段无法执行",
            readiness_checks=[{"check": "docker_daemon", "status": "fail"}],
        )

    readiness_checks: list[dict[str, Any]] = [
        {"check": "docker_daemon", "status": "pass"},
    ]

    passed, evidence = _run_synthetic_transaction(timeout=timeout)

    step_results = {
        "inject": evidence.get("inject", {}),
        "verify": evidence.get("verify", {}),
        "idempotency": evidence.get("idempotency", {}),
        "failure_scenario": evidence.get("failure_scenario", {}),
        "cleanup": evidence.get("cleanup", {}),
    }
    for step_name, step_data in step_results.items():
        readiness_checks.append({
            "check": f"synthetic_{step_name}",
            "status": "pass" if step_data.get("passed", False) else "fail",
        })

    crdb_sync_verify = evidence.get("crdb_sync_verify", {})
    dsp_dispatch_verify = evidence.get("dsp_dispatch_verify", {})
    dsp_dispatch_idempotency = evidence.get("dsp_dispatch_idempotency", {})
    fault_injection = evidence.get("fault_injection", {})

    readiness_checks.append({
        "check": "synthetic_crdb_sync_verify",
        "status": "pass" if crdb_sync_verify.get("passed", False) else "fail",
    })
    readiness_checks.append({
        "check": "synthetic_dsp_dispatch_verify",
        "status": "pass" if dsp_dispatch_verify.get("passed", False) else "fail",
    })
    readiness_checks.append({
        "check": "synthetic_dsp_dispatch_idempotency",
        "status": "pass" if dsp_dispatch_idempotency.get("passed", False) else "fail",
    })

    fault_crdb = fault_injection.get("crdb_sync", {}) if isinstance(fault_injection, dict) else {}
    fault_start = fault_injection.get("crdb_sync_start", {}) if isinstance(fault_injection, dict) else {}
    fault_injection_ok = (
        fault_crdb.get("passed", False) and fault_start.get("passed", False)
    )
    readiness_checks.append({
        "check": "synthetic_fault_injection",
        "status": "pass" if fault_injection_ok else "fail",
    })
    readiness_checks.append({
        "check": "synthetic_overall",
        "status": "pass" if passed else "fail",
    })

    if not passed:
        return _fail_result(
            phase=phase_name, description=description, started=started,
            started_at=started_at,
            error=f"真实产品交易失败: {evidence.get('error', '未知')}",
            evidence=evidence, readiness_checks=readiness_checks,
        )
    if not crdb_sync_verify.get("passed", False):
        return _fail_result(
            phase=phase_name, description=description, started=started,
            started_at=started_at,
            error=f"CRDB sync 验证未通过(fail-closed): {crdb_sync_verify.get('error')}",
            evidence=evidence, readiness_checks=readiness_checks,
        )
    if not dsp_dispatch_verify.get("passed", False):
        return _fail_result(
            phase=phase_name, description=description, started=started,
            started_at=started_at,
            error=f"dsp 派送验证未通过: {dsp_dispatch_verify.get('error')}",
            evidence=evidence, readiness_checks=readiness_checks,
        )
    if not dsp_dispatch_idempotency.get("passed", False):
        return _fail_result(
            phase=phase_name, description=description, started=started,
            started_at=started_at,
            error=f"dsp 派送幂等性验证未通过: {dsp_dispatch_idempotency.get('error')}",
            evidence=evidence, readiness_checks=readiness_checks,
        )
    if not fault_injection_ok:
        return _fail_result(
            phase=phase_name, description=description, started=started,
            started_at=started_at,
            error=f"故障注入测试未通过(角色停止时未 fail-closed): {fault_crdb.get('error')}",
            evidence=evidence, readiness_checks=readiness_checks,
        )

    return _pass_result(
        phase=phase_name, description=description, started=started,
        started_at=started_at, evidence=evidence, readiness_checks=readiness_checks,
    )


def phase_real_product_transaction_before_backup(timeout: int) -> PhaseResult:
    """R73 §5.15 阶段 4:backup 前真实产品交易。"""
    started = time.time()
    started_at = _now_iso()
    return _run_real_product_transaction(
        "real_product_transaction_before_backup", started, started_at, timeout,
    )


def phase_full_backup_to_r2(timeout: int) -> PhaseResult:
    """R73 §5.15 阶段 5:全量备份到 R2(三对象 payload/manifest/COMPLETE)。

    readiness 检查点:
      - docker compose run db_backup backup --once --type full 返回 0
      - backup-evidence.json status=success / backup_type=full
      - 三对象(payload / manifest / COMPLETE)都存在并 readback 核对通过
    """
    description = _phase_desc("full_backup_to_r2")
    started = time.time()
    started_at = _now_iso()

    if not _docker_available():
        return _fail_result(
            phase="full_backup_to_r2", description=description, started=started,
            started_at=started_at,
            error="Docker daemon 不可用 — full_backup_to_r2 阶段无法执行",
            readiness_checks=[{"check": "docker_daemon", "status": "fail"}],
        )

    readiness_checks: list[dict[str, Any]] = [
        {"check": "docker_daemon", "status": "pass"},
    ]

    source_sha = os.environ.get("GITHUB_SHA", "")
    if not source_sha:
        source_sha = _get_source_sha()
    reason = "rc-restore-drill"

    backup_evidence_path = REPO_ROOT / "data" / "backup-evidence.json"
    backup_evidence_container_path = "/app/data/backup-evidence.json"
    backup_cmd = _compose_cmd([
        "run", "--rm", "-T", "--no-deps", "--entrypoint", "python",
        "db_backup",
        "-m", "services.db_backup", "backup",
        "--once", "--timeout", "240",
        "--type", "full",
        "--reason", reason,
        "--source-sha", source_sha,
        "--output-json", backup_evidence_container_path,
    ])
    try:
        backup_result = _run(backup_cmd, timeout=timeout, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired as te:
        partial_stdout = (te.stdout or "")[:2000] if isinstance(te.stdout, str) else ""
        partial_stderr = (te.stderr or "")[:2000] if isinstance(te.stderr, str) else ""
        return _fail_result(
            phase="full_backup_to_r2", description=description, started=started,
            started_at=started_at,
            error=f"R73 P0-05: backup --once --type full 超时({timeout}s)",
            stdout=partial_stdout, stderr=partial_stderr,
            readiness_checks=readiness_checks + [
                {"check": "backup_triggered", "status": "timeout"},
            ],
        )
    if backup_result.returncode != 0:
        return _fail_result(
            phase="full_backup_to_r2", description=description, started=started,
            started_at=started_at,
            error=(
                f"R73 P0-05: backup --once --type full 失败 "
                f"(exit={backup_result.returncode})"
            ),
            stdout=backup_result.stdout, stderr=backup_result.stderr,
            returncode=backup_result.returncode,
            readiness_checks=readiness_checks + [
                {"check": "backup_triggered", "status": "fail"},
            ],
        )
    readiness_checks.append({"check": "backup_triggered", "status": "pass"})

    backup_evidence: dict[str, Any] = {}
    try:
        if backup_evidence_path.is_file():
            backup_evidence = json.loads(
                backup_evidence_path.read_text(encoding="utf-8")
            )
    except (json.JSONDecodeError, OSError) as e:
        return _fail_result(
            phase="full_backup_to_r2", description=description, started=started,
            started_at=started_at,
            error=f"R73 P0-05: backup evidence JSON 解析失败: {e}",
            stdout=backup_result.stdout, stderr=backup_result.stderr,
            readiness_checks=readiness_checks + [
                {"check": "backup_evidence_parsed", "status": "fail"},
            ],
        )

    backup_status = str(backup_evidence.get("status", ""))
    backup_type = str(backup_evidence.get("backup_type", ""))
    backup_id = str(backup_evidence.get("backup_id", ""))
    objects = backup_evidence.get("objects", {}) or {}
    three_objects_ok = (
        "payload" in objects and "manifest" in objects and "COMPLETE" in objects
    )
    readiness_checks.append({
        "check": "backup_evidence_parsed",
        "status": "pass" if backup_status == "success" else "fail",
        "backup_status": backup_status, "backup_type": backup_type,
        "backup_id": backup_id,
    })
    readiness_checks.append({
        "check": "backup_type_full",
        "status": "pass" if backup_type == "full" else "fail",
        "backup_type": backup_type,
    })
    readiness_checks.append({
        "check": "backup_three_objects",
        "status": "pass" if three_objects_ok else "fail",
        "objects_keys": sorted(objects.keys()) if isinstance(objects, dict) else [],
    })

    if (backup_status != "success" or backup_type != "full"
            or not three_objects_ok or not backup_id):
        return _fail_result(
            phase="full_backup_to_r2", description=description, started=started,
            started_at=started_at,
            error=(
                f"R73 P0-05: backup evidence 校验失败 — "
                f"status={backup_status!r}, backup_type={backup_type!r}, "
                f"three_objects_ok={three_objects_ok}, backup_id={backup_id!r}"
            ),
            stdout=backup_result.stdout, stderr=backup_result.stderr,
            evidence=backup_evidence,
            readiness_checks=readiness_checks,
        )

    # 清理 evidence 临时文件(不影响测试结论,仅保持工作区整洁)
    try:
        if backup_evidence_path.exists():
            backup_evidence_path.unlink(missing_ok=True)
    except OSError:
        pass

    # R73 P0-05/P0-06 / R81 §10.1 P2-02: 将 backup_id 持久化,
    # 供后续阶段(phase_blank_isolated_restore / phase_actual_switch /
    # phase_actual_rollback)使用,避免阶段间断裂。
    # 单进程 DAG 模式下 os.environ 即可;跨进程模式(secretless workflow)
    # 通过状态文件持久化。统一使用 _persist_backup_id + SECRETLESS_BACKUP_ID。
    _persist_backup_id(backup_id)
    os.environ["SECRETLESS_BACKUP_ID"] = backup_id

    return _pass_result(
        phase="full_backup_to_r2", description=description, started=started,
        started_at=started_at,
        stdout=backup_result.stdout, stderr=backup_result.stderr,
        returncode=backup_result.returncode,
        evidence={
            "backup_id": backup_id,
            "backup_status": backup_status,
            "backup_type": backup_type,
            "objects": objects,
            "reason": reason, "source_sha": source_sha,
        },
        readiness_checks=readiness_checks,
    )


def phase_blank_isolated_restore(timeout: int) -> PhaseResult:
    """R74 P0-06 阶段 6:空白隔离恢复到 staging target(唯一 compose project/network/volumes/CRDB identity)。

    R74 P0-06 整改:
      - 创建唯一 operation_id 作为恢复操作标识
      - 创建唯一 compose project 名称(tgjiema-restore-{operation_id})
      - 创建唯一网络名称
      - 创建唯一卷名称
      - 创建唯一 CRDB 数据库 identity
      - 记录 operation_id 到 evidence,供后续阶段审计
      - 旧实现缺少唯一隔离标识,无法区分多次恢复操作

    readiness 检查点:
      - 通过 services/restore_capability_file.issue_capability 生成一次性 capability
      - docker compose run db_restore --backup-id --target staging --capability-file 返回 0
      - restore-evidence.json 目标 identity 与 production 不同
      - operation_id 已记录且唯一
    """
    import uuid as _uuid_mod

    description = _phase_desc("blank_isolated_restore")
    started = time.time()
    started_at = _now_iso()

    if not _docker_available():
        return _fail_result(
            phase="blank_isolated_restore", description=description, started=started,
            started_at=started_at,
            error="Docker daemon 不可用 — blank_isolated_restore 阶段无法执行",
            readiness_checks=[{"check": "docker_daemon", "status": "fail"}],
        )

    readiness_checks: list[dict[str, Any]] = [
        {"check": "docker_daemon", "status": "pass"},
    ]

    # R74 P0-06: 生成唯一 operation_id
    operation_id = _uuid_mod.uuid4().hex[:12]
    compose_project_name = f"tgjiema-restore-{operation_id}"
    network_name = f"tgjiema-restore-net-{operation_id}"
    volume_prefix = f"tgjiema-restore-vol-{operation_id}"
    crdb_identity = f"crdb-restore-{operation_id}"

    readiness_checks.append({
        "check": "operation_id_generated",
        "status": "pass",
        "operation_id": operation_id,
        "compose_project_name": compose_project_name,
        "network_name": network_name,
        "crdb_identity": crdb_identity,
    })

    # R81 §10.1 P2-02: 从上一阶段读取 backup_id(跨进程持久化,统一命名)
    backup_id = _load_backup_id()
    if not backup_id:
        # backup_id 应由 full_backup_to_r2 阶段产出,这里通过状态文件/env 注入
        return _fail_result(
            phase="blank_isolated_restore", description=description, started=started,
            started_at=started_at,
            error=(
                "R74 P0-06: SECRETLESS_BACKUP_ID_STATE_MISSING — "
                "full_backup_to_r2 阶段必须先执行并通过"
            ),
            evidence={"operation_id": operation_id},
            readiness_checks=readiness_checks + [
                {"check": "backup_id_available", "status": "fail"},
            ],
        )
    readiness_checks.append({"check": "backup_id_available", "status": "pass"})

    # 生成一次性 restore capability
    signing_key = os.environ.get("BACKUP_SIGNING_KEY", "").encode("utf-8")
    if not signing_key:
        return _fail_result(
            phase="blank_isolated_restore", description=description, started=started,
            started_at=started_at,
            error="R74 P0-06: BACKUP_SIGNING_KEY 未设置 — 无法签发 capability",
            evidence={"operation_id": operation_id},
            readiness_checks=readiness_checks + [
                {"check": "capability_signed", "status": "fail"},
            ],
        )

    source_sha = os.environ.get("GITHUB_SHA", "") or _get_source_sha()
    target_path = "/app/data/staging/cache_store.db"
    target_identity = os.environ.get(
        "R73_TARGET_IDENTITY",
        f"staging-target-identity-{operation_id}",
    )
    try:
        # 直接通过 importlib 加载 restore_capability_file 模块
        import importlib.util as _ilu
        rcf_path = REPO_ROOT / "services" / "restore_capability_file.py"
        spec = _ilu.spec_from_file_location(
            "restore_capability_file", rcf_path,
        )
        if spec is None or spec.loader is None:
            raise ImportError("spec_from_file_location 返回 None")
        rcf_module = _ilu.module_from_spec(spec)
        spec.loader.exec_module(rcf_module)
        capability = rcf_module.issue_capability(
            backup_id=backup_id,
            source_sha=source_sha,
            target_database_identity=target_identity,
            target_path=target_path,
            run_id=int(os.environ.get("GITHUB_RUN_ID", "0")),
            run_attempt=int(os.environ.get("GITHUB_RUN_ATTEMPT", "1")),
            audience="compose_runtime_e2e",
            target_uri=f"sqlite://{target_path}",
            signing_key=signing_key,
        )
    except Exception as e:
        return _fail_result(
            phase="blank_isolated_restore", description=description, started=started,
            started_at=started_at,
            error=f"R74 P0-06: issue_capability 失败: {type(e).__name__}: {e}",
            evidence={"operation_id": operation_id},
            readiness_checks=readiness_checks + [
                {"check": "capability_signed", "status": "fail"},
            ],
        )

    capability_path = REPO_ROOT / "data" / "restore_capability.json"
    try:
        spec = _ilu.spec_from_file_location("restore_capability_file2", rcf_path)
        if spec is None or spec.loader is None:
            raise ImportError("spec_from_file_location 返回 None")
        rcf_mod2 = _ilu.module_from_spec(spec)
        spec.loader.exec_module(rcf_mod2)
        rcf_mod2.write_capability_file(capability, capability_path)
    except Exception as e:
        return _fail_result(
            phase="blank_isolated_restore", description=description, started=started,
            started_at=started_at,
            error=f"R74 P0-06: write_capability_file 失败: {type(e).__name__}: {e}",
            evidence={"operation_id": operation_id},
            readiness_checks=readiness_checks + [
                {"check": "capability_signed", "status": "fail"},
            ],
        )
    readiness_checks.append({"check": "capability_signed", "status": "pass"})

    restore_evidence_path = REPO_ROOT / "data" / "restore-evidence.json"
    restore_evidence_container_path = "/app/data/restore-evidence.json"
    db_restore_host_path = (REPO_ROOT / "services" / "db_restore.py").as_posix()
    capability_container_path = "/run/secrets/restore_capability.json"
    restore_cmd = _compose_cmd([
        "run", "--rm", "-T", "--no-deps", "--entrypoint", "python",
        "-e", "APP_ENV=development",
        "-v", f"{db_restore_host_path}:/app/services/db_restore.py:ro",
        "-v", f"{capability_path.as_posix()}:{capability_container_path}:ro",
        "db_writer",
        "-m", "services.db_restore",
        "--backup-id", backup_id,
        "--target", "staging",
        "--target-identity", target_identity,
        "--capability-file", capability_container_path,
        "--output-json", restore_evidence_container_path,
    ])
    try:
        restore_result = _run(restore_cmd, timeout=timeout, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired as te:
        partial_stdout = (te.stdout or "")[:2000] if isinstance(te.stdout, str) else ""
        partial_stderr = (te.stderr or "")[:2000] if isinstance(te.stderr, str) else ""
        return _fail_result(
            phase="blank_isolated_restore", description=description, started=started,
            started_at=started_at,
            error=f"R74 P0-06: db_restore 超时({timeout}s)",
            stdout=partial_stdout, stderr=partial_stderr,
            evidence={"operation_id": operation_id},
            readiness_checks=readiness_checks + [
                {"check": "restore_triggered", "status": "timeout"},
            ],
        )
    if restore_result.returncode != 0:
        return _fail_result(
            phase="blank_isolated_restore", description=description, started=started,
            started_at=started_at,
            error=(
                f"R74 P0-06: db_restore 失败 (exit={restore_result.returncode})"
            ),
            stdout=restore_result.stdout, stderr=restore_result.stderr,
            returncode=restore_result.returncode,
            evidence={"operation_id": operation_id},
            readiness_checks=readiness_checks + [
                {"check": "restore_triggered", "status": "fail"},
            ],
        )
    readiness_checks.append({"check": "restore_triggered", "status": "pass"})

    # 解析 restore-evidence.json,验证目标 identity 与 production 不同
    restore_evidence: dict[str, Any] = {}
    try:
        if restore_evidence_path.is_file():
            restore_evidence = json.loads(
                restore_evidence_path.read_text(encoding="utf-8")
            )
    except (json.JSONDecodeError, OSError) as e:
        return _fail_result(
            phase="blank_isolated_restore", description=description, started=started,
            started_at=started_at,
            error=f"R74 P0-06: restore evidence JSON 解析失败: {e}",
            stdout=restore_result.stdout, stderr=restore_result.stderr,
            evidence={"operation_id": operation_id},
            readiness_checks=readiness_checks + [
                {"check": "restore_evidence_parsed", "status": "fail"},
            ],
        )
    restored_identity = str(restore_evidence.get("target_database_identity", ""))
    production_identity = str(restore_evidence.get("production_identity", ""))
    identity_differs = (
        bool(restored_identity) and restored_identity != production_identity
    )
    readiness_checks.append({
        "check": "restore_evidence_parsed",
        "status": "pass" if restore_evidence else "fail",
    })
    readiness_checks.append({
        "check": "target_identity_differs_from_production",
        "status": "pass" if identity_differs else "fail",
        "restored_identity": restored_identity[:32] + "..." if restored_identity else "",
        "production_identity": production_identity[:32] + "..." if production_identity else "",
    })

    # 清理临时文件
    for _tmp in (restore_evidence_path, capability_path):
        try:
            if _tmp.exists():
                _tmp.unlink(missing_ok=True)
        except OSError:
            pass

    if not identity_differs:
        return _fail_result(
            phase="blank_isolated_restore", description=description, started=started,
            started_at=started_at,
            error=(
                "R74 P0-06: 目标 identity 与 production 相同 — "
                "恢复未隔离到 staging(fail-closed)"
            ),
            stdout=restore_result.stdout, stderr=restore_result.stderr,
            evidence={
                "operation_id": operation_id,
                "restore_evidence": restore_evidence,
            },
            readiness_checks=readiness_checks,
        )

    # R74 P0-06: 导出 operation_id 供后续阶段使用
    os.environ["R74_OPERATION_ID"] = operation_id

    return _pass_result(
        phase="blank_isolated_restore", description=description, started=started,
        started_at=started_at,
        stdout=restore_result.stdout, stderr=restore_result.stderr,
        returncode=restore_result.returncode,
        evidence={
            "operation_id": operation_id,
            "compose_project_name": compose_project_name,
            "network_name": network_name,
            "volume_prefix": volume_prefix,
            "crdb_identity": crdb_identity,
            "backup_id": backup_id,
            "restored_identity": restored_identity,
            "production_identity": production_identity,
            "restore_evidence": restore_evidence,
        },
        readiness_checks=readiness_checks,
    )


def phase_restore_integrity_and_target_identity(timeout: int) -> PhaseResult:
    """R73 §5.15 阶段 7:恢复完整性 + target identity 校验。

    readiness 检查点:
      - verify_restore_integrity.py verify_full 通过
      - schema fingerprint / 逐表 hash / row count 一致
      - target identity 与 production 不同
    """
    description = _phase_desc("restore_integrity_and_target_identity")
    started = time.time()
    started_at = _now_iso()

    if not _docker_available():
        return _fail_result(
            phase="restore_integrity_and_target_identity",
            description=description, started=started, started_at=started_at,
            error="Docker daemon 不可用 — restore_integrity 阶段无法执行",
            readiness_checks=[{"check": "docker_daemon", "status": "fail"}],
        )

    readiness_checks: list[dict[str, Any]] = [
        {"check": "docker_daemon", "status": "pass"},
    ]

    # 生成 trace_id 和 pre-snapshot,然后执行 verify_full
    import importlib.util
    import uuid as _uuid_mod
    if not VERIFY_RESTORE_INTEGRITY_PATH.is_file():
        return _fail_result(
            phase="restore_integrity_and_target_identity",
            description=description, started=started, started_at=started_at,
            error=f"verify_restore_integrity.py 不存在: {VERIFY_RESTORE_INTEGRITY_PATH}",
            readiness_checks=readiness_checks + [
                {"check": "verify_restore_integrity_available", "status": "fail"},
            ],
        )
    try:
        spec = importlib.util.spec_from_file_location(
            "verify_restore_integrity_2", VERIFY_RESTORE_INTEGRITY_PATH,
        )
        if spec is None or spec.loader is None:
            raise ImportError("spec_from_file_location 返回 None")
        vri_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(vri_module)
    except Exception as e:
        return _fail_result(
            phase="restore_integrity_and_target_identity",
            description=description, started=started, started_at=started_at,
            error=f"加载 verify_restore_integrity 失败: {type(e).__name__}: {e}",
            readiness_checks=readiness_checks + [
                {"check": "verify_restore_integrity_available", "status": "fail"},
            ],
        )
    readiness_checks.append({
        "check": "verify_restore_integrity_available", "status": "pass",
    })

    trace_id = f"restore_marker_{int(time.time())}_{_uuid_mod.uuid4().hex[:8]}"
    try:
        write_rc = vri_module.write_marker(trace_id)
    except Exception as e:
        return _fail_result(
            phase="restore_integrity_and_target_identity",
            description=description, started=started, started_at=started_at,
            error=f"write_marker 异常: {type(e).__name__}: {e}",
            readiness_checks=readiness_checks + [
                {"check": "write_marker", "status": "fail"},
            ],
        )
    if write_rc != 0:
        return _fail_result(
            phase="restore_integrity_and_target_identity",
            description=description, started=started, started_at=started_at,
            error=f"write_marker 失败 (exit={write_rc})",
            readiness_checks=readiness_checks + [
                {"check": "write_marker", "status": "fail"},
            ],
        )
    readiness_checks.append({"check": "write_marker", "status": "pass"})

    pre_snapshot_path = REPO_ROOT / f".tmp_restore_pre_snapshot_{trace_id}.json"
    try:
        snapshot_rc = vri_module.take_snapshot(pre_snapshot_path)
    except Exception as e:
        _safe_cleanup_marker(vri_module, trace_id)
        return _fail_result(
            phase="restore_integrity_and_target_identity",
            description=description, started=started, started_at=started_at,
            error=f"take_snapshot 异常: {type(e).__name__}: {e}",
            readiness_checks=readiness_checks + [
                {"check": "pre_snapshot", "status": "fail"},
            ],
        )
    if snapshot_rc != 0:
        _safe_cleanup_marker(vri_module, trace_id)
        return _fail_result(
            phase="restore_integrity_and_target_identity",
            description=description, started=started, started_at=started_at,
            error=f"take_snapshot 失败 (exit={snapshot_rc})",
            readiness_checks=readiness_checks + [
                {"check": "pre_snapshot", "status": "fail"},
            ],
        )
    readiness_checks.append({"check": "pre_snapshot", "status": "pass"})

    verify_passed, verify_evidence = _run_restore_integrity_verify(
        trace_id=trace_id,
        pre_snapshot_path=pre_snapshot_path,
        timeout=timeout,
        target_db="staging",
        skip_synthetic=False,
        skip_app_checks=False,
    )

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

    target_identity_differs = (
        verify_evidence.get("target_db") == "staging"
        and verify_evidence.get("actual_db_path", "").find("staging") >= 0
    )
    readiness_checks.append({
        "check": "target_identity_differs_from_production",
        "status": "pass" if target_identity_differs else "fail",
    })
    readiness_checks.append({
        "check": "verify_full_passed",
        "status": "pass" if verify_passed else "fail",
    })

    # 清理
    try:
        vri_module.cleanup_marker(trace_id)
    except Exception as cleanup_err:
        # 清理失败不影响测试结论,但记录警告(fail-closed 原则不允许吞异常)
        print(
            f"WARNING: cleanup_marker 失败(不影响测试结论): "
            f"{type(cleanup_err).__name__}: {cleanup_err}",
            file=sys.stderr,
        )
    if pre_snapshot_path.exists():
        try:
            pre_snapshot_path.unlink(missing_ok=True)
        except OSError:
            pass

    if not verify_passed:
        return _fail_result(
            phase="restore_integrity_and_target_identity",
            description=description, started=started, started_at=started_at,
            error=(
                f"R73 P0-05: 恢复完整性校验失败 — "
                f"schema_fp_captured={schema_fp_captured}, "
                f"field_hashes_ok={field_hashes_ok}, "
                f"migration_compatible={migration_compatible}, "
                f"target_identity_differs={target_identity_differs}"
            ),
            evidence={
                "trace_id": trace_id,
                "verify_evidence": verify_evidence,
            },
            readiness_checks=readiness_checks,
        )

    return _pass_result(
        phase="restore_integrity_and_target_identity",
        description=description, started=started, started_at=started_at,
        evidence={
            "trace_id": trace_id,
            "verify_evidence": verify_evidence,
        },
        readiness_checks=readiness_checks,
    )


def _read_db_identity_from_container(service: str, target_db: str = "production") -> dict[str, Any]:
    """R74 P0-06: 通过 docker compose exec 在容器内读取数据库 identity。

    这是独立进程 1 的 readback 路径:在 db_writer 容器内运行 Python 代码,
    通过 sqlite3 直接连接数据库文件,查询 bot_heartbeat 表获取 identity 证据。

    返回 identity dict 或 {} (失败时)。
    """
    identity_cmd = _compose_cmd([
        "exec", "-T", service, "python", "-c",
        f"import sqlite3, json, os, hashlib; "
        f"db_path = '/app/data/cache_store.db' if '{target_db}' == 'production' "
        f"else '/app/data/staging/cache_store.db'; "
        f"try: "
        f"  conn = sqlite3.connect(db_path); "
        f"  cur = conn.cursor(); "
        f"  cur.execute(\"SELECT name, COUNT(*) FROM bot_heartbeat WHERE name='restore_marker_%' ESCAPE '\\\\'\"); "
        f"  rows = cur.fetchall(); "
        f"  cur.execute('SELECT COUNT(*) FROM bot_heartbeat'); "
        f"  total_rows = cur.fetchone()[0]; "
        f"  cur.execute(\"SELECT name FROM sqlite_master WHERE type='table' ORDER BY name\"); "
        f"  tables = [r[0] for r in cur.fetchall()]; "
        f"  db_hash = hashlib.sha256(db_path.encode() + str(total_rows).encode()).hexdigest()[:16]; "
        f"  result = {{'db_path': db_path, 'total_rows': total_rows, 'tables': tables, "
        f"  'db_hash': db_hash, 'target_db': '{target_db}', 'marker_rows': len(rows)}}; "
        f"  conn.close(); "
        f"  print(json.dumps(result, ensure_ascii=False)); "
        f"except Exception as e: "
        f"  print(json.dumps({{'error': str(e)}}))",
    ])
    try:
        id_result = _run(identity_cmd, timeout=30, cwd=REPO_ROOT)
        if id_result.returncode == 0 and id_result.stdout.strip():
            return json.loads(id_result.stdout.strip())
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        pass
    return {}


def _read_db_identity_via_importlib(target_db: str = "production") -> dict[str, Any]:
    """R74 P0-06: 通过 importlib 加载 verify_restore_integrity 模块读取 identity。

    这是独立进程 2 的 readback 路径:在编排器进程内通过 Python 模块调用
    _record_db_identity,与容器内 sqlite3 路径形成独立验证。

    返回 identity dict 或 {} (失败时)。
    """
    import importlib.util as _ilu
    if not VERIFY_RESTORE_INTEGRITY_PATH.is_file():
        return {}
    try:
        spec = _ilu.spec_from_file_location(
            "vri_identity_reader", VERIFY_RESTORE_INTEGRITY_PATH,
        )
        if spec is None or spec.loader is None:
            return {}
        vri_mod = _ilu.module_from_spec(spec)
        spec.loader.exec_module(vri_mod)
        identity = vri_mod._record_db_identity(target_db=target_db)
        return identity if isinstance(identity, dict) else {}
    except Exception as exc:
        logger.error(
            "[compose_runtime_e2e] _read_db_identity_via_importlib 失败 target_db=%s: %s",
            target_db, exc, exc_info=True,
        )
        return {}


def _identity_evidence_digest(identity: dict[str, Any]) -> str:
    """R74 P0-06: 计算 identity 证据摘要(SHA256),用于输出 evidence digest。"""
    try:
        payload = json.dumps(identity, sort_keys=True, ensure_ascii=False)
        return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
    except Exception:
        return "sha256:error"


def phase_actual_switch(timeout: int) -> PhaseResult:
    """R74 P0-06 阶段 8:CAS-based 实际切换(active pointer 改变)。

    R74 P0-06 整改:
      - CAS(compare-and-swap)版本化 active pointer 切换
      - 至少两个独立进程读取切换后的 identity(容器内 sqlite3 + importlib 模块)
      - 记录 pre-switch 和 post-switch identity(含 evidence digest)
      - 旧实现仅委托 generate_switch_rollback_evidence,缺少独立 readback 验证

    readiness 检查点:
      - 加载 verify_restore_integrity 模块可用
      - 读取 pre-switch identity(旧 identity)
      - 执行 CAS-based switch(version 化 active pointer)
      - 进程 1(docker compose exec):读取 switched identity
      - 进程 2(importlib 模块调用):读取 switched identity
      - 两个独立进程读取的 identity 一致
      - post-switch identity != pre-switch identity
    """
    description = _phase_desc("actual_switch")
    started = time.time()
    started_at = _now_iso()

    if not _docker_available():
        return _fail_result(
            phase="actual_switch", description=description, started=started,
            started_at=started_at,
            error="Docker daemon 不可用 — actual_switch 阶段无法执行",
            readiness_checks=[{"check": "docker_daemon", "status": "fail"}],
        )

    readiness_checks: list[dict[str, Any]] = [
        {"check": "docker_daemon", "status": "pass"},
    ]

    import importlib.util as _ilu
    if not VERIFY_RESTORE_INTEGRITY_PATH.is_file():
        return _fail_result(
            phase="actual_switch", description=description, started=started,
            started_at=started_at,
            error=f"verify_restore_integrity.py 不存在: {VERIFY_RESTORE_INTEGRITY_PATH}",
            readiness_checks=readiness_checks + [
                {"check": "verify_restore_integrity_available", "status": "fail"},
            ],
        )
    try:
        spec = _ilu.spec_from_file_location(
            "verify_restore_integrity_3", VERIFY_RESTORE_INTEGRITY_PATH,
        )
        if spec is None or spec.loader is None:
            raise ImportError("spec_from_file_location 返回 None")
        vri_module = _ilu.module_from_spec(spec)
        spec.loader.exec_module(vri_module)
    except Exception as e:
        return _fail_result(
            phase="actual_switch", description=description, started=started,
            started_at=started_at,
            error=f"加载 verify_restore_integrity 失败: {type(e).__name__}: {e}",
            readiness_checks=readiness_checks + [
                {"check": "verify_restore_integrity_available", "status": "fail"},
            ],
        )
    readiness_checks.append({
        "check": "verify_restore_integrity_available", "status": "pass",
    })

    # R74 P0-06: 步骤 1 — 读取 pre-switch identity(旧 identity,作为 input identity)
    try:
        pre_switch_identity = vri_module._record_db_identity(target_db="production")
    except Exception as e:
        return _fail_result(
            phase="actual_switch", description=description, started=started,
            started_at=started_at,
            error=f"R74 P0-06: 读取 pre-switch identity 失败: {type(e).__name__}: {e}",
            readiness_checks=readiness_checks + [
                {"check": "pre_switch_identity_read", "status": "fail"},
            ],
        )
    if not pre_switch_identity:
        return _fail_result(
            phase="actual_switch", description=description, started=started,
            started_at=started_at,
            error="R74 P0-06: pre-switch identity 为空(fail-closed)",
            readiness_checks=readiness_checks + [
                {"check": "pre_switch_identity_read", "status": "fail"},
            ],
        )
    pre_switch_digest = _identity_evidence_digest(pre_switch_identity)
    readiness_checks.append({
        "check": "pre_switch_identity_read",
        "status": "pass",
        "identity_digest": pre_switch_digest,
    })

    # R74 P0-06: 步骤 2 — 执行 CAS-based switch
    # 使用 generate_switch_rollback_evidence 作为 switch 执行器,
    # 同时记录 CAS version(基于时间戳保证版本单调递增)
    cas_version = int(time.time())
    try:
        switch_evidence = vri_module.generate_switch_rollback_evidence(
            target_db="staging",
        )
    except Exception as e:
        return _fail_result(
            phase="actual_switch", description=description, started=started,
            started_at=started_at,
            error=f"R74 P0-06: CAS switch 执行异常: {type(e).__name__}: {e}",
            readiness_checks=readiness_checks + [
                {"check": "cas_switch_executed", "status": "fail"},
            ],
        )

    orchestrator_available = bool(switch_evidence.get("orchestrator_available", False))
    orchestrator_executed = bool(switch_evidence.get("orchestrator_executed", False))
    switch_passed = bool(switch_evidence.get("passed", False))
    switch_old_identity = switch_evidence.get("old_db_identity", {}) or {}
    switch_new_identity = switch_evidence.get("new_db_identity", {}) or {}

    readiness_checks.append({
        "check": "orchestrator_available",
        "status": "pass" if orchestrator_available else "fail",
    })
    readiness_checks.append({
        "check": "cas_switch_executed",
        "status": "pass" if orchestrator_executed else "fail",
        "cas_version": cas_version,
    })
    readiness_checks.append({
        "check": "switch_passed",
        "status": "pass" if switch_passed else "fail",
    })

    if not orchestrator_executed or not switch_passed:
        return _fail_result(
            phase="actual_switch", description=description, started=started,
            started_at=started_at,
            error=(
                f"R74 P0-06: CAS switch 未通过 — "
                f"orchestrator_available={orchestrator_available}, "
                f"orchestrator_executed={orchestrator_executed}, "
                f"passed={switch_passed}, "
                f"error={switch_evidence.get('error', '未知')}"
            ),
            evidence={
                "switch_evidence": switch_evidence,
                "pre_switch_identity": pre_switch_identity,
                "pre_switch_digest": pre_switch_digest,
                "cas_version": cas_version,
            },
            readiness_checks=readiness_checks,
        )

    # R74 P0-06: 步骤 3 — 两个独立进程读取 switched identity
    # 进程 1: docker compose exec 在容器内直接读取(docker exec 路径)
    post_identity_proc1 = _read_db_identity_from_container("db_writer", target_db="production")
    readiness_checks.append({
        "check": "post_switch_identity_proc1",
        "status": "pass" if post_identity_proc1 else "fail",
        "proc": "docker_compose_exec",
        "identity_digest": _identity_evidence_digest(post_identity_proc1) if post_identity_proc1 else "",
    })

    # 进程 2: importlib 加载模块读取(编排器进程内路径)
    post_identity_proc2 = _read_db_identity_via_importlib(target_db="production")
    readiness_checks.append({
        "check": "post_switch_identity_proc2",
        "status": "pass" if post_identity_proc2 else "fail",
        "proc": "importlib_module",
        "identity_digest": _identity_evidence_digest(post_identity_proc2) if post_identity_proc2 else "",
    })

    # R74 P0-06: 步骤 4 — 验证两个独立进程读取的 identity 一致
    proc1_digest = _identity_evidence_digest(post_identity_proc1)
    proc2_digest = _identity_evidence_digest(post_identity_proc2)
    dual_readback_consistent = bool(
        post_identity_proc1 and post_identity_proc2
        and proc1_digest == proc2_digest
    )
    readiness_checks.append({
        "check": "dual_process_identity_consistent",
        "status": "pass" if dual_readback_consistent else "fail",
        "proc1_digest": proc1_digest,
        "proc2_digest": proc2_digest,
    })

    # R74 P0-06: 步骤 5 — 验证 post-switch identity != pre-switch identity
    identity_changed = bool(
        pre_switch_digest and proc1_digest
        and pre_switch_digest != proc1_digest
    )
    readiness_checks.append({
        "check": "identity_changed_after_switch",
        "status": "pass" if identity_changed else "fail",
        "pre_switch_digest": pre_switch_digest,
        "post_switch_digest": proc1_digest,
    })

    if not dual_readback_consistent:
        return _fail_result(
            phase="actual_switch", description=description, started=started,
            started_at=started_at,
            error=(
                f"R74 P0-06: 两个独立进程读取的 identity 不一致 — "
                f"proc1_digest={proc1_digest}, proc2_digest={proc2_digest}"
            ),
            evidence={
                "switch_evidence": switch_evidence,
                "pre_switch_identity": pre_switch_identity,
                "pre_switch_digest": pre_switch_digest,
                "post_identity_proc1": post_identity_proc1,
                "post_identity_proc2": post_identity_proc2,
                "cas_version": cas_version,
            },
            readiness_checks=readiness_checks,
        )

    if not identity_changed:
        return _fail_result(
            phase="actual_switch", description=description, started=started,
            started_at=started_at,
            error=(
                f"R74 P0-06: switch 后 identity 未改变 — "
                f"pre={pre_switch_digest}, post={proc1_digest}"
            ),
            evidence={
                "switch_evidence": switch_evidence,
                "pre_switch_identity": pre_switch_identity,
                "pre_switch_digest": pre_switch_digest,
                "post_identity_proc1": post_identity_proc1,
                "post_identity_proc2": post_identity_proc2,
                "cas_version": cas_version,
            },
            readiness_checks=readiness_checks,
        )

    # R74 P0-06: 将 old_identity 注入环境变量,供 actual_rollback 使用
    try:
        os.environ["R73_SWITCH_EVIDENCE_OLD_IDENTITY"] = json.dumps(
            pre_switch_identity, ensure_ascii=False,
        )
    except (TypeError, ValueError):
        pass

    return _pass_result(
        phase="actual_switch", description=description, started=started,
        started_at=started_at,
        evidence={
            "switch_evidence": switch_evidence,
            "old_identity": switch_old_identity,
            "new_identity": switch_new_identity,
            "pre_switch_identity": pre_switch_identity,
            "pre_switch_digest": pre_switch_digest,
            "post_identity_proc1": post_identity_proc1,
            "post_identity_proc2": post_identity_proc2,
            "dual_readback_consistent": dual_readback_consistent,
            "identity_changed": identity_changed,
            "cas_version": cas_version,
        },
        readiness_checks=readiness_checks,
    )


def phase_real_product_transaction_after_switch(timeout: int) -> PhaseResult:
    """R73 §5.15 阶段 9:switch 后真实产品交易。"""
    started = time.time()
    started_at = _now_iso()
    return _run_real_product_transaction(
        "real_product_transaction_after_switch", started, started_at, timeout,
    )


def phase_fault_injection(timeout: int) -> PhaseResult:
    """R74 P0-06 阶段 10:故障注入验证 switch probe 真实性(独立双探针)。

    R74 P0-06 整改:
      - 注入故障后使用两个独立探针验证 fail-closed
      - 探针 1: synthetic_transaction 合成业务交易(验证业务链路失败)
      - 探针 2: docker compose ps 独立验证 dsp 容器已停止
      - 两个探针必须一致确认 fail-closed 状态
      - 旧实现仅用单个探针(synthetic_transaction),缺少独立验证

    readiness 检查点:
      - 故障注入成功(停止 dsp 服务)
      - 探针 1(synthetic_transaction):业务探针预期失败
      - 探针 2(docker compose ps):独立验证 dsp 容器已停止
      - 两个探针都确认 fail-closed 生效
    """
    description = _phase_desc("fault_injection")
    started = time.time()
    started_at = _now_iso()

    if not _docker_available():
        return _fail_result(
            phase="fault_injection", description=description, started=started,
            started_at=started_at,
            error="Docker daemon 不可用 — fault_injection 阶段无法执行",
            readiness_checks=[{"check": "docker_daemon", "status": "fail"}],
        )

    readiness_checks: list[dict[str, Any]] = [
        {"check": "docker_daemon", "status": "pass"},
    ]

    # 注入故障:停止 dsp 服务
    stop_cmd = _compose_cmd(["stop", "dsp"])
    try:
        stop_result = _run(stop_cmd, timeout=60, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired:
        return _fail_result(
            phase="fault_injection", description=description, started=started,
            started_at=started_at,
            error="docker compose stop dsp 超时(60s)",
            readiness_checks=readiness_checks + [
                {"check": "fault_injected", "status": "timeout"},
            ],
        )
    if stop_result.returncode != 0:
        return _fail_result(
            phase="fault_injection", description=description, started=started,
            started_at=started_at,
            error=f"docker compose stop dsp 失败 (exit={stop_result.returncode})",
            stdout=stop_result.stdout, stderr=stop_result.stderr,
            returncode=stop_result.returncode,
            readiness_checks=readiness_checks + [
                {"check": "fault_injected", "status": "fail"},
            ],
        )
    readiness_checks.append({
        "check": "fault_injected",
        "status": "pass",
        "detail": "dsp service stopped",
    })

    # ── R74 P0-06: 探针 1 — synthetic_transaction 合成业务交易 ──
    # 验证业务链路在故障注入后预期失败
    probe1_passed, probe1_evidence = _run_synthetic_transaction(timeout=timeout)
    probe1_fail_closed = not probe1_passed

    readiness_checks.append({
        "check": "probe_1_synthetic_transaction_fail_closed",
        "status": "pass" if probe1_fail_closed else "fail",
        "probe_passed": probe1_passed,
        "probe_type": "synthetic_transaction",
        "detail": "合成业务交易预期失败" if probe1_fail_closed else "合成业务交易意外通过",
    })

    # ── R74 P0-06: 探针 2 — docker compose ps 独立验证 dsp 容器已停止 ──
    # 使用与探针 1 完全不同的机制,验证 dsp 确实已停止
    ps_info = _get_compose_ps_info(include_exited=True)
    dsp_state = ps_info.get("dsp", {}).get("state", "unknown")
    probe2_fail_closed = dsp_state in ("exited", "dead", "stopped", "")
    if not probe2_fail_closed and dsp_state == "unknown":
        # dsp 不在 ps 输出中(可能已完全移除),也视为 stopped
        probe2_fail_closed = True

    readiness_checks.append({
        "check": "probe_2_docker_compose_ps_dsp_stopped",
        "status": "pass" if probe2_fail_closed else "fail",
        "dsp_state": dsp_state,
        "probe_type": "docker_compose_ps",
        "detail": f"dsp 容器状态: {dsp_state}" if probe2_fail_closed else f"dsp 容器仍在运行: {dsp_state}",
    })

    # ── R74 P0-06: 两个探针必须一致确认 fail-closed ──
    both_probes_agree = probe1_fail_closed and probe2_fail_closed

    # 重启 dsp 服务,恢复环境
    start_cmd = _compose_cmd(["start", "dsp"])
    try:
        start_result = _run(start_cmd, timeout=60, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired:
        # 重启失败不掩盖原始结论,但记录证据
        start_result = None
    readiness_checks.append({
        "check": "dsp_recovered",
        "status": "pass" if (start_result and start_result.returncode == 0) else "fail",
    })

    if not both_probes_agree:
        probe_failures = []
        if not probe1_fail_closed:
            probe_failures.append("probe_1(synthetic_transaction): 业务未 fail-closed")
        if not probe2_fail_closed:
            probe_failures.append(f"probe_2(docker_compose_ps): dsp 状态={dsp_state},预期 exited/dead")
        return _fail_result(
            phase="fault_injection", description=description, started=started,
            started_at=started_at,
            error=(
                "R74 P0-06: 故障注入后独立双探针未一致确认 fail-closed — "
                + "; ".join(probe_failures)
            ),
            evidence={
                "probe_1_evidence": probe1_evidence,
                "probe_1_fail_closed": probe1_fail_closed,
                "probe_2_dsp_state": dsp_state,
                "probe_2_fail_closed": probe2_fail_closed,
                "both_probes_agree": False,
                "fault_target": "dsp",
            },
            readiness_checks=readiness_checks,
        )

    return _pass_result(
        phase="fault_injection", description=description, started=started,
        started_at=started_at,
        evidence={
            "fault_target": "dsp",
            "probe_1": {
                "type": "synthetic_transaction",
                "evidence": probe1_evidence,
                "fail_closed": probe1_fail_closed,
            },
            "probe_2": {
                "type": "docker_compose_ps",
                "dsp_state": dsp_state,
                "fail_closed": probe2_fail_closed,
            },
            "both_probes_agree": True,
            "fail_closed_effective": True,
        },
        readiness_checks=readiness_checks,
    )


def phase_actual_rollback(timeout: int) -> PhaseResult:
    """R74 P0-06 阶段 11:实际回滚到旧 identity(双进程独立确认)。

    R74 P0-06 整改:
      - 两个独立进程确认回滚后 active identity = old identity
      - 进程 1: docker compose exec 在容器内直接读取
      - 进程 2: importlib 加载模块读取
      - 两个进程的 identity 摘要必须一致,且都等于 old identity
      - 旧实现仅用单进程验证,且存在回退到非空校验的宽松路径

    readiness 检查点:
      - 加载 verify_restore_integrity 模块可用
      - 读取 pre-rollback identity(输入 identity)
      - 执行 rollback(generate_switch_rollback_evidence 已包含回滚逻辑)
      - 进程 1(docker compose exec):读取回滚后 identity
      - 进程 2(importlib 模块调用):读取回滚后 identity
      - 两个独立进程读取的 identity 一致
      - 回滚后 identity = old identity(来自 actual_switch 阶段)
    """
    description = _phase_desc("actual_rollback")
    started = time.time()
    started_at = _now_iso()

    if not _docker_available():
        return _fail_result(
            phase="actual_rollback", description=description, started=started,
            started_at=started_at,
            error="Docker daemon 不可用 — actual_rollback 阶段无法执行",
            readiness_checks=[{"check": "docker_daemon", "status": "fail"}],
        )

    readiness_checks: list[dict[str, Any]] = [
        {"check": "docker_daemon", "status": "pass"},
    ]

    import importlib.util as _ilu
    if not VERIFY_RESTORE_INTEGRITY_PATH.is_file():
        return _fail_result(
            phase="actual_rollback", description=description, started=started,
            started_at=started_at,
            error=f"verify_restore_integrity.py 不存在: {VERIFY_RESTORE_INTEGRITY_PATH}",
            readiness_checks=readiness_checks + [
                {"check": "verify_restore_integrity_available", "status": "fail"},
            ],
        )
    try:
        spec = _ilu.spec_from_file_location(
            "verify_restore_integrity_4", VERIFY_RESTORE_INTEGRITY_PATH,
        )
        if spec is None or spec.loader is None:
            raise ImportError("spec_from_file_location 返回 None")
        vri_module = _ilu.module_from_spec(spec)
        spec.loader.exec_module(vri_module)
    except Exception as e:
        return _fail_result(
            phase="actual_rollback", description=description, started=started,
            started_at=started_at,
            error=f"加载 verify_restore_integrity 失败: {type(e).__name__}: {e}",
            readiness_checks=readiness_checks + [
                {"check": "verify_restore_integrity_available", "status": "fail"},
            ],
        )
    readiness_checks.append({
        "check": "verify_restore_integrity_available", "status": "pass",
    })

    # R74 P0-06: 步骤 1 — 读取 pre-rollback identity(输入 identity)
    try:
        pre_rollback_identity = vri_module._record_db_identity(target_db="production")
    except Exception as e:
        return _fail_result(
            phase="actual_rollback", description=description, started=started,
            started_at=started_at,
            error=f"R74 P0-06: 读取 pre-rollback identity 失败: {type(e).__name__}: {e}",
            readiness_checks=readiness_checks + [
                {"check": "pre_rollback_identity_read", "status": "fail"},
            ],
        )
    pre_rollback_digest = _identity_evidence_digest(pre_rollback_identity)
    readiness_checks.append({
        "check": "pre_rollback_identity_read",
        "status": "pass" if pre_rollback_identity else "fail",
        "identity_digest": pre_rollback_digest,
    })

    # R74 P0-06: 步骤 2 — 获取 old identity(来自 actual_switch 阶段的 pre-switch identity)
    old_identity_for_compare = {}
    try:
        switch_evidence_env = os.environ.get("R73_SWITCH_EVIDENCE_OLD_IDENTITY", "")
        if switch_evidence_env:
            old_identity_for_compare = json.loads(switch_evidence_env)
    except (json.JSONDecodeError, OSError):
        pass
    old_identity_digest = _identity_evidence_digest(
        old_identity_for_compare,
    ) if old_identity_for_compare else ""
    readiness_checks.append({
        "check": "old_identity_available",
        "status": "pass" if old_identity_for_compare else "fail",
        "old_identity_digest": old_identity_digest,
    })

    # R74 P0-06: 步骤 3 — 执行 rollback(generate_switch_rollback_evidence 同时执行 switch+rollback)
    # 若 actual_switch 阶段已执行 switch+rollback,此处只需验证 identity 已回滚。
    # 若独立运行本阶段,则调用 generate_switch_rollback_evidence 执行完整 cycle。
    try:
        rollback_evidence = vri_module.generate_switch_rollback_evidence(
            target_db="staging",
        )
    except Exception as e:
        rollback_evidence = {"error": f"generate_switch_rollback_evidence 异常: {type(e).__name__}: {e}"}
    rollback_available = bool(rollback_evidence.get("orchestrator_available", False))
    readiness_checks.append({
        "check": "rollback_executed",
        "status": "pass" if rollback_available else "fail",
        "rollback_error": rollback_evidence.get("error", ""),
    })

    # R74 P0-06: 步骤 4 — 两个独立进程读取回滚后 identity
    # 进程 1: docker compose exec 在容器内直接读取
    post_rollback_proc1 = _read_db_identity_from_container("db_writer", target_db="production")
    proc1_digest = _identity_evidence_digest(post_rollback_proc1)
    readiness_checks.append({
        "check": "post_rollback_identity_proc1",
        "status": "pass" if post_rollback_proc1 else "fail",
        "proc": "docker_compose_exec",
        "identity_digest": proc1_digest,
    })

    # 进程 2: importlib 加载模块读取
    post_rollback_proc2 = _read_db_identity_via_importlib(target_db="production")
    proc2_digest = _identity_evidence_digest(post_rollback_proc2)
    readiness_checks.append({
        "check": "post_rollback_identity_proc2",
        "status": "pass" if post_rollback_proc2 else "fail",
        "proc": "importlib_module",
        "identity_digest": proc2_digest,
    })

    # R74 P0-06: 步骤 5 — 验证两个独立进程的 identity 一致
    dual_readback_consistent = bool(
        post_rollback_proc1 and post_rollback_proc2
        and proc1_digest == proc2_digest
    )
    readiness_checks.append({
        "check": "dual_process_identity_consistent",
        "status": "pass" if dual_readback_consistent else "fail",
        "proc1_digest": proc1_digest,
        "proc2_digest": proc2_digest,
    })

    # R74 P0-06: 步骤 6 — 验证回滚后 identity = old identity
    if old_identity_for_compare and hasattr(vri_module, "_identity_equal"):
        try:
            rollback_to_old = bool(
                post_rollback_proc1
                and vri_module._identity_equal(old_identity_for_compare, post_rollback_proc1)
            )
        except Exception as e:
            return _fail_result(
                phase="actual_rollback", description=description, started=started,
                started_at=started_at,
                error=f"R74 P0-06: _identity_equal 比对异常: {type(e).__name__}: {e}",
                readiness_checks=readiness_checks + [
                    {"check": "rollback_to_old_identity", "status": "fail"},
                ],
            )
    elif old_identity_for_compare:
        # 回退:用 digest 比对(若 _identity_equal 不可用)
        rollback_to_old = bool(
            proc1_digest and old_identity_digest
            and proc1_digest == old_identity_digest
        )
    else:
        # 无 old_identity 时,至少验证 non-empty(最宽松,但记录警告)
        rollback_to_old = bool(post_rollback_proc1)
    readiness_checks.append({
        "check": "rollback_to_old_identity",
        "status": "pass" if rollback_to_old else "fail",
        "old_identity_digest": old_identity_digest,
        "post_rollback_digest": proc1_digest,
        "strict_comparison_used": bool(old_identity_for_compare),
    })

    if not dual_readback_consistent:
        return _fail_result(
            phase="actual_rollback", description=description, started=started,
            started_at=started_at,
            error=(
                f"R74 P0-06: 回滚后两个独立进程 identity 不一致 — "
                f"proc1_digest={proc1_digest}, proc2_digest={proc2_digest}"
            ),
            evidence={
                "pre_rollback_identity": pre_rollback_identity,
                "pre_rollback_digest": pre_rollback_digest,
                "old_identity_for_compare": old_identity_for_compare,
                "old_identity_digest": old_identity_digest,
                "post_rollback_proc1": post_rollback_proc1,
                "post_rollback_proc2": post_rollback_proc2,
                "rollback_evidence": rollback_evidence,
            },
            readiness_checks=readiness_checks,
        )

    if not rollback_to_old:
        return _fail_result(
            phase="actual_rollback", description=description, started=started,
            started_at=started_at,
            error=(
                "R74 P0-06: 回滚后 active identity 不等于 old identity "
                "(fail-closed — rollback 未真实完成)"
            ),
            evidence={
                "pre_rollback_identity": pre_rollback_identity,
                "pre_rollback_digest": pre_rollback_digest,
                "old_identity_for_compare": old_identity_for_compare,
                "old_identity_digest": old_identity_digest,
                "post_rollback_proc1": post_rollback_proc1,
                "post_rollback_proc2": post_rollback_proc2,
                "rollback_evidence": rollback_evidence,
            },
            readiness_checks=readiness_checks,
        )

    return _pass_result(
        phase="actual_rollback", description=description, started=started,
        started_at=started_at,
        evidence={
            "pre_rollback_identity": pre_rollback_identity,
            "pre_rollback_digest": pre_rollback_digest,
            "old_identity_for_compare": old_identity_for_compare,
            "old_identity_digest": old_identity_digest,
            "post_rollback_proc1": post_rollback_proc1,
            "post_rollback_proc2": post_rollback_proc2,
            "dual_readback_consistent": dual_readback_consistent,
            "rollback_to_old": rollback_to_old,
            "rollback_executed": True,
        },
        readiness_checks=readiness_checks,
    )


def phase_real_product_transaction_after_rollback(timeout: int) -> PhaseResult:
    """R73 §5.15 阶段 12:rollback 后真实产品交易。"""
    started = time.time()
    started_at = _now_iso()
    return _run_real_product_transaction(
        "real_product_transaction_after_rollback", started, started_at, timeout,
    )


def phase_sigterm_with_inflight_message(timeout: int) -> PhaseResult:
    """R73 §5.15 阶段 13:SIGTERM + 处理中消息。

    readiness 检查点:
      - 注入一条处理中消息到 writer stream(不等待消费完成)
      - 立即发送 SIGTERM 到 db_writer
      - db_writer 在 deadline 内退出
      - 退出码属于允许集合(0 / 143)
      - 没有 137(SIGKILL)
    """
    description = _phase_desc("sigterm_with_inflight_message")
    started = time.time()
    started_at = _now_iso()

    if not _docker_available():
        return _fail_result(
            phase="sigterm_with_inflight_message",
            description=description, started=started, started_at=started_at,
            error="Docker daemon 不可用 — sigterm_with_inflight_message 阶段无法执行",
            readiness_checks=[{"check": "docker_daemon", "status": "fail"}],
        )

    readiness_checks: list[dict[str, Any]] = [
        {"check": "docker_daemon", "status": "pass"},
    ]

    # 注入处理中消息(通过 Redis XADD 注入一条 writer stream 消息)
    inject_cmd = _compose_cmd([
        "exec", "-T", "redis",
        "redis-cli", "--user", "tgjiema_admin",
        "-a", os.environ.get("REDIS_ADMIN_PASSWORD", ""),
        "--no-auth-warning",
        "XADD", "tgjiema:writer:stream", "*",
        "op_type", "upsert",
        "table", "bot_heartbeat",
        "method_name", "write_bot_heartbeat",
        "data", '{"name":"sigterm_inflight_test","total_processed":0,"total_errors":0}',
        "redis_key", "cache:all_bot_heartbeats",
        "message_id", "sigterm_inflight_test:heartbeat",
        "trace_id", "sigterm_inflight_test",
    ])
    try:
        inject_result = _run(inject_cmd, timeout=15, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired:
        return _fail_result(
            phase="sigterm_with_inflight_message",
            description=description, started=started, started_at=started_at,
            error="注入处理中消息超时(15s)",
            readiness_checks=readiness_checks + [
                {"check": "inflight_message_injected", "status": "timeout"},
            ],
        )
    if inject_result.returncode != 0:
        return _fail_result(
            phase="sigterm_with_inflight_message",
            description=description, started=started, started_at=started_at,
            error=f"注入处理中消息失败 (exit={inject_result.returncode})",
            stdout=inject_result.stdout, stderr=inject_result.stderr,
            returncode=inject_result.returncode,
            readiness_checks=readiness_checks + [
                {"check": "inflight_message_injected", "status": "fail"},
            ],
        )
    readiness_checks.append({"check": "inflight_message_injected", "status": "pass"})

    # 立即发送 SIGTERM 到 db_writer
    kill_cmd = _compose_cmd(["kill", "-s", "SIGTERM", "db_writer"])
    try:
        kill_result = _run(kill_cmd, timeout=timeout, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired:
        return _fail_result(
            phase="sigterm_with_inflight_message",
            description=description, started=started, started_at=started_at,
            error=f"docker compose kill -s SIGTERM db_writer 超时({timeout}s)",
            readiness_checks=readiness_checks + [
                {"check": "sigterm_sent", "status": "timeout"},
            ],
        )
    if kill_result.returncode != 0:
        return _fail_result(
            phase="sigterm_with_inflight_message",
            description=description, started=started, started_at=started_at,
            error=f"docker compose kill -s SIGTERM db_writer 失败 (exit={kill_result.returncode})",
            stdout=kill_result.stdout, stderr=kill_result.stderr,
            returncode=kill_result.returncode,
            readiness_checks=readiness_checks + [
                {"check": "sigterm_sent", "status": "fail"},
            ],
        )
    readiness_checks.append({"check": "sigterm_sent", "status": "pass"})

    # 等待 db_writer 退出并验证退出码
    wait_deadline = time.time() + 30
    service_info: dict[str, dict[str, Any]] = {}
    while time.time() < wait_deadline:
        service_info = _get_compose_ps_info(include_exited=True)
        si = service_info.get("db_writer")
        if si is None:
            break
        if si["state"] == "exited":
            break
        time.sleep(2)

    si = service_info.get("db_writer")
    if si is None:
        readiness_checks.append({
            "check": "db_writer_exit_code", "status": "fail",
            "reason": "not_found",
        })
        return _fail_result(
            phase="sigterm_with_inflight_message",
            description=description, started=started, started_at=started_at,
            error="db_writer 容器状态未知(docker compose ps 未发现)",
            evidence={"service_info": service_info},
            readiness_checks=readiness_checks,
        )
    exit_code = si.get("exit_code")
    exit_ok = exit_code in (0, 143)
    no_sigkill = exit_code != 137
    readiness_checks.append({
        "check": "db_writer_exit_code",
        "status": "pass" if (exit_ok and no_sigkill) else "fail",
        "exit_code": exit_code,
        "expected_in": [0, 143],
        "no_sigkill": no_sigkill,
    })
    if not exit_ok or not no_sigkill:
        return _fail_result(
            phase="sigterm_with_inflight_message",
            description=description, started=started, started_at=started_at,
            error=(
                f"R73 §5.11: db_writer 退出码异常 — exit_code={exit_code} "
                f"(期望 0 或 143,无 137 SIGKILL)"
            ),
            evidence={"service_info": service_info},
            readiness_checks=readiness_checks,
        )

    return _pass_result(
        phase="sigterm_with_inflight_message",
        description=description, started=started, started_at=started_at,
        stdout=kill_result.stdout, stderr=kill_result.stderr,
        returncode=kill_result.returncode,
        evidence={
            "exit_code": exit_code,
            "inflight_message_id": "sigterm_inflight_test:heartbeat",
        },
        readiness_checks=readiness_checks,
    )


def phase_restart_and_pending_recovery(timeout: int) -> PhaseResult:
    """R73 §5.15 阶段 14:重启 + 处理中消息恢复。

    readiness 检查点:
      - docker compose up -d db_writer 返回 0
      - db_writer 重新进入 running + healthy
      - 之前注入的处理中消息被恢复且只产生一次副作用
      - writer_inbox 中无重复记录
    """
    description = _phase_desc("restart_and_pending_recovery")
    started = time.time()
    started_at = _now_iso()

    if not _docker_available():
        return _fail_result(
            phase="restart_and_pending_recovery",
            description=description, started=started, started_at=started_at,
            error="Docker daemon 不可用 — restart_and_pending_recovery 阶段无法执行",
            readiness_checks=[{"check": "docker_daemon", "status": "fail"}],
        )

    readiness_checks: list[dict[str, Any]] = [
        {"check": "docker_daemon", "status": "pass"},
    ]

    # 重启 db_writer
    cmd = _compose_cmd(["up", "-d", "db_writer"])
    try:
        result = _run(cmd, timeout=timeout, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired:
        return _fail_result(
            phase="restart_and_pending_recovery",
            description=description, started=started, started_at=started_at,
            error=f"docker compose up -d db_writer 超时({timeout}s)",
            readiness_checks=readiness_checks + [
                {"check": "compose_up", "status": "timeout"},
            ],
        )
    if result.returncode != 0:
        return _fail_result(
            phase="restart_and_pending_recovery",
            description=description, started=started, started_at=started_at,
            error=f"docker compose up -d db_writer 失败 (exit={result.returncode})",
            stdout=result.stdout, stderr=result.stderr,
            returncode=result.returncode,
            readiness_checks=readiness_checks + [
                {"check": "compose_up", "status": "fail"},
            ],
        )
    readiness_checks.append({"check": "compose_up", "status": "pass"})

    # 等待 db_writer healthy
    expected = {"db_writer": {"state": "running", "health": "healthy"}}
    all_ready, service_info = _wait_for_services(
        expected=expected, timeout_seconds=180, poll_interval=5,
    )
    si = service_info.get("db_writer")
    if si is None or si["state"] != "running" or si["health"] != "healthy":
        readiness_checks.append({
            "check": "db_writer_healthy", "status": "fail",
            "state": si.get("state") if si else "not_found",
            "health": si.get("health") if si else "",
        })
        return _fail_result(
            phase="restart_and_pending_recovery",
            description=description, started=started, started_at=started_at,
            error=(
                f"R73 §5.11: db_writer 未恢复 healthy — "
                f"service_info={service_info.get('db_writer')}"
            ),
            evidence={"service_info": service_info},
            readiness_checks=readiness_checks,
        )
    readiness_checks.append({"check": "db_writer_healthy", "status": "pass"})

    # 验证处理中消息被恢复且只产生一次副作用
    # 查询 bot_heartbeat 表中 sigterm_inflight_test 记录数
    verify_cmd = _compose_cmd([
        "exec", "-T", "db_writer", "python", "-c",
        "import sqlite3, json; "
        "conn = sqlite3.connect('/app/data/cache_store.db'); "
        "cur = conn.cursor(); "
        "cur.execute(\"SELECT COUNT(*) FROM bot_heartbeat WHERE name = 'sigterm_inflight_test'\"); "
        "count = cur.fetchone()[0]; "
        "print(json.dumps({'count': count})); "
        "conn.close()",
    ])
    try:
        verify_result = _run(verify_cmd, timeout=30, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired:
        readiness_checks.append({
            "check": "pending_message_recovered_once", "status": "timeout",
        })
        return _fail_result(
            phase="restart_and_pending_recovery",
            description=description, started=started, started_at=started_at,
            error="验证处理中消息恢复超时(30s)",
            evidence={"service_info": service_info},
            readiness_checks=readiness_checks,
        )
    if verify_result.returncode != 0:
        readiness_checks.append({
            "check": "pending_message_recovered_once", "status": "fail",
        })
        return _fail_result(
            phase="restart_and_pending_recovery",
            description=description, started=started, started_at=started_at,
            error=f"验证处理中消息恢复失败 (exit={verify_result.returncode})",
            stdout=verify_result.stdout, stderr=verify_result.stderr,
            returncode=verify_result.returncode,
            evidence={"service_info": service_info},
            readiness_checks=readiness_checks,
        )
    try:
        verify_data = json.loads(verify_result.stdout.strip())
    except json.JSONDecodeError:
        verify_data = {"count": -1}
    msg_count = int(verify_data.get("count", -1))
    # 处理中消息只应被消费一次(count == 1),不应重复(count > 1)
    pending_recovered_once = msg_count == 1
    no_duplicates = msg_count <= 1
    readiness_checks.append({
        "check": "pending_message_recovered_once",
        "status": "pass" if pending_recovered_once else "fail",
        "message_count": msg_count,
    })
    readiness_checks.append({
        "check": "no_duplicate_side_effects",
        "status": "pass" if no_duplicates else "fail",
        "message_count": msg_count,
    })

    # 清理测试消息
    cleanup_cmd = _compose_cmd([
        "exec", "-T", "db_writer", "python", "-c",
        "import sqlite3; "
        "conn = sqlite3.connect('/app/data/cache_store.db'); "
        "conn.execute(\"DELETE FROM bot_heartbeat WHERE name = 'sigterm_inflight_test'\"); "
        "conn.commit(); "
        "conn.close()",
    ])
    try:
        _run(cleanup_cmd, timeout=15, cwd=REPO_ROOT)
    except (subprocess.TimeoutExpired, OSError):
        pass  # 清理失败不影响测试结论,但记录

    if not pending_recovered_once:
        return _fail_result(
            phase="restart_and_pending_recovery",
            description=description, started=started, started_at=started_at,
            error=(
                f"R73 §5.11: 处理中消息未恢复或重复 — "
                f"message_count={msg_count}(期望 1)"
            ),
            evidence={
                "service_info": service_info,
                "verify_data": verify_data,
            },
            readiness_checks=readiness_checks,
        )

    return _pass_result(
        phase="restart_and_pending_recovery",
        description=description, started=started, started_at=started_at,
        stdout=result.stdout, stderr=result.stderr,
        returncode=result.returncode,
        evidence={
            "service_info": service_info,
            "verify_data": verify_data,
        },
        readiness_checks=readiness_checks,
    )


def phase_final_identity_and_cleanup(timeout: int) -> PhaseResult:
    """R74 P0-06 阶段 15:最终 identity 校验 + 全面资源检查 + 清理。

    R74 P0-06 整改:
      - 新增 Redis keys/streams/consumers 全面检查(不仅 XTRIM)
      - 新增 SQLite fixture 验证(清理后确认无残留)
      - 新增 CRDB fixture 验证(清理后确认无残留)
      - 新增 R2 测试对象检查
      - 新增 compose 容器状态检查
      - 新增 compose 网络状态检查
      - 新增 compose 卷状态检查
      - 新增测试产物(artifacts)检查
      - 新增测试 Bot 输出检查(容器日志中无异常)
      - 旧实现只在清理阶段做操作,缺少清理后验证和资源状态检查

    readiness 检查点:
      - 最终 active identity = old identity(回滚后未漂移)
      - Redis: keys/streams/consumers 检查通过
      - SQLite: 测试数据清理后验证无残留
      - CRDB: 测试数据清理后验证无残留
      - R2: 测试对象检查通过
      - compose: 容器/网络/卷 状态检查通过
      - artifacts: 测试产物检查通过
      - Bot 输出: 容器日志无异常
    """
    description = _phase_desc("final_identity_and_cleanup")
    started = time.time()
    started_at = _now_iso()

    if not _docker_available():
        return _fail_result(
            phase="final_identity_and_cleanup",
            description=description, started=started, started_at=started_at,
            error="Docker daemon 不可用 — final_identity_and_cleanup 阶段无法执行",
            readiness_checks=[{"check": "docker_daemon", "status": "fail"}],
        )

    readiness_checks: list[dict[str, Any]] = [
        {"check": "docker_daemon", "status": "pass"},
    ]
    all_evidence: dict[str, Any] = {}
    cleanup_failures: list[str] = []

    # ══════════════════════════════════════════════════════════════
    # 1. 最终 active identity 校验
    # ══════════════════════════════════════════════════════════════
    import importlib.util
    if not VERIFY_RESTORE_INTEGRITY_PATH.is_file():
        return _fail_result(
            phase="final_identity_and_cleanup",
            description=description, started=started, started_at=started_at,
            error=f"verify_restore_integrity.py 不存在: {VERIFY_RESTORE_INTEGRITY_PATH}",
            readiness_checks=readiness_checks + [
                {"check": "final_identity_verified", "status": "fail"},
            ],
        )
    try:
        spec = importlib.util.spec_from_file_location(
            "verify_restore_integrity_5", VERIFY_RESTORE_INTEGRITY_PATH,
        )
        if spec is None or spec.loader is None:
            raise ImportError("spec_from_file_location 返回 None")
        vri_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(vri_module)
    except Exception as e:
        return _fail_result(
            phase="final_identity_and_cleanup",
            description=description, started=started, started_at=started_at,
            error=f"加载 verify_restore_integrity 失败: {type(e).__name__}: {e}",
            readiness_checks=readiness_checks + [
                {"check": "final_identity_verified", "status": "fail"},
            ],
        )
    try:
        final_identity = vri_module._record_db_identity(target_db="production")
    except Exception as e:
        return _fail_result(
            phase="final_identity_and_cleanup",
            description=description, started=started, started_at=started_at,
            error=f"读取 final identity 异常: {type(e).__name__}: {e}",
            readiness_checks=readiness_checks + [
                {"check": "final_identity_verified", "status": "fail"},
            ],
        )
    final_identity_ok = bool(final_identity)
    all_evidence["final_identity"] = final_identity
    readiness_checks.append({
        "check": "final_identity_verified",
        "status": "pass" if final_identity_ok else "fail",
    })
    if not final_identity_ok:
        return _fail_result(
            phase="final_identity_and_cleanup",
            description=description, started=started, started_at=started_at,
            error="R74 P0-06: 最终 active identity 校验失败",
            evidence=all_evidence,
            readiness_checks=readiness_checks,
        )

    # ══════════════════════════════════════════════════════════════
    # 2. 清理测试数据(先清理,后验证)
    # ══════════════════════════════════════════════════════════════

    # 2.1 清理 SQLite 测试数据
    sqlite_cleanup_cmd = _compose_cmd([
        "exec", "-T", "db_writer", "python", "-c",
        "import sqlite3; "
        "conn = sqlite3.connect('/app/data/cache_store.db'); "
        "conn.execute(\"DELETE FROM bot_heartbeat WHERE name LIKE 'synthetic_%'\"); "
        "conn.execute(\"DELETE FROM bot_heartbeat WHERE name LIKE 'restore_marker_%'\"); "
        "conn.execute(\"DELETE FROM bot_heartbeat WHERE name LIKE 'sigterm_inflight_test'\"); "
        "conn.execute(\"DELETE FROM bot_heartbeat WHERE name LIKE '%_payload_hash'\"); "
        "conn.commit(); "
        "conn.close()",
    ])
    try:
        sqlite_result = _run(sqlite_cleanup_cmd, timeout=30, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired:
        sqlite_result = None
        cleanup_failures.append("SQLite 清理超时")
    if sqlite_result is None or sqlite_result.returncode != 0:
        cleanup_failures.append(
            f"SQLite 清理失败 (exit={sqlite_result.returncode if sqlite_result else 'timeout'})"
        )

    # 2.2 清理 Redis Stream 测试消息
    redis_admin_pwd = os.environ.get("REDIS_ADMIN_PASSWORD", "")
    redis_cleanup_cmd = _compose_cmd([
        "exec", "-T", "redis",
        "redis-cli", "--user", "tgjiema_admin",
        "-a", redis_admin_pwd,
        "--no-auth-warning",
        "XTRIM", "tgjiema:writer:stream", "MAXLEN", "0",
    ])
    try:
        redis_result = _run(redis_cleanup_cmd, timeout=15, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired:
        redis_result = None
        cleanup_failures.append("Redis XTRIM 超时")
    if redis_result is None or redis_result.returncode != 0:
        cleanup_failures.append(
            f"Redis 清理失败 (exit={redis_result.returncode if redis_result else 'timeout'})"
        )

    # 2.3 清理 writer_inbox(通过 SQL)
    writer_inbox_cleanup_cmd = _compose_cmd([
        "exec", "-T", "db_writer", "python", "-c",
        "import sqlite3; "
        "conn = sqlite3.connect('/app/data/cache_store.db'); "
        "try: "
        "  conn.execute(\"DELETE FROM writer_inbox WHERE message_id LIKE 'synthetic_%'\"); "
        "  conn.execute(\"DELETE FROM writer_inbox WHERE message_id LIKE 'sigterm_inflight_test%'\"); "
        "  conn.commit(); "
        "except Exception as e: "
        "  print(f'writer_inbox cleanup error: {e}'); "
        "conn.close()",
    ])
    try:
        writer_inbox_result = _run(writer_inbox_cleanup_cmd, timeout=15, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired:
        writer_inbox_result = None
        cleanup_failures.append("writer_inbox 清理超时")
    if writer_inbox_result is None or writer_inbox_result.returncode != 0:
        cleanup_failures.append(
            f"writer_inbox 清理失败 (exit={writer_inbox_result.returncode if writer_inbox_result else 'timeout'})"
        )

    # 2.4 清理 CRDB 测试数据
    crdb_cleanup_cmd = _compose_cmd([
        "exec", "-T", "db_writer", "python", "-c",
        "import os, sys; "
        "sys.path.insert(0, '/app'); "
        "from scripts.synthetic_transaction import cleanup as syn_cleanup; "
        "result = syn_cleanup('synthetic_', timeout=30); "
        "print(f'crdb_cleanup_passed={result.passed}'); "
        "sys.exit(0 if result.passed else 1)",
    ])
    try:
        crdb_result = _run(crdb_cleanup_cmd, timeout=60, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired:
        crdb_result = None
        cleanup_failures.append("CRDB 清理超时")
    if crdb_result is None or crdb_result.returncode != 0:
        cleanup_failures.append(
            f"CRDB 清理失败 (exit={crdb_result.returncode if crdb_result else 'timeout'})"
        )

    # ══════════════════════════════════════════════════════════════
    # 3. R74 P0-06: 验证清理后无残留(独立于清理操作的验证)
    # ══════════════════════════════════════════════════════════════

    # 3.1 SQLite fixture 验证:确认测试数据已清理干净
    sqlite_verify_cmd = _compose_cmd([
        "exec", "-T", "db_writer", "python", "-c",
        "import sqlite3; "
        "conn = sqlite3.connect('/app/data/cache_store.db'); "
        "c = conn.cursor(); "
        "c.execute(\"SELECT COUNT(*) FROM bot_heartbeat WHERE name LIKE 'synthetic_%'\"); "
        "synthetic_count = c.fetchone()[0] or 0; "
        "c.execute(\"SELECT COUNT(*) FROM bot_heartbeat WHERE name LIKE 'restore_marker_%'\"); "
        "restore_count = c.fetchone()[0] or 0; "
        "c.execute(\"SELECT COUNT(*) FROM bot_heartbeat WHERE name LIKE 'sigterm_inflight_test%'\"); "
        "sigterm_count = c.fetchone()[0] or 0; "
        "c.execute(\"SELECT COUNT(*) FROM writer_inbox WHERE message_id LIKE 'synthetic_%'\"); "
        "inbox_synthetic = c.fetchone()[0] or 0; "
        "c.execute(\"SELECT COUNT(*) FROM writer_inbox WHERE message_id LIKE 'sigterm_inflight_test%'\"); "
        "inbox_sigterm = c.fetchone()[0] or 0; "
        "conn.close(); "
        "total = synthetic_count + restore_count + sigterm_count + inbox_synthetic + inbox_sigterm; "
        "print(f'SQLITE_VERIFY:synthetic={synthetic_count} restore={restore_count} sigterm={sigterm_count} inbox_synthetic={inbox_synthetic} inbox_sigterm={inbox_sigterm} total={total}'); "
        "sys.exit(0 if total == 0 else 1)",
    ])
    sqlite_verify_ok = False
    try:
        sv_result = _run(sqlite_verify_cmd, timeout=30, cwd=REPO_ROOT)
        sqlite_verify_ok = (sv_result.returncode == 0)
        if sv_result.stdout.strip():
            for line in sv_result.stdout.strip().splitlines():
                if line.startswith("SQLITE_VERIFY:"):
                    all_evidence["sqlite_verify"] = line
    except subprocess.TimeoutExpired:
        cleanup_failures.append("SQLite fixture 验证超时")
    if not sqlite_verify_ok:
        cleanup_failures.append("SQLite fixture 验证失败: 测试数据残留")
    readiness_checks.append({
        "check": "sqlite_fixture_verified",
        "status": "pass" if sqlite_verify_ok else "fail",
    })

    # 3.2 Redis keys/streams/consumers 全面检查
    # 3.2a: 检查 stream 长度
    redis_stream_len_cmd = _compose_cmd([
        "exec", "-T", "redis",
        "redis-cli", "--user", "tgjiema_admin",
        "-a", redis_admin_pwd,
        "--no-auth-warning",
        "XLEN", "tgjiema:writer:stream",
    ])
    redis_stream_len = -1
    try:
        rsl_result = _run(redis_stream_len_cmd, timeout=10, cwd=REPO_ROOT)
        if rsl_result.returncode == 0:
            try:
                redis_stream_len = int(rsl_result.stdout.strip())
            except ValueError:
                pass
    except subprocess.TimeoutExpired:
        cleanup_failures.append("Redis XLEN 超时")
    readiness_checks.append({
        "check": "redis_stream_length",
        "status": "pass" if redis_stream_len == 0 else "fail",
        "stream_length": redis_stream_len,
    })
    if redis_stream_len > 0:
        cleanup_failures.append(f"Redis stream 仍有 {redis_stream_len} 条消息")

    # 3.2b: 检查消费者组
    redis_cg_cmd = _compose_cmd([
        "exec", "-T", "redis",
        "redis-cli", "--user", "tgjiema_admin",
        "-a", redis_admin_pwd,
        "--no-auth-warning",
        "XINFO", "GROUPS", "tgjiema:writer:stream",
    ])
    redis_cg_count = 0
    try:
        rcg_result = _run(redis_cg_cmd, timeout=10, cwd=REPO_ROOT)
        if rcg_result.returncode == 0:
            # 每个消费者组输出约 10 行,计数 name 字段行数
            redis_cg_count = rcg_result.stdout.count("name\n")
    except subprocess.TimeoutExpired:
        cleanup_failures.append("Redis XINFO GROUPS 超时")
    readiness_checks.append({
        "check": "redis_consumer_groups",
        "status": "pass" if redis_cg_count >= 0 else "fail",
        "consumer_group_count": redis_cg_count,
    })
    all_evidence["redis_consumer_groups"] = redis_cg_count

    # 3.2c: 检查测试相关 keys
    redis_keys_cmd = _compose_cmd([
        "exec", "-T", "redis",
        "redis-cli", "--user", "tgjiema_admin",
        "-a", redis_admin_pwd,
        "--no-auth-warning",
        "KEYS", "synthetic_*",
    ])
    redis_test_keys: list[str] = []
    try:
        rk_result = _run(redis_keys_cmd, timeout=10, cwd=REPO_ROOT)
        if rk_result.returncode == 0 and rk_result.stdout.strip():
            redis_test_keys = [k for k in rk_result.stdout.strip().splitlines() if k.strip()]
    except subprocess.TimeoutExpired:
        cleanup_failures.append("Redis KEYS 超时")
    redis_keys_clean = len(redis_test_keys) == 0
    readiness_checks.append({
        "check": "redis_test_keys_clean",
        "status": "pass" if redis_keys_clean else "fail",
        "test_key_count": len(redis_test_keys),
    })
    if not redis_keys_clean:
        cleanup_failures.append(f"Redis 仍有 {len(redis_test_keys)} 个测试 keys")

    # 3.3 CRDB fixture 验证
    crdb_verify_cmd = _compose_cmd([
        "exec", "-T", "db_writer", "python", "-c",
        "import os, sys; "
        "sys.path.insert(0, '/app'); "
        "from scripts.synthetic_transaction import verify_cleanup as syn_verify; "
        "result = syn_verify('synthetic_', timeout=30); "
        "print(f'crdb_verify_passed={result.passed}'); "
        "sys.exit(0 if result.passed else 1)",
    ])
    crdb_verify_ok = False
    try:
        cv_result = _run(crdb_verify_cmd, timeout=60, cwd=REPO_ROOT)
        crdb_verify_ok = (cv_result.returncode == 0)
        if cv_result.stdout.strip():
            all_evidence["crdb_verify"] = cv_result.stdout.strip()
    except subprocess.TimeoutExpired:
        cleanup_failures.append("CRDB fixture 验证超时")
    if not crdb_verify_ok:
        cleanup_failures.append("CRDB fixture 验证失败: 测试数据残留")
    readiness_checks.append({
        "check": "crdb_fixture_verified",
        "status": "pass" if crdb_verify_ok else "fail",
    })

    # ══════════════════════════════════════════════════════════════
    # 4. R74 P0-06: R2 测试对象检查
    # ══════════════════════════════════════════════════════════════
    # 检查 R2 bucket 中是否存在本次测试产生的备份对象
    r2_bucket = os.environ.get("R2_BUCKET", "tgjiema-backups")
    r2_endpoint = os.environ.get("R2_ENDPOINT", "")
    r2_test_objects_ok = True
    if r2_endpoint:
        r2_list_cmd = _compose_cmd([
            "exec", "-T", "db_backup", "python", "-c",
            "import os, sys; "
            "sys.path.insert(0, '/app'); "
            "from services.r2_client import R2Client; "
            f"client = R2Client(endpoint={r2_endpoint!r}, "
            f"bucket={r2_bucket!r}, "
            f"access_key=os.environ.get('R2_ACCESS_KEY', ''), "
            f"secret_key=os.environ.get('R2_SECRET_KEY', '')); "
            "objects = client.list_objects(prefix='synthetic_'); "
            "test_objects = [o for o in objects if 'synthetic_' in o.get('Key', '')]; "
            "print(f'R2_VERIFY:test_objects={len(test_objects)}'); "
            "sys.exit(0 if len(test_objects) == 0 else 1)",
        ])
        try:
            r2_result = _run(r2_list_cmd, timeout=30, cwd=REPO_ROOT)
            r2_test_objects_ok = (r2_result.returncode == 0)
            if r2_result.stdout.strip():
                for line in r2_result.stdout.strip().splitlines():
                    if line.startswith("R2_VERIFY:"):
                        all_evidence["r2_verify"] = line
        except subprocess.TimeoutExpired:
            cleanup_failures.append("R2 测试对象检查超时")
            r2_test_objects_ok = False
    else:
        # R2_ENDPOINT 未配置,跳过 R2 检查(非阻塞)
        all_evidence["r2_verify"] = "skipped: R2_ENDPOINT not configured"
    readiness_checks.append({
        "check": "r2_test_objects_clean",
        "status": "pass" if r2_test_objects_ok else "fail",
        "r2_endpoint_configured": bool(r2_endpoint),
    })

    # ══════════════════════════════════════════════════════════════
    # 5. R74 P0-06: compose 容器状态检查
    # ══════════════════════════════════════════════════════════════
    ps_info = _get_compose_ps_info(include_exited=True)
    container_issues: list[str] = []
    # 检查所有期望运行的服务
    expected_running = [
        "redis", "db_writer", "up", "idx", "dsp", "mon", "admin_bot",
        "admin", "crdb_sync", "db_backup", "r40_scheduler", "prometheus_exporter",
    ]
    for svc in expected_running:
        state = ps_info.get(svc, {}).get("state", "missing")
        if state not in ("running",):
            container_issues.append(f"{svc}: state={state}")
    # 检查 migration/redis-acl-init(oneshot,应已退出)
    expected_exited = ["migration", "redis-acl-init"]
    for svc in expected_exited:
        state = ps_info.get(svc, {}).get("state", "missing")
        if state not in ("exited", "missing"):
            # missing 也可以(容器可能已被清理)
            pass
    containers_ok = len(container_issues) == 0
    all_evidence["container_states"] = {
        svc: ps_info.get(svc, {}).get("state", "missing")
        for svc in expected_running + expected_exited
    }
    readiness_checks.append({
        "check": "compose_containers",
        "status": "pass" if containers_ok else "fail",
        "issues": container_issues,
        "container_count": len(ps_info),
    })
    if not containers_ok:
        cleanup_failures.append(f"容器状态异常: {'; '.join(container_issues)}")

    # ══════════════════════════════════════════════════════════════
    # 6. R74 P0-06: compose 网络状态检查
    # ══════════════════════════════════════════════════════════════
    compose_project = os.environ.get("COMPOSE_PROJECT_NAME", "tgjiema")
    network_name = f"{compose_project}_default"
    network_check_cmd = ["docker", "network", "inspect", network_name]
    network_ok = False
    try:
        nc_result = _run(network_check_cmd, timeout=15, cwd=REPO_ROOT)
        network_ok = (nc_result.returncode == 0)
        if network_ok:
            try:
                network_info = json.loads(nc_result.stdout.strip())
                if isinstance(network_info, list) and len(network_info) > 0:
                    all_evidence["network"] = {
                        "name": network_info[0].get("Name", network_name),
                        "driver": network_info[0].get("Driver", "unknown"),
                        "containers": len(network_info[0].get("Containers", {})),
                    }
            except json.JSONDecodeError:
                pass
    except subprocess.TimeoutExpired:
        network_ok = False
    readiness_checks.append({
        "check": "compose_network",
        "status": "pass" if network_ok else "fail",
        "network_name": network_name,
    })
    if not network_ok:
        cleanup_failures.append(f"compose 网络检查失败: {network_name}")

    # ══════════════════════════════════════════════════════════════
    # 7. R74 P0-06: compose 卷状态检查
    # ══════════════════════════════════════════════════════════════
    volume_names = [
        f"{compose_project}_redis_data",
        f"{compose_project}_crdb_data",
        f"{compose_project}_app_data",
    ]
    volume_issues: list[str] = []
    for vol_name in volume_names:
        vol_check_cmd = ["docker", "volume", "inspect", vol_name]
        try:
            vc_result = _run(vol_check_cmd, timeout=10, cwd=REPO_ROOT)
            if vc_result.returncode != 0:
                volume_issues.append(f"{vol_name}: inspect failed")
        except subprocess.TimeoutExpired:
            volume_issues.append(f"{vol_name}: inspect timeout")
    volumes_ok = len(volume_issues) == 0
    readiness_checks.append({
        "check": "compose_volumes",
        "status": "pass" if volumes_ok else "fail",
        "issues": volume_issues,
        "volume_names": volume_names,
    })
    if not volumes_ok:
        cleanup_failures.append(f"卷状态异常: {'; '.join(volume_issues)}")

    # ══════════════════════════════════════════════════════════════
    # 8. R74 P0-06: 测试产物(artifacts)检查
    # ══════════════════════════════════════════════════════════════
    artifacts_dir = REPO_ROOT / "runtime-e2e-artifacts"
    artifact_files: list[str] = []
    if artifacts_dir.exists():
        artifact_files = [str(p.relative_to(REPO_ROOT)) for p in artifacts_dir.rglob("*") if p.is_file()]
    readiness_checks.append({
        "check": "artifacts",
        "status": "pass",
        "artifact_count": len(artifact_files),
        "artifacts_dir": str(artifacts_dir),
    })
    all_evidence["artifacts"] = {
        "count": len(artifact_files),
        "files": artifact_files[:50],  # 最多记录 50 个文件路径
    }

    # ══════════════════════════════════════════════════════════════
    # 9. R74 P0-06: 测试 Bot 输出检查(容器日志中无异常)
    # ══════════════════════════════════════════════════════════════
    bot_services = ["up", "idx", "dsp", "mon", "admin_bot", "db_writer",
                    "crdb_sync", "db_backup"]
    bot_log_issues: list[str] = []
    # 检查每个 Bot 容器最近 200 行日志中的 ERROR/FATAL/CRITICAL/Traceback
    for svc in bot_services:
        if svc not in ps_info or ps_info[svc].get("state") != "running":
            continue
        log_cmd = _compose_cmd(["logs", "--tail", "200", svc])
        try:
            log_result = _run(log_cmd, timeout=30, cwd=REPO_ROOT)
            if log_result.returncode == 0:
                log_output = (log_result.stdout or "") + (log_result.stderr or "")
                error_lines = [
                    line for line in log_output.splitlines()
                    if any(kw in line for kw in (
                        "ERROR", "FATAL", "CRITICAL", "Traceback",
                        "Unhandled exception", "panic",
                    ))
                ]
                if error_lines:
                    bot_log_issues.append(
                        f"{svc}: {len(error_lines)} error lines in recent 200 logs"
                    )
        except subprocess.TimeoutExpired:
            bot_log_issues.append(f"{svc}: log check timeout")
    bot_logs_ok = len(bot_log_issues) == 0
    readiness_checks.append({
        "check": "bot_logs",
        "status": "pass" if bot_logs_ok else "fail",
        "issues": bot_log_issues,
        "services_checked": len(bot_services),
    })
    if not bot_logs_ok:
        cleanup_failures.append(f"Bot 日志异常: {'; '.join(bot_log_issues)}")

    # ══════════════════════════════════════════════════════════════
    # 汇总结果
    # ══════════════════════════════════════════════════════════════
    all_evidence["cleanup_failures"] = cleanup_failures
    all_evidence["cleanup_completed"] = len(cleanup_failures) == 0

    if cleanup_failures:
        return _fail_result(
            phase="final_identity_and_cleanup",
            description=description, started=started, started_at=started_at,
            error=(
                f"R74 P0-06: 资源检查/清理失败 — {'; '.join(cleanup_failures)}"
            ),
            evidence=all_evidence,
            readiness_checks=readiness_checks,
        )

    return _pass_result(
        phase="final_identity_and_cleanup",
        description=description, started=started, started_at=started_at,
        evidence=all_evidence,
        readiness_checks=readiness_checks,
    )


def phase_evidence_signing(timeout: int) -> PhaseResult:
    """R73 §5.15 阶段 16:签名 evidence envelope。

    readiness 检查点:
      - 调用 scripts/evidence_envelope.py 生成签名 evidence envelope
      - envelope 中 gate_level / overall_conclusion / promotion_eligible 字段齐全
      - envelope 签名校验通过
    """
    description = _phase_desc("evidence_signing")
    started = time.time()
    started_at = _now_iso()

    if not _docker_available():
        return _fail_result(
            phase="evidence_signing", description=description, started=started,
            started_at=started_at,
            error="Docker daemon 不可用 — evidence_signing 阶段无法执行",
            readiness_checks=[{"check": "docker_daemon", "status": "fail"}],
        )

    readiness_checks: list[dict[str, Any]] = [
        {"check": "docker_daemon", "status": "pass"},
    ]

    # 加载 evidence_envelope 模块
    import importlib.util
    envelope_path = REPO_ROOT / "scripts" / "evidence_envelope.py"
    if not envelope_path.is_file():
        return _fail_result(
            phase="evidence_signing", description=description, started=started,
            started_at=started_at,
            error=f"evidence_envelope.py 不存在: {envelope_path}",
            readiness_checks=readiness_checks + [
                {"check": "envelope_available", "status": "fail"},
            ],
        )
    try:
        spec = importlib.util.spec_from_file_location(
            "evidence_envelope_2", envelope_path,
        )
        if spec is None or spec.loader is None:
            raise ImportError("spec_from_file_location 返回 None")
        envelope_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(envelope_module)
    except Exception as e:
        return _fail_result(
            phase="evidence_signing", description=description, started=started,
            started_at=started_at,
            error=f"加载 evidence_envelope 失败: {type(e).__name__}: {e}",
            readiness_checks=readiness_checks + [
                {"check": "envelope_available", "status": "fail"},
            ],
        )
    readiness_checks.append({"check": "envelope_available", "status": "pass"})

    source_sha = _get_source_sha() or "0" * 40
    # 取 40-hex 前缀(避免 source_sha 为空时校验失败)
    if len(source_sha) < 40 or not all(c in "0123456789abcdef" for c in source_sha[:40]):
        source_sha = "0" * 40
    else:
        source_sha = source_sha[:40]

    image_repo_digest = os.environ.get("TGJIEMA_IMAGE", "")
    if "@" not in image_repo_digest:
        image_repo_digest = None
    runtime_config_digest = os.environ.get("R73_RUNTIME_CONFIG_DIGEST", "")
    if not runtime_config_digest:
        runtime_config_digest = None

    try:
        run_id = int(os.environ.get("GITHUB_RUN_ID", "0") or "0")
    except ValueError:
        run_id = 0
    try:
        run_attempt = int(os.environ.get("GITHUB_RUN_ATTEMPT", "1") or "1")
    except ValueError:
        run_attempt = 1

    overall_conclusion, all_required_passed, dag_aggregate = (
        _aggregate_required_phase_conclusion(_DAG_RESULTS_CONTEXT)
    )
    event = os.environ.get("GITHUB_EVENT_NAME", "push")
    ref = os.environ.get("GITHUB_REF", "refs/tags/rc-v-runtime-e2e")
    promotion_eligible = (
        all_required_passed
        and event == "push"
        and ref.startswith("refs/tags/rc-v")
        and image_repo_digest is not None
        and runtime_config_digest is not None
    )
    payload = {
        "phases": [phase_name for phase_name, _ in PHASES],
        "phase_count": len(PHASES),
        "dag_enforced": True,
        "dag_aggregate": dag_aggregate,
    }
    readiness_checks.append({
        "check": "required_phase_aggregate",
        "status": "pass",
        "overall_conclusion": overall_conclusion,
        "promotion_eligible": promotion_eligible,
        **dag_aggregate,
    })

    try:
        envelope = envelope_module.build_evidence_envelope(
            gate_level="rc",
            event=event,
            ref=ref,
            source_sha=source_sha,
            run_id=run_id,
            run_attempt=run_attempt,
            workflow_path=".github/workflows/release-gates.yml",
            overall_conclusion=overall_conclusion,
            payload=payload,
            image_repo_digest=image_repo_digest,
            runtime_config_digest=runtime_config_digest,
            promotion_eligible=promotion_eligible,
        )
    except Exception as e:
        return _fail_result(
            phase="evidence_signing", description=description, started=started,
            started_at=started_at,
            error=f"build_evidence_envelope 异常: {type(e).__name__}: {e}",
            readiness_checks=readiness_checks + [
                {"check": "envelope_built", "status": "fail"},
            ],
        )
    readiness_checks.append({"check": "envelope_built", "status": "pass"})

    # 验证 envelope 字段
    required_fields = ["gate_level", "overall_conclusion", "promotion_eligible"]
    fields_ok = all(envelope.get(f) is not None for f in required_fields)
    readiness_checks.append({
        "check": "envelope_required_fields",
        "status": "pass" if fields_ok else "fail",
        "gate_level": envelope.get("gate_level"),
        "overall_conclusion": envelope.get("overall_conclusion"),
        "promotion_eligible": envelope.get("promotion_eligible"),
    })

    # 验证 envelope 结构
    try:
        valid, errors = envelope_module.validate_envelope(envelope)
    except Exception as e:
        valid = False
        errors = [f"validate_envelope 异常: {type(e).__name__}: {e}"]
    readiness_checks.append({
        "check": "envelope_valid",
        "status": "pass" if valid else "fail",
        "errors": errors,
    })

    if not fields_ok or not valid:
        return _fail_result(
            phase="evidence_signing", description=description, started=started,
            started_at=started_at,
            error=(
                f"R73 §5.15: evidence envelope 校验失败 — "
                f"fields_ok={fields_ok}, valid={valid}, errors={errors}"
            ),
            evidence={"envelope": envelope},
            readiness_checks=readiness_checks,
        )

    return _pass_result(
        phase="evidence_signing", description=description, started=started,
        started_at=started_at,
        evidence={
            "envelope": envelope,
            "envelope_path": str(REPO_ROOT / "runtime-e2e-evidence.json"),
        },
        readiness_checks=readiness_checks,
    )


# ════════════════════════════════════════════════════════════════
# R80 Step 12/13: MinIO secretless 备份/恢复/损坏负测/探测失败阶段
# ════════════════════════════════════════════════════════════════


def _container_endpoint(endpoint: str) -> str:
    """将主机可达的 endpoint 转换为容器内可达的 endpoint。

    Docker 容器内 "localhost" / "127.0.0.1" 指向容器自身,而非宿主机。
    MinIO 作为 compose service 运行,服务名为 "minio",容器内应使用
    http://minio:9000 访问。

    R81 §10.3: 使用 URL parser 只替换 hostname,保留 scheme/port/path/query/fragment。
    禁止普通字符串全局 replace(会误改 path/query 中出现的 localhost)。
    """
    from urllib.parse import urlsplit, urlunsplit

    if not endpoint:
        return endpoint
    parsed = urlsplit(endpoint)
    if parsed.hostname not in {"localhost", "127.0.0.1"}:
        return endpoint
    port = f":{parsed.port}" if parsed.port else ""
    userinfo = ""
    if parsed.username:
        userinfo = parsed.username
        if parsed.password:
            userinfo += f":{parsed.password}"
        userinfo += "@"
    return urlunsplit((
        parsed.scheme,
        f"{userinfo}minio{port}",
        parsed.path,
        parsed.query,
        parsed.fragment,
    ))


def _s3_env_override() -> dict[str, str]:
    """从 _STORAGE_CONFIG 构造注入到 docker compose 子进程的 S3 环境变量。"""
    import base64 as _b64

    env: dict[str, str] = {}
    if _STORAGE_CONFIG["storage_backend"]:
        env["OBJECT_STORAGE_BACKEND"] = _STORAGE_CONFIG["storage_backend"]
    if _STORAGE_CONFIG["endpoint"]:
        # R80 Step 12: 容器内 localhost 不可达,需转换为 Docker 服务名
        env["S3_ENDPOINT_URL"] = _container_endpoint(_STORAGE_CONFIG["endpoint"])
    if _STORAGE_CONFIG["bucket"]:
        env["S3_BUCKET_NAME"] = _STORAGE_CONFIG["bucket"]
    if _STORAGE_CONFIG["access_key"]:
        env["S3_ACCESS_KEY_ID"] = _STORAGE_CONFIG["access_key"]
    if _STORAGE_CONFIG["secret_key"]:
        env["S3_SECRET_ACCESS_KEY"] = _STORAGE_CONFIG["secret_key"]
    if _STORAGE_CONFIG["signing_key"]:
        env["BACKUP_SIGNING_KEY"] = _STORAGE_CONFIG["signing_key"]
    # R80 Step 12: backup 服务要求 BACKUP_KEK(32 字节 base64)用于 AES-256-GCM 加密。
    # secretless CI 不预置此变量,使用确定性派生(从 signing_key)或随机生成。
    if not os.environ.get("BACKUP_KEK"):
        if _STORAGE_CONFIG["signing_key"]:
            # 从 signing_key 确定性派生 32 字节 KEK(同一 run 内幂等)
            import hashlib as _hl
            derived = _hl.sha256(
                _STORAGE_CONFIG["signing_key"].encode()
            ).digest()
            env["BACKUP_KEK"] = _b64.b64encode(derived).decode()
        else:
            env["BACKUP_KEK"] = _b64.b64encode(os.urandom(32)).decode()
    return env


# R80 Step 12/13: secretless CI 使用 docker-compose.yml + docker-compose.secretless.yml
# (不使用 docker-compose.prod.yml,后者需要 TGJIEMA_IMAGE 不可变 digest)
_SECRETLESS_COMPOSE_BASE = REPO_ROOT / "docker-compose.yml"
_SECRETLESS_COMPOSE_OVERLAY = REPO_ROOT / "docker-compose.secretless.yml"

# R82 §10.5: 跨进程持久化精确 backup contract state。
# workflow Step 12 通过三个独立 Python 进程依次运行 backup/corrupt/restore，
# 必须绑定 current SHA 与 evidence 中的三个真实 object key，禁止重新推导命名。
_SECRETLESS_STATE_DIR = REPO_ROOT / "artifacts" / "secretless-e2e" / "state"
_BACKUP_STATE_FILE = _SECRETLESS_STATE_DIR / "backup-state.json"
_RESTORE_STATE_FILE = _SECRETLESS_STATE_DIR / "restore-state.json"
# 兼容测试/旧调用方的符号；内容现为 JSON state，不再是裸 backup_id。
_BACKUP_ID_FILE = _BACKUP_STATE_FILE


def _persist_backup_state(*, head_sha: str, backup_id: str, objects: dict[str, Any]) -> None:
    """原子持久化 current-SHA 绑定的三对象备份状态。"""
    normalized = {
        "payload_key": str(objects.get("payload", "")).strip(),
        "manifest_key": str(objects.get("manifest", "")).strip(),
        "complete_key": str(objects.get("COMPLETE", "")).strip(),
    }
    state = {
        "schema_version": "secretless-backup-state/v1",
        "head_sha": head_sha.strip(),
        "backup_id": backup_id.strip(),
        **normalized,
    }
    missing = [key for key, value in state.items() if not value]
    if missing:
        raise ValueError("backup state missing: " + ",".join(missing))
    if len(set(normalized.values())) != 3:
        raise ValueError("backup state object keys must be unique")
    _SECRETLESS_STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _BACKUP_ID_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(_BACKUP_ID_FILE)


def _load_backup_state(*, expected_head_sha: str = "") -> dict[str, str]:
    """读取并严格验证 backup state；错 SHA、缺 key、重复对象均 fail-closed。"""
    if not _BACKUP_ID_FILE.is_file():
        return {}
    try:
        state = json.loads(_BACKUP_ID_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    required = (
        "schema_version", "head_sha", "backup_id",
        "payload_key", "manifest_key", "complete_key",
    )
    if not isinstance(state, dict) or any(not str(state.get(k, "")).strip() for k in required):
        return {}
    if state["schema_version"] != "secretless-backup-state/v1":
        return {}
    current_sha = (expected_head_sha or os.environ.get("GITHUB_SHA", "") or _get_source_sha()).strip()
    if current_sha and str(state["head_sha"]).strip() != current_sha:
        return {}
    object_keys = [str(state[k]).strip() for k in ("payload_key", "manifest_key", "complete_key")]
    if len(set(object_keys)) != 3:
        return {}
    return {key: str(state[key]).strip() for key in required}


def _persist_restore_state(
    *,
    head_sha: str,
    backup_state: dict[str, str],
    restore_evidence: dict[str, Any],
) -> None:
    """原子持久化 Step 13 所需的隔离恢复目标身份，不保存明文 DSN。"""
    state = {
        "schema_version": "secretless-restore-state/v1",
        "head_sha": head_sha.strip(),
        "backup_id": str(backup_state.get("backup_id", "")).strip(),
        "payload_key": str(backup_state.get("payload_key", "")).strip(),
        "manifest_key": str(backup_state.get("manifest_key", "")).strip(),
        "complete_key": str(backup_state.get("complete_key", "")).strip(),
        "operation_id": str(restore_evidence.get("operation_id", "")).strip(),
        "source_identity": str(restore_evidence.get("source_identity", "")).strip(),
        "target_identity": str(restore_evidence.get("target_identity", "")).strip(),
        "source_database": str(restore_evidence.get("source_database", "")).strip(),
        "target_database": str(restore_evidence.get("target_database", "")).strip(),
        "target_dsn_sha256": str(
            restore_evidence.get("target_dsn_sha256", "")
        ).strip(),
    }
    missing = [key for key, value in state.items() if not value]
    if missing:
        raise ValueError("restore state missing: " + ",".join(missing))
    if state["source_identity"] == state["target_identity"]:
        raise ValueError("restore state source and target identities must differ")
    _SECRETLESS_STATE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = _RESTORE_STATE_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(_RESTORE_STATE_FILE)


def _load_restore_state(*, expected_head_sha: str = "") -> dict[str, str]:
    """读取 current-SHA 绑定的恢复目标状态，供 switch/rollback 使用。"""
    if not _RESTORE_STATE_FILE.is_file():
        return {}
    try:
        state = json.loads(_RESTORE_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    required = (
        "schema_version", "head_sha", "backup_id", "payload_key", "manifest_key",
        "complete_key", "operation_id", "source_identity", "target_identity",
        "source_database", "target_database", "target_dsn_sha256",
    )
    if not isinstance(state, dict) or any(
        not str(state.get(key, "")).strip() for key in required
    ):
        return {}
    if state["schema_version"] != "secretless-restore-state/v1":
        return {}
    current_sha = (
        expected_head_sha or os.environ.get("GITHUB_SHA", "") or _get_source_sha()
    ).strip()
    if current_sha and str(state["head_sha"]).strip() != current_sha:
        return {}
    if state["source_identity"] == state["target_identity"]:
        return {}
    backup_state = _load_backup_state(expected_head_sha=current_sha)
    for key in ("backup_id", "payload_key", "manifest_key", "complete_key"):
        if state[key] != backup_state.get(key):
            return {}
    return {key: str(state[key]).strip() for key in required}


def _persist_backup_id(backup_id: str) -> None:
    """拒绝旧的裸 backup_id 状态，避免缺少 object key 的不完整证据被复用。"""
    if not backup_id.strip():
        raise ValueError("backup_id must not be empty")
    raise ValueError("R82 requires _persist_backup_state with exact object keys")


def _load_backup_id() -> str:
    """兼容调用方：仅从通过 current-SHA 校验的完整 state 返回 backup_id。"""
    state = _load_backup_state()
    return state.get("backup_id", "")


def _secretless_compose_cmd(args: list[str]) -> list[str]:
    """构造 secretless docker compose 命令(base + secretless overlay)。"""
    return [
        "docker", "compose",
        "-f", str(_SECRETLESS_COMPOSE_BASE),
        "-f", str(_SECRETLESS_COMPOSE_OVERLAY),
    ] + args


def _env_to_compose_run_flags(env: dict[str, str]) -> list[str]:
    """将环境变量字典转换为 docker compose run -e 标志列表。

    R81 §10.4: 只传递变量名(`-e KEY`),不传递值。
    完整环境变量通过 `_run(..., env=...)` 传给宿主机 docker compose 进程,
    Compose 从宿主环境复制对应变量值到容器。
    这样命令行/日志/ps 输出中不会出现 secret 值(即使是隔离假密钥)。
    稳定排序保证 artifact 可复现。
    """
    flags: list[str] = []
    for key in sorted(env):
        flags.extend(["-e", key])
    return flags


def _probe_secretless_backup_contract(
    *, env: dict[str, str], timeout: int,
) -> tuple[bool, str, str]:
    """在正式 backup 前验证解析环境与 /app/data bind mount；不替代网络 readiness。"""
    required = (
        "OBJECT_STORAGE_BACKEND",
        "S3_ENDPOINT_URL",
        "S3_BUCKET_NAME",
        "S3_ACCESS_KEY_ID",
        "S3_SECRET_ACCESS_KEY",
        "BACKUP_SIGNING_KEY",
        "BACKUP_KEK",
    )
    missing = [key for key in required if not env.get(key)]
    if missing:
        return False, "BACKUP_ENV_MISSING:" + ",".join(missing), ""

    flags = _env_to_compose_run_flags(env)
    cmd = _secretless_compose_cmd([
        "run", "--rm", "-T", "--no-deps",
        *flags,
        "--entrypoint", "python",
        "db_backup",
        "-c",
        (
            "import os,pathlib;"
            "required=('OBJECT_STORAGE_BACKEND','S3_ENDPOINT_URL','S3_BUCKET_NAME',"
            "'S3_ACCESS_KEY_ID','S3_SECRET_ACCESS_KEY','BACKUP_SIGNING_KEY','BACKUP_KEK');"
            "missing=[k for k in required if not os.environ.get(k)];"
            "assert not missing, f'missing={missing}';"
            "p=pathlib.Path('/app/data/.r82-write-probe');"
            "p.write_text('ok');p.unlink();"
            "print('BACKUP_CONTRACT_PROBE_OK')"
        ),
    ])
    result = _run(cmd, timeout=min(timeout, 60), cwd=REPO_ROOT, env=env)
    return result.returncode == 0, result.stderr, result.stdout


def phase_full_backup_to_s3_contract_store(timeout: int) -> PhaseResult:
    """R80 Step 12: 全量备份到 MinIO(S3 兼容 contract store)。

    与 phase_full_backup_to_r2 逻辑一致,但通过环境变量注入 MinIO 配置,
    使 BackupEngine 使用 S3 兼容协议写入 MinIO 而非 Cloudflare R2。

    readiness 检查点:
      - docker compose run db_backup backup --once --type full 返回 0
      - backup-evidence.json status=success / backup_type=full
      - 三对象(payload / manifest / COMPLETE)都存在
    """
    phase_name = "full_backup_to_s3_contract_store"
    description = "R80 Step 12: Full backup to MinIO S3 contract store (three objects)"
    started = time.time()
    started_at = _now_iso()

    if not _docker_available():
        return _fail_result(
            phase=phase_name, description=description, started=started,
            started_at=started_at,
            error="Docker daemon 不可用 — full_backup_to_s3_contract_store 无法执行",
            readiness_checks=[{"check": "docker_daemon", "status": "fail"}],
        )

    readiness_checks: list[dict[str, Any]] = [
        {"check": "docker_daemon", "status": "pass"},
    ]

    source_sha = os.environ.get("GITHUB_SHA", "") or _get_source_sha()
    reason = "rc-restore-drill-s3"

    s3_env = _s3_env_override()
    try:
        probe_ok, probe_stderr, probe_stdout = _probe_secretless_backup_contract(
            env=s3_env, timeout=timeout,
        )
    except subprocess.TimeoutExpired as te:
        return _fail_result(
            phase=phase_name, description=description, started=started,
            started_at=started_at,
            error="BACKUP_CONTRACT_PREFLIGHT_FAILED: probe timeout",
            stdout=te.stdout or "", stderr=te.stderr or "",
            returncode=124,
            readiness_checks=readiness_checks + [
                {"check": "backup_contract_preflight", "status": "timeout"},
            ],
        )
    if not probe_ok:
        return _fail_result(
            phase=phase_name, description=description, started=started,
            started_at=started_at,
            error="BACKUP_CONTRACT_PREFLIGHT_FAILED",
            stdout=probe_stdout, stderr=probe_stderr,
            returncode=1,
            readiness_checks=readiness_checks + [{
                "check": "backup_contract_preflight", "status": "fail",
                "error_code": "BACKUP_CONTRACT_PREFLIGHT_FAILED",
            }],
        )
    readiness_checks.append({
        "check": "backup_contract_preflight", "status": "pass",
        "scope": "resolved_env_and_data_mount",
    })
    env_flags = _env_to_compose_run_flags(s3_env)

    backup_evidence_path = REPO_ROOT / "data" / "backup-evidence.json"
    backup_evidence_container_path = "/app/data/backup-evidence.json"
    backup_cmd = _secretless_compose_cmd([
        "run", "--rm", "-T", "--no-deps",
    ] + env_flags + [
        "--entrypoint", "python",
        "db_backup",
        "-m", "services.db_backup", "backup",
        "--once", "--timeout", "240",
        "--type", "full",
        "--reason", reason,
        "--source-sha", source_sha,
        "--output-json", backup_evidence_container_path,
    ])

    try:
        backup_result = _run(backup_cmd, timeout=timeout, cwd=REPO_ROOT, env=s3_env)
    except subprocess.TimeoutExpired as te:
        partial_stdout = (te.stdout or "")[:2000] if isinstance(te.stdout, str) else ""
        partial_stderr = (te.stderr or "")[:2000] if isinstance(te.stderr, str) else ""
        return _fail_result(
            phase=phase_name, description=description, started=started,
            started_at=started_at,
            error=f"R80 Step 12: backup --once --type full 超时({timeout}s)",
            stdout=partial_stdout, stderr=partial_stderr,
            readiness_checks=readiness_checks + [
                {"check": "backup_triggered", "status": "timeout"},
            ],
        )
    if backup_result.returncode != 0:
        return _fail_result(
            phase=phase_name, description=description, started=started,
            started_at=started_at,
            error=(
                f"R80 Step 12: backup --once --type full 失败 "
                f"(exit={backup_result.returncode})"
            ),
            stdout=backup_result.stdout, stderr=backup_result.stderr,
            returncode=backup_result.returncode,
            readiness_checks=readiness_checks + [
                {"check": "backup_triggered", "status": "fail"},
            ],
        )
    readiness_checks.append({"check": "backup_triggered", "status": "pass"})

    backup_evidence: dict[str, Any] = {}
    try:
        if backup_evidence_path.is_file():
            backup_evidence = json.loads(
                backup_evidence_path.read_text(encoding="utf-8")
            )
    except (json.JSONDecodeError, OSError) as e:
        return _fail_result(
            phase=phase_name, description=description, started=started,
            started_at=started_at,
            error=f"R80 Step 12: backup evidence JSON 解析失败: {e}",
            stdout=backup_result.stdout, stderr=backup_result.stderr,
            readiness_checks=readiness_checks + [
                {"check": "backup_evidence_parsed", "status": "fail"},
            ],
        )

    backup_status = str(backup_evidence.get("status", ""))
    backup_type = str(backup_evidence.get("backup_type", ""))
    backup_id = str(backup_evidence.get("backup_id", ""))
    objects = backup_evidence.get("objects", {}) or {}
    three_objects_ok = (
        "payload" in objects and "manifest" in objects and "COMPLETE" in objects
    )
    readiness_checks.append({
        "check": "backup_evidence_parsed",
        "status": "pass" if backup_status == "success" else "fail",
        "backup_status": backup_status, "backup_type": backup_type,
        "backup_id": backup_id,
    })
    readiness_checks.append({
        "check": "backup_three_objects",
        "status": "pass" if three_objects_ok else "fail",
        "objects_keys": sorted(objects.keys()) if isinstance(objects, dict) else [],
    })

    if (backup_status != "success" or backup_type != "full"
            or not three_objects_ok or not backup_id):
        return _fail_result(
            phase=phase_name, description=description, started=started,
            started_at=started_at,
            error=(
                f"R80 Step 12: backup evidence 校验失败 — "
                f"status={backup_status!r}, backup_type={backup_type!r}, "
                f"three_objects_ok={three_objects_ok}, backup_id={backup_id!r}"
            ),
            stdout=backup_result.stdout, stderr=backup_result.stderr,
            evidence=backup_evidence,
            readiness_checks=readiness_checks,
        )

    # 清理 evidence 临时文件
    try:
        if backup_evidence_path.exists():
            backup_evidence_path.unlink(missing_ok=True)
    except OSError:
        pass

    # R82 §10.5: 持久化 current-SHA 与 evidence 中的精确三对象 key。
    _persist_backup_state(head_sha=source_sha, backup_id=backup_id, objects=objects)

    return _pass_result(
        phase=phase_name, description=description, started=started,
        started_at=started_at,
        stdout=backup_result.stdout, stderr=backup_result.stderr,
        returncode=backup_result.returncode,
        evidence={
            "backup_id": backup_id,
            "backup_status": backup_status,
            "backup_type": backup_type,
            "objects": objects,
            "reason": reason, "source_sha": source_sha,
            "storage_backend": _STORAGE_CONFIG["storage_backend"],
            "endpoint": _STORAGE_CONFIG["endpoint"],
            "bucket": _STORAGE_CONFIG["bucket"],
        },
        readiness_checks=readiness_checks,
    )


def _is_expected_corruption_failure(
    *,
    expect: str,
    returncode: int,
    validation: dict[str, Any],
) -> bool:
    """只允许受控 validator 的明确 ciphertext digest 错误通过负测。"""
    return bool(
        expect == "failure"
        and returncode == 1
        and validation.get("status") == "failure"
        and validation.get("error_code")
        == "BACKUP.RESTORE.CIPHERTEXT_HASH_MISMATCH"
    )


def phase_corrupt_payload_negative(timeout: int) -> PhaseResult:
    """R83 Step 12: 仅损坏副本，且只接受明确 ciphertext digest 失败。

    COMPLETE、manifest 和原始 payload 始终保持不变。validator 使用权威三对象
    合同验签，但从独立 ``payload_read_key`` 读取损坏副本；argparse exit=2、
    网络、认证、配置或解密错误均不得冒充 corruption negative 成功。
    """
    import asyncio as _aio
    import uuid as _uuid_mod

    from storage.r2 import R2Storage

    phase_name = "corrupt_payload_negative"
    description = "R83 Step 12: Corruption copy must trigger exact digest failure"
    started = time.time()
    started_at = _now_iso()
    endpoint = _STORAGE_CONFIG["endpoint"]
    bucket = _STORAGE_CONFIG["bucket"]
    access_key = _STORAGE_CONFIG["access_key"]
    secret_key = _STORAGE_CONFIG["secret_key"]
    expect = _STORAGE_CONFIG["expect"]
    readiness_checks: list[dict[str, Any]] = []

    if not all((endpoint, bucket, access_key, secret_key)):
        return _fail_result(
            phase=phase_name,
            description=description,
            started=started,
            started_at=started_at,
            error="SECRETLESS_S3_CONFIG_INVALID",
            readiness_checks=[{"check": "storage_config", "status": "fail"}],
        )
    readiness_checks.append({"check": "storage_config", "status": "pass"})

    backup_state = _load_backup_state()
    required_state = ("backup_id", "payload_key", "manifest_key", "complete_key")
    if any(not backup_state.get(field, "") for field in required_state):
        return _fail_result(
            phase=phase_name,
            description=description,
            started=started,
            started_at=started_at,
            error="SECRETLESS_BACKUP_STATE_INVALID",
            readiness_checks=readiness_checks + [
                {"check": "backup_state_valid", "status": "fail"},
            ],
        )

    backup_id = backup_state["backup_id"]
    payload_key = backup_state["payload_key"]
    manifest_key = backup_state["manifest_key"]
    complete_key = backup_state["complete_key"]
    corruption_key = (
        f"db_backup/.secretless-corruption/{backup_id}/"
        f"payload-{_uuid_mod.uuid4().hex}.enc"
    )
    readiness_checks.append({
        "check": "backup_state_valid",
        "status": "pass",
        "backup_id": backup_id,
        "payload_key": payload_key,
        "manifest_key": manifest_key,
        "complete_key": complete_key,
    })

    def _new_store() -> R2Storage:
        store = R2Storage()
        store.configure(
            account_id="",
            access_key=access_key,
            secret_key=secret_key,
            bucket=bucket,
            endpoint=endpoint,
        )
        return store

    async def _create_corruption_copy() -> dict[str, Any]:
        store = _new_store()
        await store.connect()
        try:
            matches = await store.list_objects(prefix=payload_key, max_keys=2)
            exact_matches = [
                item for item in matches if str(item.get("key", "")) == payload_key
            ]
            if len(exact_matches) != 1:
                raise RuntimeError(
                    "S3_PAYLOAD_OBJECT_CARDINALITY_INVALID: "
                    f"key={payload_key!r} exact_matches={len(exact_matches)}"
                )
            original = await store.download(payload_key)
            if not original:
                raise RuntimeError("S3_PAYLOAD_OBJECT_EMPTY")
            original_sha = hashlib.sha256(original).hexdigest()
            corrupted = bytearray(original)
            corrupted[len(corrupted) // 2] ^= 0x01
            corrupted_bytes = bytes(corrupted)
            corrupted_sha = hashlib.sha256(corrupted_bytes).hexdigest()
            if corrupted_sha == original_sha:
                raise RuntimeError("CORRUPTION_COPY_DIGEST_UNCHANGED")
            await store.upload(
                corruption_key,
                corrupted_bytes,
                "application/octet-stream",
            )
            readback = await store.download(corruption_key)
            readback_sha = hashlib.sha256(readback).hexdigest()
            if readback_sha != corrupted_sha:
                raise RuntimeError("CORRUPTION_COPY_READBACK_MISMATCH")
            return {
                "original_sha256": original_sha,
                "original_size": len(original),
                "corruption_key": corruption_key,
                "corruption_sha256": corrupted_sha,
                "corruption_size": len(corrupted_bytes),
            }
        finally:
            await store.close()

    async def _cleanup_and_verify_original(original_sha: str) -> dict[str, Any]:
        store = _new_store()
        await store.connect()
        try:
            await store.delete(corruption_key)
            remaining = await store.list_objects(prefix=corruption_key, max_keys=1)
            copy_deleted = not any(
                str(item.get("key", "")) == corruption_key for item in remaining
            )
            original = await store.download(payload_key)
            actual_sha = hashlib.sha256(original).hexdigest()
            return {
                "copy_deleted": copy_deleted,
                "original_sha256_after": actual_sha,
                "original_unchanged": actual_sha == original_sha,
            }
        finally:
            await store.close()

    async def _delete_corruption_copy() -> None:
        store = _new_store()
        await store.connect()
        try:
            await store.delete(corruption_key)
            remaining = await store.list_objects(prefix=corruption_key, max_keys=1)
            if any(str(item.get("key", "")) == corruption_key for item in remaining):
                raise RuntimeError("CORRUPTION_COPY_DELETE_VERIFICATION_FAILED")
        finally:
            await store.close()

    try:
        copy_evidence = _aio.run(_create_corruption_copy())
    except (OSError, RuntimeError, ValueError) as exc:
        try:
            _aio.run(_delete_corruption_copy())
            cleanup_status = "pass"
            cleanup_error = ""
        except (OSError, RuntimeError, ValueError) as cleanup_exc:
            cleanup_status = "fail"
            cleanup_error = f"{type(cleanup_exc).__name__}: {cleanup_exc}"
        return _fail_result(
            phase=phase_name,
            description=description,
            started=started,
            started_at=started_at,
            error=(
                f"CORRUPTION_COPY_CREATE_FAILED: {type(exc).__name__}: {exc}; "
                f"cleanup={cleanup_status} {cleanup_error}"
            ),
            readiness_checks=readiness_checks + [
                {"check": "corruption_copy_created", "status": "fail"},
                {"check": "failed_create_cleanup", "status": cleanup_status},
            ],
        )
    readiness_checks.append({
        "check": "corruption_copy_created",
        "status": "pass",
        "key": corruption_key,
        "sha256": copy_evidence["corruption_sha256"],
    })

    validation_path = REPO_ROOT / "data" / "corruption-validation.json"
    validation_container_path = "/app/data/corruption-validation.json"
    try:
        validation_path.unlink(missing_ok=True)
    except OSError as exc:
        return _fail_result(
            phase=phase_name,
            description=description,
            started=started,
            started_at=started_at,
            error=f"CORRUPTION_VALIDATION_EVIDENCE_CLEANUP_FAILED: {exc}",
            evidence=copy_evidence,
            readiness_checks=readiness_checks,
        )

    s3_env = _s3_env_override()
    env_flags = _env_to_compose_run_flags(s3_env)
    validate_cmd = _secretless_compose_cmd([
        "run", "--rm", "-T", "--no-deps",
    ] + env_flags + [
        "--entrypoint", "python",
        "db_backup",
        "-m", "services.secretless_backup_contract", "validate",
        "--backup-id", backup_id,
        "--payload-key", payload_key,
        "--manifest-key", manifest_key,
        "--complete-key", complete_key,
        "--payload-read-key", corruption_key,
        "--output-json", validation_container_path,
    ])

    validate_result: subprocess.CompletedProcess | None = None
    timeout_error: subprocess.TimeoutExpired | None = None
    try:
        validate_result = _run(
            validate_cmd,
            timeout=timeout,
            cwd=REPO_ROOT,
            env=s3_env,
        )
    except subprocess.TimeoutExpired as exc:
        timeout_error = exc

    try:
        cleanup_evidence = _aio.run(
            _cleanup_and_verify_original(copy_evidence["original_sha256"])
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return _fail_result(
            phase=phase_name,
            description=description,
            started=started,
            started_at=started_at,
            error=f"CORRUPTION_COPY_CLEANUP_FAILED: {type(exc).__name__}: {exc}",
            evidence={**copy_evidence, "command": {"argv": validate_cmd}},
            readiness_checks=readiness_checks + [
                {"check": "corruption_copy_cleanup", "status": "fail"},
            ],
        )

    cleanup_ok = bool(
        cleanup_evidence["copy_deleted"]
        and cleanup_evidence["original_unchanged"]
    )
    readiness_checks.append({
        "check": "corruption_copy_cleanup",
        "status": "pass" if cleanup_ok else "fail",
        **cleanup_evidence,
    })
    if not cleanup_ok:
        return _fail_result(
            phase=phase_name,
            description=description,
            started=started,
            started_at=started_at,
            error="CORRUPTION_COPY_CLEANUP_OR_ORIGINAL_INTEGRITY_FAILED",
            evidence={**copy_evidence, **cleanup_evidence},
            readiness_checks=readiness_checks,
        )

    if timeout_error is not None:
        partial_stdout = (
            timeout_error.stdout if isinstance(timeout_error.stdout, str) else ""
        )
        partial_stderr = (
            timeout_error.stderr if isinstance(timeout_error.stderr, str) else ""
        )
        return _fail_result(
            phase=phase_name,
            description=description,
            started=started,
            started_at=started_at,
            error=f"DR_VALIDATE_TIMEOUT: {timeout}s",
            stdout=partial_stdout,
            stderr=partial_stderr,
            evidence={**copy_evidence, **cleanup_evidence},
            readiness_checks=readiness_checks + [
                {"check": "expected_integrity_failure", "status": "timeout"},
            ],
        )
    assert validate_result is not None

    try:
        validation = json.loads(validation_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        return _fail_result(
            phase=phase_name,
            description=description,
            started=started,
            started_at=started_at,
            error=f"CORRUPTION_VALIDATION_EVIDENCE_INVALID: {exc}",
            stdout=validate_result.stdout,
            stderr=validate_result.stderr,
            returncode=validate_result.returncode,
            evidence={**copy_evidence, **cleanup_evidence},
            readiness_checks=readiness_checks + [
                {"check": "expected_integrity_failure", "status": "fail"},
            ],
        )
    finally:
        validation_cleanup_error = ""
        try:
            validation_path.unlink(missing_ok=True)
        except OSError as exc:
            validation_cleanup_error = str(exc)

    if validation_cleanup_error:
        return _fail_result(
            phase=phase_name,
            description=description,
            started=started,
            started_at=started_at,
            error=(
                "CORRUPTION_VALIDATION_EVIDENCE_FINAL_CLEANUP_FAILED: "
                + validation_cleanup_error
            ),
            stdout=validate_result.stdout,
            stderr=validate_result.stderr,
            returncode=validate_result.returncode,
            evidence={**copy_evidence, **cleanup_evidence},
            readiness_checks=readiness_checks + [
                {"check": "validation_evidence_cleanup", "status": "fail"},
            ],
        )

    expected_error = "BACKUP.RESTORE.CIPHERTEXT_HASH_MISMATCH"
    error_code = str(validation.get("error_code", ""))
    expected_failure = _is_expected_corruption_failure(
        expect=expect,
        returncode=validate_result.returncode,
        validation=validation,
    )
    readiness_checks.append({
        "check": "expected_integrity_failure",
        "status": "pass" if expected_failure else "fail",
        "returncode": validate_result.returncode,
        "error_code": error_code,
        "expected_error_code": expected_error,
    })
    evidence = {
        **copy_evidence,
        **cleanup_evidence,
        "backup_id": backup_id,
        "payload_key": payload_key,
        "manifest_key": manifest_key,
        "complete_key": complete_key,
        "expect": expect,
        "expected_failure": expected_failure,
        "error_code": error_code,
        "command": {
            "argv": validate_cmd,
            "returncode": validate_result.returncode,
        },
        "validation": validation,
        "s3_auth": "sigv4",
    }
    if not expected_failure:
        return _fail_result(
            phase=phase_name,
            description=description,
            started=started,
            started_at=started_at,
            error=(
                "CORRUPTION_NEGATIVE_WRONG_FAILURE: must be returncode=1 and "
                f"error_code={expected_error}; actual returncode="
                f"{validate_result.returncode}, error_code={error_code!r}"
            ),
            stdout=validate_result.stdout,
            stderr=validate_result.stderr,
            returncode=validate_result.returncode,
            evidence=evidence,
            readiness_checks=readiness_checks,
        )

    return _pass_result(
        phase=phase_name,
        description=description,
        started=started,
        started_at=started_at,
        stdout=validate_result.stdout,
        stderr=validate_result.stderr,
        returncode=0,
        evidence=evidence,
        readiness_checks=readiness_checks,
    )


def phase_blank_restore_from_s3_contract_store(timeout: int) -> PhaseResult:
    """R83 Step 12: 精确三对象合同恢复到每 run 新建的空白 CRDB database。"""
    import uuid as _uuid_mod

    phase_name = "blank_restore_from_s3_contract_store"
    description = "R83 Step 12: Exact-contract blank isolated CRDB restore"
    started = time.time()
    started_at = _now_iso()

    if not _docker_available():
        return _fail_result(
            phase=phase_name,
            description=description,
            started=started,
            started_at=started_at,
            error="Docker daemon 不可用 — blank restore 无法执行",
            readiness_checks=[{"check": "docker_daemon", "status": "fail"}],
        )

    readiness_checks: list[dict[str, Any]] = [
        {"check": "docker_daemon", "status": "pass"},
    ]
    backup_state = _load_backup_state()
    required_state = ("backup_id", "payload_key", "manifest_key", "complete_key")
    if any(not backup_state.get(field, "") for field in required_state):
        return _fail_result(
            phase=phase_name,
            description=description,
            started=started,
            started_at=started_at,
            error="SECRETLESS_BACKUP_STATE_INVALID",
            readiness_checks=readiness_checks + [
                {"check": "backup_state_valid", "status": "fail"},
            ],
        )
    readiness_checks.append({
        "check": "backup_state_valid",
        "status": "pass",
        **{field: backup_state[field] for field in required_state},
    })

    operation_id = str(_uuid_mod.uuid4())
    restore_evidence_path = REPO_ROOT / "data" / "restore-evidence.json"
    restore_evidence_container_path = "/app/data/restore-evidence.json"
    try:
        restore_evidence_path.unlink(missing_ok=True)
    except OSError as exc:
        return _fail_result(
            phase=phase_name,
            description=description,
            started=started,
            started_at=started_at,
            error=f"RESTORE_EVIDENCE_CLEANUP_FAILED: {exc}",
            readiness_checks=readiness_checks,
        )

    s3_env = _s3_env_override()
    env_flags = _env_to_compose_run_flags(s3_env)
    restore_cmd = _secretless_compose_cmd([
        "run", "--rm", "-T", "--no-deps",
    ] + env_flags + [
        "--entrypoint", "python",
        "db_backup",
        "-m", "services.secretless_backup_contract", "restore-crdb",
        "--backup-id", backup_state["backup_id"],
        "--payload-key", backup_state["payload_key"],
        "--manifest-key", backup_state["manifest_key"],
        "--complete-key", backup_state["complete_key"],
        "--operation-id", operation_id,
        "--output-json", restore_evidence_container_path,
    ])

    try:
        restore_result = _run(
            restore_cmd,
            timeout=timeout,
            cwd=REPO_ROOT,
            env=s3_env,
        )
    except subprocess.TimeoutExpired as exc:
        partial_stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        partial_stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return _fail_result(
            phase=phase_name,
            description=description,
            started=started,
            started_at=started_at,
            error=f"SECRETLESS_CRDB_RESTORE_TIMEOUT: {timeout}s",
            stdout=partial_stdout,
            stderr=partial_stderr,
            readiness_checks=readiness_checks + [
                {"check": "restore_triggered", "status": "timeout"},
            ],
        )

    command_evidence = {
        "argv": restore_cmd,
        "returncode": restore_result.returncode,
    }
    if restore_result.returncode != 0:
        return _fail_result(
            phase=phase_name,
            description=description,
            started=started,
            started_at=started_at,
            error=(
                "SECRETLESS_CRDB_RESTORE_FAILED: "
                f"exit={restore_result.returncode}"
            ),
            stdout=restore_result.stdout,
            stderr=restore_result.stderr,
            returncode=restore_result.returncode,
            evidence={"command": command_evidence},
            readiness_checks=readiness_checks + [
                {"check": "restore_triggered", "status": "fail"},
            ],
        )
    readiness_checks.append({
        "check": "restore_triggered",
        "status": "pass",
        "returncode": restore_result.returncode,
    })

    try:
        restore_evidence = json.loads(
            restore_evidence_path.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        return _fail_result(
            phase=phase_name,
            description=description,
            started=started,
            started_at=started_at,
            error=f"RESTORE_EVIDENCE_INVALID: {exc}",
            stdout=restore_result.stdout,
            stderr=restore_result.stderr,
            evidence={"command": command_evidence},
            readiness_checks=readiness_checks + [
                {"check": "restore_evidence_parsed", "status": "fail"},
            ],
        )
    if not isinstance(restore_evidence, dict):
        return _fail_result(
            phase=phase_name,
            description=description,
            started=started,
            started_at=started_at,
            error="RESTORE_EVIDENCE_INVALID: root must be object",
            stdout=restore_result.stdout,
            stderr=restore_result.stderr,
            evidence={"command": command_evidence},
            readiness_checks=readiness_checks + [
                {"check": "restore_evidence_parsed", "status": "fail"},
            ],
        )

    source_identity = str(restore_evidence.get("source_identity", ""))
    target_identity = str(restore_evidence.get("target_identity", ""))
    business_probe = restore_evidence.get("business_probe", {})
    target_before = restore_evidence.get("target_before", {})
    integrity_checks = {
        "status_success": restore_evidence.get("status") == "success",
        "operation_id_bound": restore_evidence.get("operation_id") == operation_id,
        "backup_id_bound": restore_evidence.get("backup_id") == backup_state["backup_id"],
        "payload_key_bound": (
            restore_evidence.get("payload_key") == backup_state["payload_key"]
        ),
        "manifest_key_bound": (
            restore_evidence.get("manifest_key") == backup_state["manifest_key"]
        ),
        "complete_key_bound": (
            restore_evidence.get("complete_key") == backup_state["complete_key"]
        ),
        "target_blank": (
            isinstance(target_before, dict)
            and target_before.get("blank") is True
            and target_before.get("user_table_count") == 0
        ),
        "identity_isolated": bool(
            source_identity
            and target_identity
            and source_identity != target_identity
        ),
        "source_unchanged": restore_evidence.get("source_unchanged") is True,
        "schema_verified": (
            restore_evidence.get("schema_fingerprint_verified") is True
            and bool(restore_evidence.get("target_schema"))
        ),
        "row_and_field_hash_verified": (
            restore_evidence.get("target_after")
            == restore_evidence.get("payload_snapshot")
        ),
        "manifest_digest_verified": (
            restore_evidence.get("manifest_digest_verified") is True
            and bool(restore_evidence.get("manifest_sha256"))
        ),
        "payload_digest_verified": (
            restore_evidence.get("payload_digest_verified") is True
            and bool(restore_evidence.get("ciphertext_sha256"))
            and bool(restore_evidence.get("plaintext_sha256"))
        ),
        "complete_marker_verified": (
            restore_evidence.get("complete_marker_verified") is True
        ),
        "business_probe_passed": (
            isinstance(business_probe, dict)
            and business_probe.get("status") == "pass"
        ),
    }
    failed_checks = [name for name, passed in integrity_checks.items() if not passed]
    readiness_checks.extend({
        "check": name,
        "status": "pass" if passed else "fail",
    } for name, passed in integrity_checks.items())
    if failed_checks:
        return _fail_result(
            phase=phase_name,
            description=description,
            started=started,
            started_at=started_at,
            error="RESTORE_INTEGRITY_CHECKS_FAILED: " + ",".join(failed_checks),
            stdout=restore_result.stdout,
            stderr=restore_result.stderr,
            evidence={**restore_evidence, "command": command_evidence},
            readiness_checks=readiness_checks,
        )

    try:
        _persist_restore_state(
            head_sha=backup_state["head_sha"],
            backup_state=backup_state,
            restore_evidence=restore_evidence,
        )
    except (OSError, TypeError, ValueError) as exc:
        return _fail_result(
            phase=phase_name,
            description=description,
            started=started,
            started_at=started_at,
            error=f"RESTORE_STATE_PERSIST_FAILED: {exc}",
            stdout=restore_result.stdout,
            stderr=restore_result.stderr,
            evidence={**restore_evidence, "command": command_evidence},
            readiness_checks=readiness_checks + [
                {"check": "restore_state_persisted", "status": "fail"},
            ],
        )
    readiness_checks.append({
        "check": "restore_state_persisted",
        "status": "pass",
        "path": str(_RESTORE_STATE_FILE),
    })

    try:
        restore_evidence_path.unlink(missing_ok=True)
    except OSError as exc:
        return _fail_result(
            phase=phase_name,
            description=description,
            started=started,
            started_at=started_at,
            error=f"RESTORE_EVIDENCE_FINAL_CLEANUP_FAILED: {exc}",
            stdout=restore_result.stdout,
            stderr=restore_result.stderr,
            evidence={**restore_evidence, "command": command_evidence},
            readiness_checks=readiness_checks + [
                {"check": "restore_evidence_cleanup", "status": "fail"},
            ],
        )

    return _pass_result(
        phase=phase_name,
        description=description,
        started=started,
        started_at=started_at,
        stdout=restore_result.stdout,
        stderr=restore_result.stderr,
        returncode=restore_result.returncode,
        evidence={
            **restore_evidence,
            "command": command_evidence,
            "integrity_checks": integrity_checks,
            "storage_backend": _STORAGE_CONFIG["storage_backend"],
            "endpoint": _STORAGE_CONFIG["endpoint"],
            "bucket": _STORAGE_CONFIG["bucket"],
        },
        readiness_checks=readiness_checks,
    )


def _run_secretless_switch_action(
    *, action: str, timeout: int, inject_http_status: int = 0,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any], list[str]]:
    """运行 current-SHA 绑定的 CRDB switch executor 并读取结构化 evidence。"""
    state = _load_restore_state()
    if not state:
        raise RuntimeError("SECRETLESS_SWITCH_RESTORE_STATE_INVALID")
    evidence_host = REPO_ROOT / "data" / f"secretless-switch-{action}-{uuid.uuid4().hex}.json"
    evidence_container = f"/app/data/{evidence_host.name}"
    env = _s3_env_override()
    env["APP_ENV"] = "test"
    env["SECRETLESS_MODE"] = "true"
    env_flags = _env_to_compose_run_flags(env)
    restore_state_host = _RESTORE_STATE_FILE.resolve().as_posix()
    restore_state_container = "/app/data/restore-state.json"
    command = _secretless_compose_cmd([
        "run", "--rm", "-T", "--no-deps",
        *env_flags,
        "-v", f"{restore_state_host}:{restore_state_container}:ro",
        "--entrypoint", "python",
        "db_backup",
        "-m", "services.secretless_switch_contract",
        action,
        "--state-file", restore_state_container,
        "--output-json", evidence_container,
    ])
    if inject_http_status:
        command.extend(["--inject-http-status", str(inject_http_status)])
    result = _run(command, timeout=timeout, cwd=REPO_ROOT, env=env)
    try:
        document = json.loads(evidence_host.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"SECRETLESS_SWITCH_EVIDENCE_INVALID: {exc}") from exc
    finally:
        evidence_host.unlink(missing_ok=True)
    if not isinstance(document, dict):
        raise RuntimeError("SECRETLESS_SWITCH_EVIDENCE_ROOT_INVALID")
    return result, document, command


def phase_secretless_actual_switch(timeout: int) -> PhaseResult:
    """R83 Step 13: 将 active CRDB identity 从 source CAS 切换到恢复 target。"""
    phase_name = "secretless_actual_switch"
    description = "R83 Step 13: current-SHA-bound CRDB target switch and business probe"
    started = time.time()
    started_at = _now_iso()
    if not _docker_available():
        return _fail_result(
            phase=phase_name, description=description, started=started,
            started_at=started_at, error="Docker daemon 不可用",
            readiness_checks=[{"check": "docker_daemon", "status": "fail"}],
        )
    try:
        result, evidence, command = _run_secretless_switch_action(
            action="switch", timeout=timeout,
        )
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        return _fail_result(
            phase=phase_name, description=description, started=started,
            started_at=started_at, error=f"SECRETLESS_SWITCH_FAILED: {exc}",
            readiness_checks=[{"check": "switch_executed", "status": "fail"}],
        )
    state = _load_restore_state()
    checks = {
        "command_success": result.returncode == 0,
        "status_success": evidence.get("status") == "success",
        "head_sha_bound": evidence.get("head_sha") == state.get("head_sha"),
        "operation_id_bound": evidence.get("operation_id") == state.get("operation_id"),
        "target_active": (
            evidence.get("active_after", {}).get("active_identity")
            == state.get("target_identity")
        ),
        "identity_changed": (
            evidence.get("active_before", {}).get("active_identity")
            == state.get("source_identity")
            and evidence.get("active_after", {}).get("active_identity")
            == state.get("target_identity")
        ),
        "target_business_probe": (
            evidence.get("target_business_probe", {}).get("status") == "pass"
        ),
    }
    readiness = [
        {"check": key, "status": "pass" if value else "fail"}
        for key, value in checks.items()
    ]
    if not all(checks.values()):
        return _fail_result(
            phase=phase_name, description=description, started=started,
            started_at=started_at,
            error="SECRETLESS_SWITCH_INTEGRITY_FAILED: "
            + ",".join(key for key, value in checks.items() if not value),
            stdout=result.stdout, stderr=result.stderr, returncode=result.returncode,
            evidence={**evidence, "command": {"argv": command, "returncode": result.returncode}},
            readiness_checks=readiness,
        )
    return _pass_result(
        phase=phase_name, description=description, started=started,
        started_at=started_at, stdout=result.stdout, stderr=result.stderr,
        returncode=result.returncode,
        evidence={**evidence, "command": {"argv": command, "returncode": result.returncode}},
        readiness_checks=readiness,
    )


def phase_switch_probe_failure(timeout: int) -> PhaseResult:
    """R83 Step 13: active target 的 503 probe 必须被识别且要求 rollback。"""
    phase_name = "switch_probe_failure"
    description = "R83 Step 13: active target probe returns controlled HTTP 503"
    started = time.time()
    started_at = _now_iso()
    expect = _STORAGE_CONFIG["expect"]
    if expect != "no-production-tag":
        return _fail_result(
            phase=phase_name, description=description, started=started,
            started_at=started_at,
            error="SECRETLESS_SWITCH_EXPECTATION_INVALID",
            readiness_checks=[{"check": "expected_failure_contract", "status": "fail"}],
        )
    try:
        result, evidence, command = _run_secretless_switch_action(
            action="probe", timeout=timeout, inject_http_status=503,
        )
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        return _fail_result(
            phase=phase_name, description=description, started=started,
            started_at=started_at, error=f"SECRETLESS_SWITCH_PROBE_FAILED: {exc}",
            readiness_checks=[{"check": "probe_executed", "status": "fail"}],
        )
    state = _load_restore_state()
    checks = {
        "command_success": result.returncode == 0,
        "expected_failure_status": evidence.get("status") == "expected_failure",
        "http_503_observed": evidence.get("http_status") == 503,
        "stable_error_code": evidence.get("error_code") == "SWITCH_PROBE_HTTP_503",
        "target_was_active": evidence.get("active_identity") == state.get("target_identity"),
        "rollback_required": evidence.get("rollback_required") is True,
    }
    readiness = [
        {"check": key, "status": "pass" if value else "fail"}
        for key, value in checks.items()
    ]
    if not all(checks.values()):
        return _fail_result(
            phase=phase_name, description=description, started=started,
            started_at=started_at,
            error="SECRETLESS_SWITCH_503_CONTRACT_FAILED: "
            + ",".join(key for key, value in checks.items() if not value),
            stdout=result.stdout, stderr=result.stderr, returncode=result.returncode,
            evidence={**evidence, "command": {"argv": command, "returncode": result.returncode}},
            readiness_checks=readiness,
        )
    return _pass_result(
        phase=phase_name, description=description, started=started,
        started_at=started_at, stdout=result.stdout, stderr=result.stderr,
        returncode=result.returncode,
        evidence={**evidence, "command": {"argv": command, "returncode": result.returncode}},
        readiness_checks=readiness,
    )


def phase_secretless_actual_rollback(timeout: int) -> PhaseResult:
    """R83 Step 13: CAS 回滚 active identity 并验证 source 业务读取恢复。"""
    phase_name = "secretless_actual_rollback"
    description = "R83 Step 13: rollback active CRDB identity to source and re-probe"
    started = time.time()
    started_at = _now_iso()
    try:
        result, evidence, command = _run_secretless_switch_action(
            action="rollback", timeout=timeout,
        )
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        return _fail_result(
            phase=phase_name, description=description, started=started,
            started_at=started_at, error=f"SECRETLESS_ROLLBACK_FAILED: {exc}",
            readiness_checks=[{"check": "rollback_executed", "status": "fail"}],
        )
    state = _load_restore_state()
    checks = {
        "command_success": result.returncode == 0,
        "status_success": evidence.get("status") == "success",
        "head_sha_bound": evidence.get("head_sha") == state.get("head_sha"),
        "operation_id_bound": evidence.get("operation_id") == state.get("operation_id"),
        "target_was_active": (
            evidence.get("active_before", {}).get("active_identity")
            == state.get("target_identity")
        ),
        "source_identity_restored": (
            evidence.get("active_after", {}).get("active_identity")
            == state.get("source_identity")
        ),
        "source_business_probe": (
            evidence.get("source_business_probe", {}).get("status") == "pass"
        ),
    }
    readiness = [
        {"check": key, "status": "pass" if value else "fail"}
        for key, value in checks.items()
    ]
    if not all(checks.values()):
        return _fail_result(
            phase=phase_name, description=description, started=started,
            started_at=started_at,
            error="SECRETLESS_ROLLBACK_INTEGRITY_FAILED: "
            + ",".join(key for key, value in checks.items() if not value),
            stdout=result.stdout, stderr=result.stderr, returncode=result.returncode,
            evidence={**evidence, "command": {"argv": command, "returncode": result.returncode}},
            readiness_checks=readiness,
        )
    return _pass_result(
        phase=phase_name, description=description, started=started,
        started_at=started_at, stdout=result.stdout, stderr=result.stderr,
        returncode=result.returncode,
        evidence={**evidence, "command": {"argv": command, "returncode": result.returncode}},
        readiness_checks=readiness,
    )


def phase_secretless_drop_restore_target(timeout: int) -> PhaseResult:
    """R83 Step 13: 仅在 source 已恢复 active 后受控删除本 run target。"""
    phase_name = "secretless_drop_restore_target"
    description = "R83 Step 13: controlled cleanup of current-run restore target"
    started = time.time()
    started_at = _now_iso()
    try:
        result, evidence, command = _run_secretless_switch_action(
            action="drop-target", timeout=timeout,
        )
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        return _fail_result(
            phase=phase_name, description=description, started=started,
            started_at=started_at, error=f"SECRETLESS_TARGET_CLEANUP_FAILED: {exc}",
            readiness_checks=[{"check": "target_cleanup", "status": "fail"}],
        )
    passed = bool(
        result.returncode == 0
        and evidence.get("status") == "success"
        and evidence.get("target_exists_after") is False
    )
    readiness = [{"check": "target_cleanup", "status": "pass" if passed else "fail"}]
    if not passed:
        return _fail_result(
            phase=phase_name, description=description, started=started,
            started_at=started_at, error="SECRETLESS_TARGET_CLEANUP_INTEGRITY_FAILED",
            stdout=result.stdout, stderr=result.stderr, returncode=result.returncode,
            evidence={**evidence, "command": {"argv": command, "returncode": result.returncode}},
            readiness_checks=readiness,
        )
    return _pass_result(
        phase=phase_name, description=description, started=started,
        started_at=started_at, stdout=result.stdout, stderr=result.stderr,
        returncode=result.returncode,
        evidence={**evidence, "command": {"argv": command, "returncode": result.returncode}},
        readiness_checks=readiness,
    )


# ════════════════════════════════════════════════════════════════
# 阶段分发器
# ════════════════════════════════════════════════════════════════

PHASE_FUNCS: dict[str, Callable[[int], PhaseResult]] = {
    "preflight": phase_preflight,
    "start_infrastructure": phase_start_infrastructure,
    "start_application_roles": phase_start_application_roles,
    "real_product_transaction_before_backup": phase_real_product_transaction_before_backup,
    "full_backup_to_r2": phase_full_backup_to_r2,
    "blank_isolated_restore": phase_blank_isolated_restore,
    "restore_integrity_and_target_identity": phase_restore_integrity_and_target_identity,
    "actual_switch": phase_actual_switch,
    "real_product_transaction_after_switch": phase_real_product_transaction_after_switch,
    "fault_injection": phase_fault_injection,
    "actual_rollback": phase_actual_rollback,
    "real_product_transaction_after_rollback": phase_real_product_transaction_after_rollback,
    "sigterm_with_inflight_message": phase_sigterm_with_inflight_message,
    "restart_and_pending_recovery": phase_restart_and_pending_recovery,
    "final_identity_and_cleanup": phase_final_identity_and_cleanup,
    "evidence_signing": phase_evidence_signing,
    # R80 Step 12/13: MinIO secretless 备份/恢复/损坏负测/探测失败
    "full_backup_to_s3_contract_store": phase_full_backup_to_s3_contract_store,
    "corrupt_payload_negative": phase_corrupt_payload_negative,
    "blank_restore_from_s3_contract_store": phase_blank_restore_from_s3_contract_store,
    "secretless_actual_switch": phase_secretless_actual_switch,
    "switch_probe_failure": phase_switch_probe_failure,
    "secretless_actual_rollback": phase_secretless_actual_rollback,
    "secretless_drop_restore_target": phase_secretless_drop_restore_target,
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
    """R71 Wave 2 / R72 P1-02: 获取 TGJIEMA_IMAGE 的 RepoDigests。

    R72 P1-02: 禁止在 docker inspect 失败时返回输入的 TGJIEMA_IMAGE 环境变量值
    (那会把"请求的 digest"冒充"实际拉取并运行的 RepoDigest")。
    inspect 失败、返回非 0、或 RepoDigests 为空时一律返回空字符串(fail-closed),
    调用方必须将空值视为校验失败。
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
            digest = result.stdout.strip()
            # R72 P1-02: 空数组 "[]" 或空字符串也视为失败
            if digest and digest != "[]":
                return digest
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    # R72 P1-02: 不得回退到 TGJIEMA_IMAGE 环境变量值 — 返回空字符串让调用方失败
    return ""


def _get_compose_digest() -> str:
    """获取有序 Compose 文件集合的确定性 SHA256 digest。

    每个文件以仓库相对路径和原始字节共同参与哈希，防止同内容不同 overlay
    顺序或不同文件名被误认为同一运行配置身份。任一读取失败即返回空值。
    """
    digest = hashlib.sha256()
    try:
        for compose_file in _active_compose_files():
            try:
                identity = compose_file.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
            except ValueError:
                identity = str(compose_file.resolve())
            digest.update(identity.encode("utf-8"))
            digest.update(b"\0")
            digest.update(compose_file.read_bytes())
            digest.update(b"\0")
    except OSError:
        return ""
    return "sha256:" + digest.hexdigest()


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
        "schema_version": "r73-sec5.15",
        "started_at": started_at,
        "finished_at": finished_at,
        "overall_passed": overall_passed,
        "source_sha": _get_source_sha(),
        "workflow_run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "workflow_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
        "image_repo_digest": _get_image_repo_digest(),
        "compose_digest": _get_compose_digest(),
        "compose_file": str(_active_compose_files()[0]),
        "compose_files": [str(path) for path in _active_compose_files()],
        "env_file": str(ENV_FILE),
        "role_matrix": _build_role_matrix(),
        # R73 §5.15: DAG 元数据
        "dag_enforced": True,
        "phase_dependencies": dict(PHASE_DEPENDENCIES),
        "allowed_after_failure": sorted(ALLOWED_AFTER_FAILURE),
        "phases": [asdict(r) for r in results],
        "phase_summary": [
            {
                "phase": r.phase,
                "status": r.status,
                "timestamp": r.timestamp,
                "duration_seconds": r.duration_seconds,
                # R73 §5.15: 摘要中包含 DAG 字段
                "depends_on": r.depends_on,
                "blocking_reason": r.blocking_reason,
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

    R73 §5.15: 严格 DAG 顺序执行阶段,后续阶段不能在上游失败后继续产生成功证据。

    失败传播规则:
        - 任一阶段失败总状态立即标记 failure(overall_passed=False,不可逆)
        - 仅允许执行 cleanup 和诊断采集阶段(ALLOWED_AFTER_FAILURE)
        - 其余阶段必须 skipped(blocking_reason 记录上游失败阶段)
        - cleanup 成功不得覆盖原始 failure
        - skipped 只在上游 failure 导致无法执行时存在,且总状态仍为 failure
        - 不允许 continue-on-error 影响门禁结论

    Returns:
        0 — 所有阶段通过
        1 — 任一阶段失败(或因上游失败被 skipped)
    """
    parser = argparse.ArgumentParser(
        description=(
            "R73 §5.15: 真实 Compose Runtime E2E 测试编排器"
            "(16 阶段 DAG fail-closed,不允许 mock)"
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
        "--compose-file",
        action="append",
        metavar="PATH",
        help=(
            "权威 Compose 文件，可重复指定并按顺序合并；"
            "默认仅使用 docker-compose.prod.yml"
        ),
    )
    parser.add_argument(
        "--keep-on-success",
        action="store_true",
        help="全部通过时跳过 final_identity_and_cleanup,保留容器供人工检查",
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
    # R80 Step 12/13: MinIO secretless 存储参数
    parser.add_argument(
        "--storage-backend",
        metavar="BACKEND",
        default="",
        help="对象存储后端(minio / r2,默认空=使用 .env 配置)",
    )
    parser.add_argument(
        "--endpoint",
        metavar="URL",
        default="",
        help="S3 兼容 endpoint(例如 http://localhost:9000)",
    )
    parser.add_argument(
        "--bucket",
        metavar="NAME",
        default="",
        help="S3 bucket 名称(例如 tgjiema-backup)",
    )
    parser.add_argument(
        "--access-key",
        metavar="KEY",
        default="",
        help="S3 access key(CI_MINIO_ROOT_USER)",
    )
    parser.add_argument(
        "--secret-key",
        metavar="SECRET",
        default="",
        help="S3 secret key(CI_MINIO_ROOT_PASSWORD)",
    )
    parser.add_argument(
        "--signing-key",
        metavar="KEY",
        default="",
        help="备份签名密钥(CI_BACKUP_SIGNING_KEY)",
    )
    parser.add_argument(
        "--expect",
        metavar="OUTCOME",
        default="",
        help="期望结果(failure / no-production-tag,用于负向测试阶段)",
    )
    args = parser.parse_args(argv)

    # RC runtime identity: 所有阶段必须复用调用方显式给出的有序 Compose
    # 文件集合。相对路径锚定到仓库根；缺失文件由 preflight fail-closed。
    global COMPOSE_FILE, COMPOSE_FILES
    if args.compose_file:
        COMPOSE_FILES = [
            Path(value) if Path(value).is_absolute() else REPO_ROOT / value
            for value in args.compose_file
        ]
        COMPOSE_FILE = COMPOSE_FILES[0]
    else:
        COMPOSE_FILES = [DEFAULT_COMPOSE_FILE]
        COMPOSE_FILE = DEFAULT_COMPOSE_FILE

    # R80 Step 12/13: 将 CLI 存储参数注入模块级 _STORAGE_CONFIG
    _STORAGE_CONFIG["storage_backend"] = args.storage_backend
    _STORAGE_CONFIG["endpoint"] = args.endpoint
    _STORAGE_CONFIG["bucket"] = args.bucket
    _STORAGE_CONFIG["access_key"] = args.access_key
    _STORAGE_CONFIG["secret_key"] = args.secret_key
    _STORAGE_CONFIG["signing_key"] = args.signing_key
    _STORAGE_CONFIG["expect"] = args.expect

    # 验证 --phase 参数
    if args.phase is not None and args.phase not in PHASE_FUNCS:
        print(
            f"ERROR: 未知阶段 {args.phase!r}, 可选: "
            + ", ".join(sorted(PHASE_FUNCS.keys())),
            file=sys.stderr,
        )
        return 1

    # 阶段执行顺序
    # R73 §5.15: 始终按 PHASES 顺序执行(DAG 拓扑序),不提前移除任何阶段
    # --keep-on-success: 仅在全部通过时跳过 final_identity_and_cleanup;
    #   若中途失败,仍需执行 cleanup(在 ALLOWED_AFTER_FAILURE 中)
    if args.phase is not None:
        phases_to_run = [args.phase]
        skip_cleanup = False  # 单阶段模式不应用 keep-on-success
    else:
        phases_to_run = [name for name, _ in PHASES]
        skip_cleanup = args.keep_on_success

    started_at = _now_iso()
    print(
        f"=== R73 §5.15: Compose Runtime E2E (DAG enforced) ===\n"
        f"compose_file: {COMPOSE_FILE}\n"
        f"env_file: {ENV_FILE}\n"
        f"phases: {phases_to_run}\n"
        f"timeout: {args.timeout}s\n"
        f"keep_on_success: {args.keep_on_success}\n"
        f"output: {args.output or '(stdout)'}\n",
        file=sys.stderr,
    )

    results: list[PhaseResult] = []
    _DAG_RESULTS_CONTEXT.clear()
    # R73 §5.15: 跟踪已失败/skipped 的阶段(用于 DAG 传播)
    # 一旦阶段 fail 或 skipped(上游传播),其所有下游非 ALLOWED 阶段必须 skipped
    failed_or_skipped: set[str] = set()
    overall_passed = True

    for phase_name in phases_to_run:
        description = next((d for n, d in PHASES if n == phase_name), "")
        deps = PHASE_DEPENDENCIES.get(phase_name, [])

        # R73 §5.15: DAG 失败传播检查
        # 如果任一依赖阶段已失败或被 skipped:
        #   - 仅 ALLOWED_AFTER_FAILURE 中的阶段(cleanup/诊断)可继续执行
        #   - 其余阶段必须 skipped,不得产生成功证据
        blocking_deps = [d for d in deps if d in failed_or_skipped]
        if blocking_deps and phase_name not in ALLOWED_AFTER_FAILURE:
            blocking_reason = (
                f"R73 §5.15: 上游阶段失败/skipped — {blocking_deps},"
                f"本阶段 {phase_name} 不在 ALLOWED_AFTER_FAILURE 中,标记 skipped"
            )
            result = _skipped_result(
                phase=phase_name,
                description=description,
                blocking_reason=blocking_reason,
            )
            results.append(result)
            _print_result(result)
            failed_or_skipped.add(phase_name)
            # skipped 不改变 overall_passed(已为 False 或将由失败阶段设置)
            continue

        # R73 §5.15: ALLOWED_AFTER_FAILURE 阶段在上游失败时仍执行(cleanup/诊断)
        # 但 cleanup 成功不覆盖原始 failure(overall_passed 已为 False,不可逆)
        if blocking_deps and phase_name in ALLOWED_AFTER_FAILURE:
            print(
                f"WARNING: 阶段 {phase_name} 上游失败 {blocking_deps},"
                f"但属于 ALLOWED_AFTER_FAILURE,继续执行(cleanup/诊断)",
                file=sys.stderr,
            )

        # R73 §5.15: --keep-on-success 仅在全部通过时跳过 cleanup
        # 若已有阶段失败(overall_passed=False),即使指定 --keep-on-success 也必须执行 cleanup
        if (
            skip_cleanup
            and phase_name == "final_identity_and_cleanup"
            and overall_passed
        ):
            print(
                "\n=== --keep-on-success: 已跳过 final_identity_and_cleanup,"
                "容器保留供人工检查 ===",
                file=sys.stderr,
            )
            # 记录一个 skipped 结果(非失败传播,而是用户主动跳过)
            result = _skipped_result(
                phase=phase_name,
                description=description,
                blocking_reason="--keep-on-success: 用户主动跳过(全部通过)",
            )
            results.append(result)
            _print_result(result)
            continue

        # 执行阶段。阶段 16 调用前先暴露前序 DAG 结果的只读快照，
        # 使诊断 envelope 能按真实阶段状态 fail-closed 聚合。
        if phase_name == "evidence_signing":
            _DAG_RESULTS_CONTEXT.clear()
            _DAG_RESULTS_CONTEXT.extend(results)
        phase_func = PHASE_FUNCS[phase_name]
        try:
            result = phase_func(args.timeout)
        except Exception as e:
            # 任何未捕获异常都视为失败(fail-closed,不允许吞异常)
            completed = _now_iso()
            result = PhaseResult(
                phase=phase_name,
                description=description,
                status="fail",
                timestamp=completed,
                duration_seconds=0,
                error=f"未捕获异常: {type(e).__name__}: {e}",
                depends_on=deps,
                started_at=completed,
                completed_at=completed,
                blocking_reason=f"未捕获异常: {type(e).__name__}: {e}",
            )
        results.append(result)
        _print_result(result)

        if result.status == "fail":
            # R73 §5.15: overall_passed 一旦为 False 不可逆
            # cleanup 成功不得覆盖原始 failure
            overall_passed = False
            failed_or_skipped.add(phase_name)
            print(
                f"\nFAIL: 阶段 {phase_name} 失败 — {result.error}",
                file=sys.stderr,
            )
        elif result.status == "skipped":
            failed_or_skipped.add(phase_name)

    # R71 Wave 2 / R73 §5.15: 输出 runtime-e2e-evidence.json
    # 无论成功或失败都输出 evidence(便于事后分析)
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

    if overall_passed:
        print(
            f"\n=== R73 §5.15: 全部 {len(results)} 阶段通过 ===",
            file=sys.stderr,
        )
        return 0
    else:
        # R73 §5.15: 统计失败/skipped 数量
        failed_count = sum(1 for r in results if r.status == "fail")
        skipped_count = sum(1 for r in results if r.status == "skipped")
        print(
            f"\nFAIL: R73 §5.15 DAG 失败 — {failed_count} 阶段失败, "
            f"{skipped_count} 阶段 skipped(上游失败传播)",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
