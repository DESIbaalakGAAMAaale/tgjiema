#!/usr/bin/env python3
"""R73 §5.24: 生产/预发布绕过模式静态扫描器。

本扫描器是 Release Gates(PR/master/RC) 体系的一部分,源自 R73 §5.24 要求。
它使用 Python AST(而非简单 grep)与 YAML 解析对全仓进行静态分析,
检测生产/预发布代码路径中禁止出现的"绕过"模式:

    - CI/GITHUB_ACTIONS 环境变量读取后返回 healthy/ready/pass(True/None/{})
    - os.environ.setdefault("ALLOW_LEGACY_RESTORE", ...) 等动态写入逃生舱变量
    - getattr/importlib/eval/exec 动态分发到 restore_writer 以绕过 scanner
    - except Exception: pass / return None / return {} 吞掉生产路径关键错误
    - 业务关键写入路径中硬编码 healthy=True/ready=True 返回
    - mark_dirty=False 在核心业务写入路径中
    - 直接 redis-cli XADD tgjiema:writer:stream(含 shell 脚本)
    - 捕获异常后返回 success/warning 以绕过门禁

YAML 工作流 patterns:

    - continue-on-error: true 在门禁 job 上(仅允许诊断/产物上传步骤)
    - || true / set +e / exit 0 包裹关键验证
    - docker build / buildx build / build-push-action 在生产部署 job 中
    - gh run list --limit 1 选择候选 run(必须使用固定 rc_run_id)
    - 浮动镜像 tag(缺少 @sha256: digest)
    - 第三方 action 未固定到完整 commit SHA
    - master/PR job 含 contents: write / deployments: write / ruleset-modify 权限
    - if: always() 然后在门禁 job 上返回 success

CI 调用方式:
    python scripts/scan_production_bypasses.py --json --output scan-result.json

退出码:
    - 0: 无违规
    - 8: 检测到 P0/P1 违规(按 R73 §5.17)
    - 2: 严重错误(参数解析失败/baseline 加载失败等)
"""
from __future__ import annotations

import argparse
import ast
import datetime as _dt
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

# PyYAML 可选(缺失时使用 regex fallback 并警告)
try:
    import yaml  # type: ignore[import-not-found]
    _HAS_YAML = True
except ImportError:
    yaml = None  # type: ignore[assignment]
    _HAS_YAML = False


# ════════════════════════════════════════════════════════════════
# 常量
# ════════════════════════════════════════════════════════════════

POLICY_VERSION = "r73-1"
REPO_ROOT = Path(__file__).resolve().parent.parent

# Python 扫描目录(相对 target root)
# R74 P1-02: scripts/ 不再整体豁免;改为按函数/调用边策略
# 关键路径(compose_runtime_e2e, synthetic_transaction, promotion, restore, break-glass)严格扫描
PY_SCAN_DIRS: tuple[str, ...] = ("services", "scripts", "database", "admin", "bots", "docker")

# 允许 except Exception: pass / return None 的函数名前缀(容错性读取/尝试性操作)
TOLERANT_FUNC_PREFIXES: tuple[str, ...] = ("get_", "try_", "_safe_", "_get_", "_try_")

# YAML/Docker 扫描目标
YAML_WORKFLOW_DIR = ".github/workflows"
COMPOSE_GLOBS: tuple[str, ...] = ("docker-compose*.yml", "docker-compose*.yaml")
DOCKERFILE_GLOBS: tuple[str, ...] = ("Dockerfile*",)

# 跳过的目录
SKIP_DIR_PARTS: tuple[str, ...] = (
    "__pycache__", ".git", "node_modules", "venv", ".venv",
    ".pytest_cache", "tests", "docs", "data", "logs",
)

# CI 环境变量名(触发 bypass 检测)
CI_ENV_VARS: frozenset[str] = frozenset({"CI", "GITHUB_ACTIONS"})

# 动态分发到 restore writer 的关键词
WRITER_DISPATCH_KEYWORDS: tuple[str, ...] = (
    "restore_writer",
    "_restore_from_backup_data",
    "_restore_crdb_tables",
    "_restore_sqlite_tables_to_db",
    "run_restore",
)

# Writer Stream key
WRITER_STREAM_KEY = "tgjiema:writer:stream"

# 允许直接 XADD 到 writer stream 的模块(单一事实源)
WRITER_STREAM_ALLOWED_MODULES: frozenset[str] = frozenset({
    "services/restore_writer.py",
    "database/redis_queue.py",
    "database/write_router.py",
    "services/outbox_worker.py",
    "services/restore_orchestrator.py",
})

# 40 字符 SHA-1 commit hash 正则
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")

# 第一方 action 前缀(允许使用 tag 而非 SHA)
FIRST_PARTY_ACTION_PREFIXES: tuple[str, ...] = ("actions/", "github/")

# 门禁 job 名称关键词
GATE_JOB_KEYWORDS: tuple[str, ...] = (
    "gate", "verify", "check", "scan", "lint", "test",
    "release", "deploy", "promote",
)

# 生产部署 job 名称关键词
PROD_JOB_KEYWORDS: tuple[str, ...] = (
    "deploy", "promote", "release", "production", "prod",
)

# 诊断/产物上传步骤关键词(允许 continue-on-error)
DIAGNOSTIC_STEP_KEYWORDS: tuple[str, ...] = (
    "upload", "artifact", "diagnostic", "debug",
    "snapshot", "report", "cleanup", "teardown",
)

# 关键验证步骤关键词(使用 word-boundary 匹配,避免 "test" 误匹配 "attestation" 等)
CRITICAL_VERIFY_KEYWORDS: tuple[str, ...] = (
    "verify", "check", "scan", "gate", "lint", "test", "validate",
)

# 编译 word-boundary 正则:仅匹配完整单词,避免 "test" 误匹配 "attestation"/"latest",
# "gate" 误匹配 "release-gates-sbom" 等
_CRITICAL_VERIFY_WORD_RES: tuple[re.Pattern, ...] = tuple(
    re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE)
    for kw in CRITICAL_VERIFY_KEYWORDS
)

# 工具性命令前缀(允许 || true — 这些命令在无匹配/无文件时返回非零是正常的)
# 例如: grep 无匹配返回 1; ls 文件不存在返回非零; openssl 格式不匹配返回非零
# R75 P0-07: 删除过宽前缀 pip/python/python3 — 包安装/解释器执行失败不得静默忽略
# R76 10.M: 删除 curl/git tag -d/git tag 全局豁免 — 关键部署/签名上下文中
# 这些命令失败必须阻断流水线,改为路径+函数+调用边判定(_is_utility_or_true
# 已通过 CRITICAL_CMD_PATTERNS 优先级保障:含 cosign sign/git tag -s/git verify-tag
# 等关键命令的行不会被 UTILITY_CMD_PREFIXES 豁免)
UTILITY_CMD_PREFIXES: tuple[str, ...] = (
    "grep", "ls", "cat", "unzip", "openssl",
    "head", "tail", "wc",
    "find", "sort", "uniq", "cut", "awk", "sed",
    "echo", "printf", "true", "cosign version",
    "git rev-parse", "git ls-files",
    "rm", "mv", "cp", "mkdir", "rmdir",
)

# 关键验证命令(禁止用 || true 包裹 — 这些命令失败必须阻断流水线)
# 若行中含这些命令且行末有 || true,则视为真实违规
CRITICAL_CMD_PATTERNS: tuple[str, ...] = (
    "bandit", "pytest", "pylint", "flake8", "mypy", "ruff",
    "cosign sign", "cosign verify",
    "git tag -s", "git tag -v", "git verify-tag", "git verify-commit",
    "docker build", "docker push", "docker buildx build",
    "verify_supply_chain", "verify_rc_identity", "verify_rc_3x",
    "scan_production_bypasses", "check_crdb_ru",
)

# 基础设施镜像前缀(豁免 digest 检查)
INFRASTRUCTURE_IMAGE_PREFIXES: tuple[str, ...] = (
    "redis:", "postgres:", "minio:", "cockroachdb/",
    "mongo:", "nginx:", "mariadb:", "mysql:", "rabbitmq:",
)

