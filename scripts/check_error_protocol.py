#!/usr/bin/env python3
"""R56 P1-5: CI AST 静态扫描 — 错误协议规则门禁。

扫描 admin/、services/、bots/、database/ 下的 Python 文件,
使用 AST 检测以下错误协议违规:

规则 1: 禁止 except Exception: pass (bare pass,吞掉异常)
规则 2: 禁止 except Exception:\\n    pass (跨行形式,同上)
规则 3: 禁止 except 块中 bare return 0 / return False (吞掉错误)
规则 4: 禁止模块边界函数返回裸字符串 (应返回 ErrorEnvelope)
规则 5: 禁止 raise ValueError/RuntimeError/Exception 携带字符串字面量
        (services/、admin/、bots/ 中必须使用 AppError)

白名单(跳过扫描):
    - services/error_codes.py  (AppError 定义模块)
    - __pycache__/             (缓存目录)
    - test_*.py               (测试文件)

允许的非 AppError 异常(不视为违规):
    - EffectReceiptError  (utils/exceptions.py)
    - DurabilityError     (services/effect_receipts.py)
    - AppError            (services/error_codes.py)

Baseline 机制:
    通过 --baseline <file> 读取允许的违规数量(violation_count),
    当前违规数 <= baseline 时 exit 0(通过),
    超过 baseline 时 exit 1(失败)。
    --strict 模式忽略 baseline,任何违规都 exit 1。

R62 P1-04 结构化 allowlist:
    observability 域不再使用 max_violations,改用结构化 allowlist。
    每个违规必须匹配 allowlist 条目(按指纹),allowlist 条目必须包含:
        file / line / fingerprint / owner / reason / expiry / ticket
    规则:
        1. 每个违规计算指纹 (sha256 of file:line:violation_type:context)
        2. 指纹匹配 allowlist 条目 → 检查 expiry
        3. expiry < today → 失败(allowlist 条目已过期)
        4. 未匹配 → real_violations(生产目标 = 0)
        5. ratchet: 总违规数 <= baseline violation_count(每个 commit 只减不增)
    --strict 模式: 任何未在 allowlist 中的违规即失败(忽略 ratchet 比较,
    只检查 real_violations == 0)。

R63 P1-10 AST 结构指纹 + 按模块分类:
    旧版(R62)指纹使用 ``file:line:violation_type:context``,行号变化即导致
    指纹全变(不稳定),审计投诉"行号变化导致 fingerprint 全变说明当前指纹
    不稳定,应使用 AST 结构/规则/函数名而非裸行号"。
    新版(R63)指纹使用 ``file:violation_type:structural_context``,其中
    structural_context = "{enclosing_function}|{source_line_content}":
        - 行号不再参与指纹(添加空行 / 上方代码重排 → 指纹不变)
        - 包含函数名(同文件不同函数的同类违规可区分)
        - 包含源行内容(同函数内多个同类违规可区分)
    allowlist 条目新增分类字段(按模块 owner/root_cause/ticket/plan):
        - owner:       负责人
        - root_cause:  根因描述(按模块)
        - ticket:      跟踪 ticket(R63-P1-10-<module>)
        - plan:        修复计划
        - expiry:      过期日(R63 默认 2026-08-18,30 天窗口)
    规则检测逻辑(Rule 1-5)不变,仅指纹计算与 allowlist 结构变化。

CI 调用方式:
    python scripts/check_error_protocol.py --strict
    python scripts/check_error_protocol.py --baseline scripts/error_protocol_baseline.json

退出码:
    0 — 通过(所有违规已 allowlist 且未过期,real_violations == 0,
         且未超过 ratchet baseline)
    1 — 失败(有未 allowlist 的违规,或 allowlist 条目已过期,
         或总违规数 > baseline violation_count)
"""
from __future__ import annotations

import argparse
import ast
import datetime
import hashlib
import json
import sys
from pathlib import Path
from typing import Iterable

# 项目根目录(scripts/ 的上一级)
REPO_ROOT = Path(__file__).resolve().parent.parent

# 待扫描的目录(相对 REPO_ROOT)
SCAN_DIRS: list[str] = ["admin", "services", "bots", "database"]

# Rule 5 仅扫描这三个目录(不含 database/)
RULE5_DIRS: list[str] = ["admin", "services", "bots"]

# 跳过的目录名(不扫描)
SKIP_DIR_PARTS: list[str] = [
    "__pycache__",
    ".git",
    "node_modules",
    "venv",
    ".venv",
    ".pytest_cache",
]

# 白名单文件(定义 AppError 等异常的模块,允许使用 raise)
ALLOWED_FILES: list[str] = [
    "services/error_codes.py",  # AppError 定义模块
]

# 允许的非 AppError 异常类名(不视为违规)
ALLOWED_EXCEPTIONS: frozenset[str] = frozenset({
    "EffectReceiptError",
    "DurabilityError",
    "AppError",
})

# Rule 5 禁止的异常类名(携带字符串字面量时)
BANNED_RAISE_EXCEPTIONS: frozenset[str] = frozenset({
    "ValueError",
    "RuntimeError",
    "Exception",
})

# Rule 4: 模块边界函数返回裸字符串的最小长度(过滤短字符串,减少误报)
MIN_BARE_STRING_LEN = 5


# ════════════════════════════════════════════════════════════
# 路径工具函数
# ════════════════════════════════════════════════════════════
def _rel_posix(path: Path) -> str:
    """返回相对 REPO_ROOT 的 POSIX 路径字符串(用 / 分隔)。

    若文件不在 REPO_ROOT 内,返回绝对路径的 POSIX 形式。
    """
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _is_skipped_path(path: Path) -> bool:
    """检查路径是否应跳过(包含缓存/依赖目录名)。"""
    rel = _rel_posix(path)
    for part in SKIP_DIR_PARTS:
        if part in rel:
            return True
    return False


def _is_allowed(path: Path) -> bool:
    """检查文件是否在白名单中(允许使用 raise)。"""
    rel = _rel_posix(path)
    for allowed in ALLOWED_FILES:
        if rel == allowed:
            return True
    return False


