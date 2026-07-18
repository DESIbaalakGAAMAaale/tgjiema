"""临时诊断脚本:对比当前工作区与 HEAD 的硬编码字符串 findings,识别新增违规。

用法:
    python scripts/_tmp_diff_hardcoded.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.scan_hardcoded_strings import (
    collect_findings,
    collect_findings_at_commit,
    _module_for_file,
    classify_finding,
    _violation_key,
)


ROOT = Path(__file__).resolve().parent.parent
HEAD = "HEAD"


def _by_module(findings):
    sets: dict[str, set[str]] = {}
    for file, line, ptype, content in findings:
        m = _module_for_file(file)
        if m is None:
            continue
        sets.setdefault(m, set()).add(_violation_key(file, content))
    return sets


def main():
    print("=" * 80)
    print("扫描当前工作区 (含未提交修改)...")
    cur_findings = collect_findings(ROOT)
    cur_sets = _by_module(cur_findings)
    print(f"  共 {len(cur_findings)} 条 findings")

    print(f"扫描 HEAD={HEAD} (基线)...")
    base_findings = collect_findings_at_commit(ROOT, HEAD)
    base_sets = _by_module(base_findings)
    print(f"  共 {len(base_findings)} 条 findings")

    print("=" * 80)
    print("模块对比 (新增 = 当前 - 基线):")
    all_modules = sorted(set(cur_sets) | set(base_sets))
    new_total = 0
    for m in all_modules:
        cur = cur_sets.get(m, set())
        base = base_sets.get(m, set())
        new = cur - base
        removed = base - cur
        if new or removed:
            print(f"  {m}: cur={len(cur)} base={len(base)} +new={len(new)} -removed={len(removed)}")
            new_total += len(new)
    print(f"  新增总数: {new_total}")

    print("=" * 80)
    print("新增违规明细 (按模块 / 文件 / 行 / 内容):")
    base_keys = {key for m in base_sets for key in base_sets[m]}
    seen = set()
    new_findings = []
    for file, line, ptype, content in cur_findings:
        m = _module_for_file(file)
        if m is None:
            continue
        vkey = _violation_key(file, content)
        if vkey in base_keys:
            continue
        if vkey in seen:
            continue
        seen.add(vkey)
        cls = classify_finding(file, ptype)
        new_findings.append((m, file, line, ptype, cls, content))

    new_findings.sort(key=lambda x: (x[0], x[1], x[2]))
    for m, file, line, ptype, cls, content in new_findings:
        print(f"  [{m}] [{cls}] {file}:{line} ({ptype})")
        print(f"      {content[:200]}")
    print(f"\n新增违规总数: {len(new_findings)}")


if __name__ == "__main__":
    main()
