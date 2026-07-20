#!/usr/bin/env python3
"""R66 P1-09: skip 测试清单生成器 — 收集 pytest skip 标记并生成结构化清单。

背景:
    R66 终审报告 P1-09: 自审报告称全量 `6037 passed, 142 skipped`,
    并排除 Telethon 依赖测试。上线前必须生成 skip inventory:
    每个 skip 必须含原因(reason)、owner、到期日(due_date)、生产影响(production_impact)。
    涉及 Telegram 主链、恢复、密钥、部署的 skip 不得归为无关历史问题。

工作流程:
    1. 扫描 tests/ 目录下所有 .py 测试文件
    2. 使用 AST 解析查找 skip 标记(主路径,无需 pytest-json-report 插件):
       - @pytest.mark.skip(reason=...) 装饰器(函数级/类级)
       - @pytest.mark.skipif(condition, reason=...) 装饰器(函数级/类级)
       - pytest.skip(reason) 函数体内调用(运行时条件 skip)
       - pytestmark = pytest.mark.skipif(...) 模块级赋值
    3. 为每个 skip 推断:
       - file_path: 测试文件相对路径
       - test_name: 测试函数/类名(或 <module> 表示模块级)
       - reason: 原始 skip 原因
       - category: 按原因关键词推断的类别(优先级:
         telethon_dependency > crdb_dependency > temporarily_disabled
         > historical_legacy > dependency_missing > uncategorized)
       - production_impact: 按文件路径推断的生产影响等级(high/medium/low)
       - owner: 按文件路径推断的责任团队
       - due_date: 从原因文本提取的到期日(若存在)
    4. 输出 JSON + 汇总统计(total / by_category / by_production_impact /
       by_owner / missing_owner / missing_due_date / critical_path_missing_due_date)

调用方式:
    # 输出到 stdout
    python scripts/collect_skip_inventory.py

    # 写入文件
    python scripts/collect_skip_inventory.py --output docs/skip_inventory.json

    # 指定测试目录
    python scripts/collect_skip_inventory.py --tests-dir /tmp/synthetic_tests

退出码:
    0: 始终(清单工具,非门禁)。R66 P1-09 要求生成清单,而非阻断 CI。
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from loguru import logger

# 项目根目录(scripts/ 的上一级)
REPO_ROOT = Path(__file__).resolve().parent.parent

# 默认测试目录
TESTS_DIR = REPO_ROOT / "tests"

# 跳过的目录(不扫描)
SKIP_DIR_PARTS: list[str] = [
    "__pycache__",
    ".git",
    "node_modules",
    "venv",
    ".venv",
    ".pytest_cache",
    # tests/a11y/ 与 tests/e2e/ 是 Playwright (TypeScript),不是 pytest
    "tests/a11y",
    "tests/e2e",
]

# ════════════════════════════════════════════════════════════════
# 类别关键词表(按优先级顺序,先匹配的类别优先)
# ════════════════════════════════════════════════════════════════
# R66 P1-09 严格要求:
#   - Telegram 主链 / 恢复 / 密钥 / 部署 相关 skip 不得归为"无关历史问题"
# 因此 telethon_dependency 优先级最高(在 historical_legacy 之前)。
#
# 注:dependency_missing 关键词列表中本应含 "Telethon" / "telegram",
# 但因 telethon_dependency 已先匹配,实际不会落到此处。
# 此处的 dependency_missing 仅用于其它依赖(aiosqlite / asyncpg / pyotp 等)。
CATEGORY_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("telethon_dependency", ("Telethon", "telethon", "telegram")),
    ("crdb_dependency", ("CRDB", "CockroachDB", "crdb")),
    ("temporarily_disabled", ("暂时", "temporary", "TODO", "FIXME", "disabled")),
    ("historical_legacy", ("历史", "legacy", "deprecated", "abandoned")),
    (
        "dependency_missing",
        (
            "not installed", "不可用", "需要", "requires", "PEP 604",
            "aiosqlite", "asyncpg", "pyotp",
        ),
    ),
]

# ════════════════════════════════════════════════════════════════
# 生产影响路径表
# ════════════════════════════════════════════════════════════════
# high: 关键路径(主链 Bot / 恢复 / 备份 / 命令总线 / 审批 / MFA / 迁移 / 门禁脚本 / CI)
# medium: services/ 或 database/ 下其它路径
# low: 其它
HIGH_IMPACT_PREFIXES: tuple[str, ...] = (
    "bots/",
    "services/restore",
    "services/backup",
    "services/db_backup",
    "services/db_restore",
    "services/command_bus",
    "services/approval",
    "services/mfa",
    "database/migrate",
    "scripts/check_",
    ".github/workflows",
)

MEDIUM_IMPACT_PREFIXES: tuple[str, ...] = (
    "services/",
    "database/",
)

# ════════════════════════════════════════════════════════════════
# Owner 路径推断表(按顺序匹配,先匹配的优先)
# ════════════════════════════════════════════════════════════════
OWNER_RULES: list[tuple[str, str]] = [
    ("bots/", "bot-team"),
    ("services/restore", "restore-team"),
    ("services/backup", "backup-team"),
    ("services/db_backup", "backup-team"),
    ("services/db_restore", "restore-team"),
    ("services/command_bus", "commandbus-team"),
    ("services/approval", "approval-team"),
    ("services/mfa", "mfa-team"),
    ("database/migrate", "db-team"),
    ("database/", "db-team"),
    ("admin/mfa", "mfa-team"),
    ("admin/passwords", "auth-team"),
    ("admin/sessions", "auth-team"),
    ("admin/", "admin-team"),
    ("scripts/check_", "platform-team"),
    (".github/workflows", "platform-team"),
]

# ════════════════════════════════════════════════════════════════
# 日期提取正则
# ════════════════════════════════════════════════════════════════
# 支持:
#   - "2025-12-31"            ISO 日期
#   - "by 2025-12-31"         "by YYYY-MM-DD"
#   - "by 2025-12"            "by YYYY-MM"
#   - "by 2025"               "by YYYY"
#   - "2025年12月31日"        中文日期
#   - "2025"                  仅年份(放最后,避免误匹配)
DATE_PATTERN_BY = re.compile(r"\bby\s+(20\d{2}(?:-\d{1,2}(?:-\d{1,2})?)?)\b", re.IGNORECASE)
DATE_PATTERN_CN = re.compile(r"(20\d{2})年(\d{1,2})月(\d{1,2})日")
DATE_PATTERN_ISO = re.compile(r"\b(20\d{2}-\d{1,2}-\d{1,2})\b")
DATE_PATTERN_YEAR = re.compile(r"\b(20\d{2})\b")


@dataclass
class SkipRecord:
    """单个 skip 记录。

    Attributes:
        file_path: 测试文件相对 REPO_ROOT 的 POSIX 路径
        test_name: 测试函数/类名(或 <module> 表示模块级 pytestmark)
        reason: 原始 skip 原因(从 reason= 关键字或位置参数提取)
        category: 推断的类别(见 CATEGORY_KEYWORDS)
        production_impact: 生产影响等级(high/medium/low)
        owner: 责任团队(如 bot-team / restore-team / unassigned)
        due_date: 从原因文本提取的到期日(规范化为 YYYY-MM-DD / YYYY-MM / YYYY),无则空字符串
        line: skip 标记所在行号
        marker_type: 标记类型(decorator / call / module_pytestmark)
    """
    file_path: str
    test_name: str
    reason: str
    category: str
    production_impact: str
    owner: str
    due_date: str
    line: int
    marker_type: str


# ════════════════════════════════════════════════════════════════
# 路径辅助
# ════════════════════════════════════════════════════════════════

def _rel_posix(path: Path) -> str:
    """返回相对 REPO_ROOT 的 POSIX 路径字符串(用 / 分隔)。"""
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _is_skipped_path(path: Path) -> bool:
    """检查路径是否应跳过(在 SKIP_DIR_PARTS 中)。"""
    rel = _rel_posix(path)
    for part in SKIP_DIR_PARTS:
        if part in rel:
            return True
    return False


def _iter_test_files(tests_dir: Path) -> Iterable[Path]:
    """遍历 tests_dir 下所有 .py 测试文件(跳过缓存/非 pytest 目录)。"""
    if not tests_dir.exists():
        return
    for py_file in tests_dir.rglob("*.py"):
        if _is_skipped_path(py_file):
            continue
        yield py_file


# ════════════════════════════════════════════════════════════════
# 推断函数
# ════════════════════════════════════════════════════════════════

def infer_category(reason: str) -> str:
    """根据原因关键词推断 skip 类别。

    按优先级顺序匹配 CATEGORY_KEYWORDS:
      1. telethon_dependency(Telegram 主链 - 最高优先级)
      2. crdb_dependency
      3. temporarily_disabled
      4. historical_legacy
      5. dependency_missing(其它依赖缺失)
      6. uncategorized(默认)

    Args:
        reason: skip 原因字符串

    Returns:
        类别名(见 CATEGORY_KEYWORDS)
    """
    if not reason:
        return "uncategorized"
    for category, keywords in CATEGORY_KEYWORDS:
        for kw in keywords:
            if kw in reason:
                return category
    return "uncategorized"


def infer_production_impact(file_path: str) -> str:
    """根据文件路径推断生产影响等级。

    Args:
        file_path: POSIX 相对路径

    Returns:
        "high" / "medium" / "low"
    """
    for prefix in HIGH_IMPACT_PREFIXES:
        if file_path.startswith(prefix):
            return "high"
    for prefix in MEDIUM_IMPACT_PREFIXES:
        if file_path.startswith(prefix):
            return "medium"
    return "low"


def infer_owner(file_path: str) -> str:
    """根据文件路径推断责任团队。

    Args:
        file_path: POSIX 相对路径

    Returns:
        团队名(如 "bot-team")或 "unassigned"
    """
    for prefix, owner in OWNER_RULES:
        if file_path.startswith(prefix):
            return owner
    return "unassigned"


def extract_due_date(reason: str) -> str:
    """从原因文本中提取到期日期。

    支持格式(按优先级):
      1. "by YYYY-MM-DD" / "by YYYY-MM" / "by YYYY"
      2. "YYYY年MM月DD日"(中文)
      3. "YYYY-MM-DD"(ISO)
      4. "YYYY"(仅年份)

    Args:
        reason: skip 原因字符串

    Returns:
        规范化为 YYYY-MM-DD / YYYY-MM / YYYY 的日期字符串,无则空字符串
    """
    if not reason:
        return ""
    # 1. "by ..." 形式(优先级最高)
    m = DATE_PATTERN_BY.search(reason)
    if m:
        return _normalize_date(m.group(1))
    # 2. 中文日期 "YYYY年MM月DD日"
    m = DATE_PATTERN_CN.search(reason)
    if m:
        year, month, day = m.group(1), m.group(2), m.group(3)
        return f"{year}-{int(month):02d}-{int(day):02d}"
    # 3. ISO 日期 "YYYY-MM-DD"
    m = DATE_PATTERN_ISO.search(reason)
    if m:
        return _normalize_date(m.group(1))
    # 4. 仅年份 "YYYY"
    m = DATE_PATTERN_YEAR.search(reason)
    if m:
        return m.group(1)
    return ""


def _normalize_date(s: str) -> str:
    """规范化日期字符串为 YYYY-MM-DD / YYYY-MM / YYYY。

    Args:
        s: 日期字符串(如 "2025-12-31" / "2025-12" / "2025")

    Returns:
        规范化后的日期字符串
    """
    parts = s.split("-")
    if len(parts) == 3 and parts[0].isdigit():
        try:
            return f"{parts[0]}-{int(parts[1]):02d}-{int(parts[2]):02d}"
        except (ValueError, IndexError):
            return s
    if len(parts) == 2 and parts[0].isdigit():
        try:
            return f"{parts[0]}-{int(parts[1]):02d}"
        except (ValueError, IndexError):
            return s
    return s


# ════════════════════════════════════════════════════════════════
# AST 解析:提取 skip 标记
# ════════════════════════════════════════════════════════════════

def _get_attribute_chain(node: ast.expr) -> list[str]:
    """提取 Attribute/Name 链的全限定名。

    例:
        pytest.mark.skip → ["pytest", "mark", "skip"]
        pytest.skip      → ["pytest", "skip"]
        pytest.mark.skipif → ["pytest", "mark", "skipif"]
    """
    parts: list[str] = []
    cur: ast.expr = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    parts.reverse()
    return parts


def _is_skip_decorator(dec: ast.expr) -> bool:
    """检查装饰器是否为 @pytest.mark.skip / @pytest.mark.skipif。

    同时处理 Call 形式(@pytest.mark.skip(...))与裸 Attribute 形式(@pytest.mark.skip)。
    """
    target = dec.func if isinstance(dec, ast.Call) else dec
    parts = _get_attribute_chain(target)
    return parts == ["pytest", "mark", "skip"] or parts == ["pytest", "mark", "skipif"]


def _extract_reason_from_decorator(call: ast.Call) -> str:
    """从 @pytest.mark.skip / @pytest.mark.skipif 装饰器 Call 节点提取 reason 字符串。

    提取规则:
      1. reason="..." 关键字参数(常量字符串优先)
      2. reason=<expr> 关键字参数(f-string / 表达式 → ast.unparse)
      3. pytest.mark.skip("...") 位置参数(skip 的第一个位置参数为 msg/reason;
         skipif 的第一个位置参数为 condition,跳过)
    """
    # reason="..." 关键字参数
    for kw in call.keywords:
        if kw.arg == "reason":
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                return kw.value.value
            # f-string 或表达式:回退到 ast.unparse(便于 f-string 模板可读)
            try:
                return ast.unparse(kw.value)
            except Exception:
                return ""
    # pytest.mark.skip("...") 位置参数
    func = call.func
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "skip"
        and call.args
    ):
        first = call.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return ""


def _is_pytest_skip_call(call: ast.Call) -> bool:
    """检查 AST Call 节点是否为 pytest.skip(...) 调用。"""
    func = call.func
    if not isinstance(func, ast.Attribute):
        return False
    return (
        isinstance(func.value, ast.Name)
        and func.value.id == "pytest"
        and func.attr == "skip"
    )


def _extract_reason_from_skip_call(call: ast.Call) -> str:
    """从 pytest.skip(reason) 调用提取 reason 字符串。"""
    # pytest.skip(reason="...")
    for kw in call.keywords:
        if kw.arg == "reason":
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                return kw.value.value
            try:
                return ast.unparse(kw.value)
            except Exception:
                return ""
    # pytest.skip("...")
    if call.args:
        first = call.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return ""


def _build_parent_map(tree: ast.AST) -> dict[int, ast.AST]:
    """构建 parent map: {node_id: parent_node}。"""
    parent_map: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parent_map[id(child)] = parent
    return parent_map


def _find_enclosing_name(node: ast.AST, parent_map: dict[int, ast.AST]) -> str | None:
    """找到节点最近的 enclosing function/class 名(向上遍历 parent map)。"""
    current = parent_map.get(id(node))
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return current.name
        current = parent_map.get(id(current))
    return None


def _build_record(
    file_path: str,
    test_name: str,
    reason: str,
    line: int,
    marker_type: str,
) -> SkipRecord:
    """构建单个 SkipRecord(应用类别/影响/owner/due_date 推断)。"""
    return SkipRecord(
        file_path=file_path,
        test_name=test_name,
        reason=reason,
        category=infer_category(reason),
        production_impact=infer_production_impact(file_path),
        owner=infer_owner(file_path),
        due_date=extract_due_date(reason),
        line=line,
        marker_type=marker_type,
    )


def collect_skips_from_source(source: str, file_path: str) -> list[SkipRecord]:
    """解析源代码字符串,提取所有 skip 标记。

    扫描:
      - 装饰器级:@pytest.mark.skip / @pytest.mark.skipif(函数/类)
      - 调用级:函数体内 pytest.skip(reason) 调用
      - 模块级:pytestmark = pytest.mark.skipif(...)

    Args:
        source: Python 源代码
        file_path: 用于 SkipRecord.file_path 的相对路径

    Returns:
        SkipRecord 列表(可能为空)
    """
    records: list[SkipRecord] = []

    try:
        tree = ast.parse(source, filename=file_path)
    except SyntaxError as e:
        logger.warning("解析失败 {}: {}", file_path, e)
        return records

    parent_map = _build_parent_map(tree)

    # 1. 装饰器级 skip:遍历 ClassDef / FunctionDef / AsyncFunctionDef
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for dec in node.decorator_list:
            if not _is_skip_decorator(dec):
                continue
            reason = ""
            if isinstance(dec, ast.Call):
                reason = _extract_reason_from_decorator(dec)
            records.append(_build_record(
                file_path, node.name, reason, node.lineno, "decorator",
            ))

    # 2. 函数体内 pytest.skip(reason) 调用
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not _is_pytest_skip_call(node):
            continue
        reason = _extract_reason_from_skip_call(node)
        enclosing = _find_enclosing_name(node, parent_map)
        test_name = enclosing or "<module>"
        records.append(_build_record(
            file_path, test_name, reason, node.lineno, "call",
        ))

    # 3. 模块级 pytestmark = pytest.mark.skipif(...) / pytest.mark.skip(...)
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign):
            continue
        for target in stmt.targets:
            if not (isinstance(target, ast.Name) and target.id == "pytestmark"):
                continue
            if not isinstance(stmt.value, ast.Call):
                continue
            if not _is_skip_decorator(stmt.value):
                continue
            reason = _extract_reason_from_decorator(stmt.value)
            records.append(_build_record(
                file_path, "<module>", reason, stmt.lineno, "module_pytestmark",
            ))

    return records


def collect_skips_from_file(file_path: Path) -> list[SkipRecord]:
    """解析单个测试文件,提取所有 skip 标记。"""
    rel = _rel_posix(file_path)
    try:
        source = file_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as e:
        logger.warning("读取失败 {}: {}", rel, e)
        return []
    return collect_skips_from_source(source, rel)


def collect_all_skips(tests_dir: Path | None = None) -> list[SkipRecord]:
    """收集所有测试文件的 skip 标记。

    Args:
        tests_dir: 测试目录(默认 tests/)

    Returns:
        所有 SkipRecord 列表
    """
    base = tests_dir or TESTS_DIR
    all_records: list[SkipRecord] = []
    scanned = 0
    for py_file in _iter_test_files(base):
        scanned += 1
        records = collect_skips_from_file(py_file)
        all_records.extend(records)
    logger.info("扫描 {} 个测试文件,收集到 {} 个 skip 标记", scanned, len(all_records))
    return all_records


# ════════════════════════════════════════════════════════════════
# 汇总统计与输出
# ════════════════════════════════════════════════════════════════

def build_summary(records: list[SkipRecord]) -> dict:
    """构建汇总统计。

    Returns:
        含以下字段的 dict:
            total: 总 skip 数
            by_category: {category: count}
            by_production_impact: {impact: count}
            by_owner: {owner: count}
            missing_owner: owner == "unassigned" 的数量
            missing_due_date: due_date 为空的数量
            critical_path_missing_due_date: production_impact == "high" 且 due_date 为空的数量
    """
    by_category: dict[str, int] = {}
    by_impact: dict[str, int] = {}
    by_owner: dict[str, int] = {}
    missing_owner = 0
    missing_due_date = 0
    critical_no_due = 0
    for r in records:
        by_category[r.category] = by_category.get(r.category, 0) + 1
        by_impact[r.production_impact] = by_impact.get(r.production_impact, 0) + 1
        by_owner[r.owner] = by_owner.get(r.owner, 0) + 1
        if r.owner == "unassigned":
            missing_owner += 1
        if not r.due_date:
            missing_due_date += 1
            if r.production_impact == "high":
                critical_no_due += 1
    return {
        "total": len(records),
        "by_category": by_category,
        "by_production_impact": by_impact,
        "by_owner": by_owner,
        "missing_owner": missing_owner,
        "missing_due_date": missing_due_date,
        "critical_path_missing_due_date": critical_no_due,
    }


def _now_iso() -> str:
    """返回当前 ISO 时间(UTC)。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def output_inventory(
    records: list[SkipRecord],
    summary: dict,
    output_path: Path | None = None,
) -> None:
    """输出 JSON 清单。

    若 output_path 为 None,JSON 输出到 stdout(日志/进度信息走 stderr);
    否则 JSON 写入文件,进度信息走 stderr。

    Args:
        records: SkipRecord 列表
        summary: build_summary() 返回的汇总 dict
        output_path: 输出文件路径(None 则输出到 stdout)
    """
    payload = {
        "description": "R66 P1-09: skip inventory — 全量 skip 测试清单(原因/owner/到期日/生产影响)",
        "generated_at": _now_iso(),
        "summary": summary,
        "skips": [asdict(r) for r in records],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if output_path is None:
        print(text)
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text, encoding="utf-8")
        print(f"[OK] skip inventory 写入 {output_path}", file=sys.stderr)
        print(
            f"     total={summary['total']}, "
            f"by_category={summary['by_category']}, "
            f"critical_path_missing_due_date={summary['critical_path_missing_due_date']}",
            file=sys.stderr,
        )


def main() -> None:
    """脚本入口。"""
    parser = argparse.ArgumentParser(
        description=(
            "R66 P1-09: skip inventory 生成器 — "
            "收集 pytest skip 标记并生成结构化清单(原因/owner/到期日/生产影响)。"
        )
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="输出 JSON 文件路径(不指定则输出到 stdout)。",
    )
    parser.add_argument(
        "--tests-dir",
        type=Path,
        default=TESTS_DIR,
        help=f"测试目录(默认 {TESTS_DIR})。",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="详细日志输出(DEBUG 级别)。",
    )
    args = parser.parse_args()

    if not args.verbose:
        # 默认 WARNING 级别(避免污染 stdout)
        logger.remove()
        logger.add(sys.stderr, level="WARNING")

    records = collect_all_skips(args.tests_dir)
    summary = build_summary(records)
    output_inventory(records, summary, args.output)

    # 始终返回 0(清单工具,非门禁)
    sys.exit(0)


if __name__ == "__main__":
    main()
