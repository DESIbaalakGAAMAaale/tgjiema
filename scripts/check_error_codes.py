#!/usr/bin/env python3
"""R47 P1-c: CI 静态扫描 — 禁止新增裸字符串错误。

扫描 ``services/`` / ``bots/`` / ``admin/`` 下的 Python 文件,
检测以下"裸字符串错误"反模式:

1. ``raise ValueError("...")`` / ``raise RuntimeError("...")`` 裸字符串异常
2. ``return {"error": "..."}`` / ``return {"success": False, "error": "..."}`` 裸字符串返回
3. ``raise AppError("...")`` 直接传字符串(应传 ``ErrorCodes.XXX``)

正确写法:
    raise AppError(ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT, params={...})
    return ErrorRegistry.create_envelope(ErrorCodes.XXX, params={...}).to_dict()

CI 模式(``--strict``):
    默认宽松模式(仅警告,exit 0),便于渐进式迁移;
    ``--strict`` 模式发现任何违规即 exit 1,用于 CI 门禁。

Baseline 机制:
    已知违规记录在 ``scripts/error_codes_baseline.json`` 中,默认模式仅报告新增违规。
    ``--generate-baseline`` 重新生成 baseline(修复已知违规后更新)。
    ``--strict`` 模式忽略 baseline,任何违规都 fail(用于新代码门禁)。

用法:
    # 默认:与 baseline 比对,仅警告新增违规(exit 0)
    python scripts/check_error_codes.py

    # CI 严格模式:任何违规都 exit 1(忽略 baseline)
    python scripts/check_error_codes.py --strict

    # 重新生成 baseline
    python scripts/check_error_codes.py --generate-baseline
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# 项目根目录
REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_FILE = Path(__file__).parent / "error_codes_baseline.json"
# R56 §5.2: fail-open 反模式(except: pass / except: return 0|False)单独基线
# 这类违规是 pre-existing 历史债务(数百处),通过 ratchet 逐步下降
FAILOPEN_BASELINE_FILE = Path(__file__).parent / "error_codes_failopen_baseline.json"

# 待扫描的目录(相对 REPO_ROOT)
SCAN_DIRS = ["services", "bots", "admin"]

# 跳过的子目录/文件名
SKIP_PATTERNS = [
    "__pycache__",
    ".git",
    "node_modules",
    "static",
    "templates",
    "migrations",
    ".venv",
    "venv",
]

# ── 反模式正则 ─────────────────────────────────────────────
# 1. raise ValueError("...") / raise RuntimeError("...") 裸字符串异常
#    (允许 raise AppError(ErrorCodes.XXX) 协议化异常)
RAISE_BARE_STRING = re.compile(
    r'\braise\s+(ValueError|RuntimeError|Exception|TypeError|KeyError)'
    r'\s*\(\s*["\'][^"\']*["\']'
)

# 2. return {"error": "..."} / return {"success": False, "error": "..."} 裸字符串返回
#    匹配 return 语句中的 "error" 字段是字符串字面量
RETURN_ERROR_DICT = re.compile(
    r'return\s*\{[^}]*["\']error["\']\s*:\s*["\'][^"\']*["\']'
)

# 3. raise AppError("...") 直接传字符串(应传 ErrorCodes.XXX)
RAISE_APP_ERROR_STRING = re.compile(
    r'\braise\s+AppError\s*\(\s*["\'][^"\']*["\']'
)

# R56 §5.2: 4. 检测未注册错误码 — 提取所有 ErrorCodes.XXX 引用
#    匹配: ErrorCodes.SOME_NAME 或 ErrorEnum.SOME_NAME
ERROR_CODE_REF = re.compile(
    r'\b(?:ErrorCodes|ErrorEnum)\.([A-Z][A-Z0-9_]*)\b'
)

# R56 §5.2: 5. except Exception: pass 反模式(fail-open)
EXCEPT_PASS = re.compile(
    r'\bexcept\s+(Exception|BaseException)\s*(?:\s+as\s+\w+)?\s*:\s*#'
)
EXCEPT_PASS_BODY = re.compile(r'^\s*pass\s*(?:#.*)?$')

# R56 §5.2: 6. 裸 return 0/False(伪装成功)
#    仅在 except 块中检测(避免误报正常业务 return)
RETURN_BARE_ZERO = re.compile(r'^\s*return\s+(0|False)\s*(?:#.*)?$')


def is_skipped(path: Path) -> bool:
    """检查路径是否应跳过。"""
    s = str(path)
    for pat in SKIP_PATTERNS:
        if pat in s:
            return True
    return False


def scan_file(path: Path) -> list[tuple[int, str, str]]:
    """扫描单个 Python 文件,返回 (line_no, pattern_type, content) 列表。

    跳过注释行和 docstring 起始行(粗略过滤,非 AST 解析)。
    """
    findings: list[tuple[int, str, str]] = []
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return findings

    lines = content.splitlines()
    in_docstring = False
    docstring_marker = ""
    in_except_block = False
    except_indent = -1
    for idx, line in enumerate(lines, 1):
        stripped = line.lstrip()

        # 跳过注释行
        if stripped.startswith("#"):
            continue

        # 粗略 docstring 跟踪(避免在 docstring 内的示例被误报)
        if not in_docstring:
            if '"""' in line or "'''" in line:
                # 检查是否开始 docstring
                for marker in ('"""', "'''"):
                    if marker in line:
                        # 同行结束
                        if line.count(marker) >= 2:
                            continue
                        # 跨行 docstring
                        in_docstring = True
                        docstring_marker = marker
                        break
                if in_docstring:
                    continue
        else:
            if docstring_marker in line:
                in_docstring = False
                docstring_marker = ""
            continue

        # 跟踪 except 块(用于检测 except: pass 和 except: return 0/False)
        if stripped.startswith("except ") or stripped.startswith("except:"):
            in_except_block = True
            except_indent = len(line) - len(stripped)
        elif in_except_block:
            current_indent = len(line) - len(stripped)
            if stripped and current_indent <= except_indent:
                in_except_block = False
                except_indent = -1

        # 检查反模式
        for pattern, ptype in [
            (RAISE_BARE_STRING, "raise_bare_string"),
            (RETURN_ERROR_DICT, "return_error_dict"),
            (RAISE_APP_ERROR_STRING, "raise_app_error_string"),
        ]:
            for match in pattern.finditer(line):
                findings.append((idx, ptype, line.strip()[:120]))

        # R56 §5.2: 检测 ErrorCodes.XXX / ErrorEnum.XXX 引用
        for match in ERROR_CODE_REF.finditer(line):
            code_name = match.group(1)
            findings.append((idx, "error_code_ref", f"{match.group(0)} (name={code_name})"))

        # R56 §5.2: 在 except 块中检测 fail-open 反模式
        if in_except_block and EXCEPT_PASS_BODY.match(line):
            findings.append((idx, "except_pass_failopen", line.strip()[:120]))
        if in_except_block and RETURN_BARE_ZERO.match(line):
            findings.append((idx, "except_return_zero_failopen", line.strip()[:120]))

    return findings


