#!/usr/bin/env python3
"""R67 P1-10: skip inventory 正式化门禁 — 强制 skip 测试具备 owner/category/due_date。

整改背景(R67 终审报告 P1-10):
    本轮提交声明 `6387 passed, 139 skipped`。生成 skip 清单:
    测试名、原因、owner、到期日、对应生产风险。
    Telegram、R2、CRDB、restore、security、deployment 相关 skip
    在上线前归零。

本脚本的范围与边界(诚实声明):
    1. 本脚本是**清单门禁**,不是 skip 归零器。
       真正的归零需要逐个修复或显式标注"接受风险并归档"(每个含
       owner + due_date + 风险等级)。本脚本强制每个 skip 必须满足
       这些元数据要求,否则 CI 失败。

    2. 本脚本调用 `scripts/collect_skip_inventory.py` 生成清单,
       然后验证以下规则:
         (a) 每个 skip 必须有 category(非 'uncategorized')
         (b) 每个 skip 必须有 owner(非 'unassigned')
         (c) 高生产影响路径(high)的 skip 必须有 due_date
             (Telegram/R2/CRDB/restore/security/deployment)
         (d) skip 总数不得超过 baseline(初始为 448;每 PR 只能减少不能增加)

    3. baseline 机制:skip 总数通过 baseline ratchet,鼓励下降但禁止上升。
       baseline 文件:scripts/skip_inventory_baseline.json

CI 调用方式:
    python scripts/check_skip_inventory_gate.py

退出码:
    0 — 所有规则通过
    1 — 检测到违规
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

# 项目根目录(scripts/ 的上一级)
REPO_ROOT = Path(__file__).resolve().parent.parent

# 依赖的清单生成器
INVENTORY_SCRIPT = REPO_ROOT / "scripts" / "collect_skip_inventory.py"

# baseline 文件(记录允许的最大 skip 总数)
BASELINE_FILE = REPO_ROOT / "scripts" / "skip_inventory_baseline.json"

# 初始 baseline(首次运行,从当前仓库状态计算并写入)
# 注:R67 上线前的目标是逐步下降到 0(尤其 high-impact 路径归零)
INITIAL_BASELINE_COUNT = 448  # 见 collect_skip_inventory.py 输出


# 高生产影响路径(必须 due_date)
HIGH_IMPACT_KEYWORDS: tuple[str, ...] = (
    "bots/",
    "services/restore",
    "services/backup",
    "services/db_backup",
    "services/db_restore",
    "services/command_bus",
    "services/approval",
    "services/mfa",
    "services/notifications",
    "services/r2",
    "services/crdb",
    "services/security",
    "database/migrate",
    "scripts/check_",
    "scripts/verify_",
    ".github/workflows",
    "admin/mfa",
    "admin/passwords",
)


def _is_high_impact(file_path: str) -> bool:
    """检查文件路径是否属于高生产影响路径。"""
    for kw in HIGH_IMPACT_KEYWORDS:
        if file_path.startswith(kw):
            return True
    return False


def _load_baseline() -> int:
    """加载 baseline 文件,返回允许的 skip 总数上限。

    文件不存在时返回 INITIAL_BASELINE_COUNT 并自动创建。
    """
    if not BASELINE_FILE.exists():
        # 首次运行:写入初始 baseline
        data = {
            "description": (
                "R67 P1-10 skip inventory baseline — ratchet 模式:"
                "每个 commit 只能减少不能增加 skip 总数"
            ),
            "skip_count": INITIAL_BASELINE_COUNT,
            "note": (
                "初始 baseline 来自 R67 上线前快照。目标是逐步下降,"
                "high-impact 路径(restore/security/deployment/Telegram/CRDB/R2)"
                "上线前归零。"
            ),
        }
        BASELINE_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return INITIAL_BASELINE_COUNT
    try:
        data = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
        return int(data.get("skip_count", INITIAL_BASELINE_COUNT))
    except (json.JSONDecodeError, ValueError, TypeError):
        return INITIAL_BASELINE_COUNT


def _generate_inventory(output_path: Path) -> dict[str, Any]:
    """调用 collect_skip_inventory.py 生成清单,返回 JSON dict。"""
    cmd = [
        sys.executable,
        str(INVENTORY_SCRIPT),
        "--output", str(output_path),
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"collect_skip_inventory.py 执行失败(exit={result.returncode}):\n"
            f"stderr: {result.stderr}"
        )
    return json.loads(output_path.read_text(encoding="utf-8"))


def check(
    *,
    output_path: Path | None = None,
) -> tuple[int, list[str]]:
    """主校验流程。

    Returns:
        (exit_code, violations)
        exit_code: 0=无违规,1=有违规
    """
    if not INVENTORY_SCRIPT.is_file():
        return 1, [f"清单生成器不存在: {INVENTORY_SCRIPT}"]

    # 1. 生成清单
    if output_path is None:
        output_path = REPO_ROOT / "scripts" / "skip_inventory_latest.json"
    try:
        inventory = _generate_inventory(output_path)
    except Exception as e:
        return 1, [f"生成清单失败: {e}"]

    skips: list[dict[str, Any]] = inventory.get("skips", [])
    summary = inventory.get("summary", {})

    total = int(summary.get("total", len(skips)))
    by_category: dict[str, int] = summary.get("by_category", {})
    uncategorized = int(by_category.get("uncategorized", 0))

    # 2. 验证规则
    violations: list[str] = []

    # (a) 每个 skip 必须有 category(非 'uncategorized')
    if uncategorized > 0:
        # 列出前 5 个 uncategorized 样本
        uncats = [s for s in skips if s.get("category") == "uncategorized"]
        violations.append(
            f"rule(a): {uncategorized} 个 skip 类别为 'uncategorized' "
            f"(必须有明确类别)。样本(前 5):"
        )
        for s in uncats[:5]:
            violations.append(
                f"  - {s.get('file_path')}::{s.get('test_name')} "
                f"(line {s.get('line')}): {s.get('reason', '')[:80]}"
            )

    # (b) 每个 skip 必须有 owner(非 'unassigned')
    unowned = [s for s in skips if s.get("owner") in ("", "unassigned", None)]
    if unowned:
        violations.append(
            f"rule(b): {len(unowned)} 个 skip 缺少 owner。样本(前 5):"
        )
        for s in unowned[:5]:
            violations.append(
                f"  - {s.get('file_path')}::{s.get('test_name')}"
            )

    # (c) 高生产影响路径的 skip 必须有 due_date
    high_impact_missing_due = [
        s for s in skips
        if _is_high_impact(s.get("file_path", ""))
        and not s.get("due_date", "")
    ]
    if high_impact_missing_due:
        violations.append(
            f"rule(c): {len(high_impact_missing_due)} 个高生产影响路径 skip "
            f"缺少 due_date(Telegram/R2/CRDB/restore/security/deployment)"
            f"。样本(前 5):"
        )
        for s in high_impact_missing_due[:5]:
            violations.append(
                f"  - {s.get('file_path')}::{s.get('test_name')} "
                f"[owner={s.get('owner')}]"
            )

    # (d) skip 总数不得超过 baseline
    baseline_count = _load_baseline()
    if total > baseline_count:
        violations.append(
            f"rule(d): skip 总数 {total} 超过 baseline {baseline_count} "
            f"(+{total - baseline_count}) — 每 commit 只能减少不能增加"
        )

    if violations:
        print(
            f"[FAIL] R67 P1-10 skip inventory 门禁检测到 "
            f"{len(violations)} 处违规:"
        )
        for v in violations:
            print(v)
        print()
        print(f"清单已写入: {output_path}")
        print(f"baseline: {BASELINE_FILE} (skip_count={baseline_count})")
        return 1, violations

    # 打印汇总
    print(
        f"[OK] R67 P1-10 skip inventory 门禁通过 "
        f"(total={total}, baseline={baseline_count}, "
        f"uncategorized={uncategorized}, high_impact_missing_due=0)"
    )
    print(f"清单已写入: {output_path}")
    if total < baseline_count:
        print(
            f"  skip 总数已下降({baseline_count} → {total}),"
            f"建议运行 --ratchet 更新 baseline"
        )
    return 0, violations


def main(argv: list[str] | None = None) -> None:
    """脚本入口。"""
    parser = argparse.ArgumentParser(
        description="R67 P1-10: skip inventory 正式化门禁",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="清单输出路径(默认: scripts/skip_inventory_latest.json)",
    )
    args = parser.parse_args(argv)
    exit_code, _ = check(output_path=args.output)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