def _is_test_file(path: Path) -> bool:
    """检查是否是测试文件(test_*.py 或 *_test.py)。"""
    name = path.name
    return name.startswith("test_") or name.endswith("_test.py")


def _is_in_rule5_dir(path: Path) -> bool:
    """检查文件是否在 Rule 5 扫描目录中(services/、admin/、bots/)。

    Rule 5 不扫描 database/ 目录(仅检查业务代码中的裸异常)。
    """
    rel = _rel_posix(path)
    for rule5_dir in RULE5_DIRS:
        if rel.startswith(rule5_dir + "/"):
            return True
    return False


def _iter_python_files() -> Iterable[Path]:
    """遍历 SCAN_DIRS 下所有 .py 文件(跳过缓存/依赖目录)。"""
    for scan_dir in SCAN_DIRS:
        scan_path = REPO_ROOT / scan_dir
        if not scan_path.exists():
            continue
        for py_file in scan_path.rglob("*.py"):
            if _is_skipped_path(py_file):
                continue
            yield py_file


# ════════════════════════════════════════════════════════════
# AST 检测辅助函数
# ════════════════════════════════════════════════════════════
def _is_exception_handler_for_exception(handler: ast.ExceptHandler) -> bool:
    """检查 except handler 是否捕获 Exception 类型。

    匹配: except Exception / except Exception as e
    不匹配: bare except / except SomeOtherError
    """
    if handler.type is None:
        return False  # bare except (无类型)
    # except Exception / except Exception as e
    if isinstance(handler.type, ast.Name) and handler.type.id == "Exception":
        return True
    return False


def _handler_body_is_only_pass(handler: ast.ExceptHandler) -> bool:
    """检查 except 块体是否仅包含 pass 语句。

    匹配:
      except Exception: pass
      except Exception:
          pass
      except Exception:
          '''docstring'''
          pass
    """
    if not handler.body:
        return False
    # 体中只有 pass 语句
    if len(handler.body) == 1 and isinstance(handler.body[0], ast.Pass):
        return True
    # 体中可能有 docstring + pass(docstring 是 Expr(Constant(str)))
    non_doc = [
        stmt for stmt in handler.body
        if not (isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str))
    ]
    if len(non_doc) == 1 and isinstance(non_doc[0], ast.Pass):
        return True
    return False


def _is_bare_zero_or_false(node: ast.AST) -> bool:
    """检查是否是 return 0 或 return False 的值节点。

    精确区分:
      return 0     → int 0 (匹配)
      return False → bool False (匹配)
      return 0.0   → float 0.0 (不匹配,避免误报)
      return True  → bool True (不匹配)
      return 1     → int 1 (不匹配)
    """
    if not isinstance(node, ast.Constant):
        return False
    val = node.value
    # return False (bool 单例)
    if isinstance(val, bool):
        return val is False
    # return 0 (int, 排除 bool)
    if isinstance(val, int):
        return val == 0
    return False


