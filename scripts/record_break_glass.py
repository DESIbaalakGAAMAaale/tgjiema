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
import contextlib
import datetime as _dt
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

# R75 P1-06: 跨平台文件锁原语(fcntl Unix / msvcrt Windows)
# 用于审计 JSONL 并发写入保护,防止 TOCTOU race 导致 hash chain 断裂
try:
    import fcntl as _fcntl  # type: ignore[import-not-found]
except ImportError:
    _fcntl = None  # type: ignore[assignment]
try:
    import msvcrt as _msvcrt  # type: ignore[import-not-found]
except ImportError:
    _msvcrt = None  # type: ignore[assignment]

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
# R73 §5.9 (P1-03): Externalized break-glass audit — 常量
# ════════════════════════════════════════════════════════════════
# R73 §5.9 要求 break-glass 审计外部化为 GitHub Issue(主要审计源),
# 仓库内 JSONL 仅作镜像副本;ruleset JSON 修改前必须签名留底;
# 合并后自动恢复 ruleset、校验 enforcement=active、重跑 current-SHA checks、
# 生成签名 closure artifact、关闭外部 issue。

R73_SCHEMA_VERSION: str = "2.0"
R73_TOOL_VERSION: str = "R73-P1-03-BREAK-GLASS-EXTERNALIZED"

# 默认仓库(可通过 --repo 覆盖)
DEFAULT_REPO: str = "maxiuquan/tgjiema"

# 默认审计 JSONL 路径(镜像副本;主要审计源是 GitHub Issue)
DEFAULT_AUDIT_PATH: str = ".github/break-glass-audit.jsonl"

# 默认快照/closure artifact 目录
DEFAULT_SNAPSHOTS_DIR: str = ".github"

# 默认 break-glass 时间窗口(分钟)
DEFAULT_DURATION_MINUTES: int = 60

# 签名密钥环境变量名(BREAK_GLASS_SIGNING_KEY 优先,回退到 BACKUP_SIGNING_KEY)
BREAK_GLASS_SIGNING_KEY_ENV: str = "BREAK_GLASS_SIGNING_KEY"
BACKUP_SIGNING_KEY_ENV: str = "BACKUP_SIGNING_KEY"

# R73 §5.9 子命令列表
R73_SUBCOMMANDS: tuple[str, ...] = ("open", "close", "verify-closed")

# ─── Mock gh CLI 状态(用于测试;--gh-mock 时使用) ───
# 固定的 mock ruleset JSON(enforcement=active,符合 R71 Solo Founder 语义)
_MOCK_RULESET_JSON: dict[str, Any] = {
    "id": 12345,
    "name": "R71 Solo Founder Branch Ruleset",
    "target": "branch",
    "source_type": "Repository",
    "enforcement": "active",
    "conditions": {
        "ref_name": {
            "include": ["refs/heads/master", "refs/heads/main"],
            "exclude": [],
        }
    },
    "rules": [
        {"type": "deletion"},
        {"type": "non_fast_forward"},
        {
            "type": "required_status_checks",
            "parameters": {
                "required_status_checks": [
                    {"context": "lint"},
                    {"context": "test (3.10)"},
                    {"context": "test (3.11)"},
                    {"context": "test (3.12)"},
                ],
                "strict_required_status_checks_policy": True,
                "do_not_enforce_on_create": False,
            },
        },
    ],
    "bypass_actors": [],
}

# Mock check-runs 响应(全部 success,模拟 current-SHA checks 通过)
_MOCK_CHECK_RUNS: dict[str, Any] = {
    "total_count": 4,
    "check_runs": [
        {"name": "lint", "conclusion": "success", "status": "completed"},
        {"name": "test (3.10)", "conclusion": "success", "status": "completed"},
        {"name": "test (3.11)", "conclusion": "success", "status": "completed"},
        {"name": "test (3.12)", "conclusion": "success", "status": "completed"},
    ],
}

# Mock issue URL(--gh-mock 时,issue 编号从审计 JSONL 已有条目数推断,保证唯一)
_MOCK_ISSUE_URL_BASE: str = "https://github.com/mock/repo/issues"


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
# R73 §5.9 (P1-03): Externalized break-glass audit — 辅助函数
# ════════════════════════════════════════════════════════════════


def _get_signing_key() -> bytes:
    """获取签名密钥(BREAK_GLASS_SIGNING_KEY 优先,回退到 BACKUP_SIGNING_KEY)。

    R73 §5.9 要求 ruleset JSON 在修改前用 HMAC-SHA256 签名留底,签名密钥
    从环境变量读取。BREAK_GLASS_SIGNING_KEY 优先(专用密钥),若未设置则
    回退到 BACKUP_SIGNING_KEY(复用备份签名密钥,减少密钥管理负担)。

    Returns:
        签名密钥的 bytes(UTF-8 编码)

    Raises:
        RuntimeError: 两个环境变量均未设置
    """
    key = os.environ.get(BREAK_GLASS_SIGNING_KEY_ENV) or os.environ.get(BACKUP_SIGNING_KEY_ENV)
    if not key:
        raise RuntimeError(
            f"未找到签名密钥 — 请设置 {BREAK_GLASS_SIGNING_KEY_ENV} 或 "
            f"{BACKUP_SIGNING_KEY_ENV} 环境变量(R73 §5.9 要求 ruleset JSON "
            "修改前必须签名留底)"
        )
    return key.encode("utf-8")


def _canonical_json_bytes(obj: Any) -> bytes:
    """将对象序列化为规范 JSON bytes(sorted keys + 紧凑分隔符)。

    规范化确保相同语义的 JSON 产生相同 bytes(便于 digest 与签名可复现)。
    """
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _compute_ruleset_digest(ruleset_json: dict[str, Any]) -> str:
    """计算 ruleset JSON 的 SHA-256 digest(canonical form)。

    Args:
        ruleset_json: GitHub Rulesets API 返回的 ruleset JSON 对象

    Returns:
        形如 "sha256:<64-char-hex>" 的 digest 字符串
    """
    return "sha256:" + hashlib.sha256(_canonical_json_bytes(ruleset_json)).hexdigest()


