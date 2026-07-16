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

CI 调用方式:
    python scripts/check_error_protocol.py --strict
    python scripts/check_error_protocol.py --baseline scripts/error_protocol_baseline.json

退出码:
    0 — 通过(违规数 <= baseline,或 --strict 且无违规)
    1 — 失败(违规数 > baseline,或 --strict 且有违规)
"""
from __future__ import annotations

import argparse
import ast
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
def main() -> int:
    """脚本入口。返回退出码。"""
    parser = argparse.ArgumentParser(
        description="R56 P1-5: 错误协议 AST 静态扫描门禁",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="严格模式:发现任何违规即 exit 1(忽略 baseline)",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="Baseline 文件路径(读取允许的违规数量,用于 ratchet)",
    )
    parser.add_argument(
        "--generate-baseline",
        type=Path,
        default=None,
        help="生成/更新 baseline 文件(记录当前违规数,用于 ratchet 下降)",
    )
    args = parser.parse_args()

    findings = collect_findings()
    current_count = len(findings)

    # ── 生成 baseline 模式 ──
    if args.generate_baseline is not None:
        data = {
            "description": "R56 P1-5 error protocol baseline (ratchet 下降,只减不增)",
            "note": "fail-open/except pass/return 0|False 是 pre-existing 历史债务。修复后重新生成以下降 violation_count。",
            "violation_count": current_count,
        }
        args.generate_baseline.parent.mkdir(parents=True, exist_ok=True)
        args.generate_baseline.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"✓ Baseline 已生成: {args.generate_baseline}")
        print(f"  violation_count: {current_count} 处 (ratchet 下降)")
        return 0

    # ── 严格模式:任何违规都失败 ──
    if args.strict:
        if findings:
            print(f"[FAIL] R56 P1-5 严格模式: 发现 {current_count} 处违规:")
            for file, line, detail in findings:
                print(f"  {file}:{line}: {detail}")
            _print_fix_suggestions()
            return 1
        print("[OK] R56 P1-5 严格模式通过: 未发现违规")
        return 0

    # ── 非 strict 模式:与 baseline 比对 ──
    baseline_count = _load_baseline_count(args.baseline)

    if current_count <= baseline_count:
        # 通过:违规数在 baseline 范围内
        print(
            f"[OK] R56 P1-5 通过: 当前违规 {current_count} 处 "
            f"<= baseline {baseline_count} 处"
        )
        if findings:
            print("已有违规(在 baseline 范围内,不阻断):")
            for file, line, detail in findings:
                print(f"  {file}:{line}: {detail}")
        return 0

    # 失败:违规数超过 baseline
    print(
        f"[FAIL] R56 P1-5 失败: 当前违规 {current_count} 处 "
        f"> baseline {baseline_count} 处 (新增 {current_count - baseline_count} 处)"
    )
    print()
    print("所有违规:")
    for file, line, detail in findings:
        print(f"  {file}:{line}: {detail}")
    _print_fix_suggestions()
    print()
    print(
        f"修复违规后更新 baseline: 将 violation_count 降为 {current_count}"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