def _is_bare_string_constant(node: ast.AST) -> bool:
    """检查是否是字符串常量节点(ast.Constant with str value)。"""
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _has_str_return_annotation(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    """检查函数是否有 -> str 返回注解(简略判断)。

    如果注解中包含 Name(id='str'),则认为函数预期返回字符串,
    跳过 Rule 4 检查(返回字符串是函数的合法行为)。

    支持:
      -> str
      -> Optional[str]
      -> Union[str, int]
    """
    ret = func_node.returns
    if ret is None:
        return False
    # -> str
    if isinstance(ret, ast.Name) and ret.id == "str":
        return True
    # Optional[str] / Union[str, ...] 等:检查 AST dump 中是否有 id='str'
    try:
        dump = ast.dump(ret)
        if "id='str'" in dump:
            return True
    except Exception:
        pass
    return False


def _is_public_module_level_func(
    node: ast.AST,
    module_body: list,
) -> bool:
    """检查是否是模块级公开函数(非 _ 开头,非 dunder,直接定义在模块体中)。

    用于 Rule 4:模块边界函数 = 直接定义在模块体中的公开函数。
    不检查类方法(类方法通过类接口暴露,非直接模块边界)。
    """
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    name = node.name
    # 跳过私有函数(_开头)
    if name.startswith("_"):
        return False
    # 跳过 dunder 方法(__init__, __str__ 等)
    if name.startswith("__") and name.endswith("__"):
        return False
    # 必须直接在模块体中(非嵌套函数)
    return node in module_body


def _get_raise_exception_name(node: ast.Raise) -> str | None:
    """从 raise 语句中提取异常类名。

    支持形式:
      raise ValueError("...")     → "ValueError"
      raise ValueError            → "ValueError"
      raise module.CustomError()  → "CustomError"
      raise                       → None (bare reraise)
    """
    exc = node.exc
    if exc is None:
        return None  # bare raise (reraise)
    # raise ValueError("...")
    if isinstance(exc, ast.Call):
        func = exc.func
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
    # raise ValueError (without call)
    if isinstance(exc, ast.Name):
        return exc.id
    if isinstance(exc, ast.Attribute):
        return exc.attr
    return None


def _raise_has_string_literal_arg(node: ast.Raise) -> bool:
    """检查 raise 语句的异常构造参数中是否有字符串字面量。

    匹配: raise ValueError("error message")
    不匹配: raise ValueError(code)  /  raise ValueError()
    """
    exc = node.exc
    if not isinstance(exc, ast.Call):
        return False
    # 检查位置参数
    for arg in exc.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return True
    # 检查关键字参数
    for kw in exc.keywords:
        if isinstance(kw.value, ast.Constant) and isinstance(kw.value, str):
            return True
    return False


def _walk_skip_nested(node: ast.AST) -> Iterable[ast.AST]:
    """递归遍历节点,跳过嵌套的 FunctionDef/AsyncFunctionDef/Lambda。

    用于 Rule 4:只检查当前函数体内的 return 语句,
    不检查嵌套函数体内的(嵌套函数有自己的作用域)。
    """
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield from _walk_skip_nested(child)


def _walk_func_body_no_nested(func_node: ast.AST) -> Iterable[ast.AST]:
    """遍历函数体中的所有节点,但不进入嵌套函数定义。"""
    for child in ast.iter_child_nodes(func_node):
        yield from _walk_skip_nested(child)


# ════════════════════════════════════════════════════════════
# 主扫描逻辑
# ════════════════════════════════════════════════════════════
def scan_file(path: Path) -> list[tuple[int, str]]:
    """扫描单个 Python 文件,返回 [(line_no, detail), ...] 违规列表。

    使用 AST 解析,检测 5 类错误协议违规。
    """
    findings: list[tuple[int, str]] = []

    # 读取文件内容(容忍编码错误)
    try:
        source = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return findings

    # 解析 AST
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return findings

    # 获取模块级函数列表(用于 Rule 4)
    module_body = tree.body
    module_level_funcs = [
        node for node in module_body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]

    # 判断当前文件是否在 Rule 5 扫描目录中
    is_rule5_dir = _is_in_rule5_dir(path)

    # ── Rule 1+2: 检查 except Exception: pass ──
    # 单行和跨行形式在 AST 中相同(ExceptHandler body=[Pass])
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if _is_exception_handler_for_exception(node) and _handler_body_is_only_pass(node):
            findings.append((
                node.lineno,
                "P1-5 规则1/2: except Exception 后直接 pass (吞掉异常, "
                "应记录日志或重新抛出 AppError)",
            ))

    # ── Rule 3: 检查 except 块中的 bare return 0 / return False ──
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        for stmt in node.body:
            # 直接 return 0 / return False
            if isinstance(stmt, ast.Return) and stmt.value is not None:
                if _is_bare_zero_or_false(stmt.value):
                    findings.append((
                        stmt.lineno,
                        "P1-5 规则3: except 块中 return 0/False "
                        "(吞掉错误,应使用 AppError 或记录日志后 reraise)",
                    ))

    # ── Rule 4: 检查模块边界函数返回裸字符串 ──
    # 模块级公开函数不应返回裸字符串(应返回 ErrorEnvelope 结构化错误)
    for func in module_level_funcs:
        if not _is_public_module_level_func(func, module_body):
            continue
        # 跳过有 -> str 注解的函数(返回字符串是合法行为)
        if _has_str_return_annotation(func):
            continue
        # 遍历函数体(跳过嵌套函数)查找 return "string"
        for node in _walk_func_body_no_nested(func):
            if isinstance(node, ast.Return) and node.value is not None:
                if _is_bare_string_constant(node.value):
                    str_val = node.value.value
                    # 过滤空字符串和短字符串(减少误报)
                    if isinstance(str_val, str) and len(str_val) >= MIN_BARE_STRING_LEN:
                        findings.append((
                            node.lineno,
                            f"P1-5 规则4: 模块边界函数 '{func.name}' "
                            f"返回裸字符串 (应返回 ErrorEnvelope 结构化错误)",
                        ))

    # ── Rule 5: 检查 raise ValueError/RuntimeError/Exception 携带字符串字面量 ──
    # 仅在 services/、admin/、bots/ 目录中检查(database/ 跳过)
    if is_rule5_dir:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise):
                continue
            exc_name = _get_raise_exception_name(node)
            if exc_name is None:
                continue
            # 跳过允许的异常类(EffectReceiptError / DurabilityError / AppError)
            if exc_name in ALLOWED_EXCEPTIONS:
                continue
            # 检查是否是禁止的异常类
            if exc_name in BANNED_RAISE_EXCEPTIONS:
                if _raise_has_string_literal_arg(node):
                    findings.append((
                        node.lineno,
                        f"P1-5 规则5: raise {exc_name}(\"...\") 携带字符串字面量 "
                        f"(必须使用 AppError(ErrorCodes.XXX, params={{...}}))",
                    ))

    return findings


def collect_findings() -> list[tuple[str, int, str]]:
    """收集所有文件的违规,返回 [(file, line_no, detail), ...]。"""
    all_findings: list[tuple[str, int, str]] = []
    for py_file in _iter_python_files():
        # 跳过白名单文件和测试文件
        if _is_allowed(py_file) or _is_test_file(py_file):
            continue
        file_findings = scan_file(py_file)
        rel = _rel_posix(py_file)
        for line_no, detail in file_findings:
            all_findings.append((rel, line_no, detail))
    return all_findings


# ════════════════════════════════════════════════════════════
# Baseline 读取
# ════════════════════════════════════════════════════════════
def _load_baseline_count(baseline_path: Path | None) -> int:
    """从 baseline 文件读取允许的违规数量。

    Baseline 文件格式(JSON):
      {
        "description": "R56 P1-5 error protocol baseline",
        "violation_count": N
      }

    文件不存在或格式错误时返回 0(不允许任何违规)。
    """
    if baseline_path is None:
        return 0
    if not baseline_path.exists():
        return 0
    try:
        data = json.loads(baseline_path.read_text(encoding="utf-8", errors="ignore"))
        return int(data.get("violation_count", 0))
    except (json.JSONDecodeError, ValueError, TypeError):
        return 0


# ════════════════════════════════════════════════════════════
# R58 P0-5: Domain-aware baseline 分类
# ════════════════════════════════════════════════════════════