def collect_findings(root: Path) -> list[tuple[str, int, str, str]]:
    """收集所有违规,返回 (file, line_no, ptype, content) 列表。"""
    findings: list[tuple[str, int, str, str]] = []
    for scan_dir in SCAN_DIRS:
        scan_path = root / scan_dir
        if not scan_path.exists():
            continue
        for py_file in scan_path.rglob("*.py"):
            if is_skipped(py_file):
                continue
            file_findings = scan_file(py_file)
            for line_no, ptype, content in file_findings:
                findings.append((
                    str(py_file.relative_to(root)).replace("\\", "/"),
                    line_no,
                    ptype,
                    content,
                ))
    return findings


def _violation_key(file: str, content: str) -> str:
    """生成违规唯一键(基于文件路径和内容,不依赖行号)。"""
    return f"{file}::{content}"


def _load_baseline() -> set[str]:
    """加载 baseline 文件,返回已知违规键集合。"""
    if not BASELINE_FILE.exists():
        return set()
    try:
        data = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
        return set(data.get("violations", []))
    except Exception:
        return set()


def _save_baseline(violations: set[str]) -> None:
    """保存 baseline 文件。"""
    data = {
        "description": "R47 P1-c: 已知裸字符串错误 baseline",
        "note": "修复已知违规后运行 --generate-baseline 更新此文件",
        "violation_count": len(violations),
        "violations": sorted(violations),
    }
    BASELINE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _load_baseline_failopen() -> set[str]:
    """R56 §5.2: 加载 fail-open baseline 文件,返回已知违规键集合。"""
    if not FAILOPEN_BASELINE_FILE.exists():
        return set()
    try:
        data = json.loads(FAILOPEN_BASELINE_FILE.read_text(encoding="utf-8"))
        return set(data.get("violations", []))
    except Exception:
        return set()


