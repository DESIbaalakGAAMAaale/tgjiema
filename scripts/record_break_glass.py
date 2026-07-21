#!/usr/bin/env python3
"""R71 P1-01 (Wave 6): Break-glass 紧急手动 override 审计日志记录脚本。

R71 P1-01 整改背景:
    R71 Solo Founder Branch Ruleset 设置 bypass_actors=[](禁止任何角色 bypass,
    包括 admin),以保证 branch protection 的 fail-closed 语义。但生产环境可能
    出现紧急情况(如 CVE 热修、生产事故恢复),需要在不通过全部 release gates
    的情况下手动 override。

    本脚本提供"break-glass"机制:
      1. 运维执行紧急 override 前,必须先调用本脚本记录审计事件
      2. 脚本要求操作员提供完整的事件元数据(operator / sha / reason /
         failed-checks / run-url / risk / rollback-plan / typed-confirmation)
      3. typed_confirmation 必须精确等于 "BREAK-GLASS-EMERGENCY"(强制操作员
         显式确认这是紧急情况,防止误用)
      4. 事件以 JSONL 格式追加到审计日志文件(每行一个 JSON 对象,append-only)
      5. followup_required=true 标记所有失败 gates 必须在 break-glass 后重跑

    本脚本**不**绕过任何 GitHub protection / ruleset,仅记录审计事件。
    实际 override 操作需通过 GitHub Admin UI / API 单独执行(且 admin bypass
    在 R71 Solo Founder Ruleset 中被禁用,因此 override 必须通过临时修改
    ruleset 或使用 GitHub admin override API 完成,所有这些操作都应在本脚本
    记录审计事件后进行)。

使用方法:
    python scripts/record_break_glass.py \\
        --operator maxiuquan \\
        --sha abc123def4567890abcdef1234567890abcdef12 \\
        --reason "emergency production hotfix for CVE-XXXX" \\
        --failed-checks "verify-rc-identity,validate-oci-rootfs" \\
        --run-url "https://github.com/owner/repo/actions/runs/123" \\
        --risk "high — bypassing RC identity verification for critical security fix" \\
        --rollback-plan "revert commit abc123, rebuild RC, rerun all gates" \\
        --typed-confirmation "BREAK-GLASS-EMERGENCY" \\
        --output break-glass-audit.jsonl

退出码:
    0: 审计事件已成功追加到输出文件
    1: 校验失败(字段缺失 / 格式错误 / typed_confirmation 不匹配)
    2: CLI 参数错误或 IO 错误
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

# ════════════════════════════════════════════════════════════════
# 常量定义
# ════════════════════════════════════════════════════════════════

SCHEMA_VERSION: str = "1.0"
TOOL_VERSION: str = "R71-WAVE6-P1-01-BREAK-GLASS"

# typed_confirmation 必须精确匹配此字符串(大小写敏感)
# 强制操作员显式输入完整的 "BREAK-GLASS-EMERGENCY" 以防止误用
EXPECTED_TYPED_CONFIRMATION: str = "BREAK-GLASS-EMERGENCY"

# 正则:40-char hex(Git SHA-1)
SOURCE_SHA_PATTERN: re.Pattern[str] = re.compile(r"^[0-9a-fA-F]{40}$")

# 正则:合法 GitHub Actions run URL
# 接受格式:
#   https://github.com/{owner}/{repo}/actions/runs/{run_id}
#   http://github.com/...  (允许 http,虽然 GitHub 强制 https)
#   大小写不敏感
GITHUB_RUN_URL_PATTERN: re.Pattern[str] = re.compile(
    r"^https?://github\.com/[^/\s]+/[^/\s]+/actions/runs/\d+(?:/[^/\s]*)?$",
    re.IGNORECASE,
)

# 必填字段列表(用于校验)
REQUIRED_FIELDS: tuple[str, ...] = (
    "operator",
    "sha",
    "reason",
    "risk",
    "rollback_plan",
    "typed_confirmation",
)

# 退出码
EXIT_SUCCESS: int = 0
EXIT_VALIDATION_FAILURE: int = 1
EXIT_CLI_ERROR: int = 2


# ════════════════════════════════════════════════════════════════
# 数据结构
# ════════════════════════════════════════════════════════════════


@dataclass
class BreakGlassEvent:
    """Break-glass 紧急 override 审计事件。"""

    event_id: str = ""
    timestamp: str = ""
    schema_version: str = SCHEMA_VERSION
    tool_version: str = TOOL_VERSION
    operator: str = ""
    sha: str = ""
    reason: str = ""
    failed_checks: list[str] = field(default_factory=list)
    run_url: str = ""
    risk: str = ""
    rollback_plan: str = ""
    typed_confirmation: str = ""
    followup_required: bool = True  # 默认 true — 所有失败 gates 必须在 break-glass 后重跑

    def to_dict(self) -> dict[str, Any]:
        """转换为 JSON 可序列化的 dict(JSONL 一行)。"""
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "schema_version": self.schema_version,
            "tool_version": self.tool_version,
            "operator": self.operator,
            "sha": self.sha,
            "reason": self.reason,
            "failed_checks": list(self.failed_checks),
            "run_url": self.run_url,
            "risk": self.risk,
            "rollback_plan": self.rollback_plan,
            "typed_confirmation": self.typed_confirmation,
            "followup_required": self.followup_required,
        }


@dataclass
class ValidationResult:
    """Break-glass 事件校验结果。"""

    valid: bool = True
    errors: list[str] = field(default_factory=list)

    def add_error(self, msg: str) -> None:
        """追加错误并标记 valid=False。"""
        self.errors.append(msg)
        self.valid = False


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════


def _now_iso() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串。"""
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _parse_failed_checks(raw: str) -> list[str]:
    """解析 --failed-checks 参数为列表。

    接受逗号分隔的字符串(空白自动剥离),空字符串返回空列表。

    Args:
        raw: 逗号分隔的 failed check 名(如 "verify-rc-identity,validate-oci-rootfs")

    Returns:
        failed check 名列表(如 ["verify-rc-identity", "validate-oci-rootfs"])
    """
    if not raw:
        return []
    # 按逗号分割并去除空白/空项
    parts = [p.strip() for p in raw.split(",")]
    # 过滤掉空字符串(避免 "a,,b" 产生中间空项)
    return [p for p in parts if p]