# domain → path 前缀映射(与 baseline.json domains.*.paths 对应)
# 文件路径匹配任一 domain.paths 即归入该 domain;未匹配的归入 "observability"
#
# R64 P1-07: 扩展高风险域覆盖范围 — 将审计报告中列出的全部高风险文件
# (security/destructive/data-integrity/financial)纳入零容忍域。
# 这些文件涉及认证授权、不可逆操作、数据一致性和财务记账,
# 其中的 except pass / return 0/False / 裸异常必须为 0(不允许 allowlist)。
DOMAIN_PATHS: dict[str, list[str]] = {
    "security": [
        "admin/passwords.py", "admin/mfa.py", "admin/sessions.py", "admin/auth.py",
        "services/button_security.py", "services/button_approval_policy.py",
        "services/approval_workflow.py", "services/approval_executor.py",
        "services/command_bus.py", "services/permission.py", "services/rbac.py",
        "services/high_risk_policy.py", "services/content_policy.py",
    ],
    "destructive": [
        "services/data_lifecycle.py", "admin/purge.py",
        "services/db_backup.py", "services/db_restore.py",
        "services/disaster_recovery.py", "services/repair_console.py",
        "services/replication_policy.py",
    ],
    "data-integrity": [
        "services/backup_dr_validate.py", "services/backup_crypto.py",
        "services/backup_engine.py", "services/effect_receipts.py",
        "services/crdb_sync_service.py", "services/crdb_sync_event_wakeup.py",
        "database/redis_queue.py", "database/cache_store.py",
        "database/db_writer.py", "database/dlq_worker.py",
        "database/migrate.py", "database/unit_of_work.py",
        "database/write_router.py",
    ],
    "financial": [
        "services/quota.py", "services/billing.py",
        "services/entitlements.py", "services/quota_ledger.py",
    ],
}

# 零容忍 domain(security/destructive/data-integrity/financial)
ZERO_TOLERANCE_DOMAINS: frozenset[str] = frozenset({
    "security", "destructive", "data-integrity", "financial"
})


# ════════════════════════════════════════════════════════════
# R62 P1-04: 结构化 allowlist 辅助函数
# ════════════════════════════════════════════════════════════

# R63 P1-10: AST 函数行映射缓存(file_path -> {start_line: (end_line, func_name)})
# 避免对同一文件重复 ast.parse(281 个违规可能集中在数十个文件)。
_AST_FUNC_CACHE: dict[str, dict[int, tuple[int, str]]] = {}


def _build_func_line_map(file_path: str) -> dict[int, tuple[int, str]]:
    """R63 P1-10: 解析文件 AST,构建 {start_line: (end_line, func_name)} 映射。

    用于通过行号查找包裹该行的函数名(指纹稳定性关键)。
    方法节点返回 "ClassName.method_name";模块级代码返回 "<module>"(由
    调用方处理)。文件不存在 / 解析失败时返回空 dict。
    """
    p = REPO_ROOT / file_path
    if not p.exists():
        return {}
    try:
        source = p.read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(source, filename=file_path)
    except (OSError, SyntaxError):
        return {}

    line_map: dict[int, tuple[int, str]] = {}

    def _walk(node: ast.AST, parent_class: str = "") -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                _walk(child, parent_class=child.name)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                end = getattr(child, "end_lineno", child.lineno) or child.lineno
                if parent_class:
                    name = f"{parent_class}.{child.name}"
                else:
                    name = child.name
                line_map[child.lineno] = (end, name)
                # 嵌套函数:类上下文不传递(嵌套函数不属于类方法)
                _walk(child, parent_class="")
            else:
                _walk(child, parent_class=parent_class)

    _walk(tree)
    return line_map


def _get_enclosing_function_name(file_path: str, line_no: int) -> str:
    """R63 P1-10: 通过 AST 查找包裹违规行的函数名。

    返回最内层包裹该行的函数/方法名;方法返回 "ClassName.method_name"。
    不在任何函数内时返回 "<module>"。

    这是 R63 P1-10 的核心:行号不再参与指纹,改用 AST 结构(函数名)+
    源行内容,使指纹在行号变化(添加空行 / 上方重排)时保持稳定。
    """
    if file_path not in _AST_FUNC_CACHE:
        _AST_FUNC_CACHE[file_path] = _build_func_line_map(file_path)
    line_map = _AST_FUNC_CACHE[file_path]
    best_name = "<module>"
    best_start = 0
    for start, (end, name) in line_map.items():
        if start <= line_no <= end and start > best_start:
            best_start = start
            best_name = name
    return best_name


def _get_source_line_context(file_path: str, line_no: int) -> str:
    """R62 P1-04: 读取文件指定行的内容(去首尾空白),作为指纹计算的上下文片段。

    文件不存在或行号越界时返回空字符串(不影响指纹计算,只是降低精度)。
    """
    try:
        p = REPO_ROOT / file_path
        if not p.exists():
            return ""
        text = p.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()
        if 1 <= line_no <= len(lines):
            return lines[line_no - 1].strip()
    except OSError:
        pass
    return ""


def _compute_structural_context(file_path: str, line_no: int) -> str:
    """R63 P1-10: 计算 AST 结构上下文(用于指纹稳定性)。

    返回 ``"{enclosing_function}|{source_line_content}"``:
        - enclosing_function: 包裹违规行的函数名(AST 查找,不依赖行号)
        - source_line_content: 违规所在源行内容(去空白)

    此上下文替代旧版仅用源行内容的做法,加入函数名后:
        - 同文件不同函数的同类违规可区分
        - 行号变化(添加空行 / 上方重排)不影响指纹
        - 函数重命名 / 源行内容变化才更新指纹(符合预期)
    """
    func_name = _get_enclosing_function_name(file_path, line_no)
    source_line = _get_source_line_context(file_path, line_no)
    return f"{func_name}|{source_line}"


def _compute_violation_fingerprint(
    file_path: str,
    line_no: int,
    violation_type: str,
    context: str,
) -> str:
    """R63 P1-10: 计算违规指纹(sha256),用于 allowlist 匹配。

    指纹由 ``file_path`` / ``violation_type`` / ``context`` 组成。
    **行号(line_no)不再参与指纹**(R63 P1-10 核心整改):
        - 旧版(R62):``file:line:violation_type:context`` — 行号变化即指纹全变
        - 新版(R63):``file:violation_type:context`` — 行号变化指纹不变

    ``context`` 应为 ``_compute_structural_context()`` 返回的结构上下文
    (函数名 + 源行内容),确保同函数内多个同类违规可区分。

    Args:
        file_path: 相对路径(POSIX)
        line_no: 违规行号(保留参数以兼容现有调用与测试,但不参与指纹)
        violation_type: 违规类型(如 "P1-5 规则1/2")
        context: 结构上下文(func_name|source_line)
    """
    # R63 P1-10: 故意不包含 line_no — 指纹必须对行号变化稳定
    raw = f"{file_path}:{violation_type}:{context[:120]}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _extract_violation_type(detail: str) -> str:
    """R62 P1-04: 从违规详情中提取违规类型标识(如 'P1-5 规则1/2', 'P1-5 规则3')。

    违规详情格式: "P1-5 规则X: <description>"
    提取首个冒号前的部分作为类型标识。
    """
    if ":" in detail:
        return detail.split(":", 1)[0].strip()
    return detail


