#!/usr/bin/env python3
"""R64 P1-12: 生产运行证据统一生成入口。

本脚本是 P1-12 生产运行证据框架的统一入口,编排以下 5 类证据生成:
    1. soak_test_7day.sh        — 7 天多实例 soak 测试
    2. blank_vps_recovery_test.sh — 3 轮空白 VPS 恢复测试
    3. chaos_bot_fault_injection.sh — 真实 provider outbox 故障注入
    4. ru_72h_verification.sh   — 72 小时 RU 空载验证
    5. verify_supply_chain.py   — 相同 digest 供应链验证

所有证据输出到 production-evidence/ 目录(可通过 --output-dir 覆盖),
并生成统一索引文件 production_evidence_index.json 用于审计回溯。

使用方法:
    # 生成全部证据(production 模式,需要真实环境)
    python scripts/generate_production_evidence.py --all

    # 只生成 soak 测试证据(--dry-run 仅编排不执行)
    python scripts/generate_production_evidence.py --soak --dry-run

    # 指定输出目录
    python scripts/generate_production_evidence.py --all --output-dir /tmp/evidence

    # 跳过某些证据(避免长时间运行)
    python scripts/generate_production_evidence.py --all --skip soak --skip ru_72h

    # 列出可用证据类型
    python scripts/generate_production_evidence.py --list

退出码:
    0: 所有调用的证据脚本成功完成
    1: 至少一个证据脚本失败(查看日志详情)
    2: 参数错误或环境不可用

R64 P1-12 验收标准:
    - 7 天多实例 soak 测试报告(包含故障矩阵 196 次注入,0 一致性违规)
    - 3 轮空白 VPS 恢复测试(连续 PASS,RPO/RTO 满足阈值)
    - 真实 provider outbox 故障注入(28 组合 × 7 cycle = 196 次,RTO ≤ 60s)
    - 72 小时 RU 空载验证(Bot 0 RU/天,集群 ≤100 RU/天)
    - 相同 digest 供应链验证(镜像 digest 与 SBOM/签名一致)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# 将项目根目录加入 sys.path
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ─── 证据类型定义 ──────────────────────────────────────────────

EVIDENCE_TYPES = {
    "soak": {
        "description": "7 天多实例 soak 测试(R55 §21)",
        "script": "scripts/soak_test_7day.sh",
        "required_args": [],  # 无必填参数(dry-run 模式)
        "production_args": [],  # production 模式参数
        "estimated_duration_minutes": 7 * 24 * 60,  # 7 天
        "report_glob": "soak_report_*.json",
    },
    "vps_recovery": {
        "description": "3 轮空白 VPS 恢复测试(R55 §22)",
        "script": "scripts/blank_vps_recovery_test.sh",
        "required_args": ["--commit-sha", "--backup-id",
                          "--approval-action-id", "--test-chat-id"],
        "production_args": [],
        "estimated_duration_minutes": 90,  # 约 1.5 小时
        "report_glob": "vps_recovery_test_report_*.json",
    },
    "chaos": {
        "description": "真实 provider outbox 故障注入(R55 §20)",
        "script": "scripts/chaos_bot_fault_injection.sh",
        "required_args": ["--bot", "--scenario"],
        "production_args": [],
        "estimated_duration_minutes": 30,  # 28 组合 × 30s + RTO 验证
        "report_glob": "chaos_report_*.json",
    },
    "ru_72h": {
        "description": "72 小时 RU 空载验证(R64 P1-10)",
        "script": "scripts/ru_72h_verification.sh",
        "required_args": [],
        "production_args": ["--hours", "72"],
        "estimated_duration_minutes": 72 * 60,  # 72 小时
        "report_glob": "ru_72h_report_*.json",
    },
    "supply_chain": {
        "description": "相同 digest 供应链验证(R64 P1-12)",
        "script": "scripts/verify_supply_chain.py",
        "required_args": [],
        "production_args": [],
        "estimated_duration_minutes": 5,
        "report_glob": "supply_chain_report_*.json",
    },
}


def _now_iso() -> str:
    """当前 UTC 时间 ISO8601 字符串。"""
    return datetime.now(timezone.utc).isoformat()


def _safe_relative_to(path: Path, base: Path) -> Path:
    """安全计算 path 相对 base 的路径,若 path 不在 base 下则返回 path 本身。

    R64 P1-12: output_dir 可能位于 _REPO_ROOT 之外(如 /tmp/evidence),
    此时不调用 relative_to(会抛 ValueError),而是返回绝对路径。
    """
    try:
        return path.relative_to(base)
    except ValueError:
        return path


def _list_evidence_types() -> None:
    """打印所有可用证据类型。"""
    print("可用证据类型:")
    print("─" * 70)
    for name, info in EVIDENCE_TYPES.items():
        print(f"  {name:<15} — {info['description']}")
        print(f"  {'':<15}   脚本: {info['script']}")
        print(f"  {'':<15}   必填参数: {', '.join(info['required_args']) or '(无)'}")
        print(f"  {'':<15}   预估时长: {info['estimated_duration_minutes']} 分钟")
        print()


def _build_evidence_command(
    evidence_type: str,
    output_dir: Path,
    dry_run: bool,
    extra_args: list[str],
) -> list[str]:
    """构建证据生成脚本的命令行。"""
    info = EVIDENCE_TYPES[evidence_type]
    script_path = _REPO_ROOT / info["script"]
    cmd = ["bash", str(script_path)] if script_path.suffix == ".sh" else \
          ["python3", str(script_path)]
    cmd.extend(["--output-dir", str(output_dir)])
    if dry_run and evidence_type in ("soak", "chaos"):
        cmd.append("--dry-run")
    if evidence_type == "ru_72h":
        # ru_72h_verification.sh 已有 --output-dir,生产模式 --hours 72
        cmd.extend(info["production_args"])
    if evidence_type == "supply_chain":
        # verify_supply_chain.py 的 --output-dir 与 --json 由脚本本身处理
        cmd.extend(["--json"])
    cmd.extend(extra_args)
    return cmd


def _find_latest_report(output_dir: Path, evidence_type: str) -> Path | None:
    """在输出目录中查找最新的证据报告文件。"""
    import glob
    pattern = EVIDENCE_TYPES[evidence_type]["report_glob"]
    matches = sorted(output_dir.glob(pattern))
    if not matches:
        return None
    return matches[-1]


def _run_evidence(
    evidence_type: str,
    output_dir: Path,
    dry_run: bool,
    extra_args: list[str],
    timeout_seconds: int = 0,
) -> dict:
    """运行单个证据生成脚本。

    Returns:
        {
            "evidence_type": str,
            "script": str,
            "command": list[str],
            "started_at": str,
            "completed_at": str,
            "duration_seconds": float,
            "exit_code": int,
            "stdout_tail": str,    # 最后 1000 字符
            "stderr_tail": str,
            "report_path": str | None,
            "report_size_bytes": int,
            "status": "passed" | "failed" | "skipped" | "error",
            "error": str | None,
        }
    """
    info = EVIDENCE_TYPES[evidence_type]
    script_path = _REPO_ROOT / info["script"]
    started_at = _now_iso()
    start_ts = time.time()

    result = {
        "evidence_type": evidence_type,
        "script": info["script"],
        "command": [],
        "started_at": started_at,
        "completed_at": "",
        "duration_seconds": 0.0,
        "exit_code": -1,
        "stdout_tail": "",
        "stderr_tail": "",
        "report_path": None,
        "report_size_bytes": 0,
        "status": "error",
        "error": None,
    }

    if not script_path.exists():
        result["error"] = f"脚本不存在: {script_path}"
        result["status"] = "skipped"
        result["completed_at"] = _now_iso()
        return result

    cmd = _build_evidence_command(evidence_type, output_dir, dry_run, extra_args)
    result["command"] = cmd

    try:
        # 设置最小环境变量(supply_chain.py 可能需要)
        env = os.environ.copy()
        if not env.get("SERVICE_ROLE"):
            env["SERVICE_ROLE"] = "prometheus_exporter"

        # timeout_seconds=0 表示不超时(实际生产证据生成可能数天)
        timeout_arg = timeout_seconds if timeout_seconds > 0 else None
        proc = subprocess.run(
            cmd,
            cwd=str(_REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_arg,
        )
        result["exit_code"] = proc.returncode
        result["stdout_tail"] = proc.stdout[-1000:] if proc.stdout else ""
        result["stderr_tail"] = proc.stderr[-1000:] if proc.stderr else ""
        result["status"] = "passed" if proc.returncode == 0 else "failed"
    except subprocess.TimeoutExpired as e:
        result["error"] = f"超时({timeout_seconds}s)"
        result["status"] = "failed"
        result["stdout_tail"] = (e.stdout or "")[-1000:] if isinstance(e.stdout, str) else ""
        result["stderr_tail"] = (e.stderr or "")[-1000:] if isinstance(e.stderr, str) else ""
    except Exception as e:
        result["error"] = f"执行异常: {e}"
        result["status"] = "error"

    result["completed_at"] = _now_iso()
    result["duration_seconds"] = round(time.time() - start_ts, 2)

    # 查找生成的报告文件
    report_path = _find_latest_report(output_dir, evidence_type)
    if report_path and report_path.exists():
        # R64 P1-12: report_path 可能位于 _REPO_ROOT 之外(如 /tmp),
        # 使用 try/except 兼容两种场景(相对路径或绝对路径)
        try:
            result["report_path"] = str(report_path.relative_to(_REPO_ROOT))
        except ValueError:
            # output_dir 不在 _REPO_ROOT 下,使用绝对路径
            result["report_path"] = str(report_path)
        result["report_size_bytes"] = report_path.stat().st_size

    return result


async def generate_evidence(
    evidence_types: list[str],
    output_dir: Path,
    dry_run: bool,
    extra_args_map: dict[str, list[str]],
    timeout_seconds: int = 0,
) -> dict:
    """生成多个证据,返回汇总报告。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    started_at = _now_iso()
    start_ts = time.time()

    results = []
    for et in evidence_types:
        print(f"\n{'═' * 70}")
        print(f"生成证据: {et} — {EVIDENCE_TYPES[et]['description']}")
        print(f"{'─' * 70}")
        extra = extra_args_map.get(et, [])
        r = _run_evidence(et, output_dir, dry_run, extra, timeout_seconds)
        results.append(r)
        print(f"  状态: {r['status']}, 退出码: {r['exit_code']}, "
              f"耗时: {r['duration_seconds']}s")
        if r["report_path"]:
            print(f"  报告: {r['report_path']} ({r['report_size_bytes']} bytes)")
        if r["error"]:
            print(f"  错误: {r['error']}")

    completed_at = _now_iso()
    duration = round(time.time() - start_ts, 2)

    # 汇总状态
    all_passed = all(r["status"] == "passed" for r in results)
    any_failed = any(r["status"] == "failed" for r in results)

    summary = {
        "schema_version": "r64_p1_12_v1",
        "generated_at": completed_at,
        "started_at": started_at,
        "duration_seconds": duration,
        # R64 P1-12: output_dir 可能位于 _REPO_ROOT 之外(如 /tmp),
        # 使用 try/except 兼容两种场景(相对路径或绝对路径)
        "output_dir": str(_safe_relative_to(output_dir, _REPO_ROOT)),
        "dry_run": dry_run,
        "evidence_count": len(results),
        "passed_count": sum(1 for r in results if r["status"] == "passed"),
        "failed_count": sum(1 for r in results if r["status"] == "failed"),
        "skipped_count": sum(1 for r in results if r["status"] == "skipped"),
        "error_count": sum(1 for r in results if r["status"] == "error"),
        "overall_status": "passed" if all_passed else (
            "failed" if any_failed else "partial"
        ),
        "results": results,
    }

    # 写入索引文件
    index_path = output_dir / "production_evidence_index.json"
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\n{'═' * 70}")
    print(f"证据索引: {index_path}")
    print(f"总状态: {summary['overall_status']} "
          f"(passed={summary['passed_count']}, failed={summary['failed_count']})")

    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="R64 P1-12: 生产运行证据统一生成入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--all", action="store_true",
        help="生成全部 5 类证据",
    )
    parser.add_argument(
        "--soak", action="store_true",
        help="生成 7 天 soak 测试证据",
    )
    parser.add_argument(
        "--vps-recovery", action="store_true",
        help="生成 3 轮空白 VPS 恢复测试证据",
    )
    parser.add_argument(
        "--chaos", action="store_true",
        help="生成故障注入证据",
    )
    parser.add_argument(
        "--ru-72h", action="store_true",
        help="生成 72 小时 RU 空载验证证据",
    )
    parser.add_argument(
        "--supply-chain", action="store_true",
        help="生成供应链验证证据",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="列出可用证据类型并退出",
    )
    parser.add_argument(
        "--output-dir", default="production-evidence",
        help="证据输出目录(默认 production-evidence/)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="dry-run 模式(仅 soak/chaos 支持,只编排不执行真实故障)",
    )
    parser.add_argument(
        "--skip", action="append", default=[],
        choices=list(EVIDENCE_TYPES.keys()),
        help="跳过指定证据类型(可多次指定)",
    )
    parser.add_argument(
        "--timeout-seconds", type=int, default=0,
        help="单个证据脚本超时秒数(0=不超时,生产环境可能需要数天)",
    )
    parser.add_argument(
        "--soak-arg", action="append", default=[],
        help="传递给 soak 脚本的额外参数(可多次指定)",
    )
    parser.add_argument(
        "--vps-recovery-arg", action="append", default=[],
        help="传递给 vps_recovery 脚本的额外参数",
    )
    parser.add_argument(
        "--chaos-arg", action="append", default=[],
        help="传递给 chaos 脚本的额外参数",
    )
    parser.add_argument(
        "--ru-72h-arg", action="append", default=[],
        help="传递给 ru_72h 脚本的额外参数",
    )
    parser.add_argument(
        "--supply-chain-arg", action="append", default=[],
        help="传递给 supply_chain 脚本的额外参数",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.list:
        _list_evidence_types()
        return 0

    # 决定要运行的证据类型
    if args.all:
        selected = list(EVIDENCE_TYPES.keys())
    else:
        selected = []
        if args.soak:
            selected.append("soak")
        if args.vps_recovery:
            selected.append("vps_recovery")
        if args.chaos:
            selected.append("chaos")
        if args.ru_72h:
            selected.append("ru_72h")
        if args.supply_chain:
            selected.append("supply_chain")

    if not selected:
        print("ERROR: 至少指定一种证据类型(--all / --soak / --vps-recovery / "
              "--chaos / --ru-72h / --supply-chain)", file=sys.stderr)
        return 2

    # 应用 --skip
    selected = [s for s in selected if s not in args.skip]
    if not selected:
        print("WARN: 所有证据类型都被 --skip 跳过,无操作", file=sys.stderr)
        return 0

    # 构建额外参数映射
    extra_args_map = {
        "soak": args.soak_arg,
        "vps_recovery": args.vps_recovery_arg,
        "chaos": args.chaos_arg,
        "ru_72h": args.ru_72h_arg,
        "supply_chain": args.supply_chain_arg,
    }

    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = _REPO_ROOT / output_dir

    print(f"R64 P1-12: 生产运行证据生成")
    print(f"输出目录: {output_dir}")
    print(f"证据类型: {', '.join(selected)}")
    print(f"Dry-run: {args.dry_run}")

    summary = asyncio.run(generate_evidence(
        evidence_types=selected,
        output_dir=output_dir,
        dry_run=args.dry_run,
        extra_args_map=extra_args_map,
        timeout_seconds=args.timeout_seconds,
    ))

    # 退出码:有任何 failed 返回 1
    if summary["failed_count"] > 0 or summary["error_count"] > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
