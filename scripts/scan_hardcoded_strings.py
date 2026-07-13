#!/usr/bin/env python3
"""R44: 扫描硬编码用户面向中文字符串,确保已接入 i18n。"""
import re
import sys
from pathlib import Path

# 中文 Unicode 范围
CJK_PATTERN = re.compile(r'[\u4e00-\u9fff]')

# 用户面向字符串模式(reply_text/answer/HTTPException/detail/f-string 等)
USER_FACING_PATTERNS = [
    # Python: reply_text("中文") / answer("中文") / detail="中文" / HTTPException(detail="中文")
    re.compile(r'(?:reply_text|answer|reply_photo|reply_document|send_message)\s*\(\s*["\']([^"\']*)["\']'),
    re.compile(r'detail\s*=\s*["\']([^"\']*)["\']'),
    re.compile(r'HTTPException\s*\([^)]*detail\s*=\s*["\']([^"\']*)["\']'),
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


def is_skipped(path: Path) -> bool:
    for pattern in SKIP_PATTERNS:
        if pattern in str(path):
            return True
    return False


def scan_python_file(path: Path) -> list[tuple[int, str, str]]:
    """扫描 Python 文件,返回 (line_no, pattern_type, content) 列表。"""
    findings = []
    try:
        content = path.read_text(encoding='utf-8')
    except Exception:
        return findings

    for i, line in enumerate(content.splitlines(), 1):
        # 跳过注释行
        stripped = line.lstrip()
        if stripped.startswith('#') or stripped.startswith('"""') or stripped.startswith("'''"):
            continue
        # 跳过 logger 调用(非用户面向)
        if 'logger.' in line or 'logging.' in line or 'print(' in line:
            continue

        for pattern in USER_FACING_PATTERNS:
            for match in pattern.finditer(line):
                matched_text = match.group(1)
                if CJK_PATTERN.search(matched_text):
                    findings.append((i, pattern.pattern[:50], matched_text[:80]))
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
        # 移除 {{ }} 内的内容后检查中文
        cleaned = re.sub(r'\{\{[^}]*\}\}', '', line)
        # 检查 >中文< 模式(标签之间的文本)
        for match in re.finditer(r'>([^<>]*[\u4e00-\u9fff][^<>]*)<', cleaned):
            text = match.group(1).strip()
            if text and CJK_PATTERN.search(text):
                findings.append((i, 'html_text', text[:80]))
    return findings


def main():
    root = Path(__file__).parent.parent
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

    if findings:
        print(f"❌ 发现 {len(findings)} 处硬编码用户面向中文字符串:")
        for file, line, ptype, content in findings[:50]:  # 限制输出前 50 个
            print(f"  {file}:{line} [{ptype}]: {content}")
        if len(findings) > 50:
            print(f"  ... 还有 {len(findings) - 50} 处")
        return 1
    else:
        print("✓ 未发现硬编码用户面向中文字符串")
        return 0


if __name__ == '__main__':
    sys.exit(main())