# ════════════════════════════════════════════════════════════════
# 输入校验
# ════════════════════════════════════════════════════════════════


def validate_event(event: BreakGlassEvent) -> ValidationResult:
    """校验 break-glass 事件的所有字段。

    校验规则:
      1. 必填字段非空:operator / sha / reason / risk / rollback_plan /
         typed_confirmation(failed_checks / run_url 可选但强烈推荐)
      2. typed_confirmation 必须精确等于 "BREAK-GLASS-EMERGENCY"
      3. sha 必须为 40-char hex(Git SHA-1)
      4. run_url(若提供)必须是合法 GitHub Actions URL

    Args:
        event: 待校验的 break-glass 事件

    Returns:
        ValidationResult(valid=True 表示所有校验通过)
    """
    result = ValidationResult()

    # 1. 必填字段非空校验
    field_to_label = {
        "operator": "operator (操作员)",
        "sha": "sha (commit SHA)",
        "reason": "reason (override 原因)",
        "risk": "risk (风险评估)",
        "rollback_plan": "rollback_plan (回滚计划)",
        "typed_confirmation": "typed_confirmation (显式确认)",
    }
    for field_name, label in field_to_label.items():
        value = getattr(event, field_name, "")
        if not value or not isinstance(value, str) or not value.strip():
            result.add_error(f"{label} 不能为空 — break-glass 事件必须提供此字段")

    # 2. typed_confirmation 必须精确匹配(大小写敏感)
    if event.typed_confirmation and event.typed_confirmation != EXPECTED_TYPED_CONFIRMATION:
        result.add_error(
            f"typed_confirmation 不匹配(期望 '{EXPECTED_TYPED_CONFIRMATION}',"
            f"实际 '{event.typed_confirmation}')— 必须显式输入完整字符串以防止误用"
        )

    # 3. sha 必须为 40-char hex(Git SHA-1)
    if event.sha and not SOURCE_SHA_PATTERN.match(event.sha):
        result.add_error(
            f"sha 格式不合法(期望 40-char hex,如 'abc123def4567890abcdef1234567890abcdef12')"
            f"实际: '{event.sha[:20]}...' (长度 {len(event.sha)})"
        )

    # 4. run_url(若提供)必须是合法 GitHub Actions URL
    if event.run_url and not GITHUB_RUN_URL_PATTERN.match(event.run_url):
        result.add_error(
            f"run_url 格式不合法(期望 'https://github.com/{{owner}}/{{repo}}/"
            f"actions/runs/{{run_id}}')实际: '{event.run_url}'"
        )

    # 5. failed_checks 应为非空列表(强烈推荐 — 告知后续 follow-up 需重跑哪些 gates)
    #    不强制非空(某些紧急场景可能无法列举全部 failed checks),但会发出 warning
    if not event.failed_checks:
        logger.warning(
            "failed_checks 为空 — 强烈建议列出所有失败的 gates 以便 follow-up 重跑; "
            "当前事件仍会记录,但 follow-up 将无法自动确定需重跑哪些 gates"
        )

    # 6. reason / risk / rollback_plan 应有最低字数(防止敷衍)
    #    不强制失败,但发出 warning(审计质量保障)
    if event.reason and len(event.reason.strip()) < 10:
        logger.warning(
            f"reason 字段过短 ({len(event.reason)} 字符) — "
            "应详细描述紧急情况以备审计追溯"
        )
    if event.risk and len(event.risk.strip()) < 5:
        logger.warning(
            f"risk 字段过短 ({len(event.risk)} 字符) — "
            "应详细评估风险等级与影响范围"
        )
    if event.rollback_plan and len(event.rollback_plan.strip()) < 10:
        logger.warning(
            f"rollback_plan 字段过短 ({len(event.rollback_plan)} 字符) — "
            "应提供可执行的回滚步骤"
        )

    return result