def _classify_domain(file_path: str) -> str:
    """R58 P0-5: 根据文件路径分类 domain。

    Args:
        file_path: 相对路径(POSIX 格式)

    Returns:
        domain 名称(security/destructive/data-integrity/financial/observability)
    """
    for domain, paths in DOMAIN_PATHS.items():
        for path in paths:
            if file_path == path or file_path.startswith(path):
                return domain
    return "observability"


def _check_domain_baseline(
    findings: list[tuple[str, int, str]],
    baseline_path: Path | None,
    strict: bool = False,
) -> tuple[bool, str, dict]:
    """R58 P0-5 + R62 P1-04: 按域名比对 baseline,支持结构化 allowlist。

    规则:
        1. security/destructive/data-integrity/financial 域必须为 0(零容忍,ratchet)
        2. observability 域使用结构化 allowlist:
           - 每个违规计算指纹(sha256 of file:line:violation_type:context)
           - 指纹匹配 allowlist 条目 → 检查 expiry
           - expiry < today → 失败(allowlist 条目已过期)
           - 未匹配 → real_violations(生产目标 = 0)
           - max_violations 已弃用(仅用于向后兼容,不参与判定)
        3. ratchet: 总违规数 <= baseline violation_count(每个 commit 只减不增)
           --strict 模式下跳过 ratchet 比较,只检查 real_violations == 0

    Args:
        findings: 扫描得到的违规列表 [(file, line, detail), ...]
        baseline_path: baseline JSON 文件路径(None 或不存在时按无 baseline 处理)
        strict: 是否为严格模式(任何未 allowlist 的违规即失败,跳过 ratchet)

    Returns:
        (passed, message, summary) — summary 包含 total/allowlisted/real/expired 计数
    """
    summary: dict = {
        "total_violations": len(findings),
        "allowlisted": 0,
        "real_violations": 0,
        "expired_entries": 0,
        "domain_counts": {},
        "real_violation_list": [],   # 真实违规详情(用于打印)
        "expired_list": [],          # 过期 allowlist 条目详情(用于打印)
    }

    # 分类 findings(按 domain 分组)
    domain_findings: dict[str, list[tuple[str, int, str]]] = {}
    for file_path, line_no, detail in findings:
        domain = _classify_domain(file_path)
        domain_findings.setdefault(domain, []).append((file_path, line_no, detail))
        summary["domain_counts"][domain] = summary["domain_counts"].get(domain, 0) + 1

    today_str = datetime.date.today().isoformat()

    # 无 baseline 处理
    if baseline_path is None or not baseline_path.exists():
        zero_violations = {
            d: c for d, c in summary["domain_counts"].items()
            if d in ZERO_TOLERANCE_DOMAINS and c > 0
        }
        if zero_violations:
            return False, f"零容忍域有违规: {zero_violations}", summary
        # 无 baseline 时 observability 也必须为 0(没有 allowlist 可放行)
        obs_count = summary["domain_counts"].get("observability", 0)
        if obs_count > 0:
            summary["real_violations"] = obs_count
            return False, (
                f"observability: {obs_count} 处违规但无 baseline allowlist "
                f"(需先运行 --generate-baseline 生成 allowlist)"
            ), summary
        return True, "通过(无 baseline,无违规)", summary

    # 加载 baseline JSON
    try:
        data = json.loads(baseline_path.read_text(encoding="utf-8", errors="ignore"))
    except (json.JSONDecodeError, OSError):
        return False, "baseline 文件解析失败", summary

    domains_cfg = data.get("domains", {})
    baseline_violation_count = int(data.get("violation_count", 0))

    # R62 P1-04: ratchet 检查 — 总违规数必须 <= baseline violation_count
    # strict 模式跳过 ratchet(只关心 real_violations == 0)
    if not strict and summary["total_violations"] > baseline_violation_count:
        return False, (
            f"ratchet 失败: 当前违规 {summary['total_violations']} > "
            f"baseline violation_count {baseline_violation_count} "
            f"(每个 commit 只能减少不能增加)"
        ), summary

    # 检查每个 domain
    failed_domains = []
    for domain, domain_findings_list in domain_findings.items():
        count = len(domain_findings_list)
        domain_cfg = domains_cfg.get(domain, {})

        if domain in ZERO_TOLERANCE_DOMAINS:
            # 零容忍域:ratchet 策略(保持 R58 P0-5 行为)
            baseline_violations = int(domain_cfg.get("baseline_violations", 0))
            if count > baseline_violations:
                failed_domains.append(
                    f"{domain}: {count} > baseline {baseline_violations} "
                    f"(target=0, ratchet 模式不允许新增违规)"
                )
            continue

        # observability 域:使用结构化 allowlist(R62 P1-04)
        allowlist = domain_cfg.get("allowlist", [])
        if not isinstance(allowlist, list):
            allowlist = []
        allowlist_by_fp: dict[str, dict] = {}
        for entry in allowlist:
            if isinstance(entry, dict):
                fp = entry.get("fingerprint", "")
                if fp:
                    allowlist_by_fp[fp] = entry

        for file_path, line_no, detail in domain_findings_list:
            violation_type = _extract_violation_type(detail)
            # R63 P1-10: 使用 AST 结构上下文(函数名 + 源行内容),不依赖行号
            context = _compute_structural_context(file_path, line_no)
            fingerprint = _compute_violation_fingerprint(
                file_path, line_no, violation_type, context,
            )

            matched_entry = allowlist_by_fp.get(fingerprint)
            if matched_entry is not None:
                expiry = str(matched_entry.get("expiry", ""))
                if expiry and expiry < today_str:
                    # 已过期:计为真实违规 + 过期条目
                    summary["expired_entries"] += 1
                    summary["real_violations"] += 1
                    summary["expired_list"].append({
                        "file": file_path,
                        "line": line_no,
                        "fingerprint": fingerprint,
                        "expiry": expiry,
                        "ticket": matched_entry.get("ticket", "?"),
                        "owner": matched_entry.get("owner", "?"),
                    })
                    failed_domains.append(
                        f"observability: {file_path}:{line_no} "
                        f"allowlist 条目已过期 (expiry={expiry}, "
                        f"today={today_str}, ticket={matched_entry.get('ticket', '?')}, "
                        f"owner={matched_entry.get('owner', '?')})"
                    )
                else:
                    # 已 allowlist 且未过期
                    summary["allowlisted"] += 1
            else:
                # 未匹配 allowlist:计为真实违规
                summary["real_violations"] += 1
                summary["real_violation_list"].append({
                    "file": file_path,
                    "line": line_no,
                    "fingerprint": fingerprint,
                    "detail": detail,
                })
                if strict:
                    failed_domains.append(
                        f"observability: {file_path}:{line_no} 未在 allowlist 中 "
                        f"(strict 模式:任何未 allowlist 的违规即失败)"
                    )
                else:
                    failed_domains.append(
                        f"observability: {file_path}:{line_no} 未在 allowlist 中 "
                        f"(real_violations 必须 = 0,生产目标;若为新增违规需更新 "
                        f"allowlist,若为存量违规需补全 allowlist 条目)"
                    )

    if failed_domains:
        return False, "; ".join(failed_domains), summary
    return True, "通过(所有 domain 在 baseline 范围内,所有违规已 allowlist)", summary


