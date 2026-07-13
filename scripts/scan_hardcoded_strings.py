#!/usr/bin/env python3
"""R44: 扫描硬编码用户面向中文字符串,确保已接入 i18n。

R44 6.3: CI 扫描 Python/HTML 中**新增的**硬编码用户文案。
使用 baseline 机制:已知违规记录在 baseline 文件中,仅新增违规导致 CI 失败。

用法:
    # 扫描(默认,与 baseline 比对)
    python scripts/scan_hardcoded_strings.py

    # 重新生成 baseline(修复已知违规后更新)
    python scripts/scan_hardcoded_strings.py --generate-baseline
"""
import json
import re
import sys
from pathlib import Path

# 中文 Unicode 范围
CJK_PATTERN = re.compile(r'[\u4e00-\u9fff]')

# 用户面向字符串模式(reply_text/answer/HTTPException/detail/f-string 等)
USER_FACING_PATTERNS = [
    # Python: reply_text("中文") / reply_text(f"中文") / answer("中文") 等
    re.compile(r'(?:reply_text|answer|reply_photo|reply_document|send_message)\s*\(\s*f?["\']([^"\']*)["\']'),
    re.compile(r'detail\s*=\s*f?["\']([^"\']*)["\']'),
    re.compile(r'HTTPException\s*\([^)]*detail\s*=\s*f?["\']([^"\']*)["\']'),
    # f-string 中的中文(仅匹配 f"..." 包含中文的)
    re.compile(r'f["\']([^"\']*[\u4e00-\u9fff][^"\']*)["\']'),
]

# 白名单目录/文件
SKIP_PATTERNS = [
    'tests/',
    'docs/',
    'scripts/',
    '__pycache__',
    '.git/',
    'node_modules/',
]

BASELINE_FILE = Path(__file__).parent / 'hardcoded_strings_baseline.json'


def is_skipped(path: Path) -> bool:
    for pattern in SKIP_PATTERNS:
        if pattern in str(path):
            return True
    return False


def _strip_string_literals(line: str) -> str:
    """移除字符串字面量,以便准确计算括号深度。

    避免字符串中的括号影响多行调用跟踪。
    """
    return re.sub(r'f?["\'][^"\']*["\']', '""', line)


def scan_python_file(path: Path) -> list[tuple[int, str, str]]:
    """扫描 Python 文件,返回 (line_no, pattern_type, content) 列表。

    改进:正确跳过多行 logger./logging./print() 调用中的 f-string。
    """
    findings = []
    try:
        content = path.read_text(encoding='utf-8')
    except Exception:
        return findings

    lines = content.splitlines()
    # 多行 logger 调用跟踪
    in_logger_call = False
    paren_depth = 0

    for idx, line in enumerate(lines, 1):
        stripped = line.lstrip()
        # 跳过注释行
        if stripped.startswith('#') or stripped.startswith('"""') or stripped.startswith("'''"):
            continue

        # 用于括号计数的行(移除字符串字面量)
        code_only = _strip_string_literals(line)

        if not in_logger_call:
            # 检查是否是 logger 调用行
            if 'logger.' in line or 'logging.' in line or 'print(' in line:
                opens = code_only.count('(')
                closes = code_only.count(')')
                if opens > closes:
                    # 多行 logger 调用开始
                    in_logger_call = True
                    paren_depth = opens - closes
                # 无论单行还是多行,跳过此行
                continue
        else:
            # 在多行 logger 调用内部
            paren_depth += code_only.count('(') - code_only.count(')')
            if paren_depth <= 0:
                in_logger_call = False
            continue

        # 正常模式匹配
        for pattern in USER_FACING_PATTERNS:
            for match in pattern.finditer(line):
                matched_text = match.group(1)
                if CJK_PATTERN.search(matched_text):
                    findings.append((idx, pattern.pattern[:50], matched_text[:80]))
    return findings


