#!/usr/bin/env python3
"""R71 P1-01 (Wave 6) + R72 P1-07: Break-glass 紧急手动 override 审计日志记录脚本。

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

R72 P1-07 整改背景:
    旧版本仅将审计事件写入仓库内的 JSONL 文件(.github/break-glass-audit.jsonl),
    该文件可随普通代码提交被修改/删除,不是独立的 append-only 证据。R72 P1-07
    要求审计不仅写入仓库文件,还自动创建 GitHub issue(通过 gh CLI)作为主要
    审计源(primary source of truth)。仓库中的 JSONL 保留为副本(secondary copy),
    但不能是唯一审计源。

    R72 P1-07 新增行为:
      - 校验通过后,先调用 `gh issue create` 创建 GitHub issue(主要审计源)
      - issue 标题: [BREAK-GLASS] <operator> emergency override for <sha[:12]>
      - issue 正文: 包含完整审计字段(operator / timestamp / sha / reason /
        failed_checks / run_url / risk / rollback_plan / event_id / schema_version)
      - issue 标签: break-glass, audit(若标签不存在则忽略,不阻断 issue 创建)
      - issue 创建成功后,将 issue_url 写入 JSONL 副本(便于追溯)
      - issue 创建失败 → 脚本以退出码 1 失败(fail-closed),不写入 JSONL
        (确保重试不会产生重复 JSONL 条目)
      - JSONL 写入失败 → 脚本以退出码 1 失败(issue 已创建,但副本缺失需人工介入)
      - 可通过 --no-create-issue 标志跳过 issue 创建(仅用于本地测试/CI dry-run)
      - 可通过 --repo owner/repo 指定目标仓库(默认使用 gh CLI 推断的当前仓库)

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
        --output .github/break-glass-audit.jsonl

    # 跳过 issue 创建(仅用于本地测试,生产环境必须创建 issue)
    python scripts/record_break_glass.py ... --no-create-issue

    # 指定目标仓库(默认使用 gh CLI 推断的当前仓库)
    python scripts/record_break_glass.py ... --repo maxiuquan/tgjiema

退出码:
    0: 审计事件已成功记录(issue 创建成功 + JSONL 写入成功)
    1: 校验失败 / issue 创建失败 / JSONL 写入失败
    2: CLI 参数错误
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import shutil
import subprocess
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

# R72 P1-07: GitHub issue 标签(break-glass + audit)
# 若仓库中标签不存在,issue 创建仍会成功(标签添加为 best-effort)
ISSUE_LABELS: tuple[str, ...] = ("break-glass", "audit")

# R72 P1-07: gh CLI 命令名(用于 shutil.which 检查可用性)
GH_CLI_BINARY: str = "gh"

# R72 P1-07: gh issue create 子进程超时(秒)
# 防止网络问题导致脚本挂起
GH_CLI_TIMEOUT_SEC: int = 60

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
    # R72 P1-07: GitHub issue URL(主要审计源),由 create_github_issue() 写入
    issue_url: str = ""

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
            "issue_url": self.issue_url,
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
# R72 P1-07: GitHub issue 创建(主要审计源)
# ════════════════════════════════════════════════════════════════


def _format_issue_title(event: BreakGlassEvent) -> str:
    """构造 GitHub issue 标题。

    Args:
        event: Break-glass 事件

    Returns:
        issue 标题字符串(如 "[BREAK-GLASS] maxiuquan emergency override for abc123def456")
    """
    sha_short = event.sha[:12] if event.sha else "unknown"
    return f"[BREAK-GLASS] {event.operator} emergency override for {sha_short}"


def _format_issue_body(event: BreakGlassEvent) -> str:
    """构造 GitHub issue 正文(Markdown 格式)。

    包含完整审计字段:operator / timestamp / sha / reason / failed_checks /
    run_url / risk / rollback_plan / event_id / schema_version / tool_version。

    Args:
        event: Break-glass 事件

    Returns:
        Markdown 格式的 issue 正文
    """
    failed_checks_str = (
        ", ".join(f"`{c}`" for c in event.failed_checks)
        if event.failed_checks
        else "(none — 强烈建议列出失败 gates)"
    )
    run_url_line = event.run_url if event.run_url else "(not provided)"
    return f"""## Break-Glass Emergency Override Audit

> **R72 P1-07**: This GitHub issue is the **primary audit source** for this
> break-glass event. The JSONL file in the repo (`.github/break-glass-audit.jsonl`)
> is a secondary copy and must not be considered the only audit source.

| Field | Value |
|-------|-------|
| Event ID | `{event.event_id}` |
| Timestamp | `{event.timestamp}` |
| Schema Version | `{event.schema_version}` |
| Tool Version | `{event.tool_version}` |
| Operator | @{event.operator} |
| Commit SHA | `{event.sha}` |
| Run URL | {run_url_line} |
| Typed Confirmation | `{event.typed_confirmation}` |
| Followup Required | `{event.followup_required}` |

### Reason

{event.reason}

### Failed Checks

{failed_checks_str}

### Risk Assessment

{event.risk}

### Rollback Plan

{event.rollback_plan}

---