def _print_allowlist_summary(summary: dict) -> None:
    """R62 P1-04: 打印 allowlist 检查摘要。"""
    print()
    print("R62 P1-04 allowlist 摘要:")
    print(f"  总违规数:        {summary['total_violations']}")
    print(f"  已 allowlist:   {summary['allowlisted']}")
    print(f"  真实违规(未放行): {summary['real_violations']}")
    print(f"  过期 allowlist:  {summary['expired_entries']}")
    if summary["domain_counts"]:
        print("  按 domain 分类:")
        for d, c in sorted(summary["domain_counts"].items()):
            print(f"    {d}: {c}")
    if summary["real_violation_list"]:
        print("  真实违规详情(前 20 条):")
        for v in summary["real_violation_list"][:20]:
            print(
                f"    {v['file']}:{v['line']} "
                f"fp={v['fingerprint'][:12]}... {v['detail'][:60]}"
            )
    if summary["expired_list"]:
        print("  过期 allowlist 条目(前 20 条):")
        for e in summary["expired_list"][:20]:
            print(
                f"    {e['file']}:{e['line']} expiry={e['expiry']} "
                f"ticket={e['ticket']} owner={e['owner']} fp={e['fingerprint'][:12]}..."
            )


def _print_fix_suggestions() -> None:
    """打印修复建议。"""
    print()
    print("修复建议:")
    print("  规则1/2: except Exception: pass → 记录日志或 raise AppError(...)")
    print("  规则3:   except 块中 return 0/False → raise AppError(...) 或 reraise")
    print("  规则4:   模块边界函数返回裸字符串 → 返回 ErrorEnvelope")
    print("  规则5:   raise ValueError/RuntimeError/Exception(\"...\") → "
          "raise AppError(ErrorCodes.XXX)")
    print()
    print("允许的非 AppError 异常: EffectReceiptError / DurabilityError / AppError")


# ════════════════════════════════════════════════════════════
# CLI 入口
# ════════════════════════════════════════════════════════════
# R62 P1-04 默认 baseline 路径(strict 模式未显式指定 --baseline 时使用)
DEFAULT_BASELINE_PATH = REPO_ROOT / "scripts" / "error_protocol_baseline.json"

# R63 P1-10: 默认 allowlist 字段值(30 天窗口,2026-07-18 + 30 天 = 2026-08-18)
# 旧版 R62 默认 2026-09-30 / R62-P1-04 已替换为 R63 分类默认值
DEFAULT_ALLOWLIST_OWNER = "maxiuquan"
DEFAULT_ALLOWLIST_REASON = "R63 P1-10 observability debt - per-module categorization (AST fingerprint)"
DEFAULT_ALLOWLIST_EXPIRY = "2026-08-18"
DEFAULT_ALLOWLIST_TICKET = "R63-P1-10"
DEFAULT_ALLOWLIST_ROOT_CAUSE = "observability debt - except pass / return 0 / bare string / raise with literal (historical)"
DEFAULT_ALLOWLIST_PLAN = "R64 refactor: migrate to AppError(ErrorCodes.XXX) + ErrorEnvelope structured errors"