def scan_html_file(path: Path) -> list[tuple[int, str, str]]:
    """扫描 HTML 文件中的硬编码中文(不在 {{ }} 内的)。"""
    findings = []
    try:
        content = path.read_text(encoding='utf-8')
    except Exception:
        return findings

    for i, line in enumerate(content.splitlines(), 1):
        # 跳过注释
        if '<!--' in line and '-->' in line:
            continue
        # 跳过 {{ }} 内的内容(Jinja2 模板变量)
        cleaned = re.sub(r'\{\{[^}]*\}\}', '', line)
        # 检查 >中文< 模式(标签之间的文本)
        for match in re.finditer(r'>([^<>]*[\u4e00-\u9fff][^<>]*)<', cleaned):
            text = match.group(1).strip()
            if text and CJK_PATTERN.search(text):
                findings.append((i, 'html_text', text[:80]))
    return findings


def _violation_key(file: str, content: str) -> str:
    """生成违规唯一键(基于文件路径和内容,不依赖行号)。

    路径分隔符归一化为 /,确保 Windows 生成的 baseline 在 Linux CI 中可用。
    """
    return f"{file.replace(chr(92), '/')}::{content}"


def _load_baseline() -> set[str]:
    """加载 baseline 文件,返回已知违规键集合。"""
    if not BASELINE_FILE.exists():
        return set()
    try:
        data = json.loads(BASELINE_FILE.read_text(encoding='utf-8'))
        return set(data.get('violations', []))
    except Exception:
        return set()


def _save_baseline(violations: set[str]) -> None:
    """保存 baseline 文件。"""
    data = {
        'description': 'R44 6.3: 已知硬编码用户面向中文字符串 baseline',
        'note': '修复已知违规后运行 --generate-baseline 更新此文件',
        'violation_count': len(violations),
        'violations': sorted(violations),
    }
    BASELINE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )


def collect_findings(root: Path) -> list[tuple[str, int, str, str]]:
    """收集所有违规,返回 (file, line_no, ptype, content) 列表。"""
    findings = []

    # 扫描 Python 文件
    for pattern in ['bots/**/*.py', 'admin/**/*.py']:
        for path in root.glob(pattern):
            if is_skipped(path):
                continue
            file_findings = scan_python_file(path)
            for line_no, ptype, content in file_findings:
                findings.append((str(path.relative_to(root)), line_no, ptype, content))

    # 扫描 HTML 文件
    for path in root.glob('admin/templates/**/*.html'):
        if is_skipped(path):
            continue
        file_findings = scan_html_file(path)
        for line_no, ptype, content in file_findings:
            findings.append((str(path.relative_to(root)), line_no, ptype, content))

    return findings


def main():
    root = Path(__file__).parent.parent
    generate_baseline = '--generate-baseline' in sys.argv

    findings = collect_findings(root)

    if generate_baseline:
        # 生成 baseline 模式
        violations = set()
        for file, line, ptype, content in findings:
            violations.add(_violation_key(file, content))
        _save_baseline(violations)
        print(f"✓ Baseline 已生成: {BASELINE_FILE.name}")
        print(f"  已知违规: {len(violations)} 处")
        return 0

    # 正常扫描模式:与 baseline 比对
    baseline = _load_baseline()
    new_findings = []
    for file, line, ptype, content in findings:
        key = _violation_key(file, content)
        if key not in baseline:
            new_findings.append((file, line, ptype, content))

    if new_findings:
        print(f"❌ 发现 {len(new_findings)} 处**新增**硬编码用户面向中文字符串(不在 baseline 中):")
        for file, line, ptype, content in new_findings[:50]:
            print(f"  {file}:{line} [{ptype}]: {content}")
        if len(new_findings) > 50:
            print(f"  ... 还有 {len(new_findings) - 50} 处")
        print(f"\n如需将已知违规加入 baseline,运行:")
        print(f"  python scripts/scan_hardcoded_strings.py --generate-baseline")
        return 1
    else:
        print(f"✓ 未发现新增硬编码用户面向中文字符串(baseline: {len(baseline)} 处已知)")
        return 0


if __name__ == '__main__':
    sys.exit(main())