- [ ] Follow-up: rerun all failed gates listed above
- [ ] Follow-up: verify rollback plan is executable
- [ ] Follow-up: link this issue to the PR/commit that performed the override
- [ ] Follow-up: close this issue only after all failed gates pass on the target branch

*This issue was auto-created by `scripts/record_break_glass.py` (R72 P1-07).*
"""


def _ensure_labels_exist(repo: str | None) -> None:
    """确保 break-glass 与 audit 标签存在(best-effort,不阻断 issue 创建)。

    Args:
        repo: 可选的 owner/repo(若为 None,gh CLI 使用当前仓库)
    """
    for label_name in ISSUE_LABELS:
        cmd = [
            GH_CLI_BINARY, "label", "create", label_name,
            "--color", "d73a4a",
            "--description", f"Break-glass audit label: {label_name}",
            "--force",
        ]
        if repo:
            cmd.extend(["--repo", repo])
        try:
            subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=GH_CLI_TIMEOUT_SEC,
            )
        except (subprocess.TimeoutExpired, OSError):
            # 标签创建失败不阻断 issue 创建(best-effort)
            logger.warning(
                f"gh label create '{label_name}' 失败(best-effort,不阻断) — "
                "issue 将不带标签创建"
            )


def create_github_issue(
    event: BreakGlassEvent,
    repo: str | None = None,
) -> tuple[bool, str]:
    """通过 gh CLI 创建 GitHub issue 作为主要审计源(R72 P1-07)。

    本函数调用 `gh issue create` 子进程创建 issue。issue 标题与正文包含
    完整审计字段(operator / timestamp / sha / reason / failed_checks /
    run_url / risk / rollback_plan / event_id / schema_version)。

    issue 创建成功后,会尝试添加 break-glass + audit 标签(best-effort,
    标签不存在则忽略,不阻断 issue 创建)。

    Args:
        event: Break-glass 事件(必须已通过校验)
        repo: 可选的 owner/repo(如 "maxiuquan/tgjiema")。若为 None,
            gh CLI 使用当前仓库(通过 git remote 推断)

    Returns:
        (success, issue_url_or_error) 元组:
          - 成功: (True, "https://github.com/owner/repo/issues/123")
          - 失败: (False, "error message")
    """
    # 1. 检查 gh CLI 是否可用
    if not shutil.which(GH_CLI_BINARY):
        return (
            False,
            f"gh CLI 未找到(PATH 中无 '{GH_CLI_BINARY}')— "
            "请安装 GitHub CLI (https://cli.github.com/) 并执行 gh auth login",
        )

    # 2. 构造 issue 标题与正文
    title = _format_issue_title(event)
    body = _format_issue_body(event)

    # 3. 调用 gh issue create(不带 --label,先创建 issue 再添加标签)
    cmd = [
        GH_CLI_BINARY, "issue", "create",
        "--title", title,
        "--body", body,
    ]
    if repo:
        cmd.extend(["--repo", repo])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=GH_CLI_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return (
            False,
            f"gh issue create 超时({GH_CLI_TIMEOUT_SEC}s)— "
            "请检查网络连接或 gh auth 状态",
        )
    except OSError as e:
        return False, f"gh issue create 执行失败: {e}"

    if result.returncode != 0:
        stderr = result.stderr.strip() if result.stderr else ""
        stdout = result.stdout.strip() if result.stdout else ""
        return (
            False,
            f"gh issue create 失败(exit={result.returncode})— "
            f"stderr: {stderr} | stdout: {stdout}",
        )

    issue_url = result.stdout.strip()
    if not issue_url or not issue_url.startswith("http"):
        return (
            False,
            f"gh issue create 返回非 URL 输出: '{issue_url}' — "
            "请检查 gh CLI 版本与认证状态",
        )

    # 4. best-effort 添加标签(不阻断 issue 创建)
    # 提取 issue number(URL 格式: https://github.com/owner/repo/issues/123)
    issue_number = issue_url.rstrip("/").split("/")[-1]
    if issue_number.isdigit():
        _ensure_labels_exist(repo)
        label_cmd = [
            GH_CLI_BINARY, "issue", "edit", issue_number,
            "--add-label", ",".join(ISSUE_LABELS),
        ]
        if repo:
            label_cmd.extend(["--repo", repo])
        try:
            label_result = subprocess.run(
                label_cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=GH_CLI_TIMEOUT_SEC,
            )
            if label_result.returncode != 0:
                logger.warning(
                    f"gh issue edit --add-label 失败(best-effort,不阻断)— "
                    f"stderr: {label_result.stderr.strip() if label_result.stderr else '(empty)'}"
                )
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.warning(
                f"gh issue edit --add-label 异常(best-effort,不阻断): {e}"
            )

    return True, issue_url


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
    create_issue: bool = True,
    repo: str | None = None,
) -> tuple[BreakGlassEvent, ValidationResult]:
    """记录 break-glass 审计事件的主函数。

    R72 P1-07 流程:
      1. 校验事件字段
      2. 若 create_issue=True,通过 gh CLI 创建 GitHub issue(主要审计源)
         - issue 创建失败 → fail-closed,不写入 JSONL(确保重试不产生重复)
      3. 将 issue_url 写入 event 对象,追加到 JSONL 文件(secondary copy)
         - JSONL 写入失败 → fail-closed(副本缺失需人工介入)

    Args:
        operator: 操作员 GitHub 用户名
        sha: 待 override 的 commit SHA(40-char hex)
        reason: override 原因(详细描述紧急情况)
        failed_checks: 失败的 gate / check 名列表
        run_url: GitHub Actions run URL(若适用)
        risk: 风险评估(详细描述风险等级与影响范围)
        rollback_plan: 回滚计划(可执行的回滚步骤)
        typed_confirmation: 显式确认字符串(必须为 "BREAK-GLASS-EMERGENCY")
        output_path: 输出 JSONL 文件路径(secondary copy)
        create_issue: 是否创建 GitHub issue(主要审计源)。默认 True。
            设为 False 仅用于本地测试/CI dry-run(生产环境必须为 True)。
        repo: 可选的 owner/repo(如 "maxiuquan/tgjiema")。若为 None,
            gh CLI 使用当前仓库。

    Returns:
        (BreakGlassEvent, ValidationResult) 元组 —
        即使校验失败也返回 event 对象(便于调试),但不会写入文件/创建 issue
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

    # R72 P1-07: 创建 GitHub issue(主要审计源)
    # 必须在 JSONL 写入之前完成 — 若 issue 创建失败,不写入 JSONL(确保重试不产生重复)
    if create_issue:
        logger.info("=== R72 P1-07: 创建 GitHub issue(主要审计源)===")
        issue_success, issue_url_or_error = create_github_issue(event, repo=repo)
        if not issue_success:
            result.add_error(
                f"创建 GitHub issue 失败(主要审计源)— {issue_url_or_error}"
            )
            logger.error(
                f"FAIL: 创建 GitHub issue 失败 — {issue_url_or_error}"
            )
            logger.error(
                "  R72 P1-07: 审计 issue 是主要审计源,创建失败时不写入 JSONL"
            )
            logger.error(
                "  (确保重试不会产生重复 JSONL 条目)。请修复 gh CLI 后重试。"
            )
            return event, result
        event.issue_url = issue_url_or_error
        logger.info(f"PASS: GitHub issue 已创建(主要审计源)— {event.issue_url}")
    else:
        logger.warning(
            "WARN: --no-create-issue 已指定,跳过 GitHub issue 创建。"
            "此模式仅用于本地测试 — 生产环境必须创建 issue(R72 P1-07)。"
        )

    # 追加到 JSONL 文件(append-only,secondary copy)
    logger.info(f"=== R71 P1-01: 追加审计事件到 JSONL 副本: {output_path} ===")
    try:
        append_event_to_jsonl(event, output_path)
    except OSError as e:
        # IO 错误 — 转换为校验失败(便于统一退出码处理)
        # R72 P1-07: issue 已创建,但 JSONL 副本写入失败 — 需人工介入
        result.add_error(
            f"写入 JSONL 副本失败: {e} — GitHub issue 已创建({event.issue_url}),"
            "但仓库副本缺失,需人工补录"
        )
        logger.error(f"FAIL: 写入 JSONL 副本失败: {e}")
        logger.error(
            f"  R72 P1-07: GitHub issue 已创建({event.issue_url}),"
            "但 JSONL 副本写入失败 — 需人工补录到仓库"
        )
        return event, result

    logger.info(
        f"PASS: break-glass 审计事件已记录 — event_id={event.event_id} "
        f"operator={event.operator} sha={event.sha[:12]}..."
    )
    logger.info(f"  GitHub issue (primary): {event.issue_url or '(skipped)'}")
    logger.info(f"  JSONL 副本 (secondary):  {output_path}")
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
            "R71 P1-01 (Wave 6) + R72 P1-07: Break-glass 紧急手动 override "
            "审计日志记录脚本。在执行紧急 override 前,必须先调用本脚本记录审计事件。"
            "R72 P1-07: 自动创建 GitHub issue 作为主要审计源,JSONL 文件为副本。"
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
        help="输出 JSONL 文件路径(append-only,每行一个事件 JSON 对象,secondary copy)",
    )
    # R72 P1-07: GitHub issue 创建控制
    parser.add_argument(
        "--no-create-issue",
        action="store_true",
        default=False,
        help="跳过 GitHub issue 创建(仅用于本地测试/CI dry-run)。"
        "生产环境必须创建 issue(R72 P1-07: issue 是主要审计源,JSONL 仅是副本)。",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="目标仓库(owner/repo 格式,如 maxiuquan/tgjiema)。"
        "若未指定,gh CLI 使用当前仓库(通过 git remote 推断)。",
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
        create_issue=not args.no_create_issue,
        repo=args.repo,
    )

    # 输出事件 JSON 到 stdout(便于管道处理 / CI 日志收集)
    print(json.dumps(event.to_dict(), ensure_ascii=False, indent=2))

    # 返回退出码
    if result.valid:
        return EXIT_SUCCESS
    return EXIT_VALIDATION_FAILURE


if __name__ == "__main__":
    sys.exit(main())