# ════════════════════════════════════════════════════════════════
# 事件持久化(JSONL append-only)
# ════════════════════════════════════════════════════════════════


def append_event_to_jsonl(event: BreakGlassEvent, output_path: Path) -> None:
    """将 break-glass 事件以 JSONL 格式追加到输出文件(append-only)。

    JSONL (JSON Lines) 格式: 每行一个独立的 JSON 对象,行尾以 \\n 结束。
    此格式支持 append-only 追加写入,且便于后续逐行解析(无需将整个文件
    加载到内存)。

    Args:
        event: 待记录的 break-glass 事件
        output_path: 输出文件路径(若不存在则创建,若存在则追加)

    Raises:
        OSError: 文件写入失败(权限不足 / 磁盘满 / 路径无效)
    """
    # 确保父目录存在
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # 序列化为 JSON 行(sort_keys=False 以保持字段顺序与 dataclass 一致)
    # separators 设置为 (",", ":") 以产生紧凑格式(无多余空格),
    # 但为了人类可读性,使用 indent=None + ensure_ascii=False
    json_line = json.dumps(
        event.to_dict(),
        ensure_ascii=False,
        sort_keys=False,
    )

    # append-only 写入(每行一个 JSON 对象)
    with open(output_path, "a", encoding="utf-8") as f:
        f.write(json_line)
        f.write("\n")


# ════════════════════════════════════════════════════════════════
# 主流程
# ════════════════════════════════════════════════════════════════


def record_break_glass(
    operator: str,
    sha: str,
    reason: str,
    failed_checks: list[str],
    run_url: str,
    risk: str,
    rollback_plan: str,
    typed_confirmation: str,
    output_path: Path,
) -> tuple[BreakGlassEvent, ValidationResult]:
    """记录 break-glass 审计事件的主函数。

    Args:
        operator: 操作员 GitHub 用户名
        sha: 待 override 的 commit SHA(40-char hex)
        reason: override 原因(详细描述紧急情况)
        failed_checks: 失败的 gate / check 名列表
        run_url: GitHub Actions run URL(若适用)
        risk: 风险评估(详细描述风险等级与影响范围)
        rollback_plan: 回滚计划(可执行的回滚步骤)
        typed_confirmation: 显式确认字符串(必须为 "BREAK-GLASS-EMERGENCY")
        output_path: 输出 JSONL 文件路径

    Returns:
        (BreakGlassEvent, ValidationResult) 元组 —
        即使校验失败也返回 event 对象(便于调试),但不会写入文件
    """
    # 构造事件对象
    event = BreakGlassEvent(
        event_id=str(uuid.uuid4()),
        timestamp=_now_iso(),
        operator=operator.strip() if operator else "",
        sha=sha.strip() if sha else "",
        reason=reason.strip() if reason else "",
        failed_checks=list(failed_checks),
        run_url=run_url.strip() if run_url else "",
        risk=risk.strip() if risk else "",
        rollback_plan=rollback_plan.strip() if rollback_plan else "",
        typed_confirmation=typed_confirmation,  # 不 strip — 严格大小写匹配
        followup_required=True,  # 总是 true — 所有失败 gates 必须在 break-glass 后重跑
    )

    # 校验事件
    logger.info("=== R71 P1-01: 校验 break-glass 事件 ===")
    result = validate_event(event)
    if not result.valid:
        logger.error(f"FAIL: break-glass 事件校验失败 — {len(result.errors)} 个错误:")
        for err in result.errors:
            logger.error(f"  - {err}")
        return event, result
    logger.info("PASS: break-glass 事件校验通过")

    # 追加到 JSONL 文件(append-only)
    logger.info(f"=== R71 P1-01: 追加审计事件到 JSONL 文件: {output_path} ===")
    try:
        append_event_to_jsonl(event, output_path)
    except OSError as e:
        # IO 错误 — 转换为校验失败(便于统一退出码处理)
        result.add_error(f"写入审计日志失败: {e}")
        logger.error(f"FAIL: 写入审计日志失败: {e}")
        return event, result

    logger.info(
        f"PASS: break-glass 审计事件已记录 — event_id={event.event_id} "
        f"operator={event.operator} sha={event.sha[:12]}..."
    )
    logger.info(f"  审计日志路径: {output_path}")
    logger.info(
        f"  followup_required={event.followup_required} — "
        "所有失败 gates 必须在 break-glass 后重跑"
    )
    return event, result


