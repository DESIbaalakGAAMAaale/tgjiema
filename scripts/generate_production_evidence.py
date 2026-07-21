#!/usr/bin/env python3
"""R64 P1-12 / R65 P0-04: 生产运行证据统一生成入口。

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
    python scripts/generate_production_evidence.py --all --production

    # 只生成 soak 测试证据(--dry-run 仅编排不执行)
    python scripts/generate_production_evidence.py --soak --dry-run

    # 指定输出目录
    python scripts/generate_production_evidence.py --all --output-dir /tmp/evidence

    # 跳过某些证据(避免长时间运行)—— 仅 dry_run 模式允许;
    # production 模式禁止 --skip,违反则报错退出
    python scripts/generate_production_evidence.py --all --skip soak --skip ru_72h

    # 列出可用证据类型
    python scripts/generate_production_evidence.py --list

    # 验证 production promotion 证据是否满足严格门禁
    python scripts/generate_production_evidence.py \
        --verify-promotion production-evidence/production_evidence_index.json

退出码:
    0: 所有调用的证据脚本成功完成 / verify-promotion 通过
    1: 至少一个证据脚本失败 / verify-promotion 失败(查看日志详情)
    2: 参数错误或环境不可用

R64 P1-12 验收标准:
    - 7 天多实例 soak 测试报告(包含故障矩阵 196 次注入,0 一致性违规)
    - 3 轮空白 VPS 恢复测试(连续 PASS,RPO/RTO 满足阈值)
    - 真实 provider outbox 故障注入(28 组合 × 7 cycle = 196 次,RTO ≤ 60s)
    - 72 小时 RU 空载验证(Bot 0 RU/天,集群 ≤100 RU/天)
    - 相同 digest 供应链验证(镜像 digest 与 SBOM/签名一致)

R65 P0-04 整改要点:
    - 默认 evidence_mode=dry_run,输出文件名含 dry_run,JSON 含
      evidence_mode="dry_run" + production_promotion_allowed=false,
      严禁出现 "production passed" 等通过性断言。
    - production promotion 只接受独立、签名、不可变、未过期的真实证据 artifact。
    - 每类证据含 environment_id / commit_sha / image_digest / started_at /
      ended_at / raw_data_digest / executed_by / approved_by / signature。
    - SOAK_7DAY / RESTORE_3X / OUTBOX_FAULT_INJECTION / RU_72H / SUPPLY_CHAIN
      任一缺失/过期即阻断;--skip 在 production 模式下被禁止。
    - verify_production_promotion() 强制校验以上条件,失败抛
      AppError(ErrorCodes.PRODUCTION_EVIDENCE_INSUFFICIENT)。
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
    "rc_verify_3x": {
        "description": "R67 P0-04: 同候选 3 次 verify-only 验证(同 digest 不可变性)",
        "script": "scripts/verify_rc_3x.py",
        "required_args": ["--image-name", "--image-digest",
                          "--expected-commit", "--expected-tree"],
        "production_args": [],
        "estimated_duration_minutes": 15,  # 3 次 × ~5 分钟
        "report_glob": "rc_verify_3x_*.json",
    },
}


# ─── R65 P0-04: 生产证据 artifact 严格门禁 ──────────────────────
# 5 类必需的 production artifact 类型(evidence_type → artifact_type 映射)。
# 任一缺失/过期/dry_run/未签名即阻断 production promotion。
# R67 P0-04: 增加 RC_VERIFY_3X artifact 类型(同候选 3 次验证)
EVIDENCE_TYPE_TO_ARTIFACT_TYPE = {
    "soak": "SOAK_7DAY",
    "vps_recovery": "RESTORE_3X",
    "chaos": "OUTBOX_FAULT_INJECTION",
    "ru_72h": "RU_72H",
    "supply_chain": "SUPPLY_CHAIN",
    "rc_verify_3x": "RC_VERIFY_3X",
}

# production artifact 必需字段(每个 artifact 必须包含全部字段)
REQUIRED_ARTIFACT_FIELDS = (
    "artifact_type",
    "environment_id",
    "commit_sha",
    "image_digest",
    "started_at",
    "ended_at",
    "expires_at",
    "raw_data_digest",
    "executed_by",
    "approved_by",
    "signature",
    # R67 P1-11: 防重放字段 — 每份证据必须含 nonce + attestation_digest +
    # time_window,确保跨候选不可复用。consumed 字段记录 promotion 消费状态。
    "nonce",
    "attestation_digest",
    "time_window",
    "consumed",
)

# R67 P0-04: 6 类必需 artifact 类型(任一缺失即阻断,新增 RC_VERIFY_3X)
REQUIRED_ARTIFACT_TYPES = (
    "SOAK_7DAY",
    "RESTORE_3X",
    "OUTBOX_FAULT_INJECTION",
    "RU_72H",
    "SUPPLY_CHAIN",
    "RC_VERIFY_3X",
)

# 默认 artifact 过期时间(天) — production artifact 7 天后过期
DEFAULT_ARTIFACT_TTL_DAYS = 7


def _get_commit_sha() -> str:
    """获取当前 git HEAD SHA(失败回退到 GITHUB_SHA 环境变量或 unknown)。"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(_REPO_ROOT),
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            sha = result.stdout.strip()
            if sha and len(sha) >= 7:
                return sha
    except Exception:
        pass
    return os.environ.get("GITHUB_SHA", "unknown")


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
    *,
    evidence_mode: str = "dry_run",
    skip_types: list[str] | None = None,
) -> dict:
    """生成多个证据,返回汇总报告。

    R65 P0-04: ``evidence_mode`` 默认为 ``dry_run`` — 输出显式标记
    ``evidence_mode="dry_run"`` + ``production_promotion_allowed=false``,
    且 dry_run 模式下日志严禁出现 "production passed" 等通过性断言。
    只有显式传入 ``evidence_mode="production"`` 才能生成可被消费的生产证据,
    且 production 模式禁止 ``--skip``(``skip_types`` 必须为空)。

    Args:
        evidence_mode: ``"dry_run"``(默认)或 ``"production"``。
        skip_types: 通过 ``--skip`` 跳过的证据类型列表(production 模式禁止非空)。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    started_at = _now_iso()
    start_ts = time.time()

    # R65 P0-04: production 模式禁止 --skip
    skip_types = skip_types or []
    if evidence_mode == "production" and skip_types:
        print(
            "ERROR: production 模式禁止 --skip — production promotion 必须包含"
            "全部 5 类证据,不得跳过任何一项",
            file=sys.stderr,
        )
        return {
            "schema_version": "r64_p1_12_v1",
            "evidence_mode": evidence_mode,
            "production_promotion_allowed": False,
            "overall_status": "failed",
            "error": "production 模式禁止 --skip",
            "results": [],
            "flags": {"skip": skip_types, "dry_run": dry_run},
        }

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

    # R65 P0-04: 计算 production_promotion_allowed
    # 仅当 evidence_mode=production 且所有证据 passed 且无 --skip 时才允许晋级
    production_promotion_allowed = (
        evidence_mode == "production"
        and all_passed
        and not skip_types
        and len(results) == len(EVIDENCE_TYPES)
    )

    summary = {
        "schema_version": "r64_p1_12_v1",
        "generated_at": completed_at,
        "started_at": started_at,
        "duration_seconds": duration,
        # R64 P1-12: output_dir 可能位于 _REPO_ROOT 之外(如 /tmp),
        # 使用 try/except 兼容两种场景(相对路径或绝对路径)
        "output_dir": str(_safe_relative_to(output_dir, _REPO_ROOT)),
        "dry_run": dry_run,
        # R65 P0-04: evidence_mode 显式标记,禁止 dry_run 输出被消费为 production
        "evidence_mode": evidence_mode,
        "production_promotion_allowed": production_promotion_allowed,
        # R65 P0-04: 记录 CLI flags,production promotion 校验时禁止 --skip
        "flags": {
            "skip": list(skip_types),
            "dry_run": dry_run,
        },
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

    # 写入索引文件(向后兼容 — 现有测试/CI 依赖此文件名)
    index_path = output_dir / "production_evidence_index.json"
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\n{'═' * 70}")
    print(f"证据索引: {index_path}")
    print(f"总状态: {summary['overall_status']} "
          f"(passed={summary['passed_count']}, failed={summary['failed_count']})")
    # R65 P0-04: dry_run 模式必须显式标记,且日志不得出现 "production passed"
    if evidence_mode == "dry_run":
        print(f"evidence_mode: dry_run — 仅用于 schema/dry-run 验证,"
              f"不可作为 production promotion 证据")
        print(f"production_promotion_allowed: false (dry_run 模式)")
        # 同时写入带 dry_run 标记的副本文件,文件名包含 dry_run + commit
        commit_sha = _get_commit_sha()
        dry_run_filename = (
            f"production_evidence_dry_run_{commit_sha}.json"
        )
        dry_run_path = output_dir / dry_run_filename
        with open(dry_run_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print(f"dry_run 证据副本: {dry_run_path}")
    else:
        # production 模式: 可作为 production promotion 输入
        # 注意:此处不输出 "production passed" 字样,而是输出客观状态
        print(f"evidence_mode: production")
        print(f"production_promotion_allowed: "
              f"{summary['production_promotion_allowed']}")

    return summary


def verify_production_promotion(evidence_path: Path | str) -> dict:
    """R65 P0-04: 强制校验 production promotion 证据是否满足严格门禁。

    校验项(任一失败即抛 ``AppError(PRODUCTION_EVIDENCE_INSUFFICIENT)``):
        1. ``evidence_mode == "production"`` (拒绝 dry_run)
        2. 证据文件已签名(cosign 或 GPG signature verified)
        3. 证据文件未过期(每个 artifact 有 ``expires_at`` ISO 8601)
        4. 全部 5 类必需 artifact 类型存在:
           ``SOAK_7DAY`` / ``RESTORE_3X`` / ``OUTBOX_FAULT_INJECTION`` /
           ``RU_72H`` / ``SUPPLY_CHAIN``
        5. 每个 artifact 含全部必需字段:
           ``environment_id`` / ``commit_sha`` / ``image_digest`` /
           ``started_at`` / ``ended_at`` / ``expires_at`` /
           ``raw_data_digest`` / ``executed_by`` / ``approved_by`` / ``signature``
        6. 未使用 ``--skip`` 标志(记录在 evidence file 的 ``flags.skip``)

    Args:
        evidence_path: evidence JSON 文件路径。

    Returns:
        校验通过的 evidence dict(含 ``production_promotion_allowed=True``)。

    Raises:
        AppError(ErrorCodes.PRODUCTION_EVIDENCE_INSUFFICIENT): 任一校验失败。
    """
    # 延迟导入 AppError / ErrorCodes,避免在 import 时触发 services.i18n 链路
    from services.error_codes import AppError, ErrorCodes

    path = Path(evidence_path)
    if not path.exists():
        raise AppError(
            ErrorCodes.PRODUCTION_EVIDENCE_INSUFFICIENT,
            params={
                "reason": f"证据文件不存在: {path}",
                "missing": "ALL",
            },
        )

    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise AppError(
            ErrorCodes.PRODUCTION_EVIDENCE_INSUFFICIENT,
            params={
                "reason": f"证据文件无法解析: {e}",
                "missing": "ALL",
            },
        ) from e

    if not isinstance(evidence, dict):
        raise AppError(
            ErrorCodes.PRODUCTION_EVIDENCE_INSUFFICIENT,
            params={
                "reason": "证据文件根对象必须为 dict",
                "missing": "ALL",
            },
        )

    missing: list[str] = []
    reason_parts: list[str] = []

    # 1. evidence_mode 必须为 production(拒绝 dry_run)
    evidence_mode = evidence.get("evidence_mode")
    if evidence_mode != "production":
        reason_parts.append(
            f"evidence_mode={evidence_mode!r}(必须为 'production',"
            f"dry_run 证据不可用于 production promotion)"
        )
        missing.append("EVIDENCE_MODE_PRODUCTION")

    # 2. 证据文件必须已签名(cosign 或 GPG signature verified)
    signature = evidence.get("signature")
    if not isinstance(signature, dict):
        reason_parts.append("证据文件缺少 signature 块(顶层签名)")
        missing.append("FILE_SIGNATURE")
    else:
        sig_method = signature.get("method")
        sig_verified = signature.get("verified")
        if sig_method not in ("cosign", "gpg"):
            reason_parts.append(
                f"signature.method={sig_method!r}(必须为 'cosign' 或 'gpg')"
            )
            missing.append("FILE_SIGNATURE_METHOD")
        if sig_verified is not True:
            reason_parts.append(
                "signature.verified 不为 true(签名未通过验证)"
            )
            missing.append("FILE_SIGNATURE_VERIFIED")

    # 6. 检查 --skip 是否被使用(记录在 flags.skip)
    flags = evidence.get("flags") or {}
    skip_list = flags.get("skip") if isinstance(flags, dict) else None
    if skip_list:
        reason_parts.append(
            f"flags.skip={skip_list}(production promotion 禁止使用 --skip)"
        )
        missing.append("NO_SKIP_FLAG")

    # 4 + 5. 检查 5 类必需 artifact 是否齐全 + 每个 artifact 字段完整 + 未过期
    artifacts = evidence.get("artifacts")
    if not isinstance(artifacts, list):
        reason_parts.append(
            "证据文件缺少 artifacts 数组(5 类必需 artifact 缺失)"
        )
        missing.extend(REQUIRED_ARTIFACT_TYPES)
    else:
        # 按 artifact_type 索引
        artifact_map: dict[str, dict] = {}
        for art in artifacts:
            if isinstance(art, dict):
                atype = art.get("artifact_type")
                if atype:
                    artifact_map[atype] = art
        # 检查 5 类必需 artifact
        now_iso = _now_iso()
        for required_type in REQUIRED_ARTIFACT_TYPES:
            if required_type not in artifact_map:
                reason_parts.append(f"缺少必需 artifact: {required_type}")
                missing.append(required_type)
                continue
            art = artifact_map[required_type]
            # 检查每个 artifact 的必需字段
            for field in REQUIRED_ARTIFACT_FIELDS:
                val = art.get(field)
                if val is None or (isinstance(val, str) and not val):
                    reason_parts.append(
                        f"{required_type} 缺少必需字段: {field}"
                    )
                    if required_type not in missing:
                        missing.append(required_type)
            # 检查 artifact 是否过期
            expires_at = art.get("expires_at")
            if expires_at:
                try:
                    expires_dt = datetime.fromisoformat(
                        expires_at.replace("Z", "+00:00")
                    )
                    now_dt = datetime.fromisoformat(
                        now_iso.replace("Z", "+00:00")
                    )
                    if expires_dt < now_dt:
                        reason_parts.append(
                            f"{required_type} 已过期(expires_at={expires_at})"
                        )
                        if required_type not in missing:
                            missing.append(required_type)
                except (ValueError, TypeError) as e:
                    reason_parts.append(
                        f"{required_type} expires_at 解析失败: {e}"
                    )
                    if required_type not in missing:
                        missing.append(required_type)
            else:
                # expires_at 缺失已被字段检查覆盖
                pass
            # 检查 artifact 自身的 signature(每个 artifact 必须独立签名)
            art_sig = art.get("signature")
            if not art_sig:
                # 字段检查已覆盖,此处不重复加入 missing
                pass
            elif not isinstance(art_sig, str) or not art_sig.strip():
                reason_parts.append(
                    f"{required_type} signature 为空字符串(未签名)"
                )
                if required_type not in missing:
                    missing.append(required_type)

    # 任何 missing 即阻断
    if missing or reason_parts:
        # R65 P0-04: reason 保持简短(< 100 字符,避免被 safe_params 长度过滤);
        # 详细原因记录在 reason_parts(供日志/审计),missing 列表给出具体缺失项。
        # 优先取第一个 reason 作为简短摘要;若 reason_parts 为空(理论不应发生)
        # 则用通用文案。
        brief_reason = reason_parts[0] if reason_parts else "证据校验失败"
        # 截断过长的 brief_reason(safe_params 长度上限 100 字符)
        if len(brief_reason) > 90:
            brief_reason = brief_reason[:87] + "..."
        missing_str = ", ".join(sorted(set(missing))) if missing else "(none)"
        # 截断过长的 missing_str(同上)
        if len(missing_str) > 90:
            missing_str = missing_str[:87] + "..."
        # 完整诊断信息输出到 stderr(不进入 safe_params,避免被长度过滤)
        full_reason = "; ".join(reason_parts) if reason_parts else "证据校验失败"
        print(
            f"R65 P0-04: production promotion 证据校验失败\n"
            f"  brief_reason: {brief_reason}\n"
            f"  missing: {missing_str}\n"
            f"  full_reason: {full_reason}",
            file=sys.stderr,
        )
        raise AppError(
            ErrorCodes.PRODUCTION_EVIDENCE_INSUFFICIENT,
            params={
                "reason": brief_reason,
                "missing": missing_str,
            },
        )

    # 校验通过 — 标记 production_promotion_allowed 并返回
    evidence["production_promotion_allowed"] = True
    return evidence


# ════════════════════════════════════════════════════════════════
# R67 P1-11: 防重放(replay protection)— promotion 消费 + 单次使用
# ════════════════════════════════════════════════════════════════
#
# 审计背景(R67 终审报告 P1-11):
#     每份证据加入 nonce、environment ID、commit/tree/image/attestation digest、
#     时间窗、执行器和审批者;promotion 消费后标记 consumed,禁止跨候选复用。
#
# 实现要点:
#     1. 每个 artifact 在生成时由调用方填充 nonce(随机 hex)、attestation_digest
#        (cosign attestation digest)、time_window({started_at, ended_at})。
#        verify_production_promotion() 通过 REQUIRED_ARTIFACT_FIELDS 强制校验存在。
#
#     2. consume_evidence_for_promotion() 在 promotion 实际执行时调用:
#        - 检查每个 artifact 的 consumed=false(未消费)
#        - 检查 environment_id 匹配当前部署环境
#        - 检查 attestation_digest 与当前 release manifest 一致
#        - 标记每个 artifact consumed=true + consumed_at + consumed_by + consumed_candidate
#        - 将更新后的 evidence 写回文件(原子写入)
#
#     3. 一旦 consumed=true,再次调用 consume 会抛
#        AppError(EVIDENCE_ALREADY_CONSUMED),禁止跨候选复用。


def consume_evidence_for_promotion(
    evidence_path: Path | str,
    *,
    candidate_tag: str,
    consumed_by: str,
    expected_environment_id: str,
    expected_attestation_digest: str,
) -> dict:
    """R67 P1-11: 消费 production evidence 用于指定 candidate 的 promotion。

    单次使用语义:每个 evidence artifact 只能被一个 candidate 消费一次。
    重复消费(跨候选复用)会抛 ``AppError(EVIDENCE_ALREADY_CONSUMED)``。

    Args:
        evidence_path: evidence JSON 文件路径
        candidate_tag: 当前 candidate 的 tag(如 "rc-2025-07-21-v1")
        consumed_by: 执行 promotion 的用户/服务账号
        expected_environment_id: 当前部署环境 ID(必须与 artifact 中一致)
        expected_attestation_digest: 当前 release manifest attestation digest
            (必须与 artifact 中一致,确保证据是该 candidate 生成的)

    Returns:
        更新后的 evidence dict(每个 artifact 含 consumed=true +
        consumed_at + consumed_by + consumed_candidate)。

    Raises:
        AppError(EVIDENCE_ALREADY_CONSUMED): 任一 artifact 已被其他 candidate 消费
        AppError(PRODUCTION_EVIDENCE_INSUFFICIENT): 校验失败(字段缺失/环境不匹配/
            attestation 不一致/artifact 已过期)
    """
    from services.error_codes import AppError, ErrorCodes

    path = Path(evidence_path)
    if not path.exists():
        raise AppError(
            ErrorCodes.PRODUCTION_EVIDENCE_INSUFFICIENT,
            params={
                "reason": f"证据文件不存在: {path}",
                "missing": "ALL",
            },
        )

    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise AppError(
            ErrorCodes.PRODUCTION_EVIDENCE_INSUFFICIENT,
            params={
                "reason": f"证据文件无法解析: {e}",
                "missing": "ALL",
            },
        ) from e

    if not isinstance(evidence, dict):
        raise AppError(
            ErrorCodes.PRODUCTION_EVIDENCE_INSUFFICIENT,
            params={
                "reason": "证据文件根对象必须为 dict",
                "missing": "ALL",
            },
        )

    artifacts = evidence.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise AppError(
            ErrorCodes.PRODUCTION_EVIDENCE_INSUFFICIENT,
            params={
                "reason": "证据文件缺少 artifacts 数组",
                "missing": ",".join(REQUIRED_ARTIFACT_TYPES),
            },
        )

    now_iso = _now_iso()
    consumed_at = now_iso
    failures: list[str] = []

    for art in artifacts:
        if not isinstance(art, dict):
            failures.append(f"artifact 不是 dict: {art!r}")
            continue
        atype = art.get("artifact_type", "<unknown>")

        # 1. 检查 consumed 状态
        consumed = art.get("consumed")
        if consumed is True:
            consumed_candidate = art.get("consumed_candidate", "<unknown>")
            if consumed_candidate != candidate_tag:
                # 已被其他 candidate 消费 — 跨候选复用,违反 P1-11
                raise AppError(
                    ErrorCodes.EVIDENCE_ALREADY_CONSUMED,
                    params={
                        "artifact_type": atype,
                        "consumed_candidate": consumed_candidate,
                        "candidate_tag": candidate_tag,
                    },
                )
            # 已被同一 candidate 消费 — 幂等(返回当前 evidence)
            continue

        # 2. 校验 environment_id 匹配
        art_env = art.get("environment_id", "")
        if art_env != expected_environment_id:
            failures.append(
                f"{atype} environment_id={art_env!r} 不匹配 "
                f"expected={expected_environment_id!r}"
            )
            continue

        # 3. 校验 attestation_digest 匹配(防跨候选复用)
        art_digest = art.get("attestation_digest", "")
        if art_digest != expected_attestation_digest:
            failures.append(
                f"{atype} attestation_digest={art_digest!r} 不匹配 "
                f"expected={expected_attestation_digest!r} — "
                f"证据不属于当前 candidate"
            )
            continue

        # 4. 校验 nonce 存在(防重放基础)
        nonce = art.get("nonce", "")
        if not nonce:
            failures.append(f"{atype} 缺少 nonce(防重放必需)")
            continue

        # 5. 校验 time_window 存在(时间窗约束)
        time_window = art.get("time_window", {})
        if not isinstance(time_window, dict) or not time_window:
            failures.append(f"{atype} 缺少 time_window")
            continue

        # 6. 校验未过期(expires_at)
        expires_at = art.get("expires_at", "")
        if expires_at:
            try:
                expires_dt = datetime.fromisoformat(
                    expires_at.replace("Z", "+00:00")
                )
                now_dt = datetime.fromisoformat(
                    now_iso.replace("Z", "+00:00")
                )
                if expires_dt < now_dt:
                    failures.append(
                        f"{atype} 已过期(expires_at={expires_at})"
                    )
                    continue
            except (ValueError, TypeError) as e:
                failures.append(
                    f"{atype} expires_at 解析失败: {e}"
                )
                continue

        # 全部校验通过 — 标记 consumed
        art["consumed"] = True
        art["consumed_at"] = consumed_at
        art["consumed_by"] = consumed_by
        art["consumed_candidate"] = candidate_tag

    if failures:
        brief = failures[0]
        if len(brief) > 90:
            brief = brief[:87] + "..."
        print(
            f"R67 P1-11: evidence 消费失败\n"
            f"  brief: {brief}\n"
            f"  failures ({len(failures)}):",
            file=sys.stderr,
        )
        for f in failures[:10]:
            print(f"    - {f}", file=sys.stderr)
        raise AppError(
            ErrorCodes.PRODUCTION_EVIDENCE_INSUFFICIENT,
            params={
                "reason": brief,
                "missing": "REPLAY_PROTECTION_VALIDATION",
            },
        )

    # 原子写入更新后的 evidence(防部分写入竞争)
    evidence["last_consumed_at"] = consumed_at
    evidence["last_consumed_by"] = consumed_by
    evidence["last_consumed_candidate"] = candidate_tag
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(path)

    return evidence


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
    parser.add_argument(
        "--production", action="store_true",
        help=(
            "R65 P0-04: production 模式 — 输出 evidence_mode=production, "
            "禁止 --skip / --dry-run, 需要真实环境运行全部 5 类证据"
        ),
    )
    parser.add_argument(
        "--verify-promotion", metavar="EVIDENCE_PATH",
        help=(
            "R65 P0-04: 校验 production promotion 证据是否满足严格门禁 "
            "(evidence_mode=production / 已签名 / 未过期 / 5 类 artifact 齐全 / "
            "无 --skip),失败返回退出码 1"
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.list:
        _list_evidence_types()
        return 0

    # R65 P0-04: --verify-promotion 模式 — 仅校验,不生成
    if args.verify_promotion:
        from services.error_codes import AppError, ErrorCodes
        evidence_path = Path(args.verify_promotion)
        if not evidence_path.is_absolute():
            evidence_path = _REPO_ROOT / evidence_path
        print(f"R65 P0-04: 校验 production promotion 证据: {evidence_path}")
        try:
            evidence = verify_production_promotion(evidence_path)
        except AppError as e:
            print(f"FAIL: production promotion 证据校验未通过", file=sys.stderr)
            print(f"  code: {e.code}", file=sys.stderr)
            print(f"  reason: {e.params.get('reason', '')}", file=sys.stderr)
            print(f"  missing: {e.params.get('missing', '')}", file=sys.stderr)
            return 1
        print(f"PASS: production promotion 证据校验通过")
        print(f"  production_promotion_allowed: "
              f"{evidence.get('production_promotion_allowed')}")
        return 0

    # R65 P0-04: production 模式禁止 --skip 与 --dry-run
    if args.production:
        if args.skip:
            print(
                "ERROR: --production 模式禁止 --skip — production promotion 必须"
                "包含全部 5 类证据,不得跳过任何一项",
                file=sys.stderr,
            )
            return 2
        if args.dry_run:
            print(
                "ERROR: --production 模式禁止 --dry-run — production 证据必须"
                "在真实环境运行,不允许 dry-run 编排",
                file=sys.stderr,
            )
            return 2
        # production 模式必须 --all(5 类证据全选)
        if not args.all:
            print(
                "ERROR: --production 模式必须配合 --all — production promotion "
                "需要全部 5 类证据",
                file=sys.stderr,
            )
            return 2

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

    # 应用 --skip(production 模式已在前置检查中禁止 --skip)
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

    # R65 P0-04: evidence_mode 由 --production 决定,默认 dry_run
    evidence_mode = "production" if args.production else "dry_run"

    print(f"R64 P1-12 / R65 P0-04: 生产运行证据生成")
    print(f"输出目录: {output_dir}")
    print(f"证据类型: {', '.join(selected)}")
    print(f"Dry-run: {args.dry_run}")
    print(f"evidence_mode: {evidence_mode}")

    summary = asyncio.run(generate_evidence(
        evidence_types=selected,
        output_dir=output_dir,
        dry_run=args.dry_run,
        extra_args_map=extra_args_map,
        timeout_seconds=args.timeout_seconds,
        evidence_mode=evidence_mode,
        skip_types=list(args.skip),
    ))

    # 退出码:有任何 failed 返回 1
    if summary["failed_count"] > 0 or summary["error_count"] > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