# R63 P1-10: 按模块分类(owner/root_cause/ticket/plan)
# 文件路径按顶级目录前缀匹配到对应模块分类,allowlist 条目自动填充分类字段。
MODULE_CATEGORIES: dict[str, dict[str, str]] = {
    "admin": {
        "owner": "maxiuquan",
        "root_cause": (
            "admin 模块历史遗留 except Exception: pass / return 0/False / "
            "模块边界函数返回裸字符串 / raise ValueError 携带字符串"
        ),
        "ticket": "R63-P1-10-admin",
        "plan": (
            "R64 重构 admin 错误处理:except 块改用 logger + raise AppError,"
            "模块边界函数返回 ErrorEnvelope,raise 改用 AppError(ErrorCodes.XXX)"
        ),
    },
    "services": {
        "owner": "maxiuquan",
        "root_cause": (
            "services 层多处 except Exception: pass / return 0/False / "
            "raise ValueError/RuntimeError/Exception 携带字符串字面量"
        ),
        "ticket": "R63-P1-10-services",
        "plan": (
            "R64 分批重构 services 错误处理:迁移到 AppError(ErrorCodes.XXX, params={...}),"
            "except 块记录日志后 reraise 或转 AppError"
        ),
    },
    "bots": {
        "owner": "maxiuquan",
        "root_cause": (
            "bots 层 except Exception: pass / 模块边界函数返回裸字符串历史遗留"
        ),
        "ticket": "R63-P1-10-bots",
        "plan": (
            "R64 重构 bots 错误处理:统一 AppError + 用户消息走 i18n(translate/_i18n_t),"
            "except 块不再吞异常"
        ),
    },
    "database": {
        "owner": "maxiuquan",
        "root_cause": (
            "database 层 except Exception: pass / return False 历史遗留"
        ),
        "ticket": "R63-P1-10-database",
        "plan": (
            "R64 重构 database 错误处理:改用 AppError + 结构化错误,"
            "except 块记录日志后 reraise"
        ),
    },
}


def _get_module_category(file_path: str) -> dict[str, str]:
    """R63 P1-10: 根据文件路径顶级目录返回模块分类字段。

    匹配规则:取 file_path 的第一段(以 / 分隔)作为模块名,
    在 MODULE_CATEGORIES 中查找。未匹配时返回默认分类(R63-P1-10)。
    """
    top = file_path.split("/", 1)[0] if "/" in file_path else file_path
    return MODULE_CATEGORIES.get(
        top,
        {
            "owner": DEFAULT_ALLOWLIST_OWNER,
            "root_cause": DEFAULT_ALLOWLIST_ROOT_CAUSE,
            "ticket": DEFAULT_ALLOWLIST_TICKET,
            "plan": DEFAULT_ALLOWLIST_PLAN,
        },
    )


def _build_allowlist_entry(
    file_path: str,
    line_no: int,
    detail: str,
    *,
    owner: str | None = None,
    reason: str = DEFAULT_ALLOWLIST_REASON,
    expiry: str = DEFAULT_ALLOWLIST_EXPIRY,
    ticket: str | None = None,
    root_cause: str | None = None,
    plan: str | None = None,
) -> dict:
    """R63 P1-10: 根据违规信息构建一个结构化 allowlist 条目。

    包含完整字段:
        file / line / fingerprint / owner / reason / expiry / ticket /
        root_cause / plan

    R63 P1-10 变更:
        - 指纹改用 AST 结构上下文(函数名 + 源行内容),不依赖行号
        - 新增 root_cause / plan 分类字段
        - owner / ticket / root_cause / plan 默认按模块自动分类
          (见 _get_module_category);调用方可显式覆盖

    Args:
        file_path: 相对路径(POSIX)
        line_no: 违规行号(仅记录在 entry.line,不参与指纹)
        detail: 违规详情
        owner: 负责人(None → 按模块分类自动填充)
        reason: 原因描述
        expiry: 过期日(ISO 日期 YYYY-MM-DD)
        ticket: 跟踪 ticket(None → 按模块分类自动填充)
        root_cause: 根因描述(None → 按模块分类自动填充)
        plan: 修复计划(None → 按模块分类自动填充)
    """
    # R63 P1-10: 按模块自动分类 owner/ticket/root_cause/plan
    category = _get_module_category(file_path)
    if owner is None:
        owner = category["owner"]
    if ticket is None:
        ticket = category["ticket"]
    if root_cause is None:
        root_cause = category["root_cause"]
    if plan is None:
        plan = category["plan"]

    violation_type = _extract_violation_type(detail)
    # R63 P1-10: AST 结构上下文(函数名 + 源行内容),不依赖行号
    context = _compute_structural_context(file_path, line_no)
    fingerprint = _compute_violation_fingerprint(
        file_path, line_no, violation_type, context,
    )
    return {
        "file": file_path,
        "line": line_no,
        "fingerprint": fingerprint,
        "owner": owner,
        "reason": reason,
        "expiry": expiry,
        "ticket": ticket,
        "root_cause": root_cause,
        "plan": plan,
    }


def main() -> int:
    """脚本入口。返回退出码。"""
    parser = argparse.ArgumentParser(
        description="R56 P1-5 + R62 P1-04: 错误协议 AST 静态扫描门禁",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "严格模式:任何未在 allowlist 中的违规即 exit 1。"
            "未显式指定 --baseline 时使用 scripts/error_protocol_baseline.json"
        ),
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="Baseline 文件路径(读取 allowlist + violation_count,用于 ratchet)",
    )
    parser.add_argument(
        "--generate-baseline",
        type=Path,
        default=None,
        help=(
            "生成/更新 baseline 文件:更新 violation_count,并为当前违规重新生成 "
            "observability.allowlist 条目(ratchet 下降)"
        ),
    )
    args = parser.parse_args()

    findings = collect_findings()
    current_count = len(findings)

    # ── 生成 baseline 模式 ──
    if args.generate_baseline is not None:
        _generate_baseline_file(args.generate_baseline, findings, current_count)
        return 0

    # 确定有效 baseline 路径
    # R63 P1-10: 默认使用 scripts/error_protocol_baseline.json(若存在),
    # 使 `python scripts/check_error_protocol.py`(无 flag)也能走 allowlist 路径。
    # --strict 模式下未显式指定 --baseline 时,同样使用默认 baseline 路径。
    # (R62 P1-04: strict 模式需要 allowlist 才能放行存量违规)
    effective_baseline = args.baseline
    if effective_baseline is None and DEFAULT_BASELINE_PATH.exists():
        effective_baseline = DEFAULT_BASELINE_PATH

    # 运行 domain 检查(支持 allowlist)
    domain_passed, domain_msg, summary = _check_domain_baseline(
        findings, effective_baseline, strict=args.strict,
    )

    if not domain_passed:
        mode_label = "strict" if args.strict else "baseline"
        print(
            f"[FAIL] R62 P1-04 ({mode_label} 模式) 检查失败: {domain_msg}"
        )
        # 输出按 domain 分类的违规详情
        domain_findings: dict[str, list] = {}
        for file, line, detail in findings:
            d = _classify_domain(file)
            domain_findings.setdefault(d, []).append((file, line, detail))
        for d, d_findings in domain_findings.items():
            print(f"[{d}] ({len(d_findings)} violations):")
            for file, line, detail in d_findings[:30]:
                print(f"  {file}:{line}: {detail}")
            if len(d_findings) > 30:
                print(f"  ... 还有 {len(d_findings) - 30} 条")
        _print_allowlist_summary(summary)
        _print_fix_suggestions()
        print()
        print("R62 P1-04: observability 域每个违规必须在 allowlist 中(未过期)")
        print("R58 P0-5: security/destructive/data-integrity/financial 域必须为 0")
        print("修复后才能通过 CI 门禁")
        return 1

    # 通过
    if args.strict:
        print(
            f"[OK] R62 P1-04 strict 模式通过: real_violations=0 "
            f"(总 {current_count} 处全部 allowlist)"
        )
    else:
        baseline_count = _load_baseline_count(effective_baseline)
        print(
            f"[OK] R62 P1-04 通过: 当前违规 {current_count} 处 "
            f"<= baseline {baseline_count} 处 (real_violations=0)"
        )
    print(f"  domain 检查: {domain_msg}")
    _print_allowlist_summary(summary)
    return 0


