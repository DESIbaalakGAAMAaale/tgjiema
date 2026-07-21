#!/usr/bin/env python3
"""R64 P1-06: Sink import-boundary AST 门禁。

整改背景(R64 终审报告 P1-06):
    业务模块(bots/、services/)此前直接调用第三方 send/edit/response API
    (telegram.Bot.send_message / query.edit_message_text / JSONResponse 等),
    绕过 ``UserMessage | ErrorEnvelope`` 类型边界。本脚本用 AST 静态阻断新增
    直调,存量违规通过 baseline 机制逐步迁移。

检测规则:

  Rule 1 (import 违规): 第三方 sink 包的 import 只允许出现在 adapter 包中
    - ``from telegram import Bot`` / ``from telegram.ext import ...``
      → 只允许在 ``services/sink_adapters/telegram_adapter.py`` 中
    - ``from fastapi.responses import JSONResponse``
      → 只允许在 ``services/sink_adapters/web_adapter.py`` 和 ``admin/`` 中
      (admin/ 暂时豁免:FastAPI 路由层迁移在后续 PR 推进)

  Rule 2 (call 违规): 业务模块禁止直接调用原生 sink API
    - ``update.message.reply_text(...)`` / ``message.reply_text(...)``
      → 应改用 ``sink_adapters.telegram_adapter.safe_reply_text(...)``
    - ``context.bot.send_message(...)`` / ``bot.send_message(...)``
      → 应改用 ``sink_adapters.telegram_adapter.safe_send_message(...)``
    - ``query.edit_message_text(...)``
      → 应改用 ``sink_adapters.telegram_adapter.safe_edit_message_text(...)``

白名单(允许直接调用的文件/目录):
    - ``services/sink_adapters/``       — adapter 包本身(原生 sink 的唯一调用方)
    - ``services/user_message.py``      — UserMessage 定义模块(render_for_send)
    - ``utils/flood_waiter.py``         — FloodWait 退避包装(adapter 内部调用)
    - ``tests/``                        — 测试代码
    - ``scripts/`` (R67 P1-08 细粒度)   — 仅 GATE_SCANNERS 类脚本可跳过自检;
                                          OFFLINE_RECOVERY_TOOLS 与
                                          GOVERNANCE_SCRIPTS 必须被扫描
    - ``admin/``                        — FastAPI 路由(暂豁免,后续 PR 迁移)

Baseline 机制(类似 ``check_error_protocol.py``):
    由于现有代码有 100+ 处违规,使用 baseline 机制 ratchet:
    - ``--baseline <file>``:当前违规数 <= baseline.violation_count 时通过
      (每个 commit 只能减少不能增加)
    - ``--strict``:任何违规都失败(未来目标,忽略 baseline)
    - ``--generate-baseline <file>``:生成/更新 baseline 文件

CI 调用方式:
    python scripts/check_sink_import_boundary.py --baseline scripts/sink_import_boundary_baseline.json
    python scripts/check_sink_import_boundary.py --strict  # 未来目标

退出码:
    0 — 通过(无违规,或当前违规数 <= baseline)
    1 — 失败(有违规且超过 baseline,或 strict 模式下任何违规)
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
SCAN_DIRS: list[str] = ["bots", "services"]

# 跳过的目录名(不扫描)
SKIP_DIR_PARTS: list[str] = [
    "__pycache__",
    ".git",
    "node_modules",
    "venv",
    ".venv",
    ".pytest_cache",
]

# 白名单前缀(允许直接调用第三方 sink 的文件/目录,相对 REPO_ROOT,POSIX 路径)
# 命中任一前缀的文件将被跳过(允许直接调用)
# R67 P1-08: scripts/ 不再整体跳过 — 通过 `is_skippable_script()` 细粒度判断。
# 仅 GATE_SCANNERS 可跳过;OFFLINE_RECOVERY_TOOLS 与 GOVERNANCE_SCRIPTS 必须被扫描。
ALLOWED_PREFIXES: list[str] = [
    "services/sink_adapters/",       # adapter 包本身(原生 sink 的唯一调用方)
    "services/user_message.py",      # UserMessage / render_for_send 定义
    "utils/flood_waiter.py",         # FloodWait 退避包装(adapter 内部调用)
    "tests/",                        # 测试代码
    "admin/",                        # FastAPI 路由(暂豁免,后续 PR 迁移)
]

# R67 P1-08: scripts/ 下可跳过的文件清单(从 _script_categories 导入)
try:
    from scripts._script_categories import is_skippable_script as _is_skippable_script_p1_08
except ImportError:
    # _script_categories 不可用时 fail-closed:不跳过任何 scripts/ 文件
    def _is_skippable_script_p1_08(rel_path: str) -> bool:
        return False

# Rule 1: import 违规配置
# 第三方 sink 包的 import 只允许出现在指定文件中
# (module_prefix, name, allowed_files)
# - ``from <module_prefix> import <name>`` 违规,除非文件在 allowed_files 中
TELEGRAM_IMPORT_ALLOWED_FILES: frozenset[str] = frozenset({
    "services/sink_adapters/telegram_adapter.py",
    "utils/flood_waiter.py",  # FloodWait 包装层(已存在的原生 API 调用方)
})

JSONRESPONSE_IMPORT_ALLOWED_FILES: frozenset[str] = frozenset({
    "services/sink_adapters/web_adapter.py",
    # admin/ 整个目录豁免(在 _is_allowed 中按前缀判断)
})

# Rule 2: call 违规检测的 sink 方法名
# 业务模块禁止直接调用以下方法(应改用 sink_adapters.*)
DISALLOWED_SINK_METHODS: frozenset[str] = frozenset({
    "reply_text",          # update.message.reply_text / message.reply_text
    "send_message",        # context.bot.send_message / bot.send_message
    "edit_message_text",   # query.edit_message_text
})

# Rule 2: 视为 sink 来源的 attribute chain 末尾名称
# 例如 update.message.reply_text / query.edit_message_text / bot.send_message
# 我们检测 ast.Call 中 func 为 ast.Attribute 且 attr 在 DISALLOWED_SINK_METHODS 中


# ════════════════════════════════════════════════════════════════
# 路径工具函数
# ════════════════════════════════════════════════════════════════
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
    """检查文件路径是否在白名单前缀中(允许直接调用第三方 sink)。

    R67 P1-08: scripts/ 细粒度判断 — 仅 GATE_SCANNERS 可跳过;
    OFFLINE_RECOVERY_TOOLS 与 GOVERNANCE_SCRIPTS 必须被扫描。
    """
    rel = _rel_posix(path)
    for prefix in ALLOWED_PREFIXES:
        if rel == prefix or rel.startswith(prefix):
            return True
    # R67 P1-08: scripts/ 细粒度判断
    if rel.startswith("scripts/") and _is_skippable_script_p1_08(rel):
        return True
    return False


def _is_test_file(path: Path) -> str:
    """检查是否是测试文件(test_*.py 或 *_test.py)。"""
    name = path.name
    return name.startswith("test_") or name.endswith("_test.py")


def _iter_python_files() -> Iterable[Path]:
    """遍历 SCAN_DIRS 下所有 .py 文件(跳过缓存/依赖目录)。"""
    for scan_dir in SCAN_DIRS:
        scan_path = REPO_ROOT / scan_dir
        if not scan_path.is_dir():
            continue
        for py_file in scan_path.rglob("*.py"):
            if _is_skipped_path(py_file):
                continue
            yield py_file


# ════════════════════════════════════════════════════════════════
# AST 检测
# ════════════════════════════════════════════════════════════════
def _find_import_violations(
    tree: ast.AST,
    file_rel: str,
) -> list[tuple[int, str, str]]:
    """Rule 1: 检测第三方 sink 包的 import 违规。

    匹配:
      - ``from telegram import Bot`` / ``from telegram.ext import ...``
        (file_rel 不在 TELEGRAM_IMPORT_ALLOWED_FILES 中)
      - ``from fastapi.responses import JSONResponse``
        (file_rel 不在 JSONRESPONSE_IMPORT_ALLOWED_FILES 中且不在 admin/ 下)

    Returns:
        [(lineno, rule, detail), ...]
    """
    violations: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        # from telegram import ... / from telegram.ext import ...
        if module == "telegram" or module.startswith("telegram."):
            if file_rel not in TELEGRAM_IMPORT_ALLOWED_FILES:
                # 收集导入的 name 列表(如 Bot / Update / ContextTypes)
                names = ", ".join(a.name for a in node.names)
                violations.append((
                    node.lineno,
                    "Rule 1 (import 违规)",
                    f"from {module} import {names} — 只允许在 "
                    f"services/sink_adapters/telegram_adapter.py / "
                    f"utils/flood_waiter.py 中导入 telegram 包",
                ))
        # from fastapi.responses import JSONResponse
        elif module == "fastapi.responses" or module.startswith("fastapi.responses."):
            # 检查是否导入 JSONResponse
            imports_json_response = any(a.name == "JSONResponse" for a in node.names)
            if imports_json_response:
                if file_rel not in JSONRESPONSE_IMPORT_ALLOWED_FILES and not file_rel.startswith("admin/"):
                    violations.append((
                        node.lineno,
                        "Rule 1 (import 违规)",
                        f"from {module} import JSONResponse — 只允许在 "
                        f"services/sink_adapters/web_adapter.py / admin/ 中导入",
                    ))
        # 通用 import: import telegram / import fastapi.responses
        elif isinstance(node, ast.Import):
            # 单独处理 ast.Import(import telegram / import fastapi.responses)
            pass  # ast.ImportFrom 已覆盖 from ... import 形式
    # 单独扫描 ast.Import 节点(``import telegram`` / ``import telegram.ext``)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if name == "telegram" or name.startswith("telegram."):
                    if file_rel not in TELEGRAM_IMPORT_ALLOWED_FILES:
                        violations.append((
                            node.lineno,
                            "Rule 1 (import 违规)",
                            f"import {name} — 只允许在 "
                            f"services/sink_adapters/telegram_adapter.py / "
                            f"utils/flood_waiter.py 中导入 telegram 包",
                        ))
    return violations


def _find_call_violations(tree: ast.AST) -> list[tuple[int, str, str]]:
    """Rule 2: 检测业务模块直接调用原生 sink API。

    匹配 ast.Call 节点:
      - func 为 ast.Attribute 且 attr 在 DISALLOWED_SINK_METHODS 中
        (例如 update.message.reply_text / query.edit_message_text /
         context.bot.send_message / bot.send_message / message.reply_text)

    Returns:
        [(lineno, rule, detail), ...]
    """
    violations: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in DISALLOWED_SINK_METHODS:
            # 构造可读的调用形式(如 update.message.reply_text)
            call_repr = _format_attribute_chain(func)
            violations.append((
                node.lineno,
                "Rule 2 (call 违规)",
                f"{call_repr}(...) — 应改用 services.sink_adapters."
                f"telegram_adapter.safe_*",
            ))
    return violations


def _format_attribute_chain(node: ast.Attribute) -> str:
    """构造可读的属性链字符串(如 ``update.message.reply_text``)。

    Args:
        node: ast.Attribute 节点

    Returns:
        形如 ``a.b.c`` 的字符串(若底层不是 Name/Attribute,用 <expr> 占位)
    """
    parts: list[str] = [node.attr]
    cur = node.value
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    else:
        parts.append("<expr>")
    # 反转后用 . 连接(构造顺序: base.attr1.attr2)
    return ".".join(reversed(parts))


def scan_file(path: Path) -> list[tuple[int, str, str]]:
    """扫描单个 Python 文件,返回 [(lineno, rule, detail), ...] 违规列表。"""
    findings: list[tuple[int, str, str]] = []
    try:
        source = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return findings
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return findings

    rel = _rel_posix(path)
    findings.extend(_find_import_violations(tree, rel))
    findings.extend(_find_call_violations(tree))
    return findings


def collect_findings() -> list[dict]:
    """收集所有文件的违规,返回 [{file, line, rule, detail}, ...]。"""
    all_findings: list[dict] = []
    for py_file in _iter_python_files():
        # 白名单中的文件跳过(允许直接调用第三方 sink)
        if _is_allowed(py_file):
            continue
        file_findings = scan_file(py_file)
        rel = _rel_posix(py_file)
        for lineno, rule, detail in file_findings:
            all_findings.append({
                "file": rel,
                "line": lineno,
                "rule": rule,
                "detail": detail,
            })
    return all_findings


# ════════════════════════════════════════════════════════════════
# Baseline 读取/生成
# ════════════════════════════════════════════════════════════════
def _load_baseline_count(baseline_path: Path | None) -> int:
    """从 baseline 文件读取允许的违规数量。

    Baseline 文件格式(JSON):
        {
          "description": "R64 P1-06 sink import boundary baseline",
          "violation_count": N
        }

    文件不存在或格式错误时返回 0(不允许任何违规)。
    """
    if baseline_path is None or not baseline_path.exists():
        return 0
    try:
        data = json.loads(baseline_path.read_text(encoding="utf-8", errors="ignore"))
        return int(data.get("violation_count", 0))
    except (json.JSONDecodeError, ValueError, TypeError):
        return 0


def _generate_baseline_file(
    baseline_path: Path,
    findings: list[dict],
    current_count: int,
) -> None:
    """生成/更新 baseline 文件。

    若 baseline 文件已存在,保留其描述性字段,只更新 violation_count
    和 violation_samples(供开发者参考)。
    """
    existing_data: dict = {}
    if baseline_path.exists():
        try:
            existing_data = json.loads(
                baseline_path.read_text(encoding="utf-8", errors="ignore")
            )
        except (json.JSONDecodeError, OSError):
            existing_data = {}

    prev_count = int(existing_data.get("violation_count", current_count))

    # 收集违规样本(按 file 分组,每个文件最多 3 条)
    samples_by_file: dict[str, list[dict]] = {}
    for v in findings:
        samples_by_file.setdefault(v["file"], []).append({
            "line": v["line"],
            "rule": v["rule"],
            "detail": v["detail"],
        })
    samples = []
    for file_path, file_findings in sorted(samples_by_file.items()):
        samples.append({
            "file": file_path,
            "count": len(file_findings),
            "examples": file_findings[:3],
        })

    data = {
        "description": (
            "R64 P1-06 sink import boundary baseline — ratchet 模式:每个 commit "
            "只能减少不能增加存量违规;新增违规由 AST 门禁阻断"
        ),
        "note": (
            "本 baseline 记录 bots/services 目录中(排除 sink_adapters/"
            "user_message.py/flood_waiter.py/admin/tests/scripts)对第三方 sink "
            "(telegram send/edit API、fastapi JSONResponse)的直接调用。"
            "迁移目标:全部改用 services/sink_adapters/* typed adapter。"
            "ratchet 策略:violation_count 只能下降不能上升;"
            "--strict 模式忽略 baseline,任何违规都失败(未来目标)。"
        ),
        "version": "R64-P1-06",
        "ratchet_strategy": (
            "ratchet: violation_count 只能减少不能增加;"
            "--strict: 任何违规都失败(忽略 baseline)"
        ),
        "violation_count": current_count,
        "previous_violation_count": prev_count,
        "violation_samples": samples,
    }

    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"✓ Baseline 已生成: {baseline_path}")
    print(f"  violation_count: {current_count} 处 (ratchet)")
    print(f"  violation_samples: {len(samples)} 个文件")
    if current_count < prev_count:
        print(
            f"  ✓ ratchet 下降: {prev_count} → {current_count} "
            f"(减少 {prev_count - current_count} 处)"
        )
    elif current_count > prev_count:
        print(
            f"  ⚠ 警告: 违规增加 {prev_count} → {current_count} "
            f"(新增 {current_count - prev_count} 处)"
        )


# ════════════════════════════════════════════════════════════════
# 修复建议
# ════════════════════════════════════════════════════════════════
def _print_fix_suggestions() -> None:
    """打印修复建议。"""
    print()
    print("修复建议:")
    print("  Rule 1 (import 违规):")
    print("    from telegram import Bot — 移到 services/sink_adapters/telegram_adapter.py")
    print("    from telegram.ext import ... — 移到 services/sink_adapters/telegram_adapter.py")
    print("    from fastapi.responses import JSONResponse — 移到 services/sink_adapters/web_adapter.py")
    print("  Rule 2 (call 违规):")
    print("    update.message.reply_text(...) → sink_adapters.telegram_adapter.safe_reply_text(update, payload)")
    print("    context.bot.send_message(...) → sink_adapters.telegram_adapter.safe_send_message(bot, chat_id, payload)")
    print("    query.edit_message_text(...) → sink_adapters.telegram_adapter.safe_edit_message_text(query, payload)")
    print("    (payload 必须为 UserMessage | ErrorEnvelope,拒绝裸 str)")
    print()
    print("白名单(允许直接调用第三方 sink):")
    for p in ALLOWED_PREFIXES:
        print(f"  - {p}")


# ════════════════════════════════════════════════════════════════
# CLI 入口
# ════════════════════════════════════════════════════════════════
def main() -> int:
    """脚本入口。返回退出码。"""
    parser = argparse.ArgumentParser(
        description="R64 P1-06: Sink import-boundary AST 静态扫描门禁",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "严格模式:任何违规都 exit 1(忽略 baseline)。"
            "未来目标 — 现阶段使用 baseline 模式 ratchet 存量违规"
        ),
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="Baseline 文件路径(读取 violation_count,用于 ratchet)",
    )
    parser.add_argument(
        "--generate-baseline",
        type=Path,
        default=None,
        help="生成/更新 baseline 文件(记录当前违规数,供 ratchet 使用)",
    )
    args = parser.parse_args()

    findings = collect_findings()
    current_count = len(findings)

    # ── 生成 baseline 模式 ──
    if args.generate_baseline is not None:
        _generate_baseline_file(args.generate_baseline, findings, current_count)
        return 0

    # ── strict 模式:任何违规都失败 ──
    if args.strict:
        if findings:
            print(
                f"[FAIL] R64 P1-06 strict 模式:检测到 {current_count} 处 "
                f"sink import-boundary 违规(目标=0):"
            )
            for v in findings[:50]:
                print(f"  - {v['file']}:{v['line']} [{v['rule']}] {v['detail']}")
            if len(findings) > 50:
                print(f"  ... 还有 {len(findings) - 50} 条")
            _print_fix_suggestions()
            return 1
        print("[OK] R64 P1-06 strict 模式通过:无 sink import-boundary 违规")
        return 0

    # ── baseline 模式:ratchet(只能减少不能增加)──
    baseline_count = _load_baseline_count(args.baseline)
    if current_count == 0:
        print(
            "[OK] R64 P1-06 通过:无 sink import-boundary 违规 "
            "(已完全迁移到 typed adapter)"
        )
        return 0
    if current_count <= baseline_count:
        print(
            f"[OK] R64 P1-06 通过:当前违规 {current_count} 处 "
            f"<= baseline {baseline_count} 处 (ratchet)"
        )
        # 打印违规摘要(供开发者参考迁移进度)
        print(f"  违规分布(前 20 条):")
        for v in findings[:20]:
            print(f"  - {v['file']}:{v['line']} [{v['rule']}] {v['detail']}")
        if len(findings) > 20:
            print(f"  ... 还有 {len(findings) - 20} 条")
        return 0

    # 当前违规 > baseline
    print(
        f"[FAIL] R64 P1-06 失败:当前违规 {current_count} 处 > "
        f"baseline {baseline_count} 处 (ratchet 不允许新增违规)"
    )
    print("  新增的违规(前 30 条):")
    for v in findings[:30]:
        print(f"  - {v['file']}:{v['line']} [{v['rule']}] {v['detail']}")
    if len(findings) > 30:
        print(f"  ... 还有 {len(findings) - 30} 条")
    _print_fix_suggestions()
    print()
    print(
        "若为存量违规(尚未在 baseline 中),请先运行 "
        "`python scripts/check_sink_import_boundary.py --generate-baseline "
        "scripts/sink_import_boundary_baseline.json` 更新 baseline"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