# ════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════


def _build_parser() -> argparse.ArgumentParser:
    """构建 CLI 参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="record_break_glass.py",
        description=(
            "R71 P1-01 (Wave 6): Break-glass 紧急手动 override 审计日志记录脚本。"
            "在执行紧急 override 前,必须先调用本脚本记录审计事件。"
        ),
    )
    parser.add_argument(
        "--operator",
        required=True,
        help="操作员 GitHub 用户名(如 maxiuquan)",
    )
    parser.add_argument(
        "--sha",
        required=True,
        help="待 override 的 commit SHA(40-char hex,如 "
        "abc123def4567890abcdef1234567890abcdef12)",
    )
    parser.add_argument(
        "--reason",
        required=True,
        help="Override 原因(详细描述紧急情况,如 'emergency production hotfix for CVE-XXXX')",
    )
    parser.add_argument(
        "--failed-checks",
        default="",
        help="失败的 gate / check 名列表(逗号分隔,如 "
        "'verify-rc-identity,validate-oci-rootfs')",
    )
    parser.add_argument(
        "--run-url",
        default="",
        help="GitHub Actions run URL(如 https://github.com/owner/repo/actions/runs/123)",
    )
    parser.add_argument(
        "--risk",
        required=True,
        help="风险评估(详细描述风险等级与影响范围,如 "
        "'high — bypassing RC identity verification for critical security fix')",
    )
    parser.add_argument(
        "--rollback-plan",
        required=True,
        help="回滚计划(可执行的回滚步骤,如 "
        "'revert commit abc123, rebuild RC, rerun all gates')",
    )
    parser.add_argument(
        "--typed-confirmation",
        required=True,
        help='显式确认字符串 — 必须精确等于 "BREAK-GLASS-EMERGENCY" '
        "(大小写敏感,强制操作员显式确认紧急情况)",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="输出 JSONL 文件路径(append-only,每行一个事件 JSON 对象)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 主入口。

    Returns:
        0 success, 1 validation failure, 2 CLI error
    """
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        # argparse 在参数错误时调用 sys.exit(2),我们转换为 EXIT_CLI_ERROR
        code = e.code if isinstance(e.code, int) else EXIT_CLI_ERROR
        return EXIT_CLI_ERROR if code != 0 else EXIT_SUCCESS

    # 配置 loguru 输出到 stderr
    logger.remove()
    logger.add(sys.stderr, level="INFO")

    # 解析 failed_checks(逗号分隔 → 列表)
    failed_checks = _parse_failed_checks(args.failed_checks)

    # 调用主流程
    output_path = Path(args.output)
    event, result = record_break_glass(
        operator=args.operator,
        sha=args.sha,
        reason=args.reason,
        failed_checks=failed_checks,
        run_url=args.run_url,
        risk=args.risk,
        rollback_plan=args.rollback_plan,
        typed_confirmation=args.typed_confirmation,
        output_path=output_path,
    )

    # 输出事件 JSON 到 stdout(便于管道处理 / CI 日志收集)
    print(json.dumps(event.to_dict(), ensure_ascii=False, indent=2))

    # 返回退出码
    if result.valid:
        return EXIT_SUCCESS
    return EXIT_VALIDATION_FAILURE


if __name__ == "__main__":
    sys.exit(main())