def _compute_event_digest(event: dict[str, Any]) -> str:
    """计算事件条目的 SHA-256 digest(用于 hash chain)。

    Args:
        event: 审计事件条目 dict

    Returns:
        形如 "sha256:<64-char-hex>" 的 digest 字符串
    """
    return "sha256:" + hashlib.sha256(
        _canonical_json_bytes(event)
    ).hexdigest()


def _get_last_event_digest(audit_path: Path) -> str:
    """读取审计 JSONL 的最后一条物理事件并返回其 digest。

    R75 P1-06: closed 事件的 previous_event_digest 必须引用审计文件中的
    最后一条物理事件(全局最后事件),而不是对应的 open entry。
    若多个 operation 交错,引用 open entry 会导致链立即断裂。

    若文件为空或不存在,返回 "sha256:genesis"。

    Args:
        audit_path: 审计 JSONL 文件路径

    Returns:
        最后一条事件的 digest(形如 "sha256:<hex>"),
        或 "sha256:genesis"(空文件/空链)
    """
    if not audit_path.exists():
        return "sha256:genesis"
    last_entry: dict[str, Any] | None = None
    for line in audit_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            last_entry = json.loads(line)
        except json.JSONDecodeError as e:
            # R74 P1-05: 损坏行导致 hash chain 断裂,不静默跳过
            logger.error(f"审计 JSONL 包含损坏行(hash chain 断裂): {e}")
            raise
    if last_entry is None:
        return "sha256:genesis"
    return _compute_event_digest(last_entry)


@contextlib.contextmanager
def _exclusive_lock(lock_path: Path):
    """跨平台独占文件锁(用于审计 JSONL 并发写入保护)。

    R75 P1-06: 防止多个 break-glass operation 并发交错导致 hash chain 断裂
    (TOCTOU race: 读取最后一条事件 → 计算 digest → 追加新事件)。

    Unix: fcntl.flock(fd, LOCK_EX) — 锁定整个文件
    Windows: msvcrt.locking(fd, LK_LOCK, 1) — 锁定 1 字节范围(阻塞)
    若两者均不可用(极少数平台),退化为无锁(仅依赖 O_APPEND 原子性)。

    Args:
        lock_path: 锁文件路径(与审计 JSONL 同目录)

    Yields:
        None(持有锁期间)
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    try:
        if _msvcrt is not None:
            # Windows: msvcrt.locking 锁定字节范围,需要文件至少有 1 字节
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
                os.fsync(fd)
            os.lseek(fd, 0, 0)
            _msvcrt.locking(fd, _msvcrt.LK_LOCK, 1)  # 阻塞直到获得锁
        elif _fcntl is not None:
            # Unix: fcntl.flock 锁定整个文件
            _fcntl.flock(fd, _fcntl.LOCK_EX)
        else:
            # 无可用锁原语 → 退化为无锁(O_APPEND 模式小写入仍原子)
            logger.warning(
                "无可用文件锁原语(fcntl/msvcrt 均不可用)— 退化为无锁,"
                "依赖 O_APPEND 原子性(仅对小于 PIPE_BUF 的写入有效)"
            )
        yield
    finally:
        if _msvcrt is not None:
            try:
                os.lseek(fd, 0, 0)
                _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        elif _fcntl is not None:
            _fcntl.flock(fd, _fcntl.LOCK_UN)
        os.close(fd)


def _append_audit_event_locked(audit_path: Path, event: dict[str, Any]) -> str:
    """原子地读取最后一条事件的 digest 并追加新事件到审计 JSONL。

    R75 P1-06: 使用文件锁防止并发写入冲突(TOCTOU race)。
    在锁内:
      1. 读取审计文件最后一条物理事件(全局最后事件)
      2. 计算其 digest 作为新事件的 previous_event_digest
      3. 追加新事件到文件(O_APPEND 模式,原子写入)

    注意:previous_event_digest 引用审计文件最后一条物理事件,而不是
    对应的 open entry。若多个 operation 交错,引用 open entry 会导致
    hash chain 立即断裂(详见 R75 P1-06)。

    Args:
        audit_path: 审计 JSONL 文件路径
        event: 待追加的事件 dict(将被原地修改 previous_event_digest 字段)

    Returns:
        新事件被赋予的 previous_event_digest 值
    """
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = audit_path.with_suffix(audit_path.suffix + ".lock")

    with _exclusive_lock(lock_path):
        # 1. 读取最后一行(全局最后物理事件)的 digest
        previous_digest = _get_last_event_digest(audit_path)
        # 2. 设置新事件的 previous_event_digest
        event["previous_event_digest"] = previous_digest
        # 3. 追加到文件(O_APPEND 模式,原子写入)
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, sort_keys=False))
            f.write("\n")

    return previous_digest


def _sign_payload(payload: bytes, key: bytes) -> str:
    """用 HMAC-SHA256 签名 payload,返回带算法前缀的 hex 签名。

    Args:
        payload: 待签名的 bytes(通常为 canonical JSON bytes)
        key: HMAC 密钥

    Returns:
        形如 "hmac-sha256:<64-char-hex>" 的签名字符串
    """
    return "hmac-sha256:" + hmac.new(key, payload, hashlib.sha256).hexdigest()


def _verify_signature(payload: bytes, signature: str, key: bytes) -> bool:
    """验证 HMAC-SHA256 签名。

    Args:
        payload: 原始 bytes
        signature: 签名字符串(接受 "hmac-sha256:<hex>" 或 bare "<hex>")
        key: HMAC 密钥

    Returns:
        True 若签名匹配(使用 hmac.compare_digest 防时序攻击)
    """
    sig_hex = signature.split(":", 1)[1] if signature.startswith("hmac-sha256:") else signature
    expected = hmac.new(key, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig_hex, expected)


def _run_gh(
    args: list[str],
    gh_mock: bool = False,
    input_text: str | None = None,
    audit_path: Path | None = None,
) -> subprocess.CompletedProcess:
    """执行 gh CLI 命令(或 --gh-mock 时返回 mock 结果)。

    所有 gh CLI 调用都通过此函数,便于测试时统一 mock。

    Args:
        args: gh CLI 参数(如 ["issue", "create", "--title", "..."])
        gh_mock: 若为 True,返回 mock 结果(不调用真实 gh)
        input_text: stdin 输入(用于 --input -)
        audit_path: 审计 JSONL 路径(mock 模式下用于推断 issue 编号)

    Returns:
        subprocess.CompletedProcess(returncode / stdout / stderr)
    """
    if gh_mock:
        return _run_gh_mock(args, input_text, audit_path)

    cmd = [GH_CLI_BINARY] + args
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            input=input_text,
            timeout=GH_CLI_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(
            cmd,
            returncode=124,
            stdout=e.stdout or "",
            stderr=f"gh CLI 超时({GH_CLI_TIMEOUT_SEC}s)",
        )
    except OSError as e:
        return subprocess.CompletedProcess(
            cmd, returncode=127, stdout="", stderr=str(e)
        )


def _run_gh_mock(
    args: list[str],
    input_text: str | None = None,
    audit_path: Path | None = None,
) -> subprocess.CompletedProcess:
    """Mock gh CLI — 返回确定性 fake 响应(用于测试)。

    Mock 行为:
      - gh issue create: 返回 mock issue URL(issue 编号从审计 JSONL 已有
        条目数 + 1 推断,保证多次 open 调用 issue 编号唯一)
      - gh issue close: 返回 success
      - gh api .../rulesets/<id>: 返回 _MOCK_RULESET_JSON
      - gh api .../commits/<sha>/check-runs: 返回 _MOCK_CHECK_RUNS(全 success)
      - gh label create: 返回 success
    """
    cmd = [GH_CLI_BINARY] + args

    # gh issue create
    if len(args) >= 2 and args[0] == "issue" and args[1] == "create":
        # 推断下一个 issue 编号(基于审计 JSONL 已有条目数 + 1)
        next_num = 1
        if audit_path is not None and audit_path.exists():
            try:
                lines = [
                    ln for ln in audit_path.read_text(encoding="utf-8").splitlines()
                    if ln.strip()
                ]
                next_num = len(lines) + 1
            except OSError:
                next_num = 1
        mock_url = f"{_MOCK_ISSUE_URL_BASE}/{next_num}"
        return subprocess.CompletedProcess(
            cmd, returncode=0, stdout=mock_url, stderr=""
        )

    # gh issue close
    if len(args) >= 2 and args[0] == "issue" and args[1] == "close":
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    # gh api repos/{owner}/{repo}/rulesets/{id}
    if len(args) >= 2 and args[0] == "api" and "/rulesets/" in args[1]:
        return subprocess.CompletedProcess(
            cmd, returncode=0,
            stdout=json.dumps(_MOCK_RULESET_JSON, ensure_ascii=False),
            stderr="",
        )

    # gh api repos/{owner}/{repo}/commits/{sha}/check-runs
    if len(args) >= 2 and args[0] == "api" and "/check-runs" in args[1]:
        return subprocess.CompletedProcess(
            cmd, returncode=0,
            stdout=json.dumps(_MOCK_CHECK_RUNS, ensure_ascii=False),
            stderr="",
        )

    # gh label create (best-effort)
    if len(args) >= 2 and args[0] == "label" and args[1] == "create":
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    # 默认: success + 空 JSON
    return subprocess.CompletedProcess(cmd, returncode=0, stdout="{}", stderr="")


def _extract_issue_number(issue_url: str) -> str:
    """从 GitHub issue URL 提取 issue 编号。

    URL 格式: https://github.com/{owner}/{repo}/issues/{number}
    """
    return issue_url.rstrip("/").split("/")[-1]


def _format_r73_issue_title(reason: str, actor: str, target_sha: str) -> str:
    """构造 R73 §5.9 break-glass issue 标题。"""
    reason_short = reason[:80] + ("..." if len(reason) > 80 else "")
    return f"BREAK-GLASS: {reason_short} (actor={actor}, sha={target_sha[:12]})"


def _format_r73_issue_body(
    reason: str,
    actor: str,
    target_sha: str,
    ruleset_id: str,
    duration_minutes: int,
    opened_at: str,
    expected_close_by: str,
) -> str:
    """构造 R73 §5.9 break-glass issue 正文(Markdown)。"""
    return f"""## R73 §5.9 Break-Glass Emergency Override