# 规则 → 严重级别映射
RULE_SEVERITY: dict[str, str] = {
    "R73-NO-CI-HEALTH-BYPASS": "P0",
    "R73-NO-LEGACY-RESTORE-ENV": "P0",
    "R73-NO-WRITER-DIRECT-INJECT": "P0",
    "R73-NO-EXCEPTION-SWALLOW-PROD": "P0",
    "R73-NO-MARK-DIRTY-FALSE-CORE": "P0",
    "R73-NO-CONTINUE-ON-ERROR-GATE": "P0",
    "R73-NO-DOCKER-BUILD-IN-PROD": "P0",
    "R73-NO-IF-ALWAYS-SUCCESS": "P0",
    "R73-NO-DYNAMIC-WRITER-DISPATCH": "P0",
    "R73-NO-FLOATING-IMAGE-TAG": "P1",
    "R73-NO-UNPINNED-ACTION": "P1",
    "R73-NO-WRITE-PERM-ON-PR": "P1",
    # R74 P1-02: 额外文本模式检测
    "R74-PIP-INSTALL-OR-TRUE": "P1",
    "R74-PLACEHOLDER-PASS": "P1",
    "R74-DEPLOYMENT-ECHO-EXIT": "P1",
}

# R75 P0-07: 函数级豁免(收紧:仅保留4个真正容错读取函数)
# R74 P1-02 的过宽豁免已删除:分析/迁移工具函数(_has_str_return_annotation,
# extract_fstring_parts, _install_fake_config_if_missing, _get_commit_sha,
# _get_schema_version, _get_table_pk_columns, _load_module_baseline,
# _baseline_total, _git_base_commit, _git_current_branch, _git_show_file,
# _git_list_files_at_commit, _migration_digest_workflow_algorithm,
# _git_rev_parse)不再豁免,改为通过 TOLERANT_FUNC_PREFIXES (get_/try_/_get_)
# 前缀匹配或调用方显式处理异常。
# R76 10.M: 删除 _run 和 _compose_cmd 全局豁免 — 这些函数可承载关键命令
# (subprocess/docker compose),不能因名称豁免。必要豁免必须精确到调用点
# 并带 expiry/digest/负向 fixture。容错语义由 TOLERANT_FUNC_PREFIXES 前缀
# 匹配(get_/try_/_get_/_try_)或调用方显式 try/except 处理。
_FUNC_LEVEL_EXEMPT_FUNCTIONS: frozenset[str] = frozenset({
    # 真正的容错读取函数(无 fallback 价值,失败返回空是合理语义)
    "_read_audit_entries",  # 审计日志读取(容错,跳过损坏行)
    "_read_db_identity_via_importlib",  # compose E2E DB identity 读取(容错,调用方处理空返回)
})

# R75 P0-07: mark_dirty=False 豁免方法已删除
# R74 P1-02 的过宽豁免(upsert_file_record_local/upsert_code_local/
# insert_pending_upload_local/upsert_user_local)已删除。
# 这些方法 mark_dirty=False 是CRDB已写入的缓存镜像场景,但 R75 要求删除过宽豁免。
# 实际代码中将 mark_dirty=False 改为 mark_dirty=True 以触发 dirty_outbox 持久化。
# 若有特殊场景需保留 mark_dirty=False,应通过调用方显式处理(如批量同步场景)。


# ════════════════════════════════════════════════════════════════
# 数据结构
# ════════════════════════════════════════════════════════════════


@dataclass
class Violation:
    """单条违规。"""

    rule_id: str
    file: str
    line: int
    symbol: str
    severity: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "file": self.file,
            "line": self.line,
            "symbol": self.symbol,
            "severity": self.severity,
            "message": self.message,
        }


# ════════════════════════════════════════════════════════════════
# 路径与文件遍历辅助
# ════════════════════════════════════════════════════════════════


def _rel_posix(path: Path, target: Path) -> str:
    """返回相对 target 的 POSIX 路径字符串。"""
    try:
        return path.relative_to(target).as_posix()
    except ValueError:
        return path.as_posix()


def _is_skipped_path(path: Path, target: Path) -> bool:
    """检查路径是否应跳过(在 SKIP_DIR_PARTS 中)。"""
    rel = _rel_posix(path, target)
    for part in SKIP_DIR_PARTS:
        if part in rel:
            return True
    return False


def _is_tolerant_func_name(name: str) -> bool:
    """检查函数名是否为容错性前缀(get_/try_/_safe_/_get_/_try_)。
    
    这些函数语义上就是"尝试性读取",except Exception: return None 是合理的容错。
    """
    if not name:
        return False
    return any(name.startswith(prefix) for prefix in TOLERANT_FUNC_PREFIXES)


def _extract_call_name(node: ast.Call) -> str | None:
    """从 ast.Call 节点提取被调用函数名(仅支持简单属性访问 a.b 和名称调用)。"""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _iter_python_files(target: Path) -> Iterable[Path]:
    """遍历扫描目录下所有 .py 文件(跳过缓存/数据目录)。"""
    for scan_dir_name in PY_SCAN_DIRS:
        scan_dir = target / scan_dir_name
        if not scan_dir.is_dir():
            continue
        for py_file in scan_dir.rglob("*.py"):
            if _is_skipped_path(py_file, target):
                continue
            yield py_file


def _iter_workflow_files(target: Path) -> Iterable[Path]:
    """遍历 .github/workflows/ 下所有 .yml/.yaml 文件。"""
    workflow_dir = target / YAML_WORKFLOW_DIR
    if not workflow_dir.is_dir():
        return
    for pattern in ("*.yml", "*.yaml"):
        for f in workflow_dir.glob(pattern):
            yield f


def _iter_compose_files(target: Path) -> Iterable[Path]:
    """遍历 docker-compose*.yml 文件。"""
    for pattern in COMPOSE_GLOBS:
        for f in target.glob(pattern):
            yield f


def _iter_dockerfiles(target: Path) -> Iterable[Path]:
    """遍历 Dockerfile* 文件。"""
    for pattern in DOCKERFILE_GLOBS:
        for f in target.glob(pattern):
            if f.is_file():
                yield f


def _iter_shell_scripts(target: Path) -> Iterable[Path]:
    """遍历 .sh 文件(repo 根 + 扫描目录)。"""
    for f in target.glob("*.sh"):
        if f.is_file():
            yield f
    for scan_dir_name in PY_SCAN_DIRS:
        scan_dir = target / scan_dir_name
        if scan_dir.is_dir():
            for f in scan_dir.rglob("*.sh"):
                if _is_skipped_path(f, target):
                    continue
                yield f


def _read_text(path: Path) -> str | None:
    """安全读取文件文本,失败时返回 None。"""
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as e:
        print(f"[WARN] Cannot read {path}: {e}", file=sys.stderr)
        return None


# ════════════════════════════════════════════════════════════════
# AST 辅助
# ════════════════════════════════════════════════════════════════


def _build_parent_map(tree: ast.AST) -> dict[int, ast.AST]:
    """构建 parent map: {node_id: parent_node}。"""
    parent_map: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parent_map[id(child)] = parent
    return parent_map