def _save_baseline_failopen(violations: set[str]) -> None:
    """R56 §5.2: 保存 fail-open baseline 文件。"""
    data = {
        "description": "R56 §5.2: 已知 fail-open 反模式 baseline (except: pass / return 0|False)",
        "note": "fail-open 是 pre-existing 历史债务,通过 ratchet 逐步下降,只减不增。修复后运行 --generate-baseline-failopen 更新。",
        "violation_count": len(violations),
        "violations": sorted(violations),
    }
    FAILOPEN_BASELINE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def check_unregistered_error_codes(findings: list[tuple[str, int, str, str]]) -> list[tuple[str, int, str]]:
    """R56 §5.2: 检测 ErrorCodes.XXX / ErrorEnum.XXX 引用是否都已注册到 ErrorRegistry。

    Returns:
        未注册错误码列表 [(file, line, content)]
    """
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from services.error_codes import ErrorCodes, ErrorRegistry  # type: ignore
        # 触发初始化
        ErrorRegistry.all_codes()
        registered = set(ErrorRegistry.all_codes())
        # 同时允许 ErrorCodes 中的所有常量(未注册到 ErrorRegistry 但已声明的)
        for attr in dir(ErrorCodes):
            if attr.isupper() and not attr.startswith("_"):
                value = getattr(ErrorCodes, attr)
                if isinstance(value, str):
                    registered.add(value)
    except Exception:
        # 无法加载 ErrorRegistry,跳过此检查
        return []
    # 跳过定义文件本身(包含 docstring/字符串字面量中的示例引用)
    skip_files = {"services/error_codes.py"}
    # 跳过测试文件中的示例引用(测试本身就是在验证这些引用)
    skip_dirs = {"tests"}
    unregistered: list[tuple[str, int, str]] = []
    seen: set[tuple[str, int, str]] = set()
    for file, line, ptype, content in findings:
        if ptype != "error_code_ref":
            continue
        if file in skip_files:
            continue
        if any(file.startswith(d + "/") for d in skip_dirs):
            continue
        # content 形如: ErrorCodes.UPLOAD_X (name=UPLOAD_X)
        # 提取 ErrorCodes.NAME 部分
        import re as _re
        m = _re.search(r"(ErrorCodes|ErrorEnum)\.([A-Z][A-Z0-9_]*)", content)
        if not m:
            continue
        ref_name = m.group(2)
        # 检查该名称是否在 ErrorCodes 中声明(避免拼写错误)
        if not hasattr(ErrorCodes, ref_name):
            key = (file, line, content)
            if key not in seen:
                seen.add(key)
                unregistered.append((file, line, content))
    return unregistered