def _generate_baseline_file(
    baseline_path: Path,
    findings: list[tuple[str, int, str]],
    current_count: int,
) -> None:
    """R62 P1-04: 生成/更新 baseline 文件,包含结构化 allowlist。

    若 baseline 文件已存在,保留其 domain 配置(描述、paths 等),
    只更新 violation_count 和 observability.allowlist。
    """
    # 尝试读取已有 baseline(保留 domain 配置)
    existing_data: dict = {}
    if baseline_path.exists():
        try:
            existing_data = json.loads(
                baseline_path.read_text(encoding="utf-8", errors="ignore")
            )
        except (json.JSONDecodeError, OSError):
            existing_data = {}

    # 保留已有 domains 配置,或使用默认结构
    domains_cfg = existing_data.get("domains", {})
    if not domains_cfg:
        domains_cfg = {
            "security": {
                "description": "认证、授权、MFA、密码、token、签名验证相关",
                "max_violations": 0,
                "baseline_violations": 0,
                "paths": DOMAIN_PATHS["security"],
            },
            "destructive": {
                "description": "删除、清除、purge、reset、备份恢复等不可逆操作",
                "max_violations": 0,
                "baseline_violations": 0,
                "paths": DOMAIN_PATHS["destructive"],
            },
            "data-integrity": {
                "description": "备份、恢复、事务、outbox、缓存、数据一致性",
                "max_violations": 0,
                "baseline_violations": 0,
                "paths": DOMAIN_PATHS["data-integrity"],
            },
            "financial": {
                "description": "配额、计费、积分、套餐等财务相关",
                "max_violations": 0,
                "baseline_violations": 0,
                "paths": DOMAIN_PATHS["financial"],
            },
            "observability": {
                "description": "日志、metric、trace 等可观测性 best-effort",
                "max_violations": 0,
                "allowlist_required": True,
                "allowlist": [],
            },
        }

    # 为当前 observability 违规重新生成 allowlist
    # (零容忍域不应有违规,若有也加入 observability allowlist 不合理 — 直接报错)
    obs_findings = [
        (f, l, d) for f, l, d in findings
        if _classify_domain(f) == "observability"
    ]
    new_allowlist = [
        _build_allowlist_entry(f, l, d) for f, l, d in obs_findings
    ]

    obs_cfg = domains_cfg.get("observability", {})
    obs_cfg["allowlist"] = new_allowlist
    obs_cfg["allowlist_required"] = True
    # max_violations=0 表示目标(R62 P1-04 弃用 max_violations,仅保留用于向后兼容)
    obs_cfg["max_violations"] = 0
    domains_cfg["observability"] = obs_cfg

    prev_count = int(existing_data.get("violation_count", current_count))

    data = {
        "description": (
            "R63 P1-10 error protocol baseline (AST 结构指纹 + 按模块分类 allowlist + "
            "ratchet, observability 目标 real_violations=0)"
        ),
        "note": (
            "R63 P1-10: observability 域使用结构化 allowlist(file/line/fingerprint/"
            "owner/reason/expiry/ticket/root_cause/plan)。每个违规必须匹配 allowlist "
            "条目且未过期。指纹基于 AST 结构(file + violation_type + "
            "enclosing_function + source_line),不依赖行号,行号变化指纹不变。"
            "allowlist 条目按模块自动分类 owner/root_cause/ticket/plan。"
            "max_violations 已弃用(仅保留用于向后兼容)。"
            "violation_count 用于 ratchet:每个 commit 只能减少不能增加。"
            "修复存量违规后重新生成以下降 violation_count 并精简 allowlist。"
        ),
        "version": "R63-P1-10",
        "ratchet_strategy": (
            "structured-allowlist: real_violations must == 0 (every violation "
            "must be in allowlist with valid expiry); "
            "ratchet: total_violations <= violation_count; "
            "R63 P1-10: fingerprint = sha256(file:violation_type:func_name|source_line) "
            "(line-number-independent)"
        ),
        "domains": domains_cfg,
        "total_max_violations": 0,
        "violation_count": current_count,
        "previous_violation_count": prev_count,
    }

    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"✓ Baseline 已生成: {baseline_path}")
    print(f"  violation_count: {current_count} 处 (ratchet 下降)")
    print(f"  observability.allowlist: {len(new_allowlist)} 条结构化条目")
    if current_count < prev_count:
        print(f"  ✓ ratchet 下降: {prev_count} → {current_count} (减少 {prev_count - current_count} 处)")
    elif current_count > prev_count:
        print(f"  ⚠ 警告: 违规增加 {prev_count} → {current_count} (新增 {current_count - prev_count} 处)")


if __name__ == "__main__":
    sys.exit(main())
