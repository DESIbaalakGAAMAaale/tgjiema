#!/usr/bin/env python3
"""R76 O12 — Secretless Release Gates 统一入口(15 阶段严格顺序)。

═══════════════════════════════════════════════════════════════════════════════
R76 整改目标(O12):
  提供单一入口命令,新拉取仓库后只需 Docker 和 Python,
  不需要任何个人或生产凭证,即可一条命令完整跑通全部功能。

严格顺序(报告 O12):
  1.  preflight        — 确认真实凭证变量全部不存在
  2.  gen-creds        — 生成单次 run 临时 key 和 MinIO 临时凭据
  3.  compose-config   — docker compose ... config 验证
  4.  start-infra      — 启动基础设施并等待 health
  5.  migrate          — 运行 migration
  6.  start-apps       — 启动全部应用角色
  7.  normal-tx        — 执行正常业务交易
  8.  fault-injection  — 执行 401/429/500/timeout/duplicate
  9.  backup-restore   — 执行备份、损坏对象负测、blank restore
  10. switch-rollback  — 执行 switch、switch probe failure、rollback
  11. manifest-verify  — 执行 manifest 生成、文件重 hash 和验签
  12. deploy-state-mtx — 执行 deployment simulator 成功/失败矩阵
  13. gen-result       — 生成 artifacts/secretless-e2e/result.json
  14. cleanup          — finally 执行 docker compose down -v --remove-orphans
  15. final-verdict    — 任一失败返回 1,全部通过才打印 SECRETLESS FUNCTIONAL GO

退出码:
  0 — 全部通过(打印 SECRETLESS FUNCTIONAL GO)
  1 — 任一阶段失败(禁止 cleanup 覆盖原失败)

凭证来源:
  - 所有临时凭证单次 run 生成,进程结束销毁
  - 禁止读取 secrets.TEST_* / secrets.R2_* / secrets.COCKROACHDB_*
  - 禁止读取真实 Bot Token / R2 Key / 生产 CRDB Key / 生产部署 Hook

使用:
  make secretless-e2e          # 通过 Makefile
  python scripts/run_secretless_release_gates.py
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

# ════════════════════════════════════════════════════════════════
# R78 10.3: PhaseResult — 稳定阶段状态数据结构
# ════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class PhaseResult:
    """R78 10.3: 机器可读阶段结果,支持稳定 error_code 分类。

    基础设施失败使用稳定错误码 `SECRETLESS_INFRA_CRDB_UNHEALTHY`;
    测试失败使用 `SECRETLESS_TEST_FAILURE`。
    最终 result.json 必须在任何退出路径都存在。
    """

    name: str
    status: Literal["success", "failure", "skipped"]
    started_at: str
    finished_at: str
    exit_code: int
    error_code: str | None = None
    artifacts: tuple[str, ...] = ()
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
        }
        if self.error_code:
            d["error_code"] = self.error_code
        if self.artifacts:
            d["artifacts"] = list(self.artifacts)
        if self.detail:
            d["detail"] = self.detail
        return d


# ════════════════════════════════════════════════════════════════
# 稳定错误码(报告 10.3)
# ════════════════════════════════════════════════════════════════

ERROR_SECRETLESS_INFRA_CRDB_UNHEALTHY = "SECRETLESS_INFRA_CRDB_UNHEALTHY"
ERROR_SECRETLESS_INFRA_MINIO_UNHEALTHY = "SECRETLESS_INFRA_MINIO_UNHEALTHY"
ERROR_SECRETLESS_INFRA_PROVIDER_UNHEALTHY = "SECRETLESS_INFRA_PROVIDER_UNHEALTHY"
ERROR_SECRETLESS_TEST_FAILURE = "SECRETLESS_TEST_FAILURE"
ERROR_SECRETLESS_MIGRATION_FAILURE = "SECRETLESS_MIGRATION_FAILURE"
ERROR_SECRETLESS_COMPOSE_CONFIG_FAILURE = "SECRETLESS_COMPOSE_CONFIG_FAILURE"
ERROR_SECRETLESS_SERVICE_GRAPH = "SECRETLESS_SERVICE_GRAPH_VIOLATION"
ERROR_SECRETLESS_CLEANUP_FAILURE = "SECRETLESS_CLEANUP_FAILURE"

# ════════════════════════════════════════════════════════════════
# 常量与配置
# ════════════════════════════════════════════════════════════════

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACT_DIR = REPO_ROOT / "artifacts" / "secretless-e2e"
COMPOSE_FILES = ["docker-compose.yml", "docker-compose.secretless.yml"]
RESULT_JSON = ARTIFACT_DIR / "result.json"
FINAL_VERDICT_JSON = ARTIFACT_DIR / "step20" / "final-verdict.json"
RUN_OWNED_ARTIFACTS: tuple[str, ...] = (
    "phases",
    "state",
    "step12",
    "step13",
    "step14",
    "step15",
    "step20",
    "compose-resolved.json",
    "service-graph.json",
    "failure-summary.json",
    "result.json",
)
REQUIRED_PHASE_FILES: dict[str, tuple[int, str, str]] = {
    "start-infra": (7, "infrastructure", "07-infrastructure.json"),
    "migrate": (8, "migration", "08-migration.json"),
    "start-apps": (9, "start-apps", "09-start-apps.json"),
    "normal-tx": (10, "normal-transaction", "10-normal-transaction.json"),
    "fault-injection": (11, "fault-matrix", "11-fault-matrix.json"),
    "backup-restore": (12, "backup-restore", "12-backup-restore.json"),
    "switch-rollback": (13, "switch-rollback", "13-switch-rollback.json"),
    "manifest-verify": (14, "candidate-manifest", "14-candidate-manifest.json"),
    "deploy-state-mtx": (15, "deployment-matrix", "15-deployment-matrix.json"),
}

# 禁止存在的真实凭证环境变量(报告 O12 step 1)
FORBIDDEN_ENV_VARS: tuple[str, ...] = (
    "TEST_UPLOAD_BOT_TOKEN",
    "TEST_INDEX_BOT_TOKEN",
    "TEST_DISPLAY_BOT_TOKEN",
    "TEST_ADMIN_BOT_TOKEN",
    "TEST_MON_BOT_TOKEN",
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "COCKROACHDB_URL",
    "DEPLOY_HOOK_URL",
    "DEPLOY_PROBE_URL",
    "BACKUP_SIGNING_KEY",
)

# 基础设施服务(报告 O12 step 4 / R79 §10.2 单 CRDB 拓扑)
# R79 §10.4: 权威服务清单 — 只启动这些服务,不再同时启动
# production 与 secretless 两套 CRDB。
INFRA_SERVICES: tuple[str, ...] = (
    "redis",
    "redis-acl-init",
    "cockroachdb",
    "minio",
    "minio-init",
    "provider-sim",
)

# 应用角色(报告 O12 step 6)
APP_ROLES: tuple[str, ...] = (
    "db_writer",
    "crdb_sync",
    "up",
    "idx",
    "dsp",
)

# 容器健康检查超时(秒)
HEALTH_TIMEOUT = 90
HEALTH_INTERVAL = 3

# Provider simulator 容器名(用于 health 检查)
PROVIDER_SIM_CONTAINER = "tgjiema-provider-sim"
MINIO_CONTAINER = "tgjiema-minio"
# R79 §10.2: 单 CRDB 拓扑 — 唯一 CRDB 容器名(基础服务键 cockroachdb)
CRDB_CONTAINER = "tgjiema-cockroachdb"


# ════════════════════════════════════════════════════════════════
# 工具函数
# ════════════════════════════════════════════════════════════════


class StageResult:
    """单阶段执行结果(R78 10.3: 兼容 PhaseResult 字段)。"""

    def __init__(
        self,
        name: str,
        status: str,  # "pass" | "fail"
        detail: str = "",
        duration: float = 0.0,
        error_code: str | None = None,
        artifacts: tuple[str, ...] = (),
    ):
        self.name = name
        self.status = status
        self.detail = detail
        self.duration = duration
        # R78 10.3: 稳定错误码 + artifacts
        self.error_code = error_code
        self.artifacts = artifacts

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "duration_seconds": round(self.duration, 2),
        }
        if self.error_code:
            d["error_code"] = self.error_code
        if self.artifacts:
            d["artifacts"] = list(self.artifacts)
        return d


def log(msg: str, *, level: str = "INFO") -> None:
    """统一日志输出。"""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] [{level}] {msg}", flush=True)


def stage_header(stage_num: int, name: str, total: int = 15) -> None:
    """阶段标题。"""
    bar = "═" * 60
    print(f"\n{bar}", flush=True)
    print(f"  Stage {stage_num}/{total}: {name}", flush=True)
    print(f"{bar}", flush=True)


def run_command(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 300,
    capture: bool = False,
) -> tuple[int, str]:
    """运行命令,返回 (exit_code, output)。

    Args:
        cmd: 命令和参数列表
        cwd: 工作目录
        env: 环境变量(覆盖默认)
        timeout: 超时秒数
        capture: 是否捕获输出

    Returns:
        (exit_code, stdout+stderr)
    """
    full_env = os.environ.copy()
    if env:
        full_env.update(env)

    try:
        result = subprocess.run(
            cmd,
            cwd=cwd or REPO_ROOT,
            env=full_env,
            timeout=timeout,
            capture_output=capture,
            text=True,
            shell=False,
        )
        output = ""
        if capture:
            output = (result.stdout or "") + (result.stderr or "")
        return result.returncode, output
    except subprocess.TimeoutExpired:
        return 124, f"Command timed out after {timeout}s: {' '.join(cmd)}"
    except FileNotFoundError as exc:
        return 127, f"Command not found: {exc}"


def docker_compose_cmd(subcmd: list[str]) -> list[str]:
    """构造 docker compose 命令。"""
    cmd: list[str] = ["docker", "compose"]
    for f in COMPOSE_FILES:
        cmd.extend(["-f", f])
    cmd.extend(subcmd)
    return cmd


def gen_hex(n_bytes: int = 32) -> str:
    """生成密码学安全随机 hex。"""
    return secrets.token_hex(n_bytes)


def _reset_run_owned_artifacts() -> None:
    """删除本工具上一 run 的已知 evidence，避免旧 phase 越权放行。

    路径必须是 ARTIFACT_DIR 的直接子项且在固定白名单中；不扫描、不触碰
    artifacts-download、outputs 或用户创建的其他文件。
    """
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    artifact_root = ARTIFACT_DIR.resolve()
    for relative in RUN_OWNED_ARTIFACTS:
        path = ARTIFACT_DIR / relative
        resolved = path.resolve()
        if resolved.parent != artifact_root:
            raise RuntimeError(f"RUN_ARTIFACT_PATH_OUTSIDE_ROOT:{relative}")
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()


def _write_required_phase(stage_key: str) -> None:
    """仅在对应本地阶段真实通过后写入 Step 7—15 权威 phase marker。"""
    step, name, filename = REQUIRED_PHASE_FILES[stage_key]
    phase_dir = ARTIFACT_DIR / "phases"
    phase_dir.mkdir(parents=True, exist_ok=True)
    document = {
        "step": step,
        "name": name,
        "status": "success",
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    path = phase_dir / filename
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


# ════════════════════════════════════════════════════════════════
# 阶段实现
# ════════════════════════════════════════════════════════════════


def stage_1_preflight() -> StageResult:
    """阶段 1:preflight — 确认真实凭证变量全部不存在。"""
    stage_header(1, "Preflight — verify no real credentials present")
    start = time.time()
    violations: list[str] = []
    for var in FORBIDDEN_ENV_VARS:
        val = os.environ.get(var, "")
        if val:
            violations.append(f"{var} is set — secretless CI must not use real credentials")
            log(f"VIOLATION: {var} is set", level="ERROR")
    if violations:
        return StageResult(
            "preflight",
            "fail",
            f"{len(violations)} forbidden env vars present: {', '.join(violations[:3])}...",
            time.time() - start,
        )
    log("✓ No real credentials present — proceeding with secretless mode")
    return StageResult("preflight", "pass", "no forbidden env vars", time.time() - start)


def stage_2_gen_creds() -> tuple[StageResult, dict[str, str]]:
    """阶段 2:生成单次 run 临时凭据。"""
    stage_header(2, "Generate ephemeral credentials (single-run only)")
    start = time.time()
    # R78 10.9 / R79 §10.2: 集中化 CRDB DSN — 由 contract 单一真源生成,
    # 所有服务引用同一值;单 CRDB 拓扑 host=cockroachdb
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from secretless_crdb_contract import (
        CRDB_DATABASE,
        CRDB_HOST,
        CRDB_SQL_PORT,
        build_secretless_crdb_url,
    )

    creds: dict[str, str] = {
        # Redis ACL
        "REDIS_HEALTH_PASSWORD": gen_hex(16),
        "REDIS_WRITER_PASSWORD": gen_hex(16),
        "REDIS_READER_PASSWORD": gen_hex(16),
        "REDIS_ADMIN_PASSWORD": gen_hex(16),
        # MinIO
        "CI_MINIO_ROOT_USER": "minio-ci",
        "CI_MINIO_ROOT_PASSWORD": gen_hex(24),
        # Provider contract
        "CI_PROVIDER_CONTRACT_TOKEN": gen_hex(24),
        "CI_PROVIDER_RECEIPT_KEY": gen_hex(32),
        # Restore / evidence / manifest HMAC
        "CI_BACKUP_SIGNING_KEY": gen_hex(32),
        "CI_MANIFEST_SIGNING_KEY": gen_hex(32),
        # R79 §10.2: 单 CRDB DSN 契约(单一真源 secretless_crdb_contract)
        "SECRETLESS_CRDB_HOST": CRDB_HOST,
        "SECRETLESS_CRDB_SQL_PORT": str(CRDB_SQL_PORT),
        "SECRETLESS_CRDB_DATABASE": CRDB_DATABASE,
        "SECRETLESS_CRDB_URL": build_secretless_crdb_url(),
    }
    # 写入 os.environ 供后续阶段使用
    for k, v in creds.items():
        os.environ[k] = v
    log(f"✓ Generated {len(creds)} ephemeral credentials (single-run, in-memory only)")
    log(f"✓ CRDB DSN contract: {creds['SECRETLESS_CRDB_URL']}")
    return (
        StageResult("gen-creds", "pass", f"{len(creds)} creds generated", time.time() - start),
        creds,
    )


def stage_3_compose_config() -> StageResult:
    """阶段 3:docker compose ... config 验证 + resolved 服务图硬门禁。"""
    stage_header(3, "docker compose config validation + service graph gate")
    start = time.time()
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    resolved_json = ARTIFACT_DIR / "compose-resolved.json"
    # R79 §10.3: 导出 resolved config(JSON)供服务图门禁与失败诊断使用
    cmd = docker_compose_cmd(["config", "--format", "json"])
    rc, output = run_command(cmd, capture=True, timeout=60)
    if rc != 0:
        # 只兼容明确不支持 --format 的旧 Compose。配置、插值、语法等真实错误
        # 必须保留原 returncode，禁止通过无条件 YAML fallback 被吞掉。
        if "unknown flag" not in output.lower():
            return StageResult(
                "compose-config",
                "fail",
                f"docker compose config failed (rc={rc}): {output[:500]}",
                time.time() - start,
                error_code=ERROR_SECRETLESS_COMPOSE_CONFIG_FAILURE,
            )
        cmd = docker_compose_cmd(["config"])
        rc, output = run_command(cmd, capture=True, timeout=60)
    if rc != 0:
        return StageResult(
            "compose-config",
            "fail",
            f"docker compose config compatibility fallback failed (rc={rc}): {output[:500]}",
            time.time() - start,
            error_code=ERROR_SECRETLESS_COMPOSE_CONFIG_FAILURE,
        )
    resolved_json.write_text(output, encoding="utf-8")

    # R79 §10.3 / P1-01: resolved 服务图硬门禁(单 CRDB / 依赖图 / DSN / 隔离 / 加固)
    graph_artifact = ARTIFACT_DIR / "service-graph.json"
    gate_cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "validate_secretless_service_graph.py"),
        str(resolved_json),
        "--export-graph",
        str(graph_artifact),
    ]
    rc, gate_output = run_command(gate_cmd, capture=True, timeout=60)
    if rc != 0:
        return StageResult(
            "compose-config",
            "fail",
            f"service graph gate failed (rc={rc}): {gate_output[:800]}",
            time.time() - start,
            error_code=ERROR_SECRETLESS_SERVICE_GRAPH,
            artifacts=(str(resolved_json), str(graph_artifact)),
        )
    log("✓ docker compose config validation passed")
    log("✓ service graph gate passed (single CRDB topology)")
    return StageResult(
        "compose-config",
        "pass",
        "compose files valid + single CRDB graph verified",
        time.time() - start,
        artifacts=(str(resolved_json), str(graph_artifact)),
    )


def _wait_container_healthy(container: str, timeout: int = HEALTH_TIMEOUT) -> bool:
    """等待容器健康。"""
    for i in range(timeout // HEALTH_INTERVAL):
        cmd = [
            "docker",
            "inspect",
            "--format",
            "{{.State.Health.Status}}",
            container,
        ]
        rc, output = run_command(cmd, capture=True, timeout=10)
        status = output.strip()
        if status == "healthy":
            return True
        if i % 5 == 0:
            log(f"  waiting for {container}: status={status} (attempt {i + 1})")
        time.sleep(HEALTH_INTERVAL)
    return False


def _collect_infra_failure_evidence(failed_container: str) -> tuple[str, ...]:
    """R79 §10.4 / P1-02: 基础设施失败时自动提取根因证据。

    写出 artifact:
      - compose-ps.txt             当前容器状态
      - <container>-inspect.json   docker inspect 全量(Status/ExitCode/Error/Health)
      - <container>-diff.txt       docker diff(rootfs 变更)
      - <container>.log            最后 300 行日志
      - compose-resolved.json      resolved Compose 配置(stage 3 已生成)
      - service-graph.json         当前服务依赖图(stage 3 已生成)
    """
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    artifacts: list[str] = []

    def _capture(cmd: list[str], dest: Path, timeout: int = 30) -> None:
        rc, out = run_command(cmd, capture=True, timeout=timeout)
        dest.write_text(out if out else f"(rc={rc}, no output)", encoding="utf-8")
        artifacts.append(str(dest))

    _capture(
        docker_compose_cmd(["ps", "-a"]),
        ARTIFACT_DIR / "compose-ps.txt",
    )
    _capture(
        ["docker", "inspect", failed_container],
        ARTIFACT_DIR / f"{failed_container}-inspect.json",
    )
    _capture(
        ["docker", "diff", failed_container],
        ARTIFACT_DIR / f"{failed_container}-diff.txt",
    )
    _capture(
        docker_compose_cmd(["logs", "--no-color", "--tail", "300", failed_container]),
        ARTIFACT_DIR / f"{failed_container}.log",
    )
    log(f"Infrastructure failure evidence collected: {artifacts}")
    return tuple(artifacts)


def stage_4_start_infra() -> StageResult:
    """阶段 4:启动基础设施并等待 health(权威服务清单,R79 §10.4)。"""
    stage_header(4, "Start secretless infrastructure")
    start = time.time()
    # R79 §10.4: 只启动权威基础设施服务清单 — 单 CRDB 拓扑下
    # 不再同时等待 production 与 secretless 两套 CRDB。
    cmd = docker_compose_cmd(["up", "-d", *INFRA_SERVICES])
    rc, output = run_command(cmd, capture=True, timeout=120)
    if rc != 0:
        evidence = _collect_infra_failure_evidence(CRDB_CONTAINER)
        return StageResult(
            "start-infra",
            "fail",
            f"docker compose up failed (rc={rc}): {output[:500]}",
            time.time() - start,
            error_code=ERROR_SECRETLESS_INFRA_CRDB_UNHEALTHY,
            artifacts=evidence,
        )
    # 等待健康(权威容器清单)
    containers = [PROVIDER_SIM_CONTAINER, MINIO_CONTAINER, CRDB_CONTAINER]
    for c in containers:
        log(f"Waiting for {c} to become healthy...")
        if not _wait_container_healthy(c):
            # R79 §10.4: 失败时提取完整根因证据(inspect/diff/300 行日志)
            evidence = _collect_infra_failure_evidence(c)
            # R78 10.3: 稳定错误码区分基础设施组件
            error_code = {
                CRDB_CONTAINER: ERROR_SECRETLESS_INFRA_CRDB_UNHEALTHY,
                MINIO_CONTAINER: ERROR_SECRETLESS_INFRA_MINIO_UNHEALTHY,
                PROVIDER_SIM_CONTAINER: ERROR_SECRETLESS_INFRA_PROVIDER_UNHEALTHY,
            }.get(c, "SECRETLESS_INFRA_UNHEALTHY")
            return StageResult(
                "start-infra",
                "fail",
                f"{c} did not become healthy — evidence: {evidence}",
                time.time() - start,
                error_code=error_code,
                artifacts=evidence,
            )
        log(f"✓ {c} is healthy")
    log("✓ All infrastructure services healthy")
    return StageResult("start-infra", "pass", "infra healthy", time.time() - start)


def stage_5_migrate() -> StageResult:
    """阶段 5:运行 migration。"""
    stage_header(5, "Run database migration")
    start = time.time()
    cmd = docker_compose_cmd(["run", "--rm", "migration"])
    rc, output = run_command(cmd, capture=True, timeout=300)
    if rc != 0:
        return StageResult(
            "migrate",
            "fail",
            f"migration failed (rc={rc}): {output[:1000]}",
            time.time() - start,
        )
    log("✓ Migration completed successfully")
    return StageResult("migrate", "pass", "migration applied", time.time() - start)


def stage_6_start_apps() -> StageResult:
    """阶段 6:启动全部应用角色。"""
    stage_header(6, "Start all application roles")
    start = time.time()
    cmd = docker_compose_cmd(["up", "-d", *APP_ROLES])
    rc, output = run_command(cmd, capture=True, timeout=120)
    if rc != 0:
        return StageResult(
            "start-apps",
            "fail",
            f"docker compose up apps failed (rc={rc}): {output[:500]}",
            time.time() - start,
        )
    # 等待应用启动
    log("Waiting 15s for application roles to initialize...")
    time.sleep(15)
    # 验证容器运行中
    ps_cmd = docker_compose_cmd(["ps"])
    _, ps_output = run_command(ps_cmd, capture=True, timeout=15)
    log(f"Containers status:\n{ps_output}")
    log("✓ All application roles started")
    return StageResult("start-apps", "pass", f"{len(APP_ROLES)} roles started", time.time() - start)


def stage_7_normal_transaction() -> StageResult:
    """阶段 7:执行正常业务交易。"""
    stage_header(7, "Normal business transaction")
    start = time.time()
    provider_url = "http://localhost:8088"
    provider_token = os.environ.get("CI_PROVIDER_CONTRACT_TOKEN", "")
    # R76 §10.4: --receipt-key 必填(receipt HMAC 验签密钥)
    receipt_key = os.environ.get("CI_PROVIDER_RECEIPT_KEY", "")
    cmd = [
        "python",
        "scripts/e2e_update_adapter.py",
        "--provider-url",
        provider_url,
        "--provider-token",
        provider_token,
        "--app-url",
        provider_url,
        "--receipt-key",
        receipt_key,
        "--mode",
        "normal-transaction",
        "--timeout",
        "120",
    ]
    rc, output = run_command(cmd, capture=True, timeout=180)
    if rc != 0:
        # 输出应用日志便于诊断
        logs_cmd = docker_compose_cmd(["logs", "--tail", "100", "up", "idx", "dsp", "db_writer"])
        _, logs_output = run_command(logs_cmd, capture=True, timeout=30)
        return StageResult(
            "normal-tx",
            "fail",
            f"normal transaction failed (rc={rc}): {output[:500]}\nApp logs:\n{logs_output[:1000]}",
            time.time() - start,
        )
    log("✓ Normal business transaction completed")
    return StageResult("normal-tx", "pass", "transaction delivered", time.time() - start)


def stage_8_fault_injection() -> StageResult:
    """阶段 8:执行 401/429/500/timeout/duplicate 故障注入矩阵。"""
    stage_header(8, "Fault injection matrix (401/429/500/timeout/duplicate)")
    start = time.time()
    provider_url = "http://localhost:8088"
    provider_token = os.environ.get("CI_PROVIDER_CONTRACT_TOKEN", "")

    faults = [
        ("401", "failure", 60),
        ("429", "retry-then-success", 60),
        ("500", "bounded-retry-then-failure", 60),
        ("timeout", "timeout-failure", 30),
        ("duplicate", "idempotent-success", 60),
    ]

    # R76 §10.4: --receipt-key 必填(receipt HMAC 验签密钥)
    receipt_key = os.environ.get("CI_PROVIDER_RECEIPT_KEY", "")
    for fault, expect, timeout in faults:
        log(f"  Injecting fault={fault} (expect={expect})...")
        cmd = [
            "python",
            "scripts/e2e_update_adapter.py",
            "--provider-url",
            provider_url,
            "--provider-token",
            provider_token,
            "--app-url",
            provider_url,
            "--receipt-key",
            receipt_key,
            "--mode",
            "fault-injection",
            "--fault",
            fault,
            "--expect",
            expect,
            "--timeout",
            str(timeout),
        ]
        rc, _ = run_command(cmd, capture=True, timeout=timeout + 30)
        if rc != 0:
            return StageResult(
                "fault-injection",
                "fail",
                f"fault={fault} expected={expect} failed (rc={rc})",
                time.time() - start,
            )
        log(f"  ✓ fault={fault} passed")

    log("✓ Fault injection matrix passed")
    return StageResult(
        "fault-injection",
        "pass",
        f"{len(faults)} fault scenarios passed",
        time.time() - start,
    )


def stage_9_backup_restore() -> StageResult:
    """阶段 9:执行备份、损坏对象负测、blank restore。"""
    stage_header(9, "Backup, corruption negative test, blank restore")
    start = time.time()
    step_dir = ARTIFACT_DIR / "step12"
    step_dir.mkdir(parents=True, exist_ok=True)
    common_args = [
        "--storage-backend", "minio",
        "--endpoint", "http://localhost:9000",
        "--bucket", "tgjiema-backup",
        "--access-key", os.environ.get("CI_MINIO_ROOT_USER", ""),
        "--secret-key", os.environ.get("CI_MINIO_ROOT_PASSWORD", ""),
        "--signing-key", os.environ.get("CI_BACKUP_SIGNING_KEY", ""),
    ]
    phases = (
        ("full_backup_to_s3_contract_store", (), 300),
        ("corrupt_payload_negative", ("--expect", "failure"), 120),
        ("blank_restore_from_s3_contract_store", (), 300),
    )
    evidence_paths: list[str] = []
    for phase, extra, timeout in phases:
        evidence = step_dir / f"{phase}.json"
        log(f"  Running {phase}...")
        cmd = [
            sys.executable,
            "scripts/compose_runtime_e2e.py",
            "--phase", phase,
            *common_args,
            *extra,
            "--output", str(evidence),
        ]
        rc, output = run_command(cmd, capture=True, timeout=timeout)
        evidence_paths.append(str(evidence))
        # expected-failure 的内部校验失败由 harness 消化并将 wrapper 置为 pass；
        # 因此三个 wrapper 都必须 rc=0，任何 rc=2/网络/认证/配置错误都 fail-closed。
        if rc != 0:
            return StageResult(
                "backup-restore",
                "fail",
                f"phase={phase} failed (wrapper_rc={rc}): {output[:800]}",
                time.time() - start,
                artifacts=tuple(evidence_paths),
            )
        log(f"  ✓ {phase} passed")

    log("✓ Backup and restore completed")
    return StageResult(
        "backup-restore",
        "pass",
        "full backup + validated corruption failure + isolated blank restore passed",
        time.time() - start,
        artifacts=tuple(evidence_paths),
    )


def stage_10_switch_rollback() -> StageResult:
    """阶段 10:执行真实 target switch、受控 503、rollback 和 target cleanup。"""
    stage_header(10, "Switch, controlled 503, rollback, target cleanup")
    start = time.time()
    step_dir = ARTIFACT_DIR / "step13"
    step_dir.mkdir(parents=True, exist_ok=True)
    signing_key = os.environ.get("CI_BACKUP_SIGNING_KEY", "")
    phases = (
        ("secretless_actual_switch", (), "actual_switch.json", 180),
        ("switch_probe_failure", ("--expect", "no-production-tag"), "switch_probe_failure.json", 60),
        ("secretless_actual_rollback", (), "actual_rollback.json", 180),
        ("secretless_drop_restore_target", (), "target_cleanup.json", 180),
    )
    evidence_paths: list[str] = []
    for phase, extra, filename, timeout in phases:
        evidence = step_dir / filename
        log(f"  Running {phase}...")
        cmd = [
            sys.executable,
            "scripts/compose_runtime_e2e.py",
            "--phase", phase,
            "--signing-key", signing_key,
            *extra,
            "--output", str(evidence),
        ]
        rc, output = run_command(cmd, capture=True, timeout=timeout)
        evidence_paths.append(str(evidence))
        if rc != 0:
            return StageResult(
                "switch-rollback",
                "fail",
                f"phase={phase} failed (wrapper_rc={rc}): {output[:800]}",
                time.time() - start,
                artifacts=tuple(evidence_paths),
            )
        log(f"  ✓ {phase} passed")

    log("✓ Switch, controlled failure, rollback, and cleanup completed")
    return StageResult(
        "switch-rollback",
        "pass",
        "target switch + controlled 503 + source rollback + target cleanup passed",
        time.time() - start,
        artifacts=tuple(evidence_paths),
    )


def stage_11_manifest_verify() -> StageResult:
    """阶段 11:构建 current-SHA candidate manifest 并执行五层验证。"""
    stage_header(11, "Candidate manifest build and five-layer validation")
    start = time.time()
    step_dir = ARTIFACT_DIR / "step14"
    step_dir.mkdir(parents=True, exist_ok=True)
    manifest = step_dir / "candidate-manifest.json"
    signing_key = os.environ.get("CI_MANIFEST_SIGNING_KEY", "")
    source_sha = _resolve_head_sha()

    resolved_compose = ARTIFACT_DIR / "compose-resolved.json"
    build_cmd = [
        sys.executable,
        "scripts/build_secretless_candidate_manifest.py",
        "--artifact-dir", str(ARTIFACT_DIR),
        "--output", str(manifest),
        "--resolved-compose", str(resolved_compose),
        "--compose-service", "db_backup",
        "--signing-key", signing_key,
        "--expected-sha", source_sha,
    ]
    rc, output = run_command(build_cmd, capture=True, timeout=120)
    if rc != 0:
        return StageResult(
            "manifest-verify",
            "fail",
            f"manifest build failed (rc={rc}): {output[:800]}",
            time.time() - start,
            artifacts=(str(manifest),),
        )

    validate_cmd = [
        sys.executable,
        "scripts/validate_candidate_manifest.py",
        "--manifest", str(manifest),
        "--schema", "schemas/secretless-candidate-manifest.schema.json",
        "--artifact-dir", str(ARTIFACT_DIR),
        "--expected-run-id", os.environ.get("GITHUB_RUN_ID", ""),
        "--expected-run-attempt", os.environ.get("GITHUB_RUN_ATTEMPT", ""),
        "--expected-workflow-path", ".github/workflows/secretless-contract-e2e.yml",
        "--expected-event", os.environ.get("GITHUB_EVENT_NAME", ""),
        "--expected-ref", os.environ.get("GITHUB_REF", ""),
        "--expected-source-sha", source_sha,
        "--verification-key", signing_key,
        "--strict",
    ]
    rc, output = run_command(validate_cmd, capture=True, timeout=120)
    if rc != 0:
        return StageResult(
            "manifest-verify",
            "fail",
            f"manifest validation failed (rc={rc}): {output[:800]}",
            time.time() - start,
            artifacts=(str(manifest),),
        )
    log("✓ Candidate manifest five-layer cross-validation passed")
    return StageResult(
        "manifest-verify",
        "pass",
        "current SHA/tree/image/runtime/artifact identity manifest validated",
        time.time() - start,
        artifacts=(str(manifest),),
    )


def stage_12_deploy_state_matrix() -> StageResult:
    """阶段 12:使用同一签名 manifest identity 执行 deployment 成功/失败矩阵。"""
    stage_header(12, "Deployment state machine immutable-identity matrix")
    start = time.time()
    step_dir = ARTIFACT_DIR / "step15"
    step_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = ARTIFACT_DIR / "step14" / "candidate-manifest.json"
    rollback_path = ARTIFACT_DIR / "step13" / "actual_rollback.json"
    try:
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return StageResult("deploy-state-mtx", "fail", f"candidate manifest invalid: {exc}", time.time() - start)
    source_sha = str(candidate.get("source_sha", ""))
    image_digest = str(candidate.get("image_digest", ""))
    runtime_digest = str(candidate.get("runtime_config_digest", ""))
    repo_digest = f"ghcr.io/secretless/tgjiema@{image_digest}"

    sim_cmd = [sys.executable, "tests/support/deployment_simulator.py", "--port", "8099"]
    sim_proc = subprocess.Popen(
        sim_cmd,
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    log(f"  Started deployment simulator (PID={sim_proc.pid})")
    time.sleep(3)
    scenarios = (
        ("success", "deployment-state-success.json", False),
        ("runtime_config_drift", "deployment-state-config-drift.json", True),
        ("business_probe_failure", "deployment-state-probe-fail.json", True),
    )
    state_paths: list[Path] = []
    try:
        for scenario, filename, expect_failure in scenarios:
            state_path = step_dir / filename
            state_paths.append(state_path)
            log(f"  Running scenario={scenario} (expect_failure={expect_failure})...")
            cmd = [
                sys.executable,
                "scripts/deployment_state_machine.py",
                "run",
                "--state-file", str(state_path),
                "--production-tag", f"rc-v1.0.83-secretless-{scenario}",
                "--source-sha", source_sha,
                "--image-repo-digest", repo_digest,
                "--runtime-config-digest", runtime_digest,
                "--candidate-manifest", str(candidate_path),
                "--rollback-evidence", str(rollback_path),
                "--deploy-hook-url", "http://localhost:8099/deploy-hook",
                "--deploy-probe-url", "http://localhost:8099",
                "--scenario", scenario,
                "--probe-max-attempts", "3",
                "--probe-interval", "1",
            ]
            if expect_failure:
                cmd.extend(["--expect", "failure"])
            rc, output = run_command(cmd, capture=True, timeout=120)
            # --expect failure 已严格读取 state file 并反转为 rc=0；此处所有 wrapper
            # 都必须成功，不能把任意非零退出码当作负测通过。
            if rc != 0:
                return StageResult(
                    "deploy-state-mtx",
                    "fail",
                    f"scenario={scenario} failed contract (wrapper_rc={rc}): {output[:800]}",
                    time.time() - start,
                    artifacts=tuple(str(path) for path in state_paths),
                )
            log(f"  ✓ scenario={scenario} passed")

        try:
            documents = [json.loads(path.read_text(encoding="utf-8")) for path in state_paths]
        except (OSError, json.JSONDecodeError) as exc:
            return StageResult("deploy-state-mtx", "fail", f"deployment state evidence invalid: {exc}", time.time() - start)
        terminals_ok = (
            documents[0].get("current_state") == "verified"
            and all(doc.get("current_state") == "failed" for doc in documents[1:])
            and len({doc.get("candidate_manifest_sha256") for doc in documents}) == 1
            and all(
                doc.get("identity_restored") is True
                and doc.get("rollback_source_identity") == doc.get("source_database_identity")
                and doc.get("source_sha") == source_sha
                for doc in documents
            )
        )
        if not terminals_ok:
            return StageResult(
                "deploy-state-mtx",
                "fail",
                "deployment state terminal or immutable identity contract mismatch",
                time.time() - start,
                artifacts=tuple(str(path) for path in state_paths),
            )
        log("✓ Deployment state machine matrix passed with shared immutable identity")
        return StageResult(
            "deploy-state-mtx",
            "pass",
            "success verified; runtime drift and probe failure reached validated failed terminals",
            time.time() - start,
            artifacts=tuple(str(path) for path in state_paths),
        )
    finally:
        if sim_proc.poll() is None:
            sim_proc.terminate()
            try:
                sim_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                sim_proc.kill()
                sim_proc.wait()


def stage_13_gen_result(stage_results: list[StageResult]) -> StageResult:
    """阶段 13:写入非权威本地 stage summary；不得自行宣告 GO。"""
    stage_header(13, "Generate non-authoritative local stage summary")
    start = time.time()
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    failed = [stage for stage in stage_results if stage.status == "fail"]
    result = {
        "schema_version": "secretless-local-stage-summary/v1",
        "status": "ready_for_final_verification" if not failed else "failed",
        "head_sha": _resolve_head_sha(),
        "run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
        "workflow_path": ".github/workflows/secretless-contract-e2e.yml",
        "event": os.environ.get("GITHUB_EVENT_NAME", ""),
        "ref": os.environ.get("GITHUB_REF", ""),
        "run_timestamp": datetime.now(timezone.utc).isoformat(),
        "result": "PENDING_STRICT_FINAL_VERIFICATION" if not failed else "SECRETLESS_FUNCTIONAL_NO_GO",
        "stages": [stage.to_dict() for stage in stage_results],
        "total_stages": len(stage_results),
        "passed_stages": sum(1 for stage in stage_results if stage.status == "pass"),
        "failed_stages": len(failed),
    }
    RESULT_JSON.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"✓ non-authoritative stage summary generated at {RESULT_JSON}")
    return StageResult("gen-result", "pass", f"written to {RESULT_JSON}", time.time() - start)


def stage_14_cleanup() -> StageResult:
    """阶段 14:cleanup；失败作为 secondary error，且在无主失败时阻止 GO。"""
    stage_header(14, "Cleanup (always)")
    start = time.time()
    cmd = docker_compose_cmd(["down", "-v", "--remove-orphans"])
    rc, output = run_command(cmd, capture=True, timeout=120)
    if rc != 0:
        log(f"cleanup returned non-zero (rc={rc}); original failure remains primary", level="WARN")
        return StageResult(
            "cleanup",
            "fail",
            f"docker compose down failed (rc={rc}): {output[:800]}",
            time.time() - start,
            error_code=ERROR_SECRETLESS_CLEANUP_FAILURE,
        )
    log("✓ Cleanup completed")
    return StageResult("cleanup", "pass", "docker compose down executed", time.time() - start)


def stage_15_final_verdict(all_business_stages_passed: bool) -> StageResult:
    """阶段 15:由 finalizer + Step 20 verifier 唯一授权最终 GO。"""
    stage_header(15, "Strict final verdict")
    finalize_cmd = [
        sys.executable,
        "scripts/finalize_secretless_result.py",
        "--job-status", "success" if all_business_stages_passed else "failure",
        "--expected-sha", _resolve_head_sha(),
        "--expected-run-id", os.environ.get("GITHUB_RUN_ID", ""),
        "--expected-run-attempt", os.environ.get("GITHUB_RUN_ATTEMPT", ""),
        "--expected-workflow-path", ".github/workflows/secretless-contract-e2e.yml",
        "--expected-event", os.environ.get("GITHUB_EVENT_NAME", ""),
        "--output", str(RESULT_JSON),
    ]
    rc, output = run_command(finalize_cmd, capture=True, timeout=60)
    if rc != 0:
        return StageResult(
            "final-verdict",
            "fail",
            f"result finalizer failed (rc={rc}): {output[:800]}",
        )
    if not all_business_stages_passed:
        print("\n" + "═" * 60, flush=True)
        print("  SECRETLESS FUNCTIONAL NO-GO", flush=True)
        print("═" * 60, flush=True)
        return StageResult(
            "final-verdict",
            "fail",
            "one or more business stages failed; finalized NO-GO evidence written",
            artifacts=(str(RESULT_JSON),),
        )

    verify_cmd = [
        sys.executable,
        "scripts/verify_secretless_final_result.py",
        "--result", str(RESULT_JSON),
        "--expected-sha", _resolve_head_sha(),
        "--expected-run-id", os.environ.get("GITHUB_RUN_ID", ""),
        "--expected-run-attempt", os.environ.get("GITHUB_RUN_ATTEMPT", ""),
        "--expected-workflow-path", ".github/workflows/secretless-contract-e2e.yml",
        "--expected-event", os.environ.get("GITHUB_EVENT_NAME", ""),
        "--output", str(FINAL_VERDICT_JSON),
    ]
    rc, output = run_command(verify_cmd, capture=True, timeout=60)
    if rc != 0:
        return StageResult(
            "final-verdict",
            "fail",
            f"strict Step 20 verifier rejected result (rc={rc}): {output[:800]}",
            artifacts=(str(RESULT_JSON), str(FINAL_VERDICT_JSON)),
        )

    print("\n" + "═" * 60, flush=True)
    print("  SECRETLESS FUNCTIONAL GO", flush=True)
    print("═" * 60, flush=True)
    return StageResult(
        "final-verdict",
        "pass",
        "strict Step 20 verifier authorized SECRETLESS FUNCTIONAL GO",
        artifacts=(str(RESULT_JSON), str(FINAL_VERDICT_JSON)),
    )


# ════════════════════════════════════════════════════════════════
# 主流程
# ════════════════════════════════════════════════════════════════


def _resolve_head_sha() -> str:
    """解析当前 HEAD SHA(身份绑定;失败返回空串)。"""
    rc, out = run_command(["git", "rev-parse", "HEAD"], capture=True, timeout=10)
    return out.strip() if rc == 0 else ""


def _write_in_progress_result(head_sha: str) -> None:
    """R79 §10.5 / P0-04: 主流程最开始即创建 in_progress result.json。

    任何提前退出路径(workflow 级中断、异常、阶段失败)都必须存在
    result.json;首失败 phase 与 exit code 由最终写入保留。
    """
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    initial = {
        "schema_version": "secretless-e2e/v1",
        "status": "in_progress",
        "head_sha": head_sha,
        "run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
        "workflow_path": ".github/workflows/secretless-contract-e2e.yml",
        "event": os.environ.get("GITHUB_EVENT_NAME", ""),
        "ref": os.environ.get("GITHUB_REF", ""),
        "run_timestamp": datetime.now(timezone.utc).isoformat(),
        "phases": [],
    }
    RESULT_JSON.write_text(json.dumps(initial, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    """主入口:执行 15 阶段严格顺序,任一失败返回 1。"""
    log("R76 O12: Secretless Release Gates — single entry point")
    log(f"Repository: {REPO_ROOT}")
    log(f"Artifact dir: {ARTIFACT_DIR}")

    # R83: 每次 run 先清除固定白名单内的工具自有 evidence，防止上一 run 的
    # success phase 被本次 finalizer 读取；不扫描或删除其他项目/用户文件。
    head_sha = _resolve_head_sha()
    try:
        _reset_run_owned_artifacts()
    except (OSError, RuntimeError) as exc:
        _write_in_progress_result(head_sha)
        log(f"ERROR: failed to reset run-owned artifacts: {exc}", level="ERROR")
        return 1

    # R79 §10.5: 最开始即写 in_progress result.json(任何退出路径都有 result.json)
    _write_in_progress_result(head_sha)

    # 前置检查:docker 可用
    if not shutil.which("docker"):
        log("ERROR: docker not found in PATH", level="ERROR")
        return 1
    if not shutil.which("python"):
        log("ERROR: python not found in PATH", level="ERROR")
        return 1

    stage_results: list[StageResult] = []
    first_failure: StageResult | None = None

    # 阶段 1-13:顺序执行,任一失败记录但不立即退出(cleanup 必须执行)
    # 但报告 O12 step 15 要求"任一失败返回 1",所以失败后跳过后续业务阶段,直接 cleanup
    stages_to_run: list = [
        ("preflight", stage_1_preflight),
        ("gen-creds", stage_2_gen_creds),
        ("compose-config", stage_3_compose_config),
        ("start-infra", stage_4_start_infra),
        ("migrate", stage_5_migrate),
        ("start-apps", stage_6_start_apps),
        ("normal-tx", stage_7_normal_transaction),
        ("fault-injection", stage_8_fault_injection),
        ("backup-restore", stage_9_backup_restore),
        ("switch-rollback", stage_10_switch_rollback),
        ("manifest-verify", stage_11_manifest_verify),
        ("deploy-state-mtx", stage_12_deploy_state_matrix),
    ]

    for name, func in stages_to_run:
        try:
            if name == "gen-creds":
                result, _ = func()  # type: ignore[misc]
            else:
                result = func()  # type: ignore[assignment]
        except Exception as exc:
            result = StageResult(name, "fail", f"exception: {type(exc).__name__}: {exc}")
            log(f"Stage {name} raised exception: {exc}", level="ERROR")
        stage_results.append(result)
        if result.status == "pass" and name in REQUIRED_PHASE_FILES:
            try:
                _write_required_phase(name)
            except OSError as exc:
                result = StageResult(
                    name,
                    "fail",
                    f"failed to persist required phase evidence: {exc}",
                )
                stage_results[-1] = result
        if result.status == "fail" and first_failure is None:
            first_failure = result
            log(f"Stage {name} FAILED — skipping remaining business stages", level="WARN")
            break

    # 阶段 13:写本地 stage summary。最终 result.json 由阶段 15 finalizer 覆盖。
    try:
        gen_result = stage_13_gen_result(stage_results)
        stage_results.append(gen_result)
    except Exception as exc:
        gen_result = StageResult("gen-result", "fail", f"exception: {exc}")
        stage_results.append(gen_result)
        if first_failure is None:
            first_failure = gen_result

    # 阶段 14:cleanup (always,即使前面失败)
    try:
        cleanup_result = stage_14_cleanup()
        stage_results.append(cleanup_result)
        if cleanup_result.status == "fail" and first_failure is None:
            first_failure = cleanup_result
    except Exception as exc:
        cleanup_failure = StageResult(
            "cleanup",
            "fail",
            f"exception: {exc}",
            error_code=ERROR_SECRETLESS_CLEANUP_FAILURE,
        )
        stage_results.append(cleanup_failure)
        if first_failure is None:
            first_failure = cleanup_failure
        log(f"Cleanup raised exception: {exc}", level="WARN")

    # 阶段 15:最终判定。只有 strict verifier 的 pass 才允许返回 0。
    all_business_stages_passed = first_failure is None
    final_result = stage_15_final_verdict(all_business_stages_passed)
    stage_results.append(final_result)
    return 0 if final_result.status == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
