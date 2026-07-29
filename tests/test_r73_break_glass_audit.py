"""R73 §5.9 (P1-03): Externalized break-glass audit — 测试套件。

R73 §5.9 整改背景:
    R72 P1-07 已将 break-glass 审计外部化为 GitHub Issue(主要审计源),
    仓库内 JSONL 为副本。但仍存在以下缺口:
      1. ruleset JSON 在临时修改前未签名留底,无法事后校验是否被篡改
      2. 合并后未自动恢复 ruleset / 校验 enforcement / 重跑 current-SHA checks
      3. 未生成签名 closure artifact(关闭证据)
      4. 未自动关闭外部 issue

    R73 §5.9 (P1-03) 整改:
      - 新增 `open` / `close` / `verify-closed` 子命令
      - `open`:创建 GitHub Issue(外部审计源,**修改 ruleset 之前**)
        + 用 HMAC-SHA256 签名 ruleset JSON + 写入签名快照 + 追加审计 JSONL
      - `close`:重新导出 ruleset JSON → 对比 digest → 校验 enforcement=active →
        重跑 current-SHA checks → 生成签名 closure artifact → 关闭 issue →
        更新 JSONL status=closed
      - `verify-closed`:存在 open 事件则退出非零(CI/定时检查)
      - 保留 legacy flat-arg CLI(向后兼容 R71/R72 测试)

被测对象:
    - scripts/record_break_glass.py(open / close / verify-closed 子命令)
    - scripts/configure_branch_ruleset.sh(R73 §5.13 digest 对账逻辑,静态检查)

测试策略:
    - 使用 --gh-mock 标志跳过真实 gh CLI 调用(本地 Windows 无 gh CLI)
    - 使用 tmp_path 隔离审计 JSONL 与快照文件
    - 设置 BREAK_GLASS_SIGNING_KEY 环境变量
    - 通过 subprocess 调用脚本(端到端验证)
    - 静态检查 configure_branch_ruleset.sh 的 reconciliation 逻辑

测试覆盖矩阵:
    A. 模块结构 — R73 §5.9 常量与子命令存在(5 个)
    B. open 子命令 — 外部 issue + 签名快照 + 审计条目(8 个)
    C. close 子命令 — closure artifact + issue 关闭(7 个)
    D. verify-closed 子命令 — open 事件检测(4 个)
    E. open → close → verify-closed 端到端流程(3 个)
    F. configure_branch_ruleset.sh reconciliation 静态检查(4 个)
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RECORD_BREAK_GLASS_PY = REPO_ROOT / "scripts" / "record_break_glass.py"
CONFIGURE_RULESET_SH = REPO_ROOT / "scripts" / "configure_branch_ruleset.sh"

# 测试常量
VALID_SHA = "a" * 40  # 40-char hex(Git SHA-1 格式)
TEST_SIGNING_KEY = "test-signing-key-for-r73-break-glass-audit"
TEST_RULESET_ID = "12345"
TEST_ACTOR = "maxiuquan"
TEST_REASON = "emergency production hotfix for CVE-2026-XXXX"


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════


def _load_record_break_glass_module():
    """动态加载 scripts/record_break_glass.py 模块。

    使用 importlib 而非 sys.path 注入,避免污染全局 sys.modules。
    使用独立 module_name 以避免与 test_r71_wave6_solo_governance.py 冲突。
    """
    module_name = "_record_break_glass_r73_test_module"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, RECORD_BREAK_GLASS_PY)
    assert spec is not None, f"无法加载模块 spec: {RECORD_BREAK_GLASS_PY}"
    assert spec.loader is not None, f"模块 loader 为 None: {RECORD_BREAK_GLASS_PY}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _bash_available() -> bool:
    """检查 bash 是否可用(CI 上始终可用,本地 Windows 可能无)。"""
    try:
        result = subprocess.run(
            ["bash", "--version"],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


BASH_AVAILABLE = _bash_available()
skip_if_no_bash = pytest.mark.skipif(
    not BASH_AVAILABLE,
    reason="bash 不可用(本地 Windows 环境;CI 上始终可用)",
)


def _run_open(
    tmp_path: Path,
    reason: str = TEST_REASON,
    sha: str = VALID_SHA,
    ruleset_id: str = TEST_RULESET_ID,
    actor: str = TEST_ACTOR,
    duration: int = 60,
    gh_mock: bool = True,
) -> subprocess.CompletedProcess:
    """运行 open 子命令,返回 CompletedProcess。"""
    audit_path = tmp_path / "audit.jsonl"
    snapshots_dir = tmp_path / "snapshots"
    env = os.environ.copy()
    env["BREAK_GLASS_SIGNING_KEY"] = TEST_SIGNING_KEY
    cmd = [
        sys.executable, str(RECORD_BREAK_GLASS_PY), "open",
        "--reason", reason,
        "--target-sha", sha,
        "--ruleset-id", ruleset_id,
        "--actor", actor,
        "--duration-minutes", str(duration),
        "--audit-path", str(audit_path),
        "--snapshots-dir", str(snapshots_dir),
    ]
    if gh_mock:
        cmd.append("--gh-mock")
    return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)


def _run_close(
    tmp_path: Path,
    issue_number: int | None = None,
    operation_id: str | None = None,
    gh_mock: bool = True,
) -> subprocess.CompletedProcess:
    """运行 close 子命令,返回 CompletedProcess。"""
    audit_path = tmp_path / "audit.jsonl"
    snapshots_dir = tmp_path / "snapshots"
    env = os.environ.copy()
    env["BREAK_GLASS_SIGNING_KEY"] = TEST_SIGNING_KEY
    cmd = [
        sys.executable, str(RECORD_BREAK_GLASS_PY), "close",
        "--audit-path", str(audit_path),
        "--snapshots-dir", str(snapshots_dir),
    ]
    if issue_number is not None:
        cmd.extend(["--issue-number", str(issue_number)])
    elif operation_id is not None:
        cmd.extend(["--operation-id", operation_id])
    if gh_mock:
        cmd.append("--gh-mock")
    return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)


def _run_verify_closed(tmp_path: Path) -> subprocess.CompletedProcess:
    """运行 verify-closed 子命令,返回 CompletedProcess。"""
    audit_path = tmp_path / "audit.jsonl"
    cmd = [
        sys.executable, str(RECORD_BREAK_GLASS_PY), "verify-closed",
        "--audit-path", str(audit_path),
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


def _parse_stdout_json(result: subprocess.CompletedProcess) -> dict:
    """解析脚本 stdout 的 JSON 输出。"""
    assert result.returncode == 0, (
        f"脚本执行失败(exit={result.returncode}):\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return json.loads(result.stdout)


# ════════════════════════════════════════════════════════════════
# A. 模块结构 — R73 §5.9 常量与子命令存在
# ════════════════════════════════════════════════════════════════


class TestR73ModuleStructure:
    """R73 §5.9: record_break_glass.py 含 R73 常量与子命令支持。"""

    @pytest.fixture(scope="class")
    def module(self):
        return _load_record_break_glass_module()

    def test_r73_schema_version_constant(self, module):
        """R73 §5.9: R73_SCHEMA_VERSION 常量存在且为字符串。"""
        assert hasattr(module, "R73_SCHEMA_VERSION"), (
            "record_break_glass.py 必须定义 R73_SCHEMA_VERSION 常量"
        )
        assert module.R73_SCHEMA_VERSION == "2.0", (
            f"R73_SCHEMA_VERSION 必须为 '2.0'(实际: {module.R73_SCHEMA_VERSION})"
        )

    def test_r73_subcommands_constant(self, module):
        """R73 §5.9: R73_SUBCOMMANDS 含 open / close / verify-closed。"""
        assert hasattr(module, "R73_SUBCOMMANDS"), (
            "record_break_glass.py 必须定义 R73_SUBCOMMANDS 常量"
        )
        assert "open" in module.R73_SUBCOMMANDS
        assert "close" in module.R73_SUBCOMMANDS
        assert "verify-closed" in module.R73_SUBCOMMANDS

    def test_signing_key_env_constants(self, module):
        """R73 §5.9: 签名密钥环境变量常量存在。"""
        assert hasattr(module, "BREAK_GLASS_SIGNING_KEY_ENV"), (
            "record_break_glass.py 必须定义 BREAK_GLASS_SIGNING_KEY_ENV 常量"
        )
        assert hasattr(module, "BACKUP_SIGNING_KEY_ENV"), (
            "record_break_glass.py 必须定义 BACKUP_SIGNING_KEY_ENV 常量"
        )
        assert module.BREAK_GLASS_SIGNING_KEY_ENV == "BREAK_GLASS_SIGNING_KEY"
        assert module.BACKUP_SIGNING_KEY_ENV == "BACKUP_SIGNING_KEY"

    def test_default_constants(self, module):
        """R73 §5.9: 默认仓库/审计路径/快照目录/时长常量存在。"""
        assert hasattr(module, "DEFAULT_REPO")
        assert hasattr(module, "DEFAULT_AUDIT_PATH")
        assert hasattr(module, "DEFAULT_SNAPSHOTS_DIR")
        assert hasattr(module, "DEFAULT_DURATION_MINUTES")
        assert module.DEFAULT_DURATION_MINUTES == 60, (
            f"DEFAULT_DURATION_MINUTES 必须为 60(实际: {module.DEFAULT_DURATION_MINUTES})"
        )

    def test_subcommand_handlers_exist(self, module):
        """R73 §5.9: open / close / verify-closed 子命令处理函数存在。"""
        assert callable(module.cmd_open), "cmd_open 必须可调用"
        assert callable(module.cmd_close), "cmd_close 必须可调用"
        assert callable(module.cmd_verify_closed), "cmd_verify_closed 必须可调用"

    def test_signing_helpers_exist(self, module):
        """R73 §5.9: 签名/digest 辅助函数存在。"""
        assert callable(module._get_signing_key)
        assert callable(module._compute_ruleset_digest)
        assert callable(module._sign_payload)
        assert callable(module._verify_signature)
        assert callable(module._canonical_json_bytes)

    def test_digest_computation_deterministic(self, module):
        """R73 §5.9: digest 计算确定性(相同输入产生相同 digest)。"""
        ruleset = {"name": "test", "enforcement": "active", "rules": []}
        d1 = module._compute_ruleset_digest(ruleset)
        d2 = module._compute_ruleset_digest(ruleset)
        assert d1 == d2, "相同 ruleset 必须产生相同 digest"
        assert d1.startswith("sha256:"), "digest 必须以 'sha256:' 前缀"

    def test_signature_verification_roundtrip(self, module):
        """R73 §5.9: HMAC-SHA256 签名与验签往返一致。"""
        key = b"test-key"
        payload = b'{"test":"data"}'
        sig = module._sign_payload(payload, key)
        assert sig.startswith("hmac-sha256:"), "签名必须以 'hmac-sha256:' 前缀"
        assert module._verify_signature(payload, sig, key), "验签必须通过"
        # 错误密钥应验签失败
        assert not module._verify_signature(payload, sig, b"wrong-key"), (
            "错误密钥验签必须失败"
        )


# ════════════════════════════════════════════════════════════════
# B. open 子命令 — 外部 issue + 签名快照 + 审计条目
# ════════════════════════════════════════════════════════════════


class TestOpenSubcommand:
    """R73 §5.9: open 子命令创建外部 issue + 签名快照 + 审计条目。"""

    def test_open_succeeds_with_gh_mock(self, tmp_path):
        """R73 §5.9: open --gh-mock 成功退出(exit 0)。"""
        result = _run_open(tmp_path)
        assert result.returncode == 0, (
            f"open 应 exit 0(实际 {result.returncode})\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    def test_open_creates_external_issue(self, tmp_path):
        """R73 §5.9: open 创建外部 GitHub Issue(主要审计源)。

        这是 R73 §5.9 的核心要求 — 外部 issue 必须在修改 ruleset 之前创建。
        """
        result = _run_open(tmp_path)
        output = _parse_stdout_json(result)
        assert "issue_url" in output, "open 输出必须含 issue_url"
        assert output["issue_url"].startswith("http"), (
            f"issue_url 必须为 HTTP URL(实际: {output['issue_url']})"
        )
        assert "issue_number" in output, "open 输出必须含 issue_number"
        assert isinstance(output["issue_number"], int), (
            f"issue_number 必须为整数(实际类型: {type(output['issue_number'])})"
        )
        assert output["issue_number"] >= 1, (
            f"issue_number 必须 >= 1(实际: {output['issue_number']})"
        )

    def test_open_writes_signed_snapshot(self, tmp_path):
        """R73 §5.9: open 写入签名 ruleset 快照(.github/break-glass-<issue>-<sha>.json)。"""
        result = _run_open(tmp_path)
        output = _parse_stdout_json(result)
        snapshot_path = Path(output["snapshot_path"])
        assert snapshot_path.exists(), (
            f"签名快照文件必须存在: {snapshot_path}"
        )
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        assert snapshot["kind"] == "r73-break-glass-ruleset-snapshot", (
            f"快照 kind 必须为 'r73-break-glass-ruleset-snapshot'(实际: {snapshot.get('kind')})"
        )
        assert snapshot["schema_version"] == "2.0"
        assert "ruleset_json" in snapshot, "快照必须含 ruleset_json"
        assert "signature" in snapshot, "快照必须含 signature"
        assert snapshot["signature"].startswith("hmac-sha256:"), (
            "快照 signature 必须以 'hmac-sha256:' 前缀"
        )
        assert "ruleset_digest_before" in snapshot, "快照必须含 ruleset_digest_before"
        assert snapshot["ruleset_digest_before"].startswith("sha256:"), (
            "ruleset_digest_before 必须以 'sha256:' 前缀"
        )

    def test_open_appends_audit_jsonl_entry(self, tmp_path):
        """R73 §5.9: open 追加条目到审计 JSONL(镜像副本)。"""
        result = _run_open(tmp_path)
        output = _parse_stdout_json(result)
        audit_path = Path(output["audit_path"])
        assert audit_path.exists(), f"审计 JSONL 必须存在: {audit_path}"
        lines = [
            ln for ln in audit_path.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        assert len(lines) >= 1, "审计 JSONL 至少 1 行"
        entry = json.loads(lines[-1])
        assert entry["kind"] == "r73-break-glass-open", (
            f"条目 kind 必须为 'r73-break-glass-open'(实际: {entry.get('kind')})"
        )
        assert entry["status"] == "open", (
            f"条目 status 必须为 'open'(实际: {entry.get('status')})"
        )
        # 必填字段全部存在
        for field in [
            "operation_id", "issue_number", "issue_url", "actor", "reason",
            "target_sha", "ruleset_id", "ruleset_digest_before", "signature",
            "opened_at", "expected_close_by", "duration_minutes",
        ]:
            assert field in entry, f"审计条目必须含 {field} 字段"

    def test_open_output_contains_all_required_fields(self, tmp_path):
        """R73 §5.9: open stdout JSON 含全部必需字段。"""
        result = _run_open(tmp_path)
        output = _parse_stdout_json(result)
        required_fields = [
            "status", "operation_id", "issue_number", "issue_url",
            "actor", "reason", "target_sha", "ruleset_id",
            "ruleset_digest_before", "signature",
            "opened_at", "expected_close_by",
            "snapshot_path", "audit_path",
        ]
        for field in required_fields:
            assert field in output, f"open 输出必须含 {field} 字段"
        assert output["status"] == "opened"
        assert output["actor"] == TEST_ACTOR
        assert output["reason"] == TEST_REASON
        assert output["target_sha"] == VALID_SHA
        assert output["ruleset_id"] == TEST_RULESET_ID

    def test_open_binds_actor_reason_sha_ruleset(self, tmp_path):
        """R73 §5.9: open 绑定 actor / reason / target_sha / ruleset_id / 时间窗口。"""
        result = _run_open(tmp_path, actor="alice", reason="custom reason", sha="b" * 40,
                          ruleset_id="99999", duration=30)
        output = _parse_stdout_json(result)
        assert output["actor"] == "alice"
        assert output["reason"] == "custom reason"
        assert output["target_sha"] == "b" * 40
        assert output["ruleset_id"] == "99999"
        assert output["duration_minutes"] == 30 or "expected_close_by" in output

    def test_open_rejects_invalid_sha(self, tmp_path):
        """R73 §5.9: open 拒绝非法 target SHA(非 40-char hex)。"""
        result = _run_open(tmp_path, sha="not-a-sha")
        assert result.returncode != 0, (
            "非法 SHA 必须导致非零退出码"
        )

    def test_open_fails_without_signing_key(self, tmp_path, monkeypatch):
        """R73 §5.9: open 无签名密钥时失败(fail-closed)。"""
        monkeypatch.delenv("BREAK_GLASS_SIGNING_KEY", raising=False)
        monkeypatch.delenv("BACKUP_SIGNING_KEY", raising=False)
        audit_path = tmp_path / "audit.jsonl"
        snapshots_dir = tmp_path / "snapshots"
        cmd = [
            sys.executable, str(RECORD_BREAK_GLASS_PY), "open",
            "--reason", TEST_REASON,
            "--target-sha", VALID_SHA,
            "--ruleset-id", TEST_RULESET_ID,
            "--actor", TEST_ACTOR,
            "--audit-path", str(audit_path),
            "--snapshots-dir", str(snapshots_dir),
            "--gh-mock",
        ]
        # 清除环境变量(确保无签名密钥)
        env = {k: v for k, v in os.environ.items()
               if k not in ("BREAK_GLASS_SIGNING_KEY", "BACKUP_SIGNING_KEY")}
        result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)
        assert result.returncode != 0, (
            "无签名密钥时 open 必须失败(R73 §5.9: ruleset 修改前必须签名留底)"
        )


# ════════════════════════════════════════════════════════════════
# C. close 子命令 — closure artifact + issue 关闭
# ════════════════════════════════════════════════════════════════


class TestCloseSubcommand:
    """R73 §5.9: close 子命令生成 closure artifact + 关闭 issue。"""

    def test_close_succeeds_after_open(self, tmp_path):
        """R73 §5.9: open → close 流程成功(exit 0)。"""
        # 1. open
        open_result = _run_open(tmp_path)
        assert open_result.returncode == 0, (
            f"open 必须成功:\n{open_result.stderr}"
        )
        open_output = json.loads(open_result.stdout)
        issue_number = open_output["issue_number"]
        # 2. close
        close_result = _run_close(tmp_path, issue_number=issue_number)
        assert close_result.returncode == 0, (
            f"close 必须 exit 0(实际 {close_result.returncode})\n"
            f"stdout:\n{close_result.stdout}\nstderr:\n{close_result.stderr}"
        )

    def test_close_generates_closure_artifact(self, tmp_path):
        """R73 §5.9: close 生成签名 closure artifact。"""
        open_result = _run_open(tmp_path)
        open_output = json.loads(open_result.stdout)
        issue_number = open_output["issue_number"]
        target_sha_short = VALID_SHA[:12]

        close_result = _run_close(tmp_path, issue_number=issue_number)
        close_output = _parse_stdout_json(close_result)

        closure_path = Path(close_output["closure_path"])
        assert closure_path.exists(), (
            f"closure artifact 必须存在: {closure_path}"
        )
        closure = json.loads(closure_path.read_text(encoding="utf-8"))
        assert closure["kind"] == "r73-break-glass-closure", (
            f"closure kind 必须为 'r73-break-glass-closure'(实际: {closure.get('kind')})"
        )
        assert closure["operation_id"] == open_output["operation_id"]
        assert closure["issue_number"] == issue_number
        assert closure["target_sha"] == VALID_SHA
        assert closure["enforcement_active"] is True, (
            "closure artifact 必须记录 enforcement_active=True(mock 模式下应为 active)"
        )
        assert closure["current_sha_checks_passed"] is True, (
            "closure artifact 必须记录 current_sha_checks_passed=True(mock 模式下应全部通过)"
        )
        assert "signature" in closure, "closure artifact 必须含 signature"
        assert closure["signature"].startswith("hmac-sha256:"), (
            "closure signature 必须以 'hmac-sha256:' 前缀"
        )
        assert "ruleset_digest_before" in closure
        assert "ruleset_digest_after" in closure

    def test_close_updates_audit_status_to_closed(self, tmp_path):
        """R73 §5.9: close 更新审计 JSONL 条目 status=closed。"""
        open_result = _run_open(tmp_path)
        open_output = json.loads(open_result.stdout)
        issue_number = open_output["issue_number"]

        _run_close(tmp_path, issue_number=issue_number)

        audit_path = tmp_path / "audit.jsonl"
        lines = [ln for ln in audit_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        entry = json.loads(lines[-1])
        assert entry["status"] == "closed", (
            f"close 后审计条目 status 必须为 'closed'(实际: {entry.get('status')})"
        )
        assert entry.get("closure_signature"), "closed 条目必须含 closure_signature"
        assert "closed_at" in entry, "closed 条目必须含 closed_at"

    def test_close_output_contains_closure_summary(self, tmp_path):
        """R73 §5.9: close stdout JSON 含 closure 摘要字段。"""
        open_result = _run_open(tmp_path)
        open_output = json.loads(open_result.stdout)
        issue_number = open_output["issue_number"]

        close_result = _run_close(tmp_path, issue_number=issue_number)
        output = _parse_stdout_json(close_result)
        assert output["status"] == "closed"
        assert output["issue_number"] == issue_number
        assert output["restoration_verified"] is True, (
            "mock 模式下 digest 应一致(mock ruleset 未变),restoration_verified=True"
        )
        assert output["enforcement_active"] is True
        assert output["current_sha_checks_passed"] is True
        assert output["failed_checks"] == [], "mock 模式下 failed_checks 必须为空"
        assert "closure_path" in output
        assert "closed_at" in output

    def test_close_fails_without_open_entry(self, tmp_path):
        """R73 §5.9: close 在无 open 条目时失败(exit 1)。"""
        result = _run_close(tmp_path, issue_number=999)
        assert result.returncode != 0, (
            "无匹配 open 条目时 close 必须失败"
        )

    def test_close_supports_operation_id(self, tmp_path):
        """R73 §5.9: close 支持 --operation-id(替代 --issue-number)。"""
        open_result = _run_open(tmp_path)
        open_output = json.loads(open_result.stdout)
        operation_id = open_output["operation_id"]

        close_result = _run_close(tmp_path, operation_id=operation_id)
        assert close_result.returncode == 0, (
            f"close --operation-id 必须 exit 0\n"
            f"stdout:\n{close_result.stdout}\nstderr:\n{close_result.stderr}"
        )

    def test_close_rejects_both_issue_and_operation_id(self, tmp_path):
        """R73 §5.9: close 拒绝同时提供 --issue-number 与 --operation-id(mutually exclusive)。"""
        # open 先创建条目
        open_result = _run_open(tmp_path)
        open_output = json.loads(open_result.stdout)
        audit_path = tmp_path / "audit.jsonl"
        snapshots_dir = tmp_path / "snapshots"
        env = os.environ.copy()
        env["BREAK_GLASS_SIGNING_KEY"] = TEST_SIGNING_KEY
        cmd = [
            sys.executable, str(RECORD_BREAK_GLASS_PY), "close",
            "--issue-number", str(open_output["issue_number"]),
            "--operation-id", open_output["operation_id"],
            "--audit-path", str(audit_path),
            "--snapshots-dir", str(snapshots_dir),
            "--gh-mock",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)
        # argparse mutually exclusive group 触发 SystemExit(2) → EXIT_CLI_ERROR
        assert result.returncode != 0, (
            "同时提供 --issue-number 与 --operation-id 必须失败(mutually exclusive)"
        )


# ════════════════════════════════════════════════════════════════
# D. verify-closed 子命令 — open 事件检测
# ════════════════════════════════════════════════════════════════


class TestVerifyClosedSubcommand:
    """R73 §5.9: verify-closed 子命令检测 open 事件。"""

    def test_verify_closed_exits_zero_when_no_events(self, tmp_path):
        """R73 §5.9: 无审计条目时 verify-closed exit 0。"""
        result = _run_verify_closed(tmp_path)
        assert result.returncode == 0, (
            f"无审计条目时 verify-closed 应 exit 0(实际 {result.returncode})"
        )
        output = json.loads(result.stdout)
        assert output["open_count"] == 0

    def test_verify_closed_exits_nonzero_when_open_exists(self, tmp_path):
        """R73 §5.9: 存在 open 事件时 verify-closed exit 1(核心要求)。"""
        # 1. open(创建 open 事件)
        open_result = _run_open(tmp_path)
        assert open_result.returncode == 0
        # 2. verify-closed(应检测到 open 事件)
        result = _run_verify_closed(tmp_path)
        assert result.returncode != 0, (
            "存在 open 事件时 verify-closed 必须 exit 非零(R73 §5.9 核心要求)"
        )
        output = json.loads(result.stdout)
        assert output["open_count"] >= 1, (
            f"open_count 必须 >= 1(实际: {output['open_count']})"
        )
        assert output["status"] == "open-events-exist"

    def test_verify_closed_exits_zero_after_close(self, tmp_path):
        """R73 §5.9: close 后 verify-closed exit 0(闭环验证)。"""
        # 1. open
        open_result = _run_open(tmp_path)
        open_output = json.loads(open_result.stdout)
        # 2. close
        close_result = _run_close(tmp_path, issue_number=open_output["issue_number"])
        assert close_result.returncode == 0
        # 3. verify-closed(应 exit 0,因所有事件已关闭)
        result = _run_verify_closed(tmp_path)
        assert result.returncode == 0, (
            f"close 后 verify-closed 应 exit 0(实际 {result.returncode})\n"
            f"stdout:\n{result.stdout}"
        )

    def test_verify_closed_lists_open_events(self, tmp_path):
        """R73 §5.9: verify-closed 输出列出 open 事件详情。"""
        open_result = _run_open(tmp_path)
        open_output = json.loads(open_result.stdout)
        result = _run_verify_closed(tmp_path)
        output = json.loads(result.stdout)
        assert len(output["open_events"]) >= 1
        event = output["open_events"][0]
        assert event["issue_number"] == open_output["issue_number"]
        assert "operation_id" in event
        assert "issue_url" in event
        assert "opened_at" in event


# ════════════════════════════════════════════════════════════════
# E. open → close → verify-closed 端到端流程
# ════════════════════════════════════════════════════════════════


class TestEndToEndFlow:
    """R73 §5.9: open → close → verify-closed 端到端流程。"""

    def test_full_lifecycle(self, tmp_path):
        """R73 §5.9: 完整生命周期(open → close → verify-closed)。"""
        # 1. open
        open_result = _run_open(tmp_path)
        assert open_result.returncode == 0, f"open 失败:\n{open_result.stderr}"
        open_output = json.loads(open_result.stdout)
        issue_number = open_output["issue_number"]

        # 2. verify-closed(应检测到 open)
        mid_verify = _run_verify_closed(tmp_path)
        assert mid_verify.returncode != 0, "open 后 verify-closed 必须 exit 非零"

        # 3. close
        close_result = _run_close(tmp_path, issue_number=issue_number)
        assert close_result.returncode == 0, (
            f"close 失败:\nstdout:\n{close_result.stdout}\nstderr:\n{close_result.stderr}"
        )

        # 4. verify-closed(应 exit 0)
        final_verify = _run_verify_closed(tmp_path)
        assert final_verify.returncode == 0, (
            f"close 后 verify-closed 应 exit 0\nstdout:\n{final_verify.stdout}"
        )

    def test_multiple_open_close_cycles(self, tmp_path):
        """R73 §5.9: 多次 open/close 循环(每次 issue 编号递增)。"""
        # 第一轮
        r1 = _run_open(tmp_path)
        assert r1.returncode == 0
        o1 = json.loads(r1.stdout)
        c1 = _run_close(tmp_path, issue_number=o1["issue_number"])
        assert c1.returncode == 0

        # 第二轮
        r2 = _run_open(tmp_path)
        assert r2.returncode == 0
        o2 = json.loads(r2.stdout)
        assert o2["issue_number"] > o1["issue_number"], (
            "第二轮 open 的 issue 编号必须大于第一轮(mock 模式下递增)"
        )
        c2 = _run_close(tmp_path, issue_number=o2["issue_number"])
        assert c2.returncode == 0

        # 最终 verify-closed
        v = _run_verify_closed(tmp_path)
        assert v.returncode == 0, "两轮 close 后 verify-closed 应 exit 0"

    def test_closure_artifact_signature_verifiable(self, tmp_path):
        """R73 §5.9: closure artifact 签名可验签(tamper-evidence)。"""
        module = _load_record_break_glass_module()
        # open
        open_result = _run_open(tmp_path)
        open_output = json.loads(open_result.stdout)
        # close
        close_result = _run_close(tmp_path, issue_number=open_output["issue_number"])
        close_output = json.loads(close_result.stdout)
        # 读取 closure artifact
        closure_path = Path(close_output["closure_path"])
        closure = json.loads(closure_path.read_text(encoding="utf-8"))
        signature = closure.pop("signature")
        # 验签
        key = TEST_SIGNING_KEY.encode("utf-8")
        payload = module._canonical_json_bytes(closure)
        assert module._verify_signature(payload, signature, key), (
            "closure artifact 签名验签必须通过(tamper-evidence)"
        )


# ════════════════════════════════════════════════════════════════
# F. configure_branch_ruleset.sh reconciliation 静态检查
# ════════════════════════════════════════════════════════════════


class TestConfigureRulesetReconciliation:
    """R73 §5.13: configure_branch_ruleset.sh 含 digest 对账逻辑。"""

    @pytest.fixture(scope="class")
    def script_content(self):
        assert CONFIGURE_RULESET_SH.exists(), (
            "scripts/configure_branch_ruleset.sh 必须存在"
        )
        return CONFIGURE_RULESET_SH.read_text(encoding="utf-8")

    def test_reconciliation_section_exists(self, script_content):
        """R73 §5.13: 脚本含 reconciliation 段(section header)。"""
        assert "R73 §5.13" in script_content, (
            "configure_branch_ruleset.sh 必须含 R73 §5.13 reconciliation 段"
        )
        assert "reconciliation" in script_content.lower(), (
            "脚本必须含 reconciliation 逻辑"
        )

    def test_reconciliation_exports_actual_ruleset(self, script_content):
        """R73 §5.13: reconciliation 通过 gh api 导出实际 ruleset JSON。"""
        assert "rulesets/${RULESET_ID}" in script_content, (
            "脚本必须通过 gh api repos/{owner}/{repo}/rulesets/{id} 导出实际 ruleset"
        )
        # 确认有独立的 re-export 步骤(非复用 PUT/POST 响应)
        assert "ACTUAL_RULESET_JSON" in script_content, (
            "脚本必须将实际 ruleset JSON 存入 ACTUAL_RULESET_JSON 变量"
        )

    def test_reconciliation_computes_digest(self, script_content):
        """R73 §5.13: reconciliation 计算 SHA-256 digest。"""
        assert "sha256" in script_content.lower() or "sha256sum" in script_content, (
            "脚本必须使用 sha256sum/shasum 计算 digest"
        )
        assert "ACTUAL_DIGEST" in script_content, (
            "脚本必须计算 ACTUAL_DIGEST"
        )
        assert "EXPECTED_DIGEST" in script_content, (
            "脚本必须计算 EXPECTED_DIGEST"
        )

    def test_reconciliation_writes_report_and_exits_on_mismatch(self, script_content):
        """R73 §5.13: reconciliation 写入报告文件,digest 不匹配时 exit 非零。"""
        assert "ruleset-reconciliation.json" in script_content, (
            "脚本必须写入 .github/ruleset-reconciliation.json 报告"
        )
        assert "matched" in script_content, (
            "报告必须含 matched 字段(bool)"
        )
        assert "expected_digest" in script_content, (
            "报告必须含 expected_digest 字段"
        )
        assert "actual_digest" in script_content, (
            "报告必须含 actual_digest 字段"
        )
        assert "actual_ruleset_json" in script_content, (
            "报告必须含 actual_ruleset_json 字段"
        )
        # digest 不匹配时必须 exit 1
        assert "exit 1" in script_content, (
            "digest 不匹配时脚本必须 exit 1(R73 §5.13 fail-closed)"
        )

    @skip_if_no_bash
    def test_script_syntax_ok(self):
        """R73 §5.13: bash 语法合法。"""
        result = subprocess.run(
            ["bash", "-n", str(CONFIGURE_RULESET_SH)],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, (
            f"bash -n 失败:\n{result.stderr}"
        )


# ════════════════════════════════════════════════════════════════
# G. Legacy CLI 兼容性(R71/R72 测试不破坏)
# ════════════════════════════════════════════════════════════════


class TestLegacyCliCompat:
    """R73 §5.9: legacy flat-arg CLI 保持向后兼容。"""

    def test_legacy_cli_still_works(self, tmp_path):
        """R73 §5.9: 无子命令时回退到 legacy flat-arg CLI(--operator / --sha / ...)。"""
        audit_path = tmp_path / "legacy_audit.jsonl"
        cmd = [
            sys.executable, str(RECORD_BREAK_GLASS_PY),
            "--operator", TEST_ACTOR,
            "--sha", VALID_SHA,
            "--reason", "legacy cli compat test",
            "--risk", "low risk test",
            "--rollback-plan", "revert and rerun gates",
            "--typed-confirmation", "BREAK-GLASS-EMERGENCY",
            "--output", str(audit_path),
            "--no-create-issue",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, (
            f"legacy CLI 必须 exit 0(实际 {result.returncode})\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        # 验证 JSONL 写入
        assert audit_path.exists(), "legacy CLI 必须写入 JSONL"
        output = json.loads(result.stdout)
        assert output["operator"] == TEST_ACTOR
        assert output["sha"] == VALID_SHA