> **R73 §5.9**: This GitHub issue is the **primary audit source** for this
> break-glass event. The JSONL file in the repo (`.github/break-glass-audit.jsonl`)
> is a secondary mirror copy and must not be the only audit source.

| Field | Value |
|-------|-------|
| Actor | @{actor} |
| Target SHA | `{target_sha}` |
| Ruleset ID | `{ruleset_id}` |
| Duration | {duration_minutes} minutes |
| Opened At | `{opened_at}` |
| Expected Close By | `{expected_close_by}` |

### Reason

{reason}

### Auto-Close Checklist (R73 §5.9)

- [ ] Ruleset restored to pre-break-glass state (digest matches)
- [ ] `enforcement=active` verified via `gh api`
- [ ] current-SHA required checks passed
- [ ] Signed closure artifact generated
- [ ] This issue auto-closed by `record_break_glass.py close`

### Tamper-Evidence

- Ruleset JSON was signed with HMAC-SHA256 **before** modification.
- Signed snapshot stored at `.github/break-glass-<issue>-<sha-short>.json`.
- Closure artifact will be generated at `.github/break-glass-closure-<issue>-<sha-short>.json`.

*This issue was auto-created by `scripts/record_break_glass.py open` (R73 §5.9 P1-03).*
"""


def _create_break_glass_issue(
    reason: str,
    actor: str,
    target_sha: str,
    ruleset_id: str,
    duration_minutes: int,
    opened_at: str,
    expected_close_by: str,
    repo: str,
    gh_mock: bool = False,
    audit_path: Path | None = None,
) -> tuple[int, str]:
    """通过 gh CLI 创建 GitHub issue(R73 §5.9 外部审计源)。

    Args:
        reason: break-glass 原因
        actor: 操作员用户名
        target_sha: 待 override 的 commit SHA(40-char hex)
        ruleset_id: 待修改的 ruleset ID
        duration_minutes: 预期持续时间(分钟)
        opened_at: 开启时间(ISO 8601)
        expected_close_by: 预期关闭时间(ISO 8601)
        repo: 目标仓库(owner/repo)
        gh_mock: 是否使用 mock gh CLI
        audit_path: 审计 JSONL 路径(mock 模式下用于推断 issue 编号)

    Returns:
        (issue_number, issue_url) 元组

    Raises:
        RuntimeError: gh issue create 失败
    """
    title = _format_r73_issue_title(reason, actor, target_sha)
    body = _format_r73_issue_body(
        reason, actor, target_sha, ruleset_id,
        duration_minutes, opened_at, expected_close_by,
    )
    args = [
        "issue", "create",
        "--title", title,
        "--body", body,
        "--label", "break-glass",
        "--repo", repo,
    ]
    result = _run_gh(args, gh_mock=gh_mock, audit_path=audit_path)
    if result.returncode != 0:
        raise RuntimeError(
            f"gh issue create 失败(exit={result.returncode})— "
            f"stderr: {result.stderr.strip()}"
        )
    issue_url = result.stdout.strip()
    if not issue_url.startswith("http"):
        raise RuntimeError(
            f"gh issue create 返回非 URL 输出: {issue_url!r}"
        )
    issue_number_str = _extract_issue_number(issue_url)
    if not issue_number_str.isdigit():
        raise RuntimeError(
            f"无法从 issue URL 提取编号: {issue_url!r}"
        )
    return int(issue_number_str), issue_url


def _export_ruleset_json(
    ruleset_id: str, repo: str, gh_mock: bool = False
) -> dict[str, Any]:
    """通过 gh api 导出当前 ruleset JSON。

    Args:
        ruleset_id: ruleset ID
        repo: 目标仓库(owner/repo)
        gh_mock: 是否使用 mock gh CLI

    Returns:
        ruleset JSON 对象

    Raises:
        RuntimeError: gh api 调用失败或响应非 JSON
    """
    endpoint = f"repos/{repo}/rulesets/{ruleset_id}"
    result = _run_gh(["api", endpoint], gh_mock=gh_mock)
    if result.returncode != 0:
        raise RuntimeError(
            f"gh api {endpoint} 失败(exit={result.returncode})— "
            f"stderr: {result.stderr.strip()}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"gh api {endpoint} 返回非 JSON 响应: {result.stdout[:200]!r} — {e}"
        )


def _verify_enforcement_active(
    ruleset_id: str, repo: str, gh_mock: bool = False
) -> tuple[bool, str]:
    """校验 ruleset enforcement=active。

    Returns:
        (is_active, enforcement_value) 元组
    """
    ruleset = _export_ruleset_json(ruleset_id, repo, gh_mock=gh_mock)
    enforcement = str(ruleset.get("enforcement", ""))
    return enforcement == "active", enforcement


def _run_current_sha_checks(
    ruleset_id: str,
    target_sha: str,
    repo: str,
    gh_mock: bool = False,
) -> tuple[bool, list[str]]:
    """重跑/校验 current-SHA required checks。

    从 ruleset 提取 required_status_checks.context 列表,然后查询
    `gh api repos/{owner}/{repo}/commits/{sha}/check-runs`,校验所有
    required context 的 conclusion 均为 "success"。

    Args:
        ruleset_id: ruleset ID(用于提取 required checks 列表)
        target_sha: 待校验的 commit SHA
        repo: 目标仓库
        gh_mock: 是否使用 mock gh CLI

    Returns:
        (all_passed, failed_checks) 元组 — all_passed=True 表示全部通过,
        failed_checks 为未通过(或缺失)的 context 名列表
    """
    # 1. 从 ruleset 提取 required status checks
    ruleset = _export_ruleset_json(ruleset_id, repo, gh_mock=gh_mock)
    required_contexts: list[str] = []
    for rule in ruleset.get("rules", []):
        if rule.get("type") == "required_status_checks":
            for check in rule.get("parameters", {}).get("required_status_checks", []):
                ctx = check.get("context", "") if isinstance(check, dict) else str(check)
                if ctx:
                    required_contexts.append(ctx)

    # 2. 查询 check-runs
    endpoint = f"repos/{repo}/commits/{target_sha}/check-runs"
    result = _run_gh(["api", endpoint], gh_mock=gh_mock)
    if result.returncode != 0:
        raise RuntimeError(
            f"gh api {endpoint} 失败(exit={result.returncode})— "
            f"stderr: {result.stderr.strip()}"
        )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"gh api {endpoint} 返回非 JSON: {result.stdout[:200]!r} — {e}"
        )

    # 3. 构建 context -> conclusion 映射
    conclusions: dict[str, str] = {}
    for run in data.get("check_runs", []):
        name = run.get("name", "")
        conclusions[name] = str(run.get("conclusion", ""))

    # 4. 校验所有 required context
    failed: list[str] = []
    for ctx in required_contexts:
        if conclusions.get(ctx) != "success":
            failed.append(ctx)
    return len(failed) == 0, failed


def _close_break_glass_issue(
    issue_number: int, repo: str, gh_mock: bool = False
) -> bool:
    """关闭 GitHub issue 并添加 closure comment。"""
    args = [
        "issue", "close", str(issue_number),
        "--comment", "Closed: ruleset restored, current-SHA checks passed",
        "--repo", repo,
    ]
    result = _run_gh(args, gh_mock=gh_mock)
    return result.returncode == 0


def _read_audit_entries(audit_path: Path) -> list[dict[str, Any]]:
    """读取 break-glass 审计 JSONL 全部条目。

    兼容旧版(R71/R72,无 status / kind 字段)与新版(R73,有 status=open/closed)。
    旧版条目视为已关闭(不阻断 verify-closed)。
    """
    if not audit_path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in audit_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError as e:
            # R74 P1-05: 损坏行导致 hash chain 断裂,不静默跳过
            logger.error(f"审计 JSONL 包含损坏行(hash chain 断裂): {e}")
            raise
    return entries


def _find_open_entry(
    entries: list[dict[str, Any]],
    issue_number: int | None,
    operation_id: str | None,
) -> dict[str, Any] | None:
    """按 issue_number 或 operation_id 查找 status=open 的审计条目。"""
    for entry in entries:
        # 仅 R73 §5.9 条目有 status 字段;旧版条目无 status,视为非 open
        if entry.get("status") != "open":
            continue
        if issue_number is not None and entry.get("issue_number") == issue_number:
            return entry
        if operation_id is not None and entry.get("operation_id") == operation_id:
            return entry
    return None


def verify_hash_chain(audit_path: Path) -> tuple[bool, str]:
    """验证审计 JSONL 的 hash chain 完整性。

    Returns:
        (valid, error_message) - valid=True 表示链完整
    """
    entries = _read_audit_entries(audit_path)
    if not entries:
        return True, "empty chain"

    for i in range(1, len(entries)):
        prev_entry = entries[i - 1]
        curr_entry = entries[i]
        expected_prev_digest = _compute_event_digest(prev_entry)
        actual_prev_digest = curr_entry.get("previous_event_digest", "")
        if expected_prev_digest != actual_prev_digest:
            return False, (
                f"hash chain broken at entry {i}: "
                f"expected {expected_prev_digest[:20]}..., "
                f"got {actual_prev_digest[:20]}..."
            )
    return True, "hash chain verified"


# ════════════════════════════════════════════════════════════════
# R73 §5.9 (P1-03): 子命令处理器
# ════════════════════════════════════════════════════════════════


def cmd_open(argv: list[str]) -> int:
    """R73 §5.9 open 子命令:创建外部审计 issue + 签名 ruleset 快照。

    流程(R73 §5.9):
      a. 创建 GitHub Issue(外部审计源,**在修改 ruleset 之前**)
      b. 捕获 issue URL 与编号
      c. 通过 gh api 导出当前 ruleset JSON
      d. 计算 ruleset JSON 的 SHA-256 digest
      e. 用 HMAC-SHA256 签名 ruleset JSON(BREAK_GLASS_SIGNING_KEY 优先)
      f. 写入签名快照到 .github/break-glass-<issue>-<sha-short>.json
      g. 追加条目到 .github/break-glass-audit.jsonl(镜像副本)
      h. 输出 JSON 到 stdout
    """
    parser = argparse.ArgumentParser(
        prog="record_break_glass.py open",
        description=(
            "R73 §5.9 (P1-03): 创建外部审计 issue + 签名 ruleset 快照。"
            "必须在临时修改 ruleset **之前**调用。"
        ),
    )
    parser.add_argument(
        "--reason", required=True,
        help="break-glass 原因(详细描述紧急情况)",
    )
    parser.add_argument(
        "--target-sha", required=True,
        help="待 override 的 commit SHA(40-char hex)",
    )
    parser.add_argument(
        "--ruleset-id", required=True,
        help="待修改的 GitHub Ruleset ID",
    )
    parser.add_argument(
        "--actor", required=True,
        help="操作员 GitHub 用户名(如 maxiuquan)",
    )
    parser.add_argument(
        "--duration-minutes", type=int, default=DEFAULT_DURATION_MINUTES,
        help=f"预期持续时间(分钟,默认 {DEFAULT_DURATION_MINUTES})",
    )
    parser.add_argument(
        "--repo", default=DEFAULT_REPO,
        help=f"目标仓库(owner/repo,默认 {DEFAULT_REPO})",
    )
    parser.add_argument(
        "--audit-path", default=DEFAULT_AUDIT_PATH,
        help=f"审计 JSONL 路径(默认 {DEFAULT_AUDIT_PATH})",
    )
    parser.add_argument(
        "--snapshots-dir", default=DEFAULT_SNAPSHOTS_DIR,
        help=f"快照/closure artifact 目录(默认 {DEFAULT_SNAPSHOTS_DIR})",
    )
    parser.add_argument(
        "--gh-mock", action="store_true", default=False,
        help="使用 mock gh CLI(仅用于测试)",
    )

    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else EXIT_CLI_ERROR
        return EXIT_CLI_ERROR if code != 0 else EXIT_SUCCESS

    # 校验 target SHA
    if not SOURCE_SHA_PATTERN.match(args.target_sha):
        logger.error(
            f"--target-sha 必须为 40-char hex(实际: {args.target_sha[:20]}... 长度 {len(args.target_sha)})"
        )
        return EXIT_VALIDATION_FAILURE

    # 获取签名密钥
    try:
        signing_key = _get_signing_key()
    except RuntimeError as e:
        logger.error(str(e))
        return EXIT_VALIDATION_FAILURE

    audit_path = Path(args.audit_path)
    snapshots_dir = Path(args.snapshots_dir)
    opened_at = _now_iso()
    expected_close_by = (
        _dt.datetime.now(_dt.timezone.utc)
        + _dt.timedelta(minutes=args.duration_minutes)
    ).isoformat()
    target_sha_short = args.target_sha[:12]

    # ─── Step a-b: 创建 GitHub issue(外部审计源,**修改 ruleset 之前**) ───
    logger.info("=== R73 §5.9 open: 创建外部审计 issue(修改 ruleset 之前)===")
    try:
        issue_number, issue_url = _create_break_glass_issue(
            reason=args.reason,
            actor=args.actor,
            target_sha=args.target_sha,
            ruleset_id=args.ruleset_id,
            duration_minutes=args.duration_minutes,
            opened_at=opened_at,
            expected_close_by=expected_close_by,
            repo=args.repo,
            gh_mock=args.gh_mock,
            audit_path=audit_path,
        )
    except RuntimeError as e:
        logger.error(f"FAIL: 创建 GitHub issue 失败 — {e}")
        logger.error(
            "  R73 §5.9: 外部审计 issue 是主要审计源,创建失败时不修改 ruleset。"
            "请修复 gh CLI 后重试。"
        )
        return EXIT_VALIDATION_FAILURE
    logger.info(f"PASS: 审计 issue 已创建 — #{issue_number} {issue_url}")

    # ─── Step c: 导出当前 ruleset JSON ───
    logger.info(
        f"=== R73 §5.9 open: 导出当前 ruleset JSON(id={args.ruleset_id})==="
    )
    try:
        ruleset_json = _export_ruleset_json(
            args.ruleset_id, args.repo, gh_mock=args.gh_mock
        )
    except RuntimeError as e:
        logger.error(f"FAIL: 导出 ruleset JSON 失败 — {e}")
        return EXIT_VALIDATION_FAILURE

    # ─── Step d: 计算 digest ───
    ruleset_digest_before = _compute_ruleset_digest(ruleset_json)
    logger.info(f"PASS: ruleset digest = {ruleset_digest_before}")

    # ─── Step e: 签名 ruleset JSON ───
    signature = _sign_payload(_canonical_json_bytes(ruleset_json), signing_key)
    logger.info(f"PASS: signature = {signature[:40]}...")

    # ─── 生成 operation_id ───
    operation_id = (
        f"bg-{issue_number}-{target_sha_short}-"
        f"{int(_dt.datetime.now(_dt.timezone.utc).timestamp())}"
    )

    # ─── Step f: 写入签名快照 ───
    snapshot_path = (
        snapshots_dir / f"break-glass-{issue_number}-{target_sha_short}.json"
    )
    snapshot: dict[str, Any] = {
        "schema_version": R73_SCHEMA_VERSION,
        "kind": "r73-break-glass-ruleset-snapshot",
        "operation_id": operation_id,
        "issue_number": issue_number,
        "issue_url": issue_url,
        "actor": args.actor,
        "reason": args.reason,
        "target_sha": args.target_sha,
        "ruleset_id": args.ruleset_id,
        "ruleset_digest_before": ruleset_digest_before,
        "signature": signature,
        "signed_at": opened_at,
        "ruleset_json": ruleset_json,
    }
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(f"PASS: 签名快照已写入 — {snapshot_path}")

    # ─── Step g: 追加到审计 JSONL(镜像副本) ───
    # R74 P1-05: 追加 previous_event_digest 形成 hash chain
    # R75 P1-06: 使用文件锁防止并发写入冲突(TOCTOU race);
    #            previous_event_digest 引用审计文件最后一条物理事件
    audit_entry: dict[str, Any] = {
        "schema_version": R73_SCHEMA_VERSION,
        "kind": "r73-break-glass-open",
        "operation_id": operation_id,
        "issue_number": issue_number,
        "issue_url": issue_url,
        "actor": args.actor,
        "reason": args.reason,
        "target_sha": args.target_sha,
        "ruleset_id": args.ruleset_id,
        "ruleset_digest_before": ruleset_digest_before,
        "signature": signature,
        "opened_at": opened_at,
        "expected_close_by": expected_close_by,
        "duration_minutes": args.duration_minutes,
        "status": "open",
        # previous_event_digest 由 _append_audit_event_locked 填充
    }
    _append_audit_event_locked(audit_path, audit_entry)
    logger.info(f"PASS: 审计条目已追加 — {audit_path}")

    # ─── Step h: 输出 JSON 到 stdout ───
    output: dict[str, Any] = {
        "status": "opened",
        "operation_id": operation_id,
        "issue_number": issue_number,
        "issue_url": issue_url,
        "actor": args.actor,
        "reason": args.reason,
        "target_sha": args.target_sha,
        "ruleset_id": args.ruleset_id,
        "ruleset_digest_before": ruleset_digest_before,
        "signature": signature,
        "opened_at": opened_at,
        "expected_close_by": expected_close_by,
        "duration_minutes": args.duration_minutes,
        "snapshot_path": str(snapshot_path),
        "audit_path": str(audit_path),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))

    logger.info(f"=== R73 §5.9 open 完成 — operation_id={operation_id} ===")
    logger.info(f"  外部 issue: {issue_url}")
    logger.info(f"  快照: {snapshot_path}")
    logger.info(f"  审计 JSONL: {audit_path}")
    logger.info(
        f"  现在可以临时修改 ruleset {args.ruleset_id}。"
        f"完成后运行: record_break_glass.py close --issue-number {issue_number}"
    )
    return EXIT_SUCCESS


def cmd_close(argv: list[str]) -> int:
    """R73 §5.9 close 子命令:校验恢复 + 生成 closure artifact + 关闭 issue。

    流程(R73 §5.9):
      a. 读取审计条目(按 --issue-number 或 --operation-id)
      b. 重新导出当前 ruleset JSON
      c. 计算 digest,与 pre-break-glass digest 对比(校验恢复)
      d. 校验 enforcement=active
      e. 重跑 current-SHA required checks
      f. 生成签名 closure artifact
      g. 关闭 GitHub issue(带 closure comment)
      h. 更新审计 JSONL 条目 status=closed
      i. 输出 JSON closure 摘要
    """
    parser = argparse.ArgumentParser(
        prog="record_break_glass.py close",
        description=(
            "R73 §5.9 (P1-03): 校验 ruleset 已恢复 + 生成 closure artifact + 关闭 issue。"
            "合并后必须调用以闭环 break-glass 事件。"
        ),
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--issue-number", type=int,
        help="待关闭的 break-glass issue 编号",
    )
    group.add_argument(
        "--operation-id",
        help="待关闭的 break-glass operation_id(open 时生成)",
    )
    parser.add_argument(
        "--repo", default=DEFAULT_REPO,
        help=f"目标仓库(owner/repo,默认 {DEFAULT_REPO})",
    )
    parser.add_argument(
        "--audit-path", default=DEFAULT_AUDIT_PATH,
        help=f"审计 JSONL 路径(默认 {DEFAULT_AUDIT_PATH})",
    )
    parser.add_argument(
        "--snapshots-dir", default=DEFAULT_SNAPSHOTS_DIR,
        help=f"closure artifact 目录(默认 {DEFAULT_SNAPSHOTS_DIR})",
    )
    parser.add_argument(
        "--gh-mock", action="store_true", default=False,
        help="使用 mock gh CLI(仅用于测试)",
    )

    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else EXIT_CLI_ERROR
        return EXIT_CLI_ERROR if code != 0 else EXIT_SUCCESS

    # 获取签名密钥
    try:
        signing_key = _get_signing_key()
    except RuntimeError as e:
        logger.error(str(e))
        return EXIT_VALIDATION_FAILURE

    # ─── Step a: 读取审计条目 ───
    audit_path = Path(args.audit_path)
    entries = _read_audit_entries(audit_path)
    open_entry = _find_open_entry(entries, args.issue_number, args.operation_id)
    if not open_entry:
        logger.error("FAIL: 未找到匹配的 open break-glass 审计条目")
        logger.error(f"  审计路径: {audit_path}")
        logger.error(
            f"  查询条件: issue_number={args.issue_number} "
            f"operation_id={args.operation_id}"
        )
        return EXIT_VALIDATION_FAILURE

    operation_id = str(open_entry.get("operation_id", ""))
    logger.info(
        f"=== R73 §5.9 close: 找到 open 条目 — operation_id={operation_id} ==="
    )

    issue_number = int(open_entry["issue_number"])
    target_sha = str(open_entry["target_sha"])
    ruleset_id = str(open_entry["ruleset_id"])
    ruleset_digest_before = str(open_entry["ruleset_digest_before"])
    target_sha_short = target_sha[:12]

    # ─── Step b: 重新导出当前 ruleset JSON ───
    logger.info(
        f"=== R73 §5.9 close: 重新导出 ruleset JSON(id={ruleset_id})==="
    )
    try:
        ruleset_after = _export_ruleset_json(
            ruleset_id, args.repo, gh_mock=args.gh_mock
        )
    except RuntimeError as e:
        logger.error(f"FAIL: 重新导出 ruleset JSON 失败 — {e}")
        return EXIT_VALIDATION_FAILURE

    # ─── Step c: 计算 digest 并对比 ───
    ruleset_digest_after = _compute_ruleset_digest(ruleset_after)
    logger.info(f"PASS: ruleset digest after = {ruleset_digest_after}")
    restoration_verified = ruleset_digest_after == ruleset_digest_before
    if restoration_verified:
        logger.info("PASS: 恢复已校验 — digest 与 pre-break-glass 一致")
    else:
        logger.warning("WARN: digest 不一致 — ruleset 可能已被有意修改")
        logger.warning(f"  before: {ruleset_digest_before}")
        logger.warning(f"  after:  {ruleset_digest_after}")

    # ─── Step d: 校验 enforcement=active ───
    logger.info("=== R73 §5.9 close: 校验 enforcement=active ===")
    enforcement_active, enforcement_value = _verify_enforcement_active(
        ruleset_id, args.repo, gh_mock=args.gh_mock
    )
    if not enforcement_active:
        logger.error(
            f"FAIL: enforcement != active(实际: {enforcement_value})— "
            "R73 §5.9: ruleset 必须为 active 才能关闭 break-glass"
        )
        return EXIT_VALIDATION_FAILURE
    logger.info("PASS: enforcement=active")

    # ─── Step e: 重跑 current-SHA required checks ───
    logger.info(
        f"=== R73 §5.9 close: 重跑 current-SHA checks(sha={target_sha_short}...)==="
    )
    try:
        checks_passed, failed_checks = _run_current_sha_checks(
            ruleset_id, target_sha, args.repo, gh_mock=args.gh_mock
        )
    except RuntimeError as e:
        logger.error(f"FAIL: 重跑 current-SHA checks 失败 — {e}")
        return EXIT_VALIDATION_FAILURE
    if not checks_passed:
        logger.error(
            f"FAIL: current-SHA checks 未通过 — {len(failed_checks)} 项失败:"
        )
        for fc in failed_checks:
            logger.error(f"  - {fc}")
        logger.error(
            "  R73 §5.9: 所有 required checks 必须通过才能关闭 break-glass"
        )
        return EXIT_VALIDATION_FAILURE
    logger.info("PASS: 所有 current-SHA checks 通过")

    # ─── Step f: 生成签名 closure artifact ───
    closed_at = _now_iso()
    closure_payload: dict[str, Any] = {
        "schema_version": R73_SCHEMA_VERSION,
        "kind": "r73-break-glass-closure",
        "operation_id": operation_id,
        "issue_number": issue_number,
        "target_sha": target_sha,
        "ruleset_id": ruleset_id,
        "ruleset_digest_before": ruleset_digest_before,
        "ruleset_digest_after": ruleset_digest_after,
        "restoration_verified": restoration_verified,
        "enforcement_active": enforcement_active,
        "current_sha_checks_passed": checks_passed,
        "failed_checks": failed_checks,
        "closed_at": closed_at,
        "closed_by": str(open_entry.get("actor", "")),
    }
    closure_signature = _sign_payload(
        _canonical_json_bytes(closure_payload), signing_key
    )
    closure_payload["signature"] = closure_signature

    closure_path = (
        Path(args.snapshots_dir)
        / f"break-glass-closure-{issue_number}-{target_sha_short}.json"
    )
    closure_path.parent.mkdir(parents=True, exist_ok=True)
    closure_path.write_text(
        json.dumps(closure_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(f"PASS: closure artifact 已写入 — {closure_path}")

    # ─── Step g: 关闭 GitHub issue ───
    logger.info(f"=== R73 §5.9 close: 关闭 GitHub issue #{issue_number} ===")
    issue_closed = _close_break_glass_issue(
        issue_number, args.repo, gh_mock=args.gh_mock
    )
    if not issue_closed:
        # R75 P1-06: issue 关闭失败为致命错误 — 不得追加 closed 事件
        # operation 保持 open 状态,operator 须修复 gh CLI 后重试
        logger.error(
            "FAIL: gh issue close 失败 — R75 P1-06: issue 关闭失败为致命错误,"
            "不得追加 closed 事件。operation 保持 open 状态。"
            f"closure artifact 已写入({closure_path}),但需修复 gh CLI 后"
            f"重试: record_break_glass.py close --issue-number {issue_number}"
        )
        return EXIT_VALIDATION_FAILURE
    logger.info(f"PASS: issue #{issue_number} 已关闭")

    # ─── Step h: 追加 closed 事件到审计 JSONL(append-only hash chain) ───
    # R74 P1-05: 追加 closed 事件替代重写历史(append-only hash chain)
    # R75 P1-06: previous_event_digest 引用审计文件最后一条物理事件(全局最后),
    #            而不是对应的 open entry,防止并发交错导致链断裂。
    #            使用文件锁防止并发写入冲突(TOCTOU race)。
    #            closed 事件只有在 issue 关闭成功 + ruleset 恢复 +
    #            current-SHA checks 通过后才追加。
    closed_event: dict[str, Any] = {
        "schema_version": R73_SCHEMA_VERSION,
        "kind": "r73-break-glass-closed",
        "operation_id": operation_id,
        "issue_number": issue_number,
        "target_sha": target_sha,
        "ruleset_id": ruleset_id,
        "ruleset_digest_before": ruleset_digest_before,
        "ruleset_digest_after": ruleset_digest_after,
        "restoration_verified": restoration_verified,
        "enforcement_active": enforcement_active,
        "current_sha_checks_passed": checks_passed,
        "closed_at": closed_at,
        "closure_signature": closure_signature,
        "closure_path": str(closure_path),
        "issue_closed": issue_closed,
        "status": "closed",
        # previous_event_digest 由 _append_audit_event_locked 填充
    }
    _append_audit_event_locked(audit_path, closed_event)
    logger.info("PASS: closed 事件已追加到审计 JSONL — append-only hash chain")

    # ─── Step i: 输出 JSON closure 摘要 ───
    output: dict[str, Any] = {
        "status": "closed",
        "operation_id": operation_id,
        "issue_number": issue_number,
        "target_sha": target_sha,
        "ruleset_id": ruleset_id,
        "ruleset_digest_before": ruleset_digest_before,
        "ruleset_digest_after": ruleset_digest_after,
        "restoration_verified": restoration_verified,
        "enforcement_active": enforcement_active,
        "current_sha_checks_passed": checks_passed,
        "failed_checks": failed_checks,
        "closed_at": closed_at,
        "closure_path": str(closure_path),
        "issue_closed": issue_closed,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))

    logger.info(f"=== R73 §5.9 close 完成 — operation_id={operation_id} ===")
    return EXIT_SUCCESS


def cmd_verify_closed(argv: list[str]) -> int:
    """R73 §5.9 verify-closed 子命令:存在 open 事件则退出非零。

    用于 CI / 定时检查:确保没有遗漏的 open break-glass 事件。
    旧版(R71/R72)条目无 status 字段,视为已关闭(不阻断)。
    """
    parser = argparse.ArgumentParser(
        prog="record_break_glass.py verify-closed",
        description=(
            "R73 §5.9 (P1-03): 校验所有 break-glass 事件已关闭。"
            "若存在 status=open 的条目,退出码 1。"
        ),
    )
    parser.add_argument(
        "--audit-path", default=DEFAULT_AUDIT_PATH,
        help=f"审计 JSONL 路径(默认 {DEFAULT_AUDIT_PATH})",
    )

    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else EXIT_CLI_ERROR
        return EXIT_CLI_ERROR if code != 0 else EXIT_SUCCESS

    audit_path = Path(args.audit_path)

    # R74 P1-05: 先验证 hash chain 完整性
    chain_valid, chain_msg = verify_hash_chain(audit_path)
    if not chain_valid:
        logger.error(f"FAIL: 审计 JSONL hash chain 断裂 — {chain_msg}")
        return EXIT_VALIDATION_FAILURE
    logger.info(f"PASS: hash chain — {chain_msg}")

    entries = _read_audit_entries(audit_path)
    # R76 P0-01: close 子命令采用 append-only hash chain 追加 closed 事件,
    # 不修改原 open 条目。verify-closed 必须排除已有对应 closed 事件的 open 条目,
    # 否则已闭环的 operation 会永远报 open(FP),阻断 CI。
    closed_operation_ids: set[str] = set()
    for e in entries:
        if e.get("status") == "closed" and e.get("operation_id"):
            closed_operation_ids.add(str(e["operation_id"]))
    open_entries = [
        e for e in entries
        if e.get("status") == "open"
        and str(e.get("operation_id", "")) not in closed_operation_ids
    ]

    if not open_entries:
        output: dict[str, Any] = {
            "status": "ok",
            "open_count": 0,
            "audit_path": str(audit_path),
            "message": "无 open break-glass 事件",
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return EXIT_SUCCESS

    # 存在 open 事件 — 退出非零
    output = {
        "status": "open-events-exist",
        "open_count": len(open_entries),
        "audit_path": str(audit_path),
        "open_events": [
            {
                "operation_id": e.get("operation_id", ""),
                "issue_number": e.get("issue_number"),
                "issue_url": e.get("issue_url", ""),
                "actor": e.get("actor", ""),
                "reason": e.get("reason", ""),
                "target_sha": e.get("target_sha", ""),
                "opened_at": e.get("opened_at", ""),
                "expected_close_by": e.get("expected_close_by", ""),
            }
            for e in open_entries
        ],
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    logger.error(f"FAIL: 发现 {len(open_entries)} 个 open break-glass 事件:")
    for e in open_entries:
        logger.error(
            f"  - #{e.get('issue_number')} {e.get('issue_url', '')} "
            f"(opened {e.get('opened_at', '')})"
        )
    return EXIT_VALIDATION_FAILURE


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

    R73 §5.9 (P1-03): 支持 open / close / verify-closed 子命令。
    无子命令时回退到 legacy flat-arg CLI(向后兼容 R71/R72 测试与脚本)。

    Returns:
        0 success, 1 validation failure, 2 CLI error
    """
    if argv is None:
        argv = sys.argv[1:]

    # R73 §5.9: 子命令分发
    if argv and argv[0] in R73_SUBCOMMANDS:
        return _run_subcommand(argv)

    # Legacy flat-arg CLI(向后兼容 R71 P1-01 + R72 P1-07)
    return _run_legacy_cli(argv)


def _run_legacy_cli(argv: list[str]) -> int:
    """Legacy flat-arg CLI(R71 P1-01 + R72 P1-07)。

    保留原 CLI 接口(--operator / --sha / --reason / --risk / --rollback-plan /
    --typed-confirmation / --output / --no-create-issue / --repo)以兼容
    现有测试与 CI 脚本。新代码应使用 `open` / `close` / `verify-closed` 子命令。
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


def _run_subcommand(argv: list[str]) -> int:
    """R73 §5.9 子命令分发器。"""
    command = argv[0]
    rest = argv[1:]

    # 配置 loguru 输出到 stderr
    logger.remove()
    logger.add(sys.stderr, level="INFO")

    if command == "open":
        return cmd_open(rest)
    if command == "close":
        return cmd_close(rest)
    if command == "verify-closed":
        return cmd_verify_closed(rest)
    # 不应到达此处(main 已校验)
    logger.error(f"未知子命令: {command}")
    return EXIT_CLI_ERROR


if __name__ == "__main__":
    sys.exit(main())
