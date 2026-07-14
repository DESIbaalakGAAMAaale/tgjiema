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

        # 检查反模式
        for pattern, ptype in [
            (RAISE_BARE_STRING, "raise_bare_string"),
            (RETURN_ERROR_DICT, "return_error_dict"),
            (RAISE_APP_ERROR_STRING, "raise_app_error_string"),
        ]:
            for match in pattern.finditer(line):
                findings.append((idx, ptype, line.strip()[:120]))

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


def main() -> int:
    """脚本入口。返回退出码(0=成功,1=失败)。"""
    generate_baseline = "--generate-baseline" in sys.argv
    strict = "--strict" in sys.argv

    findings = collect_findings(REPO_ROOT)

    if generate_baseline:
        print("⚠️  警告: --generate-baseline 仅用于初始基线生成,不应在 PR 中使用。")
        print("   PR 中新增违规应修复后接入 ErrorCodes,而非纳入基线。")
        print("   CI 中应使用 --strict 模式确保违规数为 0。")
        print()
        violations = set()
        for file, line, ptype, content in findings:
            violations.add(_violation_key(file, content))
        _save_baseline(violations)
        print(f"✓ Baseline 已生成: {BASELINE_FILE.name}")
        print(f"  已知违规: {len(violations)} 处")
        return 0

    if strict:
        # 严格模式:任何违规都失败(忽略 baseline)
        if findings:
            print(f"❌ R47 P1-c 严格模式: 发现 {len(findings)} 处裸字符串错误:")
            for file, line, ptype, content in findings[:50]:
                print(f"  {file}:{line} [{ptype}]: {content}")
            if len(findings) > 50:
                print(f"  ... 还有 {len(findings) - 50} 处")
            print("\n请使用 AppError(ErrorCodes.XXX, params={...}) 替代裸字符串:")
            print("  - raise ValueError('xxx') → raise AppError(ErrorCodes.XXX, params={...})")
            print("  - return {'error': 'xxx'} → return ErrorRegistry.create_envelope(...).to_dict()")
            return 1
        print("✓ R47 P1-c 严格模式通过: 未发现裸字符串错误")
        return 0

    # 默认宽松模式:与 baseline 比对,仅警告新增违规
    baseline = _load_baseline()
    new_findings = []
    for file, line, ptype, content in findings:
        key = _violation_key(file, content)
        if key not in baseline:
            new_findings.append((file, line, ptype, content))

    if new_findings:
        print(
            f"⚠️  R47 P1-c 宽松模式: 发现 {len(new_findings)} 处"
            f"**新增**裸字符串错误(不在 baseline 中):"
        )
        for file, line, ptype, content in new_findings[:50]:
            print(f"  {file}:{line} [{ptype}]: {content}")
        if len(new_findings) > 50:
            print(f"  ... 还有 {len(new_findings) - 50} 处")
        print(
            "\n请修复新增违规并接入 ErrorCodes,而非扩大基线。"
            "\n修复后运行 --generate-baseline 更新基线。"
            "\nCI 中使用 --strict 模式可强制 0 违规。"
        )
        # 默认宽松模式 exit 0(仅警告,不阻断)
        return 0

    print(
        f"✓ R47 P1-c 宽松模式通过: 未发现新增裸字符串错误"
        f"(baseline: {len(baseline)} 处已知)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
