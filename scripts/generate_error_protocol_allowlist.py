#!/usr/bin/env python3
"""R62 P1-04: 生成 error_protocol 结构化 allowlist 的迁移脚本。

用途:
    运行现有 scanner (scripts/check_error_protocol.py) 收集当前所有违规,
    为每个 observability 域违规生成结构化 allowlist 条目,输出为 JSON 文件。

    生成的 allowlist 条目包含完整字段:
        file / line / fingerprint / owner / reason / expiry / ticket

    可用于:
        1. 首次为 baseline 生成 allowlist
        2. 违规变化后重新生成 allowlist(新增违规需补全条目;
           已修复违规的条目会被自动剔除)

用法:
    # 生成 allowlist 到默认输出路径(scripts/error_protocol_allowlist.generated.json)
    python scripts/generate_error_protocol_allowlist.py

    # 指定输出路径
    python scripts/generate_error_protocol_allowlist.py --output /path/to/allowlist.json

    # 自定义默认字段值(owner/reason/expiry/ticket)
    python scripts/generate_error_protocol_allowlist.py \\
        --owner maxiuquan \\
        --reason "R62 observability debt - pending refactor" \\
        --expiry 2026-09-30 \\
        --ticket R62-P1-04

    # 同时更新 baseline 文件(将生成的 allowlist 写入 scripts/error_protocol_baseline.json)
    python scripts/generate_error_protocol_allowlist.py --update-baseline

输出 JSON 格式:
    {
      "generated_at": "2026-07-18T...",
      "total_violations": 278,
      "allowlist": [
        {
          "file": "admin/__init__.py",
          "line": 622,
          "fingerprint": "sha256...",
          "owner": "maxiuquan",
          "reason": "R62 observability debt - pending refactor",
          "expiry": "2026-09-30",
          "ticket": "R62-P1-04"
        },
        ...
      ]
    }
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

# 确保 scripts/ 目录在 sys.path 中,以便导入 check_error_protocol
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# 导入 scanner 模块(复用其扫描逻辑和指纹计算函数)
import check_error_protocol as scanner  # noqa: E402

# 默认输出路径
DEFAULT_OUTPUT_PATH = SCRIPTS_DIR / "error_protocol_allowlist.generated.json"
# 默认 baseline 路径(--update-baseline 时使用)
DEFAULT_BASELINE_PATH = SCRIPTS_DIR / "error_protocol_baseline.json"


def generate_allowlist(
    *,
    owner: str = scanner.DEFAULT_ALLOWLIST_OWNER,
    reason: str = scanner.DEFAULT_ALLOWLIST_REASON,
    expiry: str = scanner.DEFAULT_ALLOWLIST_EXPIRY,
    ticket: str = scanner.DEFAULT_ALLOWLIST_TICKET,
) -> tuple[list[dict], int]:
    """运行 scanner 收集违规,生成结构化 allowlist 条目列表。

    Args:
        owner: allowlist 条目默认 owner
        reason: allowlist 条目默认 reason
        expiry: allowlist 条目默认 expiry (ISO 日期字符串 YYYY-MM-DD)
        ticket: allowlist 条目默认 ticket

    Returns:
        (allowlist_entries, total_count)
        allowlist_entries: 结构化 allowlist 条目列表(仅 observability 域)
        total_count: scanner 发现的违规总数(含零容忍域,若有)
    """
    # 校验 expiry 格式(R62 P1-04: 必须是合法 ISO 日期)
    try:
        date.fromisoformat(expiry)
    except ValueError as exc:
        raise ValueError(
            f"expiry 必须是 ISO 日期格式 (YYYY-MM-DD),收到: {expiry!r}"
        ) from exc

    # 复用 scanner 的 collect_findings 收集所有违规
    findings = scanner.collect_findings()
    total_count = len(findings)

    # 仅 observability 域违规加入 allowlist
    # 零容忍域(security/destructive/data-integrity/financial)违规不应被 allowlist
    # —— 它们必须直接修复
    allowlist: list[dict] = []
    for file_path, line_no, detail in findings:
        domain = scanner._classify_domain(file_path)
        if domain == "observability":
            entry = scanner._build_allowlist_entry(
                file_path, line_no, detail,
                owner=owner, reason=reason, expiry=expiry, ticket=ticket,
            )
            allowlist.append(entry)

    return allowlist, total_count


def write_allowlist_json(
    output_path: Path,
    allowlist: list[dict],
    total_count: int,
) -> None:
    """将 allowlist 写入 JSON 文件(包含元数据)。"""
    data = {
        "description": (
            "R62 P1-04 generated allowlist (由 generate_error_protocol_allowlist.py 生成)。"
            "每条记录对应一处 observability 域违规,包含 file/line/fingerprint/"
            "owner/reason/expiry/ticket 字段。复制到 scripts/error_protocol_baseline.json "
            "的 domains.observability.allowlist 字段使用。"
        ),
        "generated_at": date.today().isoformat(),
        "total_violations": total_count,
        "allowlist_count": len(allowlist),
        "allowlist": allowlist,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def update_baseline_allowlist(
    baseline_path: Path,
    allowlist: list[dict],
    total_count: int,
) -> None:
    """将生成的 allowlist 写入 baseline 文件的 observability.allowlist 字段。

    保留 baseline 的其他配置(domain paths / descriptions 等),只更新:
        - domains.observability.allowlist
        - domains.observability.max_violations = 0 (目标)
        - violation_count = total_count (ratchet)
        - previous_violation_count = 旧 violation_count
    """
    # 读取已有 baseline
    existing_data: dict = {}
    if baseline_path.exists():
        try:
            existing_data = json.loads(
                baseline_path.read_text(encoding="utf-8", errors="ignore")
            )
        except (json.JSONDecodeError, OSError):
            existing_data = {}

    prev_count = int(existing_data.get("violation_count", total_count))

    # 更新 observability.allowlist
    domains_cfg = existing_data.get("domains", {})
    obs_cfg = domains_cfg.get("observability", {})
    obs_cfg["allowlist"] = allowlist
    obs_cfg["allowlist_required"] = True
    obs_cfg["max_violations"] = 0  # R62 P1-04: max_violations 已弃用,设为 0 (目标)
    domains_cfg["observability"] = obs_cfg

    data = {
        "description": (
            "R62 P1-04 error protocol baseline (结构化 allowlist + ratchet, "
            "observability 目标 real_violations=0)"
        ),
        "note": (
            "R62 P1-04: observability 域使用结构化 allowlist(file/line/fingerprint/"
            "owner/reason/expiry/ticket)。每个违规必须匹配 allowlist 条目且未过期。"
            "max_violations 已弃用(仅保留用于向后兼容)。"
            "violation_count 用于 ratchet:每个 commit 只能减少不能增加。"
            "由 generate_error_protocol_allowlist.py --update-baseline 更新。"
        ),
        "version": "R62-P1-04",
        "ratchet_strategy": (
            "structured-allowlist: real_violations must == 0 (every violation "
            "must be in allowlist with valid expiry); "
            "ratchet: total_violations <= violation_count"
        ),
        "domains": domains_cfg,
        "total_max_violations": 0,
        "violation_count": total_count,
        "previous_violation_count": prev_count,
    }

    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    """脚本入口。"""
    parser = argparse.ArgumentParser(
        description=(
            "R62 P1-04: 运行 scanner 收集违规,生成结构化 allowlist JSON 文件。"
            "可用于首次生成或违规变化后重新生成 allowlist。"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help=f"输出 JSON 文件路径 (默认: {DEFAULT_OUTPUT_PATH})",
    )
    parser.add_argument(
        "--owner",
        default=scanner.DEFAULT_ALLOWLIST_OWNER,
        help=f"allowlist 条目默认 owner (默认: {scanner.DEFAULT_ALLOWLIST_OWNER})",
    )
    parser.add_argument(
        "--reason",
        default=scanner.DEFAULT_ALLOWLIST_REASON,
        help=f"allowlist 条目默认 reason (默认: {scanner.DEFAULT_ALLOWLIST_REASON!r})",
    )
    parser.add_argument(
        "--expiry",
        default=scanner.DEFAULT_ALLOWLIST_EXPIRY,
        help=(
            f"allowlist 条目默认 expiry (ISO 日期 YYYY-MM-DD, "
            f"默认: {scanner.DEFAULT_ALLOWLIST_EXPIRY})"
        ),
    )
    parser.add_argument(
        "--ticket",
        default=scanner.DEFAULT_ALLOWLIST_TICKET,
        help=f"allowlist 条目默认 ticket (默认: {scanner.DEFAULT_ALLOWLIST_TICKET})",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help=(
            "同时更新 baseline 文件的 observability.allowlist 字段"
            f"(默认 baseline: {DEFAULT_BASELINE_PATH})"
        ),
    )
    parser.add_argument(
        "--baseline-path",
        type=Path,
        default=DEFAULT_BASELINE_PATH,
        help=f"--update-baseline 时的 baseline 文件路径 (默认: {DEFAULT_BASELINE_PATH})",
    )
    args = parser.parse_args()

    # 生成 allowlist
    allowlist, total_count = generate_allowlist(
        owner=args.owner,
        reason=args.reason,
        expiry=args.expiry,
        ticket=args.ticket,
    )

    # 写入输出 JSON
    write_allowlist_json(args.output, allowlist, total_count)
    print(f"✓ Allowlist 已生成: {args.output}")
    print(f"  总违规数: {total_count}")
    print(f"  observability allowlist 条目数: {len(allowlist)}")
    print(f"  默认 owner:  {args.owner}")
    print(f"  默认 reason: {args.reason}")
    print(f"  默认 expiry: {args.expiry}")
    print(f"  默认 ticket: {args.ticket}")

    # 可选:更新 baseline 文件
    if args.update_baseline:
        update_baseline_allowlist(args.baseline_path, allowlist, total_count)
        print()
        print(f"✓ Baseline 已更新: {args.baseline_path}")
        print(f"  violation_count: {total_count}")
        print(f"  observability.allowlist: {len(allowlist)} 条")
        prev_count = total_count  # 简化:实际值在 update_baseline_allowlist 内读取
        print("  提示: 运行 scanner 验证: "
              f"python scripts/check_error_protocol.py --baseline {args.baseline_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