def _find_enclosing_function(
    node: ast.AST, parent_map: dict[int, ast.AST],
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """找到节点最近的 enclosing FunctionDef 节点。"""
    current = parent_map.get(id(node))
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current
        current = parent_map.get(id(current))
    return None


def _is_env_read_call(call_node: ast.Call, var_names: frozenset[str]) -> bool:
    """检查 Call 是否在读取指定环境变量。

    覆盖:
        - os.getenv("CI")
        - os.environ.get("CI")
        - os.environ.get("CI", default)
    """
    func = call_node.func
    # os.getenv("VAR")
    if isinstance(func, ast.Attribute) and func.attr == "getenv":
        if call_node.args and isinstance(call_node.args[0], ast.Constant):
            val = call_node.args[0].value
            if isinstance(val, str) and val in var_names:
                return True
    # os.environ.get("VAR")
    if isinstance(func, ast.Attribute) and func.attr == "get":
        if isinstance(func.value, ast.Attribute):
            inner = func.value
            if (
                isinstance(inner.value, ast.Name)
                and inner.value.id == "os"
                and inner.attr == "environ"
            ):
                if call_node.args and isinstance(call_node.args[0], ast.Constant):
                    val = call_node.args[0].value
                    if isinstance(val, str) and val in var_names:
                        return True
    return False


def _is_environ_subscript(node: ast.Subscript, var_names: frozenset[str]) -> bool:
    """检查 Subscript 是否为 os.environ["CI"] 形式。"""
    value = node.value
    if isinstance(value, ast.Attribute):
        if (
            isinstance(value.value, ast.Name)
            and value.value.id == "os"
            and value.attr == "environ"
        ):
            slice_node = node.slice
            # Python 3.9+: slice 直接是表达式; Python 3.8: Index 节点
            if isinstance(slice_node, ast.Index):  # type: ignore[attr-defined]
                slice_node = slice_node.value  # type: ignore[attr-defined]
            if isinstance(slice_node, ast.Constant):
                val = slice_node.value
                if isinstance(val, str) and val in var_names:
                    return True
    return False


def _is_bypass_return_value(return_node: ast.Return) -> bool:
    """检查 Return 是否返回 bypass 值。

    覆盖:
        - return True
        - return None / bare return
        - return {}
        - return healthy=True / ready=True (关键字参数)
        - return status=pass / status=success / status=warning
    """
    value = return_node.value
    if value is None:
        return True  # bare return = return None
    if isinstance(value, ast.Constant):
        if value.value is True:
            return True  # return True
        if value.value is None:
            return True  # return None
        if isinstance(value.value, str) and value.value in ("success", "warning", "pass"):
            return True  # return "success" / "warning" / "pass"
    if isinstance(value, ast.Dict) and not value.keys:
        return True  # return {}
    # return SomeCall(healthy=True, ready=True, status="pass")
    if isinstance(value, ast.Call):
        for kw in value.keywords:
            if kw.arg in ("healthy", "ready") and isinstance(kw.value, ast.Constant):
                if kw.value.value is True:
                    return True
            if kw.arg == "status" and isinstance(kw.value, ast.Constant):
                if kw.value.value in ("pass", "success", "warning"):
                    return True
    # return {"status": "pass"} / {"healthy": True}
    if isinstance(value, ast.Dict):
        for key, val in zip(value.keys, value.values):
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                if key.value in ("healthy", "ready") and isinstance(val, ast.Constant):
                    if val.value is True:
                        return True
                if key.value == "status" and isinstance(val, ast.Constant):
                    if val.value in ("pass", "success", "warning"):
                        return True
    return False


def _if_test_reads_ci_env(if_node: ast.If) -> bool:
    """检查 If 节点的 test 是否读取 CI/GITHUB_ACTIONS 环境变量。"""
    for node in ast.walk(if_node.test):
        if isinstance(node, ast.Call) and _is_env_read_call(node, CI_ENV_VARS):
            return True
        if isinstance(node, ast.Subscript) and _is_environ_subscript(node, CI_ENV_VARS):
            return True
    return False


def _is_broad_except(handler: ast.ExceptHandler) -> bool:
    """检查 ExceptHandler 是否捕获 broad Exception / BaseException / bare except。"""
    if handler.type is None:
        return True  # bare except
    if isinstance(handler.type, ast.Name) and handler.type.id in ("Exception", "BaseException"):
        return True
    if isinstance(handler.type, ast.Tuple):
        for elt in handler.type.elts:
            if isinstance(elt, ast.Name) and elt.id in ("Exception", "BaseException"):
                return True
    return False


# ════════════════════════════════════════════════════════════════
# Python AST 检测器
# ════════════════════════════════════════════════════════════════


def _detect_ci_health_bypass(
    tree: ast.AST, rel: str, parent_map: dict[int, ast.AST],
) -> list[Violation]:
    """R73-NO-CI-HEALTH-BYPASS (P0): CI/GITHUB_ACTIONS 读取后返回 bypass 值。

    检测 if os.getenv("CI")/os.getenv("GITHUB_ACTIONS"): return True/None/{}/healthy=True。
    """
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        if not _if_test_reads_ci_env(node):
            continue
        # 检查 if body 是否含 bypass return
        has_bypass = False
        for stmt in node.body:
            for child in ast.walk(stmt):
                if isinstance(child, ast.Return) and _is_bypass_return_value(child):
                    has_bypass = True
                    break
            if has_bypass:
                break
        if not has_bypass:
            continue
        func_node = _find_enclosing_function(node, parent_map)
        symbol = func_node.name if func_node else "<module>"
        line = node.lineno
        violations.append(Violation(
            rule_id="R73-NO-CI-HEALTH-BYPASS",
            file=rel,
            line=line,
            symbol=symbol,
            severity="P0",
            message=(
                f"函数 {symbol} 读取 CI/GITHUB_ACTIONS 环境变量后在 if 分支中"
                f"返回 bypass 值(True/None/{{}}/healthy=True/ready=True/status=pass)"
                f" — 生产路径不得通过 CI 变量绕过健康检查"
            ),
        ))
    return violations


def _detect_legacy_restore_env(tree: ast.AST, rel: str) -> list[Violation]:
    """R73-NO-LEGACY-RESTORE-ENV (P0): ALLOW_LEGACY_RESTORE 自动设置。

    检测:
        - os.environ.setdefault("ALLOW_LEGACY_RESTORE", ...)
        - os.environ["ALLOW_LEGACY_RESTORE"] = ...
        - os.putenv("ALLOW_LEGACY_RESTORE", ...)
    """
    violations: list[Violation] = []
    target_var = "ALLOW_LEGACY_RESTORE"
    for node in ast.walk(tree):
        # os.environ.setdefault("ALLOW_LEGACY_RESTORE", ...) / os.putenv(...)
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                func_name = func.attr
                # os.environ.setdefault / os.putenv
                is_setdefault = func_name == "setdefault" and isinstance(
                    func.value, ast.Attribute
                ) and isinstance(func.value.value, ast.Name) and (
                    func.value.value.id == "os" and func.value.attr == "environ"
                )
                is_putenv = (
                    func_name == "putenv"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "os"
                )
                if (is_setdefault or is_putenv) and node.args:
                    if isinstance(node.args[0], ast.Constant):
                        val = node.args[0].value
                        if isinstance(val, str) and val == target_var:
                            violations.append(Violation(
                                rule_id="R73-NO-LEGACY-RESTORE-ENV",
                                file=rel,
                                line=node.lineno,
                                symbol=f"{func_name}",
                                severity="P0",
                                message=(
                                    f"os.{func_name}({target_var!r}, ...) 自动设置"
                                    f"逃生舱环境变量 — 生产环境不得自动启用"
                                    f" ALLOW_LEGACY_RESTORE"
                                ),
                            ))
        # os.environ["ALLOW_LEGACY_RESTORE"] = ...
        if isinstance(node, ast.Assign):
            for target_node in node.targets:
                if isinstance(target_node, ast.Subscript):
                    if _is_environ_subscript(target_node, frozenset({target_var})):
                        violations.append(Violation(
                            rule_id="R73-NO-LEGACY-RESTORE-ENV",
                            file=rel,
                            line=node.lineno,
                            symbol="environ_setitem",
                            severity="P0",
                            message=(
                                f"os.environ[{target_var!r}] = ... 直接写入"
                                f"逃生舱环境变量 — 生产环境不得自动启用"
                                f" ALLOW_LEGACY_RESTORE"
                            ),
                        ))
    return violations


def _detect_dynamic_writer_dispatch(tree: ast.AST, rel: str) -> list[Violation]:
    """R73-NO-DYNAMIC-WRITER-DISPATCH (P0): getattr/importlib/eval/exec 动态分发到 restore_writer。"""
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        func_name: str | None = None
        if isinstance(func, ast.Attribute):
            func_name = func.attr
        elif isinstance(func, ast.Name):
            func_name = func.id
        if func_name not in ("getattr", "eval", "exec", "import_module"):
            continue
        # 检查参数中是否引用 writer dispatch 关键词
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                if any(kw in arg.value for kw in WRITER_DISPATCH_KEYWORDS):
                    violations.append(Violation(
                        rule_id="R73-NO-DYNAMIC-WRITER-DISPATCH",
                        file=rel,
                        line=node.lineno,
                        symbol=func_name,
                        severity="P0",
                        message=(
                            f"{func_name}() 动态分发到 restore_writer"
                            f" (引用 {arg.value!r}) — 禁止用动态分发"
                            f"绕过 scanner 的静态白名单"
                        ),
                    ))
                    break
    return violations


def _detect_exception_swallow(
    tree: ast.AST, rel: str, parent_map: dict[int, ast.AST],
) -> list[Violation]:
    """R73-NO-EXCEPTION-SWALLOW-PROD (P0): except Exception: pass/return None/{}/True/success。

    仅检测单语句 except body(pass / return None / return {} / return True /
    return "success" 等),不误报含日志记录的处理器。

    误报豁免(R74 P1-02 整改):
        - scripts/ 目录整体豁免已移除,改为函数级豁免(_FUNC_LEVEL_EXEMPT_FUNCTIONS)
        - 函数名以 get_/try_/_safe_/_get_/_try_ 开头的容错性读取函数允许
          except Exception: return None/{}(语义为"尝试性读取,失败返回空")
    R76 10.M: _run 和 _compose_cmd 不再全局豁免(可承载关键命令);
    必要豁免必须精确到调用点并带 expiry/digest/负向 fixture。
    """
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if not _is_broad_except(node):
            continue
        body = node.body
        if len(body) != 1:
            continue  # 多语句 body 视为有处理(如日志记录),不报
        stmt = body[0]
        is_violation = False
        detail = ""
        if isinstance(stmt, ast.Pass):
            is_violation = True
            detail = "except Exception: pass — 静默吞掉异常"
        elif isinstance(stmt, ast.Return):
            if _is_bypass_return_value(stmt):
                is_violation = True
                val = stmt.value
                if val is None:
                    detail = "except Exception: return None — 静默返回 None"
                elif isinstance(val, ast.Constant):
                    if val.value is True:
                        detail = "except Exception: return True — 异常后返回 success(bypass gate)"
                    elif val.value is None:
                        detail = "except Exception: return None — 静默返回 None"
                    elif isinstance(val.value, str):
                        detail = (
                            f"except Exception: return {val.value!r}"
                            f" — 异常后返回 success/warning(bypass gate)"
                        )
                elif isinstance(val, ast.Dict):
                    if not val.keys:
                        detail = "except Exception: return {} — 静默返回空 dict"
                    else:
                        detail = "except Exception: return {...status: pass...} — 异常后返回 success(bypass gate)"
        if not is_violation:
            continue
        func_node = _find_enclosing_function(node, parent_map)
        symbol = func_node.name if func_node else "<module>"
        # R74 P1-02: 函数级豁免替代 scripts/ 目录整体豁免
        # R76 10.M: 仅保留真正容错读取函数(_read_audit_entries/
        # _read_db_identity_via_importlib),不允许生产关键路径绕过
        if symbol in _FUNC_LEVEL_EXEMPT_FUNCTIONS:
            continue
        # 误报豁免: 容错性读取函数(get_/try_/_safe_/_get_/_try_)允许
        # except Exception: return None/{}/True(语义为"尝试性读取,失败返回空")
        if isinstance(stmt, ast.Return) and _is_tolerant_func_name(symbol):
            continue
        violations.append(Violation(
            rule_id="R73-NO-EXCEPTION-SWALLOW-PROD",
            file=rel,
            line=node.lineno,
            symbol=symbol,
            severity="P0",
            message=f"{detail} (in {symbol}) — 生产路径不得吞掉关键错误",
        ))
    return violations


def _detect_mark_dirty_false(tree: ast.AST, rel: str) -> list[Violation]:
    """R73-NO-MARK-DIRTY-FALSE-CORE (P0): mark_dirty=False 在业务关键写入路径。

    R75 P0-07: 删除 _MARK_DIRTY_FALSE_EXEMPT_METHODS 过宽豁免。
    所有 mark_dirty=False 调用均视为违规,需在调用方显式改为 mark_dirty=True。
    若批量同步等场景确需 mark_dirty=False,应在调用方显式处理异常或使用专用批量API。
    """
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg == "mark_dirty" and isinstance(kw.value, ast.Constant):
                if kw.value.value is False:
                    violations.append(Violation(
                        rule_id="R73-NO-MARK-DIRTY-FALSE-CORE",
                        file=rel,
                        line=node.lineno,
                        symbol="<call>",
                        severity="P0",
                        message=(
                            "调用中 mark_dirty=False — 业务关键写入路径"
                            "必须标记 dirty 以触发持久化,禁止静默跳过"
                        ),
                    ))
    return violations


def _detect_writer_direct_inject_python(tree: ast.AST, rel: str) -> list[Violation]:
    """R73-NO-WRITER-DIRECT-INJECT (P0): 直接 XADD 到 writer stream(非授权模块)。"""
    if rel in WRITER_STREAM_ALLOWED_MODULES:
        return []
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "xadd":
            if node.args and isinstance(node.args[0], ast.Constant):
                val = node.args[0].value
                if isinstance(val, str) and val == WRITER_STREAM_KEY:
                    violations.append(Violation(
                        rule_id="R73-NO-WRITER-DIRECT-INJECT",
                        file=rel,
                        line=node.lineno,
                        symbol="xadd",
                        severity="P0",
                        message=(
                            f"直接 .xadd({WRITER_STREAM_KEY!r}, ...) "
                            f"— writer stream 必须通过 restore_writer / "
                            f"redis_queue / write_router 写入(capability-seal)"
                        ),
                    ))
    return violations


# ════════════════════════════════════════════════════════════════
# R74 P1-02 额外文本模式检测
# ════════════════════════════════════════════════════════════════

# pip install 后跟 || true 的模式(正则)
_PIP_INSTALL_OR_TRUE_RE = re.compile(
    r"pip\s+(?:install|uninstall|freeze).*?\|\|\s*true",
    re.IGNORECASE,
)

# deployment echo 模式(echo 后跟部署相关文本 + exit 0)
_DEPLOYMENT_ECHO_RE = re.compile(
    r"echo\s+[\"'].*?(?:deploy(?:ing|ed|ment)?|release|publish|ship).*?[\"'].*?exit\s+0",
    re.IGNORECASE,
)


def _detect_r74_extra_patterns(source: str, rel: str) -> list[Violation]:
    """R74 P1-02: 额外文本模式检测。

    检测:
        - pip install ... || true (Python 字符串字面量中的 pip 安装容错)
        - placeholder 注释后紧跟 pass (占位实现)
        - deployment echo 后跟 exit 0 (部署 echo 绕过)

    注意: 跳过自身(scan_production_bypasses.py),因本扫描器自身包含
    示例正则和文档字符串,会触发自检测假阳性。
    """
    if rel == "scripts/scan_production_bypasses.py":
        return []
    violations: list[Violation] = []
    lines = source.splitlines()
    for i, line in enumerate(lines):
        line_no = i + 1

        # 1. pip install ... || true
        if _PIP_INSTALL_OR_TRUE_RE.search(line):
            violations.append(Violation(
                rule_id="R74-PIP-INSTALL-OR-TRUE",
                file=rel,
                line=line_no,
                symbol="<pip-install>",
                severity="P1",
                message=(
                    "pip install/uninstall/freeze 后跟 || true — "
                    "包安装/卸载/冻结失败不得静默忽略,"
                    "必须显式检查退出码"
                ),
            ))

        # 2. placeholder 注释后紧跟 pass
        if "placeholder" in line.lower() and "#" in line:
            # 检查后续 3 行内是否有 pass
            for j in range(i + 1, min(i + 4, len(lines))):
                if lines[j].strip() == "pass" or lines[j].strip().startswith("pass "):
                    violations.append(Violation(
                        rule_id="R74-PLACEHOLDER-PASS",
                        file=rel,
                        line=line_no,
                        symbol="<placeholder>",
                        severity="P1",
                        message=(
                            "placeholder 注释后紧跟 pass — "
                            "占位实现不得通过 pass 静默跳过,"
                            "必须抛出 NotImplementedError 或完成实现"
                        ),
                    ))
                    break

        # 3. deployment echo 后跟 exit 0
        if _DEPLOYMENT_ECHO_RE.search(line):
            violations.append(Violation(
                rule_id="R74-DEPLOYMENT-ECHO-EXIT",
                file=rel,
                line=line_no,
                symbol="<deploy-echo>",
                severity="P1",
                message=(
                    "部署 echo 后跟 exit 0 — "
                    "部署相关输出不得与 exit 0 同处一行,"
                    "必须显式检查部署结果后再退出"
                ),
            ))

    return violations


def scan_python_file(path: Path, rel: str) -> list[Violation]:
    """扫描单个 Python 文件,返回所有违规。"""
    source = _read_text(path)
    if source is None:
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as e:
        print(f"[WARN] SyntaxError in {rel}: {e}", file=sys.stderr)
        return []
    parent_map = _build_parent_map(tree)
    violations: list[Violation] = []
    violations.extend(_detect_ci_health_bypass(tree, rel, parent_map))
    violations.extend(_detect_legacy_restore_env(tree, rel))
    violations.extend(_detect_dynamic_writer_dispatch(tree, rel))
    violations.extend(_detect_exception_swallow(tree, rel, parent_map))
    violations.extend(_detect_mark_dirty_false(tree, rel))
    violations.extend(_detect_writer_direct_inject_python(tree, rel))
    violations.extend(_detect_r74_extra_patterns(source, rel))
    return violations


# ════════════════════════════════════════════════════════════════
# Shell 脚本检测
# ════════════════════════════════════════════════════════════════


def scan_shell_file(path: Path, rel: str) -> list[Violation]:
    """扫描 shell 脚本中的 redis-cli XADD writer stream 模式。"""
    content = _read_text(path)
    if content is None:
        return []
    violations: list[Violation] = []
    for i, line in enumerate(content.splitlines(), 1):
        upper = line.upper()
        if "REDIS-CLI" in upper and "XADD" in upper and WRITER_STREAM_KEY in line:
            violations.append(Violation(
                rule_id="R73-NO-WRITER-DIRECT-INJECT",
                file=rel,
                line=i,
                symbol="<shell>",
                severity="P0",
                message=(
                    f"直接 redis-cli XADD {WRITER_STREAM_KEY} "
                    f"— writer stream 必须通过 restore_writer capability-seal 写入"
                ),
            ))
    return violations


# ════════════════════════════════════════════════════════════════
# YAML 工作流检测
# ════════════════════════════════════════════════════════════════


def _get_workflow_on(workflow: dict[str, Any]) -> Any:
    """获取 workflow 的 'on' 字段(YAML 解析 'on' 为 True 的兼容)。"""
    if "on" in workflow:
        return workflow["on"]
    if True in workflow:  # YAML 1.1 将 on 解析为 True
        return workflow[True]
    return None


def _runs_on_pr_or_master(workflow: dict[str, Any]) -> bool:
    """检查 workflow 是否在 PR 或 master/main push 上运行。"""
    on = _get_workflow_on(workflow)
    if on is None:
        return False
    if isinstance(on, str):
        return on in ("pull_request", "push")
    if isinstance(on, list):
        return any(t in ("pull_request", "push") for t in on)
    if isinstance(on, dict):
        if "pull_request" in on:
            return True
        if "push" in on:
            push_cfg = on["push"]
            if isinstance(push_cfg, dict):
                branches = push_cfg.get("branches", [])
                if any(b in ("master", "main") for b in branches):
                    return True
            else:
                return True  # push 无 branch 过滤 = 所有分支
    return False


def _find_line_in_text(text: str, pattern: str) -> int:
    """在文本中查找 pattern 的 1-based 行号,找不到返回 0。"""
    for i, line in enumerate(text.splitlines(), 1):
        if pattern in line:
            return i
    return 0


def _is_diagnostic_step(step: dict[str, Any]) -> bool:
    """检查步骤是否为诊断/产物上传(允许 continue-on-error)。

    误报豁免(R73 §5.24 整改):
        - 签名步骤(以 "sign" 开头或 "sign" 作为独立单词)即使名称中含 "artifact"
          也不豁免(例如 "Sign source artifact with cosign" 不是产物上传,是签名门禁)
        - "Download signed production_evidence.json" 含 "signed"(形容词)而非 "sign"
          (动词),仍视为诊断步骤(下载操作允许 continue-on-error)
    """
    name = str(step.get("name", "")).lower()
    run = str(step.get("run", "")).lower()
    uses = str(step.get("uses", "")).lower()
    # 签名步骤不得豁免:以 "sign" 开头 或 "sign" 作为独立单词(排除 "signed"/"signing")
    # 例如 "Sign source artifact with cosign (keyless)" 是签名门禁,非产物上传
    if name.startswith("sign") or re.search(r"\bsign\b", name):
        return False
    if re.search(r"\bcosign sign\b", run) or re.search(r"\bsign-blob\b", run):
        return False
    for kw in DIAGNOSTIC_STEP_KEYWORDS:
        if kw in name or kw in run or kw in uses:
            return True
    return False


def _find_step_continue_on_error_line(
    text: str, step_name: str,
) -> int:
    """在 workflow 文本中查找特定 step 的 continue-on-error 行号。

    先定位 step name 行,然后在其后 20 行内查找 continue-on-error。
    若 step name 为空或未找到,回退到第一个 continue-on-error。
    """
    if not step_name:
        return _find_line_in_text(text, "continue-on-error")
    text_lines = text.splitlines()
    # 查找 step name 行(模糊匹配,step name 可能被截断)
    step_line_idx = -1
    for i, line in enumerate(text_lines):
        if step_name in line or (
            len(step_name) > 10 and step_name[:10] in line
        ):
            step_line_idx = i
            break
    if step_line_idx < 0:
        return _find_line_in_text(text, "continue-on-error")
    # 在 step name 行后 20 行内查找 continue-on-error
    for i in range(step_line_idx, min(step_line_idx + 20, len(text_lines))):
        if "continue-on-error" in text_lines[i].lower():
            return i + 1
    return step_line_idx + 1


def _check_step_continue_on_error(
    step: dict[str, Any], job_name: str, rel: str, text: str,
) -> list[Violation]:
    """R73-NO-CONTINUE-ON-ERROR-GATE (P0): continue-on-error: true 在门禁步骤。"""
    violations: list[Violation] = []
    coe = step.get("continue-on-error")
    if coe is not True and coe != "true":
        return violations
    if _is_diagnostic_step(step):
        return violations  # 诊断/产物上传步骤允许
    step_name = str(step.get("name", step.get("uses", step.get("run", ""))))[:60]
    line = _find_step_continue_on_error_line(text, step_name)
    violations.append(Violation(
        rule_id="R73-NO-CONTINUE-ON-ERROR-GATE",
        file=rel,
        line=line,
        symbol=f"{job_name}/{step_name}",
        severity="P0",
        message=(
            f"步骤 '{step_name}' 在 job '{job_name}' 上设置 continue-on-error: true"
            f" — 门禁步骤不得忽略失败(诊断/产物上传除外)"
        ),
    ))
    return violations


def _is_critical_step(name: str, run: str) -> bool:
    """检查步骤是否为关键验证步骤(使用 word-boundary 匹配关键词)。

    使用 \\bkeyword\\b 正则避免 "test" 误匹配 "attestation"/"latest",
    "gate" 误匹配 "release-gates-sbom" 等假阳性。
    """
    for word_re in _CRITICAL_VERIFY_WORD_RES:
        if word_re.search(name) or word_re.search(run):
            return True
    return False


def _strip_comment_lines(text: str) -> str:
    """剥离 shell 注释行(# 开头),避免在注释中误匹配 'exit 0' / '|| true'。

    保留 shebang(#!)行(虽然 workflow run 块中少见)。
    """
    out_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#") and not stripped.startswith("#!"):
            out_lines.append("")  # 保留行号占位
        else:
            out_lines.append(line)
    return "\n".join(out_lines)


def _is_utility_or_true(line: str) -> bool:
    """检查 || true 是否包裹工具性命令(grep/ls/cat 等允许)。

    R73 §5.24 整改:工具性命令(grep 无匹配返回 1、ls 文件不存在返回非零、
    openssl 格式不匹配返回非零等)使用 || true 是合理的容错,不算掩盖门禁失败。

    判断逻辑:
        1. 若行中含 CRITICAL_CMD_PATTERNS(bandit/pytest/cosign sign 等)→ 不豁免(真实违规)
        2. 否则,若行中含 UTILITY_CMD_PREFIXES(grep/ls/cat 等)→ 豁免(工具容错)
        3. 否则 → 不豁免(保守报违规)
    """
    # 取 || true 之前的部分
    idx = line.find("||")
    if idx < 0:
        return False
    before = line[:idx]
    lower_before = before.lower()
    # 1. 含关键验证命令 → 不豁免(bandit/pytest/cosign sign 等不得被 || true 包裹)
    for critical_cmd in CRITICAL_CMD_PATTERNS:
        if critical_cmd in lower_before:
            return False
    # 2. 含工具性命令 → 豁免(grep/ls/cat 等容错性返回非零是合理的)
    for util_cmd in UTILITY_CMD_PREFIXES:
        if util_cmd in lower_before:
            return True
    # 3. 无关键命令也无工具命令 → 不豁免(保守报违规)
    return False


def _check_step_critical_wrap(
    step: dict[str, Any], job_name: str, rel: str, text: str,
) -> list[Violation]:
    """R73-NO-CONTINUE-ON-ERROR-GATE (P0): || true / set +e / exit 0 包裹关键验证。

    误报豁免(R73 §5.24 整改):
        - 注释行中的 || true / exit 0 / set +e 不报(shell 注释)
        - 工具性命令(grep/ls/cat/openssl 等)的 || true 不报(容错性返回非零)
        - word-boundary 匹配关键词,避免 "test" 误匹配 "attestation" 等
    """
    violations: list[Violation] = []
    run = str(step.get("run", ""))
    if not run:
        return violations
    name = str(step.get("name", ""))
    # 使用 word-boundary 匹配关键关键词
    if not _is_critical_step(name, run):
        return violations
    # 剥离注释行后再检测,避免注释中的 "exit 0" / "|| true" 误报
    run_no_comments = _strip_comment_lines(run)
    lower_run = run_no_comments.lower()
    patterns_found: list[str] = []
    has_or_true = "|| true" in lower_run or "||true" in lower_run
    has_set_plus_e = "set +e" in lower_run
    has_exit_0 = "exit 0" in lower_run
    if has_or_true:
        patterns_found.append("|| true")
    if has_set_plus_e:
        patterns_found.append("set +e")
    if has_exit_0:
        patterns_found.append("exit 0")
    if not patterns_found:
        return violations
    # 检查每行:仅当 || true 包裹非工具命令时才报违规
    # exit 0 / set +e 需要更精细判断
    real_violation = False
    if has_or_true:
        for line in run_no_comments.splitlines():
            lower_line = line.lower()
            if "|| true" in lower_line or "||true" in lower_line:
                if not _is_utility_or_true(line):
                    real_violation = True
                    break
    if has_set_plus_e:
        # set +e 保守报违规(需 case-by-case 判断)
        # 例外:若 set +e 后跟 set -e 且捕获 exit code($?),视为合理捕获模式
        if not (has_set_plus_e and "set -e" in lower_run and "$?" in run_no_comments):
            real_violation = True
    if has_exit_0:
        # exit 0 误报豁免:若 run text 同时含 exit 1(或 exit "${VAR}" 形式),
        # 说明是条件性退出(if/else 分支),不是掩盖失败的 blanket exit 0
        has_exit_1 = "exit 1" in lower_run
        has_exit_var = bool(re.search(r'exit\s+"\$\{?\w+\}?"', run_no_comments))
        has_exit_nonzero = bool(re.search(r'exit\s+[2-9]\b', run_no_comments))
        if not (has_exit_1 or has_exit_var or has_exit_nonzero):
            real_violation = True
    if not real_violation:
        return violations
    step_name = str(step.get("name", run[:40]))[:60]
    # 找到第一个非工具性 || true 的行号;若没有则用第一个 pattern 的行号
    line_no = 0
    for i, line in enumerate(text.splitlines(), 1):
        lower_line = line.lower()
        if "|| true" in lower_line or "||true" in lower_line:
            if not _is_utility_or_true(line):
                line_no = i
                break
        if "set +e" in lower_line and "set +e" in patterns_found:
            line_no = i
            break
        if "exit 0" in lower_line and "exit 0" in patterns_found:
            # 跳过注释行
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            line_no = i
            break
    if line_no == 0:
        line_no = _find_line_in_text(text, patterns_found[0])
    violations.append(Violation(
        rule_id="R73-NO-CONTINUE-ON-ERROR-GATE",
        file=rel,
        line=line_no,
        symbol=f"{job_name}/{step_name}",
        severity="P0",
        message=(
            f"关键验证步骤 '{step_name}' 被 {' + '.join(patterns_found)} 包裹"
            f" — 门禁验证不得用 || true/set +e/exit 0 掩盖失败"
        ),
    ))
    return violations


def _check_step_docker_build(
    step: dict[str, Any], job_name: str, rel: str, text: str,
) -> list[Violation]:
    """R73-NO-DOCKER-BUILD-IN-PROD (P0): docker build/buildx/build-push 在生产部署 job。"""
    is_prod = any(kw in job_name.lower() for kw in PROD_JOB_KEYWORDS)
    if not is_prod:
        return []
    violations: list[Violation] = []
    run = str(step.get("run", "")).lower()
    uses = str(step.get("uses", "")).lower()
    step_name = str(step.get("name", uses or run[:40]))[:60]
    line = 0
    if "docker build" in run or "docker buildx build" in run:
        line = _find_line_in_text(text, "docker build")
        violations.append(Violation(
            rule_id="R73-NO-DOCKER-BUILD-IN-PROD",
            file=rel,
            line=line,
            symbol=f"{job_name}/{step_name}",
            severity="P0",
            message=(
                f"生产部署 job '{job_name}' 中包含 docker build/buildx"
                f" — 生产部署必须使用预构建的不可变 digest 镜像,"
                f"不得在部署阶段重新构建"
            ),
        ))
    if "docker/build-push-action" in uses:
        line = _find_line_in_text(text, "build-push-action")
        violations.append(Violation(
            rule_id="R73-NO-DOCKER-BUILD-IN-PROD",
            file=rel,
            line=line,
            symbol=f"{job_name}/{step_name}",
            severity="P0",
            message=(
                f"生产部署 job '{job_name}' 使用 docker/build-push-action"
                f" — 生产部署必须使用预构建的不可变 digest 镜像"
            ),
        ))
    return violations


def _check_step_floating_image_tag(
    step: dict[str, Any], job_name: str, rel: str, text: str,
) -> list[Violation]:
    """R73-NO-FLOATING-IMAGE-TAG (P1): uses: docker://image:tag 缺少 @sha256 digest。"""
    violations: list[Violation] = []
    uses = str(step.get("uses", ""))
    if not uses.startswith("docker://"):
        return violations
    image = uses[len("docker://"):]
    if "@sha256:" in image:
        return violations
    step_name = str(step.get("name", uses))[:60]
    line = _find_line_in_text(text, "docker://")
    violations.append(Violation(
        rule_id="R73-NO-FLOATING-IMAGE-TAG",
        file=rel,
        line=line,
        symbol=f"{job_name}/{step_name}",
        severity="P1",
        message=(
            f"docker:// 镜像 '{image}' 缺少 @sha256 digest"
            f" — 生产容器必须固定到不可变 digest"
        ),
    ))
    return violations


def _check_step_unpinned_action(
    step: dict[str, Any], job_name: str, rel: str, text: str,
) -> list[Violation]:
    """R73-NO-UNPINNED-ACTION (P1): 第三方 action 未固定到完整 commit SHA。"""
    violations: list[Violation] = []
    uses = str(step.get("uses", ""))
    if not uses or uses.startswith("./"):
        return violations  # 本地 action
    if "@" not in uses:
        return violations
    parts = uses.rsplit("@", 1)
    if len(parts) != 2:
        return violations
    repo, ref = parts
    if _SHA40_RE.match(ref):
        return violations  # 已固定到 40 字符 SHA
    # 第一方 action(actions/*, github/*)允许使用 tag
    if repo.startswith(FIRST_PARTY_ACTION_PREFIXES):
        return violations
    step_name = str(step.get("name", uses))[:60]
    line = _find_line_in_text(text, uses)
    violations.append(Violation(
        rule_id="R73-NO-UNPINNED-ACTION",
        file=rel,
        line=line,
        symbol=f"{job_name}/{step_name}",
        severity="P1",
        message=(
            f"第三方 action '{uses}' 未固定到完整 commit SHA"
            f" (ref={ref!r}) — 必须使用 @<40-char-sha> 防止供应链篡改"
        ),
    ))
    return violations


def _check_step_if_always_success(
    step: dict[str, Any], job_name: str, rel: str, text: str,
) -> list[Violation]:
    """R73-NO-IF-ALWAYS-SUCCESS (P0): if: always() 在门禁 job 上返回 success。"""
    is_gate = any(kw in job_name.lower() for kw in GATE_JOB_KEYWORDS)
    if not is_gate:
        return []
    if_cond = str(step.get("if", ""))
    if "always()" not in if_cond:
        return []
    run = str(step.get("run", "")).lower()
    # 检查是否在 if: always() 后强制返回 success
    has_success_force = (
        "exit 0" in run
        or "success" in run
        or "::success::" in run
        or "conclusion" in run and "success" in run
    )
    if not has_success_force:
        # if: always() 本身在门禁 job 上即可疑(step 失败后仍执行)
        # 但仅当步骤会输出 success 时才报违规
        # 没有明显 success 强制时,不报(可能是 cleanup)
        return []
    step_name = str(step.get("name", run[:40]))[:60]
    line = _find_line_in_text(text, "always()")
    out: list[Violation] = []
    out.append(Violation(
        rule_id="R73-NO-IF-ALWAYS-SUCCESS",
        file=rel,
        line=line,
        symbol=f"{job_name}/{step_name}",
        severity="P0",
        message=(
            f"门禁 job '{job_name}' 步骤 '{step_name}' 使用 if: always()"
            f" 后强制返回 success — 不得掩盖门禁失败"
        ),
    ))
    return out


def _job_runs_only_on_safe_events(job: dict[str, Any]) -> bool:
    """检查 job 的 if 条件是否限制为安全事件(非 PR/push)。

    R73 §5.24 整改:某些 job 虽然 workflow 在 PR/push 上触发,但 job 级 if 条件
    限制为仅在 workflow_dispatch / tag push / merged PR 上运行,这种 job 不算
    "PR/master 触发的 job",豁免写权限检查。

    豁免的 if 模式:
        - github.event_name == 'workflow_dispatch'  (仅手动触发)
        - github.event.pull_request.merged == true  (仅合并后触发,post-merge)
        - startsWith(github.ref, 'refs/tags/...')    (仅 tag push,非分支 push)
    """
    if_cond = str(job.get("if", ""))
    if not if_cond:
        return False
    # 仅 workflow_dispatch 触发
    if "github.event_name" in if_cond and "workflow_dispatch" in if_cond:
        # 简单检查:if 条件中含 workflow_dispatch 且不含 pull_request / push
        if "pull_request" not in if_cond and "push" not in if_cond:
            return True
    # 仅 merged PR 触发(post-merge 操作,如分支删除)
    if "github.event.pull_request.merged" in if_cond and "true" in if_cond:
        return True
    # 仅 tag push 触发(非分支 push)
    if "startsWith(github.ref, 'refs/tags/" in if_cond and "refs/heads" not in if_cond:
        return True
    return False


def _check_write_permissions(
    workflow: dict[str, Any], job_name: str, job: dict[str, Any],
    rel: str, text: str,
) -> list[Violation]:
    """R73-NO-WRITE-PERM-ON-PR (P1): PR/master job 含 contents/deployments/ruleset write 权限。

    误报豁免(R73 §5.24 整改):
        - job 级 if 条件限制为 workflow_dispatch / merged PR / tag push 的 job 豁免
          (这些 job 不在 PR/master 分支 push 时运行,无未授权修改风险)
    """
    if not _runs_on_pr_or_master(workflow):
        return []
    # job 级 if 条件限制为安全事件(workflow_dispatch / merged PR / tag push)→ 豁免
    if _job_runs_only_on_safe_events(job):
        return []
    violations: list[Violation] = []
    # 检查 job 级 permissions;若未设置则继承 workflow 级
    job_perms = job.get("permissions")
    workflow_perms = workflow.get("permissions")
    perms_to_check: dict[str, Any] = {}
    if isinstance(job_perms, dict):
        perms_to_check = job_perms
    elif isinstance(job_perms, str):
        if job_perms == "write-all":
            perms_to_check = {"contents": "write", "deployments": "write"}
    elif job_perms is None and isinstance(workflow_perms, dict):
        perms_to_check = workflow_perms
    elif isinstance(workflow_perms, str):
        if workflow_perms == "write-all":
            perms_to_check = {"contents": "write", "deployments": "write"}
    flagged_perms: list[str] = []
    for perm_name, perm_val in perms_to_check.items():
        if perm_val != "write":
            continue
        if perm_name in ("contents", "deployments"):
            flagged_perms.append(perm_name)
        elif "ruleset" in perm_name.lower():
            flagged_perms.append(perm_name)
    if not flagged_perms:
        return []
    # 找到该 job 的 permissions 行号(优先 job 级,fallback 到 workflow 级)
    line = 0
    if isinstance(job_perms, dict):
        # 在 job 定义后查找 permissions
        job_pattern = f"  {job_name}:"
        line = _find_line_in_text(text, job_pattern)
        if line > 0:
            # 在 job 行之后查找 permissions: write
            text_lines = text.splitlines()
            for i in range(line, min(line + 20, len(text_lines))):
                if "permissions:" in text_lines[i]:
                    line = i + 1
                    break
    if line == 0:
        line = _find_line_in_text(text, "write")
    violations.append(Violation(
        rule_id="R73-NO-WRITE-PERM-ON-PR",
        file=rel,
        line=line,
        symbol=job_name,
        severity="P1",
        message=(
            f"PR/master job '{job_name}' 含写权限 {flagged_perms}"
            f" — PR/master 触发的 job 不得有 contents/deployments/ruleset"
            f" 写权限(防止未授权修改)"
        ),
    ))
    return violations


def scan_workflow_file(path: Path, rel: str) -> list[Violation]:
    """扫描单个 GitHub Actions workflow YAML 文件。"""
    content = _read_text(path)
    if content is None:
        return []
    if _HAS_YAML:
        try:
            data = yaml.safe_load(content)
        except yaml.YAMLError as e:  # type: ignore[union-attr]
            print(f"[WARN] YAML parse error in {rel}: {e}", file=sys.stderr)
            return []
        if not isinstance(data, dict):
            return []
    else:
        # regex fallback:基础文本检测
        return _scan_workflow_regex_fallback(content, rel)
    violations: list[Violation] = []
    jobs = data.get("jobs", {})
    if not isinstance(jobs, dict):
        return violations
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        steps = job.get("steps", [])
        if not isinstance(steps, list):
            continue
        # job 级权限检查
        violations.extend(_check_write_permissions(data, job_name, job, rel, content))
        for step in steps:
            if not isinstance(step, dict):
                continue
            violations.extend(_check_step_continue_on_error(step, job_name, rel, content))
            violations.extend(_check_step_critical_wrap(step, job_name, rel, content))
            violations.extend(_check_step_docker_build(step, job_name, rel, content))
            violations.extend(_check_step_floating_image_tag(step, job_name, rel, content))
            violations.extend(_check_step_unpinned_action(step, job_name, rel, content))
            violations.extend(_check_step_if_always_success(step, job_name, rel, content))
    return violations


def _scan_workflow_regex_fallback(content: str, rel: str) -> list[Violation]:
    """PyYAML 不可用时的 regex fallback(基础检测,警告)。"""
    violations: list[Violation] = []
    # continue-on-error: true
    for i, line in enumerate(content.splitlines(), 1):
        if "continue-on-error:" in line and "true" in line.lower():
            violations.append(Violation(
                rule_id="R73-NO-CONTINUE-ON-ERROR-GATE",
                file=rel, line=i, symbol="<regex>", severity="P0",
                message="continue-on-error: true (regex fallback 检测,PyYAML 不可用)",
            ))
        if "if:" in line and "always()" in line:
            violations.append(Violation(
                rule_id="R73-NO-IF-ALWAYS-SUCCESS",
                file=rel, line=i, symbol="<regex>", severity="P0",
                message="if: always() (regex fallback 检测,PyYAML 不可用)",
            ))
    # 未固定 action
    for match in re.finditer(r"uses:\s*(\S+@\S+)", content):
        uses = match.group(1)
        if uses.startswith("./"):
            continue
        parts = uses.rsplit("@", 1)
        if len(parts) == 2 and not _SHA40_RE.match(parts[1]):
            if not parts[0].startswith(FIRST_PARTY_ACTION_PREFIXES):
                line = content[:match.start()].count("\n") + 1
                violations.append(Violation(
                    rule_id="R73-NO-UNPINNED-ACTION",
                    file=rel, line=line, symbol="<regex>", severity="P1",
                    message=f"第三方 action {uses} 未固定到 SHA (regex fallback)",
                ))
    return violations


# ════════════════════════════════════════════════════════════════
# docker-compose 检测
# ════════════════════════════════════════════════════════════════


def _is_infra_image(image: str) -> bool:
    """判断是否为基础设施镜像(豁免 digest 检查)。"""
    for prefix in INFRASTRUCTURE_IMAGE_PREFIXES:
        if image.startswith(prefix):
            return True
    return False


def scan_compose_file(path: Path, rel: str) -> list[Violation]:
    """扫描 docker-compose YAML 文件中的浮动镜像 tag 与 docker build。"""
    content = _read_text(path)
    if content is None:
        return []
    if not _HAS_YAML:
        # regex fallback
        violations: list[Violation] = []
        for i, line in enumerate(content.splitlines(), 1):
            if "build:" in line:
                violations.append(Violation(
                    rule_id="R73-NO-DOCKER-BUILD-IN-PROD",
                    file=rel, line=i, symbol="<regex>", severity="P0",
                    message="build: 字段 (regex fallback,PyYAML 不可用)",
                ))
        return violations
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as e:  # type: ignore[union-attr]
        print(f"[WARN] YAML parse error in {rel}: {e}", file=sys.stderr)
        return []
    if not isinstance(data, dict):
        return []
    services = data.get("services", {})
    if not isinstance(services, dict):
        return []
    violations: list[Violation] = []
    is_prod_compose = "prod" in path.name.lower()
    for svc_name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        # build: 字段(生产 compose 中禁止)
        if is_prod_compose and "build" in svc:
            line = _find_line_in_text(content, "build:")
            violations.append(Violation(
                rule_id="R73-NO-DOCKER-BUILD-IN-PROD",
                file=rel, line=line, symbol=svc_name, severity="P0",
                message=(
                    f"生产 compose 服务 '{svc_name}' 含 build: 字段"
                    f" — 生产 compose 必须使用预构建不可变 image digest"
                ),
            ))
        # 浮动镜像 tag(仅生产 compose 检查)
        if is_prod_compose:
            image = str(svc.get("image", ""))
            if image and not _is_infra_image(image):
                # 跳过变量引用(如 ${TGJIEMA_IMAGE})
                if image.startswith("${"):
                    continue
                if "@sha256:" not in image:
                    line = _find_line_in_text(content, "image:")
                    violations.append(Violation(
                        rule_id="R73-NO-FLOATING-IMAGE-TAG",
                        file=rel, line=line, symbol=svc_name, severity="P1",
                        message=(
                            f"生产 compose 服务 '{svc_name}' image '{image}'"
                            f" 缺少 @sha256 digest — 必须固定到不可变 digest"
                        ),
                    ))
    return violations


# ════════════════════════════════════════════════════════════════
# Dockerfile 检测
# ════════════════════════════════════════════════════════════════


def scan_dockerfile(path: Path, rel: str) -> list[Violation]:
    """扫描 Dockerfile 中的浮动基础镜像 tag。

    误报豁免(R73 §5.24 整改):
        - FROM ${ARG_NAME} 引用的镜像,若 ARG 默认值含 @sha256: digest,视为已固定
          (例如: ARG PYTHON_IMAGE=python:3.12-slim@sha256:... → FROM ${PYTHON_IMAGE})
    """
    content = _read_text(path)
    if content is None:
        return []
    violations: list[Violation] = []
    # 解析所有 ARG 定义,记录带 digest 的 ARG 默认值
    arg_values: dict[str, str] = {}
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("ARG "):
            # ARG NAME=value 或 ARG NAME
            arg_part = stripped[4:].strip()
            if "=" in arg_part:
                arg_name, _, arg_val = arg_part.partition("=")
                arg_name = arg_name.strip()
                arg_val = arg_val.strip().strip('"').strip("'")
                arg_values[arg_name] = arg_val
    for i, line in enumerate(content.splitlines(), 1):
        stripped = line.strip()
        if not stripped.upper().startswith("FROM "):
            continue
        parts = stripped.split(None, 1)
        if len(parts) < 2:
            continue
        image_ref = parts[1].split()[0]
        # 去掉 AS alias
        upper_rest = stripped.upper()
        if " AS " in upper_rest:
            image_ref = image_ref.split()[0]
        if image_ref == "scratch":
            continue
        if "@sha256:" in image_ref:
            continue
        # FROM ${ARG_NAME} 引用:检查 ARG 默认值是否含 digest
        if image_ref.startswith("${") and image_ref.endswith("}"):
            arg_name = image_ref[2:-1]
            arg_val = arg_values.get(arg_name, "")
            if "@sha256:" in arg_val:
                continue  # ARG 默认值已固定 digest,豁免
        # 基础设施镜像豁免
        if _is_infra_image(image_ref):
            continue
        violations.append(Violation(
            rule_id="R73-NO-FLOATING-IMAGE-TAG",
            file=rel,
            line=i,
            symbol="FROM",
            severity="P1",
            message=(
                f"FROM '{image_ref}' 缺少 @sha256 digest"
                f" — 基础镜像必须固定到不可变 digest 保证供应链安全"
            ),
        ))
    return violations


# ════════════════════════════════════════════════════════════════
# 主扫描流程
# ════════════════════════════════════════════════════════════════


def _get_source_sha(target: Path) -> str:
    """获取 git HEAD SHA(失败返回 'unknown')。"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(target),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        pass
    return "unknown"


def scan(target: Path) -> tuple[list[Violation], int]:
    """主扫描流程。

    Args:
        target: 扫描目标根目录

    Returns:
        (violations, files_scanned)
    """
    if not _HAS_YAML:
        print(
            "[WARN] PyYAML 不可用 — YAML 文件使用 regex fallback,"
            "覆盖范围有限(建议 pip install pyyaml)",
            file=sys.stderr,
        )
    violations: list[Violation] = []
    files_scanned = 0

    # Python 文件
    for py_file in _iter_python_files(target):
        files_scanned += 1
        rel = _rel_posix(py_file, target)
        violations.extend(scan_python_file(py_file, rel))

    # Shell 脚本
    for sh_file in _iter_shell_scripts(target):
        files_scanned += 1
        rel = _rel_posix(sh_file, target)
        violations.extend(scan_shell_file(sh_file, rel))

    # GitHub Actions workflows
    for wf_file in _iter_workflow_files(target):
        files_scanned += 1
        rel = _rel_posix(wf_file, target)
        violations.extend(scan_workflow_file(wf_file, rel))

    # docker-compose 文件
    for compose_file in _iter_compose_files(target):
        files_scanned += 1
        rel = _rel_posix(compose_file, target)
        violations.extend(scan_compose_file(compose_file, rel))

    # Dockerfile
    for dockerfile in _iter_dockerfiles(target):
        files_scanned += 1
        rel = _rel_posix(dockerfile, target)
        violations.extend(scan_dockerfile(dockerfile, rel))

    # 按严重级别与文件排序(P0 优先,然后按 file/line)
    severity_order = {"P0": 0, "P1": 1}
    violations.sort(key=lambda v: (severity_order.get(v.severity, 9), v.file, v.line))
    return violations, files_scanned


def build_result(
    violations: list[Violation], files_scanned: int, source_sha: str,
) -> dict[str, Any]:
    """构建 JSON 结果 dict。"""
    by_severity: dict[str, int] = {"P0": 0, "P1": 0}
    for v in violations:
        if v.severity in by_severity:
            by_severity[v.severity] += 1
    passed = by_severity["P0"] == 0 and by_severity["P1"] == 0
    return {
        "passed": passed,
        "policy_version": POLICY_VERSION,
        "source_sha": source_sha,
        "scanned_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "violations": [v.to_dict() for v in violations],
        "summary": {
            "files_scanned": files_scanned,
            "violations_by_severity": by_severity,
        },
    }


# ════════════════════════════════════════════════════════════════
# CLI 入口
# ════════════════════════════════════════════════════════════════


def main() -> None:
    """脚本入口。"""
    parser = argparse.ArgumentParser(
        description=(
            "R73 §5.24: 生产/预发布绕过模式静态扫描器"
            " — 检测 CI 健康绕过、legacy restore 逃生舱、writer 直接注入、"
            "异常吞掉、mark_dirty=False、continue-on-error 门禁绕过、"
            "docker build in prod、浮动镜像 tag、未固定 action、"
            "PR/master 写权限、if:always() success、动态 writer 分发。"
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式输出扫描结果到 stdout",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="将 JSON 结果写入指定文件(与 --json 可同时使用)",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=Path.cwd(),
        help="扫描目标根目录(默认: 当前工作目录)",
    )
    parser.add_argument(
        "--verify-only",
        type=Path,
        metavar="BASELINE",
        help=(
            "对比模式:仅当当前违规数 ≤ baseline 中的违规数时退出 0"
            "(用于 CI 门禁,防止引入新违规)"
        ),
    )
    args = parser.parse_args()

    target: Path = args.target.resolve()
    if not target.is_dir():
        print(f"[ERROR] 目标目录不存在: {target}", file=sys.stderr)
        sys.exit(2)

    violations, files_scanned = scan(target)
    source_sha = _get_source_sha(target)
    result = build_result(violations, files_scanned, source_sha)

    # 写入 --output 文件
    if args.output is not None:
        try:
            args.output.write_text(
                json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError as e:
            print(f"[ERROR] 无法写入输出文件 {args.output}: {e}", file=sys.stderr)
            sys.exit(2)

    # --json 输出到 stdout
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))

    # --verify-only 模式:与 baseline 对比
    if args.verify_only is not None:
        try:
            baseline_data = json.loads(
                args.verify_only.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as e:
            print(
                f"[ERROR] 无法加载 baseline {args.verify_only}: {e}",
                file=sys.stderr,
            )
            sys.exit(2)
        baseline_count = len(baseline_data.get("violations", []))
        current_count = len(violations)
        if current_count <= baseline_count:
            print(
                f"[OK] 无新违规引入 (current={current_count}, "
                f"baseline={baseline_count})"
            )
            sys.exit(0)
        else:
            print(
                f"[FAIL] 检测到新违规 (current={current_count}, "
                f"baseline={baseline_count})",
                file=sys.stderr,
            )
            sys.exit(8)

    # 默认模式:任一 P0/P1 违规 → exit 8
    p0_count = result["summary"]["violations_by_severity"]["P0"]
    p1_count = result["summary"]["violations_by_severity"]["P1"]
    if p0_count == 0 and p1_count == 0:
        if not args.json:
            print(
                f"[OK] R73 §5.24 生产绕过扫描通过"
                f" (扫描 {files_scanned} 个文件,无 P0/P1 违规)"
            )
        sys.exit(0)
    if not args.json:
        print(
            f"[FAIL] R73 §5.24 生产绕过扫描检测到违规"
            f" (P0={p0_count}, P1={p1_count}, 扫描 {files_scanned} 个文件):"
        )
        for v in violations:
            print(
                f"  - [{v.severity}] {v.rule_id} {v.file}:{v.line}"
                f" ({v.symbol}) — {v.message}"
            )
    sys.exit(8)


if __name__ == "__main__":
    main()
