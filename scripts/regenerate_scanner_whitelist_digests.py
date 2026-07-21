#!/usr/bin/env python3
"""R67 P1-07: 重新生成 scanner 白名单的 AST signature + source digest。

当 ``scripts/check_restore_no_legacy_writer.py`` 的 PRECISE_WHITELIST 条目
因函数源码修改而过期时,运行本脚本计算新的 signature/digest,并打印
可直接粘贴到 PRECISE_WHITELIST 中的字面量。

使用方法:
    # 计算所有白名单条目的新 signature/digest
    python3 scripts/regenerate_scanner_whitelist_digests.py

    # 仅计算指定条目
    python3 scripts/regenerate_scanner_whitelist_digests.py \\
        --file services/db_restore.py --function run_restore

退出码:
    0: 所有条目计算成功
    1: 至少一个条目的函数未找到

注意:本脚本不自动修改 ``check_restore_no_legacy_writer.py`` 文件 —
团队应人工审核变更(确认函数语义未变),再将新 signature/digest 粘贴到
PRECISE_WHITELIST 中。这避免"自动通过"白名单漂移。

当一个文件中有多个同名函数(例如 command_bus.py 中有 14 个 _handler 闭包),
本脚本会列出所有同名函数及其行范围,并标记与白名单当前 signature/digest
匹配的条目。团队根据行范围和 reason 字段人工选择正确的条目。
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_restore_no_legacy_writer as gate_mod  # noqa: E402


def _find_all_function_nodes(
    tree: ast.AST, function_name: str,
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """在 AST 中查找所有名为 function_name 的函数定义。

    R67 P1-07: 一个文件可能有多个同名函数(如 command_bus.py 中
    有 14 个 _handler 闭包,每个 make_*_command 一个)。本函数返回
    全部,由调用方根据行范围/signature/disambiguator 选择正确的一个。
    """
    nodes: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == function_name:
                nodes.append(node)
    return nodes


def _find_parent_function_name(
    node: ast.AST, parent_map: dict[int, ast.AST],
) -> str | None:
    """找到节点的父函数名(用于区分嵌套闭包)。

    对于嵌套函数(如 make_restore_backup_command 内的 _handler),
    返回外层函数名(make_restore_backup_command)。
    对于顶层函数,返回 None。
    """
    current = parent_map.get(id(node))
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.name
        current = parent_map.get(id(current))
    return None


def _build_parent_map(tree: ast.AST) -> dict[int, ast.AST]:
    """构建 parent map。"""
    parent_map: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parent_map[id(child)] = parent
    return parent_map


def _compute_entry(entry: dict) -> list[tuple[str, str, int, int, str | None, bool, bool]] | None:
    """计算白名单条目对应的函数的所有候选 signature/digest。

    当文件中有多个同名函数时,返回所有候选;否则返回单元素列表。

    Returns:
        [(ast_signature, source_digest, lineno, end_lineno, parent_function,
          sig_matches, src_matches), ...] 或 None(文件/函数未找到)
        sig_matches: 当前 signature 是否与 entry["ast_signature"] 匹配
        src_matches: 当前 source_digest 是否与 entry["source_digest"] 匹配
    """
    file_path = REPO_ROOT / entry["file"]
    if not file_path.exists():
        print(f"ERROR: 文件不存在: {file_path}", file=sys.stderr)
        return None
    source = file_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(file_path))
    func_nodes = _find_all_function_nodes(tree, entry["function"])
    if not func_nodes:
        print(
            f"ERROR: 函数 {entry['function']} 未在 {entry['file']} 中找到",
            file=sys.stderr,
        )
        return None
    parent_map = _build_parent_map(tree)
    results = []
    for func_node in func_nodes:
        sig = gate_mod.compute_ast_signature(func_node)
        src = gate_mod.compute_source_digest(func_node, source)
        parent_name = _find_parent_function_name(func_node, parent_map)
        sig_match = sig == entry["ast_signature"]
        src_match = src == entry["source_digest"]
        results.append((
            sig, src,
            func_node.lineno, func_node.end_lineno or 0,
            parent_name,
            sig_match, src_match,
        ))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "R67 P1-07: 重新生成 scanner 白名单的 AST signature + source digest"
        ),
    )
    parser.add_argument(
        "--file",
        help="仅计算指定文件的白名单条目(POSIX 相对路径)",
    )
    parser.add_argument(
        "--function",
        help="仅计算指定函数名的白名单条目",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="仅验证现有 signature/digest 是否仍然匹配,不打印可粘贴字面量",
    )
    args = parser.parse_args()

    entries = gate_mod.PRECISE_WHITELIST
    if args.file:
        entries = [e for e in entries if e["file"] == args.file]
    if args.function:
        entries = [e for e in entries if e["function"] == args.function]

    if not entries:
        print("无匹配的白名单条目。")
        return 1

    if args.verify_only:
        print(f"# R67 P1-07: 验证 {len(entries)} 个白名单条目的 signature/digest")
        print()

    exit_code = 0
    for entry in entries:
        results = _compute_entry(entry)
        if results is None:
            exit_code = 1
            continue

        # R67 P1-07 hotfix: source_digest 是主授权信号(跨 Python 版本稳定)。
        # ast_signature 仅作诊断 — 跨版本失配不视为 STALE。
        # 仅当 source_digest 不匹配时才标记 STALE(函数源码已修改)。
        matching = [r for r in results if r[6]]  # r[6] = src_matches
        if matching:
            current_match = matching[0]
            sig_match = matching[0][5]  # 诊断用
            if sig_match:
                status = "OK"
            else:
                status = "OK (ast_signature 跨版本失配,非阻塞)"
        else:
            current_match = None
            status = "STALE"

        if args.verify_only:
            print(f"# {entry['file']}::{entry['function']}() — {status}")
            if status == "STALE":
                print(f"#   source_digest 不匹配 — 函数源码已修改,需要更新:")
                print(f"#   共找到 {len(results)} 个同名函数候选:")
                for r in results:
                    sig, src, lineno, end_lineno, parent_name, _, _ = r
                    parent_str = f" (parent: {parent_name})" if parent_name else ""
                    print(f"#     lines {lineno}-{end_lineno}{parent_str}:")
                    print(f"#       ast_signature: {sig}")
                    print(f"#       source_digest: {src}")
                print()
                exit_code = 1
            continue

        # 非 verify-only:打印可粘贴字面量
        print(f"# {'=' * 70}")
        print(f"# {entry['file']}::{entry['function']}() — {status}")
        print(f"#   reason: {entry['reason']}")
        print(f"#   allowed_callees: {sorted(entry['allowed_callees'])}")
        print(f"#   共找到 {len(results)} 个同名函数候选")

        if len(results) == 1:
            # 唯一候选,直接打印
            sig, src, lineno, end_lineno, parent_name, _, _ = results[0]
            print(f"#   候选: lines {lineno}-{end_lineno}"
                  f"{f' (parent: {parent_name})' if parent_name else ''}")
            print(f'    "file": "{entry["file"]}",')
            print(f'    "function": "{entry["function"]}",')
            print(f'    "ast_signature": "{sig}",')
            print(f'    "source_digest": "{src}",')
            print(f'    "allowed_callees": frozenset({sorted(entry["allowed_callees"])!r}),')
            print()
        else:
            # 多个候选,列出全部
            print(f"#   多个候选,请根据行范围和 reason 选择正确的:")
            for i, r in enumerate(results):
                sig, src, lineno, end_lineno, parent_name, sig_match, src_match = r
                parent_str = f" (parent: {parent_name})" if parent_name else ""
                match_str = ""
                if sig_match and src_match:
                    match_str = " [CURRENT MATCH]"
                elif sig_match:
                    match_str = " [SIG MATCH]"
                elif src_match:
                    match_str = " [SRC MATCH]"
                print(f"#   候选 {i + 1}: lines {lineno}-{end_lineno}{parent_str}{match_str}")
                print(f'    # "file": "{entry["file"]}",')
                print(f'    # "function": "{entry["function"]}",')
                print(f'    # "ast_signature": "{sig}",')
                print(f'    # "source_digest": "{src}",')
                print(f'    # "allowed_callees": frozenset({sorted(entry["allowed_callees"])!r}),')
                print()

    if exit_code == 0:
        if args.verify_only:
            print("# 所有条目 signature/digest 验证通过。")
        else:
            print("# 所有条目计算成功。")
    else:
        if args.verify_only:
            print("# 部分条目 signature/digest 已过期,需要更新。", file=sys.stderr)
        else:
            print("# 部分条目计算失败(函数未找到),请检查。", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
