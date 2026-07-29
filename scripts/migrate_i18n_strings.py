#!/usr/bin/env python3
"""R55 i18n migration: extract hardcoded Chinese strings to locale JSONs.

This is a one-time migration tool that:
  1. Scans Python and HTML files for hardcoded user-visible Chinese strings
     (using the same patterns as scan_hardcoded_strings.py)
  2. Generates dot-notation keys for each unique string
  3. Adds the strings to locales/zh-CN.json and locales/en-US.json
  4. Replaces the hardcoded strings in source code with i18n.translate() calls
     (Python: _i18n_t("key", ...); HTML: {{ t("key") }})

The script is idempotent: running it multiple times produces the same result.

Usage:
    python scripts/migrate_i18n_strings.py
    python scripts/migrate_i18n_strings.py --dry-run  # preview without writing
    python scripts/migrate_i18n_strings.py --file bots/up_bot.py  # single file
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCALES_DIR = ROOT / "locales"
ZH_CN_PATH = LOCALES_DIR / "zh-CN.json"
EN_US_PATH = LOCALES_DIR / "en-US.json"

CJK_PATTERN = re.compile(r'[\u4e00-\u9fff]')
LOGGER_CALL_RE = re.compile(r'\b(?:logger|logging|log)\.[a-zA-Z_]+\s*\(')
PRINT_CALL_RE = re.compile(r'\bprint\s*\(')
SKIP_SUBSTRINGS = ('tests/', 'docs/', 'scripts/', '__pycache__', '.git/', 'node_modules/')


def get_key_prefix(rel_path: str) -> str | None:
    """Map file path to key namespace prefix."""
    rel = rel_path.replace('\\', '/')
    if rel == 'bots/up_bot.py':
        return 'bot.up'
    if rel == 'bots/idx_bot.py':
        return 'bot.idx'
    if rel == 'bots/dsp_bot.py':
        return 'bot.dsp'
    if rel == 'bots/mon_bot.py':
        return 'bot.mon'
    if rel.startswith('bots/admin_bot/'):
        return f'bot.admin_bot.{Path(rel).stem}'
    if rel.startswith('admin/templates/'):
        return f'ui.admin.{Path(rel).stem}'
    if rel.startswith('admin/'):
        return f'admin.{Path(rel).stem}'
    if rel.startswith('services/mon/'):
        return f'services.mon.{Path(rel).stem}'
    if rel.startswith('services/'):
        return f'services.{Path(rel).stem}'
    return None


def is_skipped(path: Path) -> bool:
    return any(s in str(path) for s in SKIP_SUBSTRINGS)


def is_in_logger_context(lines: list[str], lineno: int) -> bool:
    """Check if a line is in a logger./print() call context (within ±2 lines)."""
    for offset in (-2, -1, 0, 1, 2):
        i = lineno - 1 + offset
        if 0 <= i < len(lines):
            line = lines[i]
            if LOGGER_CALL_RE.search(line) or PRINT_CALL_RE.search(line):
                return True
    return False


def _get_position(content: str, lineno: int, col_offset: int) -> int:
    """Convert (lineno, col_offset) to absolute position in content.

    Note: Python AST's col_offset / end_col_offset are UTF-8 BYTE offsets,
    not character offsets. For lines containing non-ASCII characters (e.g.,
    Chinese), byte offset > character offset. We must convert to character
    offset to correctly index into the Python str content.
    """
    pos = 0
    for _ in range(lineno - 1):
        nl = content.find('\n', pos)
        if nl == -1:
            return len(content)
        pos = nl + 1
    # pos is now at the start of the target line (character position)
    line_start = pos
    nl = content.find('\n', line_start)
    line_end = nl if nl != -1 else len(content)
    line_str = content[line_start:line_end]
    line_bytes = line_str.encode('utf-8')
    if col_offset >= len(line_bytes):
        return line_end
    # Decode the first col_offset bytes back to get the character count
    char_count = len(line_bytes[:col_offset].decode('utf-8', errors='replace'))
    return line_start + char_count


def _add_to_nested_dict(data: dict, dot_key: str, value: str) -> None:
    """Add a value to a nested dict using dot notation.

    e.g., _add_to_nested_dict(data, 'bot.up.s1', 'hello')
    creates data['bot']['up']['s1'] = 'hello'
    """
    parts = dot_key.split('.')
    d = data
    for p in parts[:-1]:
        if p not in d or not isinstance(d[p], dict):
            d[p] = {}
        d = d[p]
    d[parts[-1]] = value


def _collect_existing_keys(d: dict, prefix: str = '') -> set[str]:
    """Collect all flat keys from a nested dict."""
    keys: set[str] = set()
    for k, v in d.items():
        full = f'{prefix}.{k}' if prefix else k
        if isinstance(v, dict):
            keys.update(_collect_existing_keys(v, full))
        else:
            keys.add(full)
    return keys


def _slugify_identifier(expr: str) -> str:
    """Convert an expression to a valid Python identifier slug."""
    slug = re.sub(r'[^a-zA-Z0-9_]', '_', expr)
    slug = re.sub(r'_+', '_', slug).strip('_')
    if slug and slug[0].isdigit():
        slug = '_' + slug
    return slug


def extract_fstring_parts(node: ast.JoinedStr) -> list[tuple[str, str, str]]:
    """Extract parts from a JoinedStr (f-string) AST node.

    Returns list of (kind, source, format_spec) tuples.
    kind: 'text' or 'expr'
    source: the text content (for 'text') or expression source (for 'expr')
    format_spec: the format spec string (e.g., '.2f') or '' (for 'expr' parts only)
    """
    parts: list[tuple[str, str, str]] = []
    for val in node.values:
        if isinstance(val, ast.Constant) and isinstance(val.value, str):
            parts.append(('text', val.value, ''))
        elif isinstance(val, ast.FormattedValue):
            try:
                expr_src = ast.unparse(val.value)
            except Exception:
                expr_src = 'None'
            spec_text = ''
            if val.format_spec is not None and isinstance(val.format_spec, ast.JoinedStr):
                spec_text = ''.join(
                    v.value for v in val.format_spec.values
                    if isinstance(v, ast.Constant) and isinstance(v.value, str)
                )
            parts.append(('expr', expr_src, spec_text))
        else:
            try:
                parts.append(('text', str(ast.unparse(val)), ''))
            except Exception as _e:
                print(f"[WARN] extract_fstring_parts: ast.unparse failed: {_e}", file=sys.stderr)
    return parts


def build_locale_string_and_kwargs(
    parts: list[tuple[str, str, str]],
) -> tuple[str, list[tuple[str, str]]]:
    """Build locale string and kwargs list from f-string parts.

    Returns:
        (locale_string, kwargs_list)
        locale_string: string with {placeholder} markers
        kwargs_list: list of (placeholder_name, expression) tuples
    """
    locale_parts: list[str] = []
    kwargs: list[tuple[str, str]] = []
    used_names: set[str] = set()
    expr_counter = 0

    for ptype, pval, spec in parts:
        if ptype == 'text':
            # Escape literal { and } for .format()
            escaped = pval.replace('{', '{{').replace('}', '}}')
            locale_parts.append(escaped)
        else:  # expr
            expr = pval
            # Generate a placeholder name
            if re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', expr):
                name = expr
            else:
                name = _slugify_identifier(expr)
                if not name:
                    name = f'p{expr_counter}'

            # Ensure uniqueness
            base_name = name
            while name in used_names:
                expr_counter += 1
                name = f'{base_name}_{expr_counter}'
            used_names.add(name)
            expr_counter += 1

            # Include format spec in the placeholder
            if spec:
                locale_parts.append('{' + name + ':' + spec + '}')
            else:
                locale_parts.append('{' + name + '}')
            kwargs.append((name, expr))

    return ''.join(locale_parts), kwargs


def _is_docstring(node: ast.AST, tree: ast.AST) -> bool:
    """Check if a string node is a docstring."""
    # A docstring is the first statement in a function/class/module body
    for parent in ast.walk(tree):
        if hasattr(parent, 'body') and isinstance(getattr(parent, 'body', None), list):
            body = parent.body
            if body and body[0] is node:
                return True
            # Check if node is the value of an Expr statement that is body[0]
            if body and isinstance(body[0], ast.Expr) and body[0].value is node:
                return True
    return False


def process_python_file(
    file_path: Path,
    rel_path: str,
    zh_data: dict,
    en_data: dict,
    key_registry: set[str],
) -> tuple[str | None, int]:
    """Process a Python file: extract strings and replace with i18n calls."""
    try:
        content = file_path.read_text(encoding='utf-8')
    except Exception:
        return None, 0

    try:
        tree = ast.parse(content)
    except SyntaxError:
        return None, 0

    lines = content.splitlines()
    prefix = get_key_prefix(rel_path)
    if prefix is None:
        return None, 0

    # Collect all string literals with Chinese (excluding logger context and docstrings)
    replacements: list[tuple[int, int, str]] = []
    counter = 0

    # Build a set of docstring node ids
    docstring_nodes: set[int] = set()
    for parent in ast.walk(tree):
        if hasattr(parent, 'body') and isinstance(getattr(parent, 'body', None), list):
            body = parent.body
            if body and isinstance(body[0], ast.Expr):
                expr_node = body[0]
                if isinstance(expr_node.value, (ast.Constant, ast.JoinedStr)):
                    docstring_nodes.add(id(expr_node.value))

    # Build a set of node ids that are descendants of any JoinedStr.
    # These are string literals inside f-string expressions (e.g., the '已配置'
    # in f"...{'已配置' if x else '未配置'}..."). They should NOT be replaced
    # individually because:
    # 1. If the parent JoinedStr is in logger context, it's skipped — but the
    #    inner Constants might not be detected as logger context (different lineno)
    # 2. Replacing them with _i18n_t("...") causes quote conflicts in Python < 3.12
    # 3. The parent JoinedStr replacement handles the entire f-string
    fstring_descendants: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for child in ast.walk(node):
                if child is not node:
                    fstring_descendants.add(id(child))

    for node in ast.walk(tree):
        is_fstring = isinstance(node, ast.JoinedStr)
        is_constant_str = isinstance(node, ast.Constant) and isinstance(node.value, str)

        if not (is_fstring or is_constant_str):
            continue

        # Skip docstrings
        if id(node) in docstring_nodes:
            continue

        # Skip Constant nodes that are descendants of a JoinedStr
        # (they're inside f-string expressions and shouldn't be replaced individually)
        if is_constant_str and id(node) in fstring_descendants:
            continue

        # Check for Chinese content
        if is_fstring:
            parts = extract_fstring_parts(node)
            full_text = ''.join(p[1] for p in parts)
        else:
            full_text = node.value
            parts = []

        if not CJK_PATTERN.search(full_text):
            continue

        # Skip if in logger context
        if is_in_logger_context(lines, node.lineno):
            continue

        # Generate key
        counter += 1
        key = f'{prefix}.s{counter}'
        while key in key_registry:
            counter += 1
            key = f'{prefix}.s{counter}'
        key_registry.add(key)

        # Build locale string and kwargs
        if is_fstring:
            locale_str, kwargs = build_locale_string_and_kwargs(parts)
        else:
            locale_str = node.value
            kwargs = []

        # Add to locale JSONs (zh-CN gets the Chinese text; en-US gets a stub)
        _add_to_nested_dict(zh_data, key, locale_str)
        _add_to_nested_dict(en_data, key, locale_str)

        # Build replacement code (use single quotes for key to avoid conflicts
        # when the call is inside an f-string using double quotes)
        if kwargs:
            kwargs_str = ', '.join(f'{name}={expr}' for name, expr in kwargs)
            replacement = f"_i18n_t('{key}', {kwargs_str})"
        else:
            replacement = f"_i18n_t('{key}')"

        # Get source text position
        start_pos = _get_position(content, node.lineno, node.col_offset)
        end_pos = _get_position(content, node.end_lineno, node.end_col_offset)
        replacements.append((start_pos, end_pos, replacement))

    if not replacements:
        return content, 0

    # Filter out overlapping/nested replacements.
    # ast.walk visits child nodes of JoinedStr (e.g., the ast.Constant literal
    # parts inside an f-string). Both the outer JoinedStr and inner Constant
    # would be collected, causing overlapping ranges. Keep only the outermost
    # (widest) replacement for any overlapping group.
    replacements.sort(key=lambda x: (x[0], -(x[1] - x[0])))  # by start asc, then widest first
    filtered: list[tuple[int, int, str]] = []
    current_end = -1
    for start_pos, end_pos, replacement in replacements:
        if start_pos >= current_end:
            # No overlap with the previous kept replacement
            filtered.append((start_pos, end_pos, replacement))
            current_end = end_pos
        # else: this range is nested within/overlaps the previous kept range — skip it
    replacements = filtered

    # Sort by start_pos descending (to preserve offsets during replacement)
    replacements.sort(key=lambda x: x[0], reverse=True)

    # Apply replacements
    new_content = content
    for start_pos, end_pos, replacement in replacements:
        new_content = new_content[:start_pos] + replacement + new_content[end_pos:]

    return new_content, len(replacements)


def process_html_file(
    file_path: Path,
    rel_path: str,
    zh_data: dict,
    en_data: dict,
    key_registry: set[str],
) -> tuple[str | None, int]:
    """Process an HTML file: extract Chinese text and replace with Jinja2 calls."""
    try:
        content = file_path.read_text(encoding='utf-8')
    except Exception:
        return None, 0

    prefix = get_key_prefix(rel_path)
    if prefix is None:
        return None, 0

    counter = 0
    total_replacements = 0
    lines = content.split('\n')
    new_lines: list[str] = []

    for line in lines:
        # Skip comment lines
        if '<!--' in line and '-->' in line:
            new_lines.append(line)
            continue

        # Remove {{ }} content for matching
        cleaned = re.sub(r'\{\{[^}]*\}\}', '', line)

        # Find all >中文< patterns
        matches = list(re.finditer(r'>([^<>]*[\u4e00-\u9fff][^<>]*)<', cleaned))
        if not matches:
            new_lines.append(line)
            continue

        # Process matches in reverse order to preserve positions
        new_line = line
        for match in reversed(matches):
            text = match.group(1).strip()
            if not text or not CJK_PATTERN.search(text):
                continue

            counter += 1
            key = f'{prefix}.s{counter}'
            while key in key_registry:
                counter += 1
                key = f'{prefix}.s{counter}'
            key_registry.add(key)

            # Add to locale JSONs
            _add_to_nested_dict(zh_data, key, text)
            _add_to_nested_dict(en_data, key, text)

            # Replace >text< with >{{ t("key") }}< in the original line
            start = match.start()
            end = match.end()
            new_str = '>{{ t("' + key + '") }}<'
            new_line = new_line[:start] + new_str + new_line[end:]
            total_replacements += 1

        new_lines.append(new_line)

    new_content = '\n'.join(new_lines)
    if new_content != content:
        return new_content, total_replacements
    return content, 0


def ensure_python_imports(content: str) -> str:
    """Ensure the file imports _i18n_t (translate alias).

    If the file already imports from services.i18n but doesn't have `translate as _i18n_t`,
    add it to the existing import. Otherwise, add a new import line.
    """
    if "_i18n_t(" not in content:
        # No _i18n_t calls, no need to import
        return content

    # Check if _i18n_t is already defined/imported
    if re.search(r'\b_i18n_t\b', content.split('\n', 50)[0] if '\n' in content else ''):
        # Already imported in the first 50 lines
        return content

    # Quick check: is _i18n_t anywhere as an identifier (defined, imported, or assigned)?
    if re.search(r'(?:import|def)\s+_i18n_t\b|as\s+_i18n_t\b|_i18n_t\s*=', content):
        return content

    # Find existing import from services.i18n
    pattern = re.compile(
        r'^(from\s+services\.i18n\s+import\s+)([^#\n]+?)(\s*(?:#.*)?)$',
        re.MULTILINE,
    )
    match = pattern.search(content)
    if match:
        existing_imports = match.group(2).strip()
        # Check if translate is already imported (under any alias)
        if 'translate' not in existing_imports:
            # Add translate as _i18n_t
            new_imports = existing_imports.rstrip(',').rstrip()
            if ',' not in new_imports and ' as ' not in new_imports:
                new_imports = f'{new_imports}, translate as _i18n_t'
            else:
                new_imports = f'{new_imports}, translate as _i18n_t'
            new_line = f'{match.group(1)}{new_imports}{match.group(3)}'
            return content[: match.start()] + new_line + content[match.end():]

    # No existing import from services.i18n — add a new import line
    # Find a good place: after the last "from ... import" or "import ..." line at the top
    lines = content.split('\n')
    insert_idx = 0
    for i, line in enumerate(lines[:80]):
        stripped = line.strip()
        if stripped.startswith('from ') or stripped.startswith('import '):
            insert_idx = i + 1
            # Handle multi-line imports with unclosed parenthesis
            if '(' in stripped and ')' not in stripped:
                # Scan forward for the closing ')' line
                for j in range(i + 1, min(len(lines), 80)):
                    insert_idx = j + 1
                    if ')' in lines[j]:
                        break
        elif stripped == '' and insert_idx > 0:
            # Allow blank lines after imports
            continue
        elif stripped and not stripped.startswith('#') and insert_idx > 0:
            break

    new_import = 'from services.i18n import translate as _i18n_t'
    lines.insert(insert_idx, new_import)
    return '\n'.join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='i18n migration: extract hardcoded strings')
    parser.add_argument('--dry-run', action='store_true', help='Preview without writing')
    parser.add_argument('--file', type=str, default=None, help='Process a single file')
    args = parser.parse_args(argv)

    # Load existing locale JSONs
    zh_data = json.loads(ZH_CN_PATH.read_text(encoding='utf-8'))
    en_data = json.loads(EN_US_PATH.read_text(encoding='utf-8'))

    key_registry = _collect_existing_keys(zh_data)

    # Collect files to process
    files_to_process: list[Path] = []
    if args.file:
        files_to_process.append(ROOT / args.file)
    else:
        for pattern in ['bots/**/*.py', 'admin/**/*.py', 'services/**/*.py']:
            for path in ROOT.glob(pattern):
                if is_skipped(path):
                    continue
                files_to_process.append(path)
        for path in ROOT.glob('admin/templates/**/*.html'):
            if is_skipped(path):
                continue
            files_to_process.append(path)

    total_replacements = 0
    files_modified = 0
    for path in files_to_process:
        rel = str(path.relative_to(ROOT)).replace('\\', '/')
        if path.suffix == '.py':
            new_content, n = process_python_file(path, rel, zh_data, en_data, key_registry)
            if new_content is not None and n > 0:
                new_content = ensure_python_imports(new_content)
                if not args.dry_run:
                    path.write_text(new_content, encoding='utf-8')
                print(f'  {rel}: {n} replacements')
                total_replacements += n
                files_modified += 1
        elif path.suffix == '.html':
            new_content, n = process_html_file(path, rel, zh_data, en_data, key_registry)
            if new_content is not None and n > 0:
                if not args.dry_run:
                    path.write_text(new_content, encoding='utf-8')
                print(f'  {rel}: {n} replacements')
                total_replacements += n
                files_modified += 1

    if not args.dry_run and total_replacements > 0:
        ZH_CN_PATH.write_text(
            json.dumps(zh_data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8'
        )
        EN_US_PATH.write_text(
            json.dumps(en_data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8'
        )

    print(f'\nTotal: {total_replacements} replacements across {files_modified} files')
    print(f'Total keys in registry: {len(key_registry)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