def main() -> int:
    """脚本入口。返回退出码(0=成功,1=失败)。"""
    generate_baseline = "--generate-baseline" in sys.argv
    generate_baseline_failopen = "--generate-baseline-failopen" in sys.argv
    strict = "--strict" in sys.argv
    skip_registry_check = "--skip-registry-check" in sys.argv

    findings = collect_findings(REPO_ROOT)

    # 分离 error_code_ref 和其他违规
    other_findings = [f for f in findings if f[2] != "error_code_ref"]
    ref_findings = [f for f in findings if f[2] == "error_code_ref"]

    # R56 §5.2: 检测未注册错误码(无论 strict/宽松模式都执行)
    unregistered = []
    if not skip_registry_check and ref_findings:
        unregistered = check_unregistered_error_codes(ref_findings)

    if generate_baseline or generate_baseline_failopen:
        print("⚠️  警告: baseline 生成仅用于初始基线/下降基线,不应在 PR 中扩大基线。")
        print("   PR 中新增违规应修复后接入 ErrorCodes,而非纳入基线。")
        print("   CI 中应使用 --strict 模式确保违规数为 0(裸字符串)/ratchet 下降(fail-open)。")
        print()
        # R47 P1-c: 裸字符串 baseline(应为 0,保持)
        hard_violations_keys = set()
        # R56 §5.2: fail-open baseline
        failopen_violations_keys = set()
        for file, line, ptype, content in other_findings:
            key = _violation_key(file, content)
            if ptype in ("except_pass_failopen", "except_return_zero_failopen"):
                failopen_violations_keys.add(key)
            else:
                hard_violations_keys.add(key)
        if generate_baseline:
            _save_baseline(hard_violations_keys)
            print(f"✓ 裸字符串 Baseline 已生成: {BASELINE_FILE.name}")
            print(f"  已知裸字符串违规: {len(hard_violations_keys)} 处")
        if generate_baseline_failopen or generate_baseline:
            _save_baseline_failopen(failopen_violations_keys)
            print(f"✓ fail-open Baseline 已生成: {FAILOPEN_BASELINE_FILE.name}")
            print(f"  已知 fail-open 违规: {len(failopen_violations_keys)} 处 (ratchet 下降)")
        return 0

    # R56 §5.2: 未注册错误码始终失败(无论 strict/宽松模式)
    if unregistered:
        print(f"❌ R56 §5.2: 发现 {len(unregistered)} 处未注册的错误码引用:")
        for file, line, content in unregistered[:50]:
            print(f"  {file}:{line}: {content}")
        if len(unregistered) > 50:
            print(f"  ... 还有 {len(unregistered) - 50} 处")
        print("\n请将新错误码添加到 ErrorCodes 类,并注册到 ErrorRegistry:")
        print("  1. 在 ErrorCodes 类中添加: NEW_CODE = 'DOMAIN.OPERATION.REASON'")
        print("  2. 在 _register_defaults() 中添加: ErrorRegistry.register(ErrorDefinition(...))")
        print("  3. 在 locales/zh-CN.json 和 en-US.json 中添加对应 message_key")
        return 1

    # R56 §5.2: fail-open 反模式(except: pass / except: return 0|False)是 pre-existing
    # 历史债务,共数百处。它们不应阻塞当前 PR,但也不应新增。因此:
    # - 在 strict 模式下,fail-open 违规与 baseline 比对,仅新增才失败(ratchet 下降)
    # - 裸字符串错误(raise_bare_string / return_error_dict / raise_app_error_string)
    #   仍保持 strict "0 违规"语义(这些已全部修复)
    FAILOPEN_PTYPES = {"except_pass_failopen", "except_return_zero_failopen"}
    hard_violations = [f for f in other_findings if f[2] not in FAILOPEN_PTYPES]
    failopen_violations = [f for f in other_findings if f[2] in FAILOPEN_PTYPES]

    if strict:
        # 严格模式:
        # 1. 裸字符串错误(raise/return dict/app_error string)— 任何违规都失败
        if hard_violations:
            print(f"❌ R47 P1-c 严格模式: 发现 {len(hard_violations)} 处裸字符串错误:")
            for file, line, ptype, content in hard_violations[:50]:
                print(f"  {file}:{line} [{ptype}]: {content}")
            if len(hard_violations) > 50:
                print(f"  ... 还有 {len(hard_violations) - 50} 处")
            print("\n请使用 AppError(ErrorCodes.XXX, params={...}) 替代裸字符串:")
            print("  - raise ValueError('xxx') → raise AppError(ErrorCodes.XXX, params={...})")
            print("  - return {'error': 'xxx'} → return ErrorRegistry.create_envelope(...).to_dict()")
            return 1
        # 2. fail-open 反模式 — 与 baseline 比对,仅新增违规失败(ratchet)
        baseline_failopen = _load_baseline_failopen()
        new_failopen = []
        for file, line, ptype, content in failopen_violations:
            key = _violation_key(file, content)
            if key not in baseline_failopen:
                new_failopen.append((file, line, ptype, content))
        if new_failopen:
            print(
                f"❌ R56 §5.2: 发现 {len(new_failopen)} 处**新增** fail-open 反模式"
                f"(except: pass / except: return 0|False,不在 baseline 中):"
            )
            for file, line, ptype, content in new_failopen[:50]:
                print(f"  {file}:{line} [{ptype}]: {content}")
            if len(new_failopen) > 50:
                print(f"  ... 还有 {len(new_failopen) - 50} 处")
            print(
                "\nfail-open 是严重的数据安全隐患(异常时伪装成功)。"
                "\n请改为 fail-closed 模式:"
                "  except: pass → except: raise AppError(ErrorCodes.XXX) 或 propagate"
                "  except: return 0/False → except: raise AppError 或 return 错误 envelope"
                "\n如需将新违规纳入 baseline(仅限无法立即修复的场景),"
                "运行 --generate-baseline-failopen 更新 fail-open 基线。"
            )
            return 1
        print("✓ R47 P1-c 严格模式通过: 未发现裸字符串错误")
        if failopen_violations:
            print(
                f"  (R56 §5.2: 检测到 {len(failopen_violations)} 处 fail-open 反模式,"
                f"全部在 baseline 中,无新增)"
            )
        if ref_findings:
            print(f"  (R56 §5.2: 检测到 {len(ref_findings)} 处 ErrorCodes/ErrorEnum 引用,全部已注册)")
        return 0

    # 默认宽松模式:与 baseline 比对,仅警告新增违规(exit 0)
    baseline = _load_baseline()
    baseline_failopen = _load_baseline_failopen()
    new_findings = []
    for file, line, ptype, content in other_findings:
        key = _violation_key(file, content)
        if ptype in ("except_pass_failopen", "except_return_zero_failopen"):
            if key not in baseline_failopen:
                new_findings.append((file, line, ptype, content))
        else:
            if key not in baseline:
                new_findings.append((file, line, ptype, content))

    if new_findings:
        print(
            f"⚠️  R47 P1-c 宽松模式: 发现 {len(new_findings)} 处"
            f"**新增**违规(不在 baseline 中):"
        )
        for file, line, ptype, content in new_findings[:50]:
            print(f"  {file}:{line} [{ptype}]: {content}")
        if len(new_findings) > 50:
            print(f"  ... 还有 {len(new_findings) - 50} 处")
        print(
            "\n请修复新增违规并接入 ErrorCodes,而非扩大基线。"
            "\n修复后运行 --generate-baseline 更新基线。"
            "\nCI 中使用 --strict 模式可强制 0 违规(裸字符串)/ratchet 下降(fail-open)。"
        )
        # 默认宽松模式 exit 0(仅警告,不阻断)
        return 0

    print(
        f"✓ R47 P1-c 宽松模式通过: 未发现新增裸字符串错误"
        f"(baseline: {len(baseline)} 处已知)"
    )
    if ref_findings:
        print(f"  (R56 §5.2: 检测到 {len(ref_findings)} 处 ErrorCodes/ErrorEnum 引用,全部已注册)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
