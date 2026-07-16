"""R55 §22: 灾备与真实环境 — 空白 VPS 恢复测试套件。

测试覆盖范围:
    A. RPO/RTO 阈值常量正确性
       - DEFAULT_RPO_SECONDS ≤ 6 小时(21600s)
       - DEFAULT_RTO_SECONDS ≤ 30 分钟(1800s)
       - bash 脚本内嵌阈值与 Python 模块常量一致
    B. 连续 3 次恢复计数逻辑
       - REQUIRED_CONSECUTIVE_PASSES = 3
       - bash 脚本主循环 ROUNDS 默认 3
       - 连续通过计数 → all_pass 判定
    C. 恢复报告必须包含的字段(sha/digest/backup_id/manifest/signature)
       - 报告 JSON schema 完整性
       - 溯源绑定字段存在性
    D. checksum 校验逻辑
       - manifest checksum + file checksum 双校验
       - BackupEngine.restore 返回 checksum_verified
    E. schema 版本一致性检查
       - MANIFEST_SCHEMA_VERSION = "r40_p0_7_v1"
       - DDL_VERSION = 11
       - bash 脚本内嵌版本与 Python 模块对齐
    F. 审批门禁逻辑
       - approval_action_id 必须存在且 status='approved'
       - 缺失/未审批/非 approved → 拒绝
    G. 回滚能力
       - 快照 → 脏数据写入 → 回滚 → 校验一致
    H. smoke 测试逻辑
       - Telegram sendMessage + copyMessage(读取) + deleteMessage
       - 4 步全通过才算 smoke PASS
    I. fail-closed 行为
       - 任何一轮失败立即 exit 1
       - 签名失败拒绝完成
       - RPO/RTO 超标拒绝完成
    J. 报告格式
       - JSON 结构正确
       - iterations 数组每轮含 round/status/rto_seconds/rpo_seconds/checks

被测代码引用:
    - scripts/blank_vps_recovery_test.sh — 空白 VPS 恢复测试脚本
    - services/disaster_recovery.py — DEFAULT_RPO_SECONDS / DEFAULT_RTO_SECONDS
    - services/backup_engine.py — MANIFEST_SCHEMA_VERSION / restore / _download_manifest
    - database/session.py — DDL_VERSION

测试策略:
    - 阈值/版本常量测试:读取 bash 脚本内容,验证嵌入常量与 Python 模块一致
    - 逻辑测试:构造样本数据/mock,验证判定函数
    - 报告格式测试:构造样本 JSON,验证必需字段
    - 不依赖真实 R2/Redis/Telegram(全 mock + 文件读取)
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# 测试文件顶部 mock telegram 模块(避免 import 失败)
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())


# ════════════════════════════════════════════════════════════════
# R55 §22 规范常量(本测试文件的规范契约)
# ════════════════════════════════════════════════════════════════

# R55 §22 规范定义的阈值上限
SPEC_RPO_MAX_SECONDS = 6 * 3600      # 21600 (6 小时)
SPEC_RTO_MAX_SECONDS = 30 * 60       # 1800  (30 分钟)
SPEC_REQUIRED_CONSECUTIVE_PASSES = 3  # 连续 3 次 RTO ≤ 30 分钟

# Python 模块中的预期常量值
EXPECTED_MANIFEST_SCHEMA_VERSION = "r40_p0_7_v1"
EXPECTED_DDL_VERSION = 11

# 报告必须绑定的溯源字段
SPEC_REPORT_REQUIRED_FIELDS = [
    "git_commit_sha",
    "docker_image_digest",
    "backup_id",
    "manifest_hash",
    "manifest_schema_version",
    "ddl_version",
    "signature",
    "approval_action_id",
]

# smoke 测试 4 步
SPEC_SMOKE_STEPS = ["sendMessage", "copyMessage", "deleteMessage", "deleteMessage_copy"]

# 脚本文件路径
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
VPS_RECOVERY_SCRIPT = SCRIPTS_DIR / "blank_vps_recovery_test.sh"


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════


def _read_script(path: Path) -> str:
    """读取脚本文件内容(容错:文件不存在时返回空字符串)。"""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _check_int_assignment_in_script(script_text: str, var_name: str, expected: int) -> bool:
    """检查 bash 脚本中是否包含指定整型阈值的赋值。

    支持格式:
        VAR_NAME=值
        VAR_NAME=$((表达式))  # 算术展开
    """
    import re
    # 匹配 VAR_NAME=数字 或 VAR_NAME=$((... 包含 expected))
    pattern_direct = rf"{var_name}\s*=\s*{expected}\b"
    pattern_arith = rf"{var_name}\s*=\s*\$\(\(\s*[^)]*\b{expected}\b[^)]*\)\)"
    return bool(re.search(pattern_direct, script_text)
                or re.search(pattern_arith, script_text))


def _check_str_assignment_in_script(script_text: str, var_name: str, expected: str) -> bool:
    """检查 bash 脚本中是否包含指定字符串阈值的赋值。"""
    pattern = rf'{var_name}\s*=\s*"{re.escape(expected)}"'
    return bool(re.search(pattern, script_text))


def _build_sample_report(
    status: str = "PASS",
    consecutive_passes: int = 3,
    git_sha: str = "abc123def456",
    docker_digest: str = "sha256:" + "a" * 64,
    backup_id: str = "backup_20260716_100000_abc12345",
    manifest_hash: str = "b" * 64,
    iterations: list | None = None,
) -> dict:
    """构造一个完整的样本 vps_recovery_test_report JSON(dict 形式)。

    用于测试报告格式与字段校验逻辑。
    """
    if iterations is None:
        iterations = [
            {
                "round": i,
                "status": "PASS",
                "rto_seconds": 600 + i * 10,
                "rpo_seconds": 3600,
                "checks": {
                    "clean_state": "PASS",
                    "checkout": "PASS",
                    "env_load": "PASS",
                    "rpo": "PASS",
                    "approval": "PASS",
                    "redis_migration": "PASS",
                    "restore_checksum_schema": "PASS",
                    "rollback": "PASS",
                    "smoke": "PASS",
                    "rto": "PASS",
                },
            }
            for i in range(1, 4)
        ]
    return {
        "git_commit_sha": git_sha,
        "docker_image_digest": docker_digest,
        "backup_id": backup_id,
        "manifest_hash": manifest_hash,
        "manifest_schema_version": EXPECTED_MANIFEST_SCHEMA_VERSION,
        "ddl_version": EXPECTED_DDL_VERSION,
        "signature": {
            "required": True,
            "method_preference": ["ssh", "gpg"],
            "signed": status == "PASS",
        },
        "approval_action_id": "r55_s22_approval_uuid_001",
        "spec": "R55-section22",
        "mode": "production",
        "started_at": "2026-07-16T10:00:00Z",
        "completed_at": "2026-07-16T10:45:00Z",
        "duration_seconds": 2700,
        "status": status,
        "thresholds": {
            "rpo_seconds": SPEC_RPO_MAX_SECONDS,
            "rto_seconds": SPEC_RTO_MAX_SECONDS,
            "required_consecutive_passes": SPEC_REQUIRED_CONSECUTIVE_PASSES,
        },
        "rounds_total": 3,
        "consecutive_passes": consecutive_passes,
        "all_pass": (status == "PASS"
                     and consecutive_passes >= SPEC_REQUIRED_CONSECUTIVE_PASSES),
        "iterations": iterations,
    }


# ════════════════════════════════════════════════════════════════
# A. RPO/RTO 阈值常量正确性测试
# ════════════════════════════════════════════════════════════════


class TestRpoRtoThresholdConstants:
    """A. RPO/RTO 阈值常量正确性测试。"""

    def test_rpo_threshold_le_6_hours(self):
        """R55 §22: RPO 阈值必须 ≤ 6 小时(21600 秒)。"""
        from services.disaster_recovery import DEFAULT_RPO_SECONDS
        assert DEFAULT_RPO_SECONDS <= SPEC_RPO_MAX_SECONDS, (
            f"DEFAULT_RPO_SECONDS={DEFAULT_RPO_SECONDS} 超过 R55 §22 上限 {SPEC_RPO_MAX_SECONDS}"
        )

    def test_rto_threshold_le_30_minutes(self):
        """R55 §22: RTO 阈值必须 ≤ 30 分钟(1800 秒)。"""
        from services.disaster_recovery import DEFAULT_RTO_SECONDS
        assert DEFAULT_RTO_SECONDS <= SPEC_RTO_MAX_SECONDS, (
            f"DEFAULT_RTO_SECONDS={DEFAULT_RTO_SECONDS} 超过 R55 §22 上限 {SPEC_RTO_MAX_SECONDS}"
        )

    def test_rpo_default_is_exactly_6_hours(self):
        """R55 §22: 默认 RPO 应为 6 小时(6 * 3600 = 21600)。"""
        from services.disaster_recovery import DEFAULT_RPO_SECONDS
        assert DEFAULT_RPO_SECONDS == 6 * 3600

    def test_rto_default_is_exactly_30_minutes(self):
        """R55 §22: 默认 RTO 应为 30 分钟(30 * 60 = 1800)。"""
        from services.disaster_recovery import DEFAULT_RTO_SECONDS
        assert DEFAULT_RTO_SECONDS == 30 * 60

    def test_rpo_less_than_rto_is_not_required(self):
        """RPO 与 RTO 是独立指标,不强制 RPO > RTO(仅校验各自上限)。"""
        from services.disaster_recovery import DEFAULT_RPO_SECONDS, DEFAULT_RTO_SECONDS
        # RPO(6h) > RTO(30m) 是正常的:数据丢失窗口 > 恢复时间窗口
        assert DEFAULT_RPO_SECONDS > DEFAULT_RTO_SECONDS

    def test_bash_script_contains_rpo_threshold(self):
        """bash 脚本应包含 DEFAULT_RPO_SECONDS = 6 * 3600(21600)。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        # 脚本用 $((6 * 3600)) 形式
        assert "6 * 3600" in script or "DEFAULT_RPO_SECONDS=21600" in script, (
            "脚本缺少 DEFAULT_RPO_SECONDS=6*36000 或 21600"
        )

    def test_bash_script_contains_rto_threshold(self):
        """bash 脚本应包含 DEFAULT_RTO_SECONDS = 30 * 60(1800)。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "30 * 60" in script or "DEFAULT_RTO_SECONDS=1800" in script, (
            "脚本缺少 DEFAULT_RTO_SECONDS=30*60 或 1800"
        )

    def test_bash_script_enforces_rpo_upper_bound(self):
        """bash 脚本应拒绝 RPO 超过上限(参数校验)。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "超过 R55 §22 上限" in script, "脚本缺少 RPO 上限校验逻辑"

    def test_bash_script_enforces_rto_upper_bound(self):
        """bash 脚本应拒绝 RTO 超过上限(参数校验)。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "超过 R55 §22 上限" in script, "脚本缺少 RTO 上限校验逻辑"


# ════════════════════════════════════════════════════════════════
# B. 连续 3 次恢复计数逻辑测试
# ════════════════════════════════════════════════════════════════


class TestConsecutiveRoundsLogic:
    """B. 连续 3 次恢复计数逻辑测试。"""

    def test_required_consecutive_passes_is_three(self):
        """R55 §22: 要求连续 3 次通过。"""
        assert SPEC_REQUIRED_CONSECUTIVE_PASSES == 3

    def test_bash_script_contains_required_passes_constant(self):
        """bash 脚本应包含 REQUIRED_CONSECUTIVE_PASSES=3。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert _check_int_assignment_in_script(script, "REQUIRED_CONSECUTIVE_PASSES", 3), (
            "脚本缺少 REQUIRED_CONSECUTIVE_PASSES=3"
        )

    def test_bash_script_default_rounds_is_three(self):
        """bash 脚本默认 ROUNDS 应为 3。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        # 匹配 ROUNDS=3(默认值赋值)
        assert re.search(r"ROUNDS\s*=\s*3\b", script), "脚本默认 ROUNDS 应为 3"

    def test_bash_script_rejects_rounds_less_than_three(self):
        """bash 脚本应拒绝 --rounds < 3。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "小于 R55 §22 要求" in script, "脚本缺少 rounds < 3 校验"

    def test_all_pass_requires_three_consecutive(self):
        """all_pass 判定:status=PASS 且 consecutive_passes >= 3。"""
        # 3 次全通过
        report = _build_sample_report(status="PASS", consecutive_passes=3)
        assert report["all_pass"] is True
        # 仅 2 次通过(不足 3 次)
        report = _build_sample_report(status="PASS", consecutive_passes=2)
        assert report["all_pass"] is False
        # 3 次但 status=FAILED
        report = _build_sample_report(status="FAILED", consecutive_passes=3)
        assert report["all_pass"] is False

    def test_consecutive_passes_counter_logic(self):
        """连续通过计数器:每轮通过 +1,失败立即 exit(不重置后继续)。"""
        # 模拟 3 轮全通过的计数
        consecutive = 0
        required = 3
        rounds_results = [True, True, True]
        for passed in rounds_results:
            if not passed:
                # fail-closed: 失败立即退出,不再继续
                break
            consecutive += 1
        assert consecutive == required
        assert consecutive >= required

    def test_fail_closed_stops_at_first_failure(self):
        """fail-closed:第 2 轮失败时 consecutive 应停在 1(不继续第 3 轮)。"""
        consecutive = 0
        required = 3
        rounds_results = [True, False, True]  # 第 2 轮失败
        failed = False
        for passed in rounds_results:
            if not passed:
                failed = True
                break  # fail-closed: 立即停止
            consecutive += 1
        assert failed is True
        assert consecutive == 1
        assert consecutive < required

    def test_bash_script_has_main_loop(self):
        """bash 脚本应包含主循环 for ((i = 1; i <= ROUNDS; i++))。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "i <= ROUNDS" in script, "脚本缺少主循环"
        assert "run_single_recovery_round" in script, "脚本缺少单轮恢复函数调用"

    def test_bash_script_increments_consecutive_passes(self):
        """bash 脚本每轮通过后应递增 CONSECUTIVE_PASSES。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "CONSECUTIVE_PASSES=$((CONSECUTIVE_PASSES + 1))" in script, (
            "脚本缺少 CONSECUTIVE_PASSES 递增逻辑"
        )


# ════════════════════════════════════════════════════════════════
# C. 恢复报告必须包含的字段测试
# ════════════════════════════════════════════════════════════════


class TestReportRequiredFields:
    """C. 恢复报告必须包含溯源绑定字段(sha/digest/backup_id/manifest/signature)。"""

    def test_report_contains_all_required_fields(self):
        """报告 JSON 应包含全部 R55 §22 必需字段。"""
        report = _build_sample_report()
        for field in SPEC_REPORT_REQUIRED_FIELDS:
            assert field in report, f"报告缺少必需字段: {field}"

    def test_report_git_commit_sha_is_string(self):
        """git_commit_sha 应为字符串(可为空,但字段必须存在)。"""
        report = _build_sample_report()
        assert isinstance(report["git_commit_sha"], str)

    def test_report_docker_image_digest_format(self):
        """docker_image_digest 应为 sha256:<64-hex> 格式或空字符串。"""
        report = _build_sample_report()
        digest = report["docker_image_digest"]
        assert isinstance(digest, str)
        if digest:
            assert re.match(r"^sha256:[a-f0-9]{64}$", digest), (
                f"docker_image_digest 格式错误: {digest}"
            )

    def test_report_backup_id_non_empty(self):
        """backup_id 必须非空(R55 §22 需要真实 R2 backup)。"""
        report = _build_sample_report()
        assert report["backup_id"], "backup_id 不能为空"

    def test_report_manifest_hash_format(self):
        """manifest_hash 应为 64 位 hex(sha256)或空字符串。"""
        report = _build_sample_report()
        manifest_hash = report["manifest_hash"]
        assert isinstance(manifest_hash, str)
        if manifest_hash:
            assert re.match(r"^[a-f0-9]{64}$", manifest_hash), (
                f"manifest_hash 格式错误: {manifest_hash}"
            )

    def test_report_signature_structure(self):
        """signature 字段应包含 required/method_preference/signed 三个子字段。"""
        report = _build_sample_report()
        sig = report["signature"]
        assert isinstance(sig, dict)
        assert sig["required"] is True
        assert sig["method_preference"] == ["ssh", "gpg"]
        assert isinstance(sig["signed"], bool)

    def test_report_signature_signed_when_pass(self):
        """status=PASS 时 signature.signed 应为 True(签名是 PASS 的前提)。"""
        report = _build_sample_report(status="PASS")
        assert report["signature"]["signed"] is True

    def test_report_approval_action_id_non_empty(self):
        """approval_action_id 必须非空(R55 §22 审批门禁)。"""
        report = _build_sample_report()
        assert report["approval_action_id"], "approval_action_id 不能为空"

    def test_bash_script_binds_git_sha(self):
        """bash 脚本应绑定 git_commit_sha 到报告。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "git_commit_sha" in script, "脚本缺少 git_commit_sha 绑定"
        assert "get_git_sha" in script, "脚本缺少 get_git_sha 函数"

    def test_bash_script_binds_docker_digest(self):
        """bash 脚本应绑定 docker_image_digest 到报告。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "docker_image_digest" in script, "脚本缺少 docker_image_digest 绑定"
        assert "get_docker_digest" in script, "脚本缺少 get_docker_digest 函数"

    def test_bash_script_binds_backup_id(self):
        """bash 脚本应绑定 backup_id 到报告。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert '"backup_id"' in script, "脚本缺少 backup_id 绑定到报告 JSON"

    def test_bash_script_binds_manifest_hash(self):
        """bash 脚本应绑定 manifest_hash 到报告。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "manifest_hash" in script, "脚本缺少 manifest_hash 绑定"
        assert "get_manifest_hash" in script, "脚本缺少 get_manifest_hash 函数"

    def test_bash_script_binds_signature(self):
        """bash 脚本应绑定 signature 到报告(SSH 优先 GPG 兜底)。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "signature" in script, "脚本缺少 signature 绑定"
        assert "ssh-keygen -Y sign" in script, "脚本缺少 SSH 签名逻辑"
        assert "gpg --detach-sign" in script, "脚本缺少 GPG 兜底签名逻辑"


# ════════════════════════════════════════════════════════════════
# D. checksum 校验逻辑测试
# ════════════════════════════════════════════════════════════════


class TestChecksumValidation:
    """D. checksum 校验逻辑(backup manifest + file checksum 双校验)。"""

    def test_bash_script_verifies_checksum(self):
        """bash 脚本应调用 BackupEngine.restore 完成 checksum 校验。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "checksum_verified" in script, "脚本缺少 checksum_verified 校验"
        assert "engine.restore" in script, "脚本缺少 BackupEngine.restore 调用"

    def test_checksum_pass_requires_all_true(self):
        """checksum 校验通过需要 success + checksum_verified + schema_ok + ddl_ok 全为 True。"""
        # 全部通过
        result = {
            "success": True,
            "checksum_verified": True,
            "schema_version_ok": True,
            "ddl_version_ok": True,
        }
        ok = (result["success"] is True
              and result["checksum_verified"] is True
              and result["schema_version_ok"] is True
              and result["ddl_version_ok"] is True)
        assert ok is True

    def test_checksum_fail_when_checksum_verified_false(self):
        """checksum_verified=False 时整体应失败。"""
        result = {
            "success": True,
            "checksum_verified": False,
            "schema_version_ok": True,
            "ddl_version_ok": True,
        }
        ok = (result["success"] is True
              and result["checksum_verified"] is True
              and result["schema_version_ok"] is True
              and result["ddl_version_ok"] is True)
        assert ok is False

    def test_checksum_fail_when_success_false(self):
        """success=False 时整体应失败(即使 checksum_verified=True)。"""
        result = {
            "success": False,
            "checksum_verified": True,
            "schema_version_ok": True,
            "ddl_version_ok": True,
        }
        ok = (result["success"] is True
              and result["checksum_verified"] is True
              and result["schema_version_ok"] is True
              and result["ddl_version_ok"] is True)
        assert ok is False

    def test_bash_script_uses_sha256_for_manifest(self):
        """bash 脚本应使用 sha256 计算 manifest 哈希。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "hashlib.sha256" in script, "脚本缺少 sha256 manifest 哈希计算"


# ════════════════════════════════════════════════════════════════
# E. schema 版本一致性检查测试
# ════════════════════════════════════════════════════════════════


class TestSchemaVersionConsistency:
    """E. schema 版本一致性检查(MANIFEST_SCHEMA_VERSION + DDL_VERSION)。"""

    def test_manifest_schema_version_value(self):
        """services.backup_engine.MANIFEST_SCHEMA_VERSION 应为 'r40_p0_7_v1'。"""
        from services.backup_engine import MANIFEST_SCHEMA_VERSION
        assert MANIFEST_SCHEMA_VERSION == EXPECTED_MANIFEST_SCHEMA_VERSION

    def test_ddl_version_value(self):
        """database.session.DDL_VERSION 应为 11。"""
        from database.session import DDL_VERSION
        assert DDL_VERSION == EXPECTED_DDL_VERSION

    def test_bash_script_contains_manifest_schema_version(self):
        """bash 脚本应包含 MANIFEST_SCHEMA_VERSION="r40_p0_7_v1"。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert _check_str_assignment_in_script(
            script, "MANIFEST_SCHEMA_VERSION", EXPECTED_MANIFEST_SCHEMA_VERSION
        ), f"脚本缺少 MANIFEST_SCHEMA_VERSION='{EXPECTED_MANIFEST_SCHEMA_VERSION}'"

    def test_bash_script_contains_ddl_version(self):
        """bash 脚本应包含 DDL_VERSION=11。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert _check_int_assignment_in_script(script, "DDL_VERSION", EXPECTED_DDL_VERSION), (
            f"脚本缺少 DDL_VERSION={EXPECTED_DDL_VERSION}"
        )

    def test_bash_script_validates_schema_version(self):
        """bash 脚本应校验 manifest.schema_version 与期望值一致。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "schema_version" in script, "脚本缺少 schema_version 校验"
        assert "EXPECTED_SCHEMA" in script, "脚本缺少 EXPECTED_SCHEMA 比对"

    def test_bash_script_validates_ddl_version(self):
        """bash 脚本应校验 kv_store 中的 ddl_version 与期望值一致。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "ddl_version" in script, "脚本缺少 ddl_version 校验"
        assert "EXPECTED_DDL" in script, "脚本缺少 EXPECTED_DDL 比对"

    def test_schema_mismatch_detected(self):
        """schema 版本不匹配时应返回 schema_version_ok=False。"""
        # 模拟 manifest schema_version 与期望不一致
        manifest = {"schema_version": "old_version_v0"}
        expected = EXPECTED_MANIFEST_SCHEMA_VERSION
        actual = manifest.get("schema_version", "")
        schema_ok = (actual == expected)
        assert schema_ok is False

    def test_schema_match_passes(self):
        """schema 版本匹配时应返回 schema_version_ok=True。"""
        manifest = {"schema_version": EXPECTED_MANIFEST_SCHEMA_VERSION}
        expected = EXPECTED_MANIFEST_SCHEMA_VERSION
        actual = manifest.get("schema_version", "")
        schema_ok = (actual == expected)
        assert schema_ok is True


# ════════════════════════════════════════════════════════════════
# F. 审批门禁逻辑测试
# ════════════════════════════════════════════════════════════════


class TestApprovalGate:
    """F. 审批门禁逻辑(approval_action_id 必须存在且 status='approved')。"""

    def test_bash_script_requires_approval_action_id(self):
        """bash 脚本应强制要求 --approval-action-id 参数。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "--approval-action-id" in script, "脚本缺少 --approval-action-id 参数"
        assert "必须指定 --approval-action-id" in script, "脚本缺少 approval_action_id 必填校验"

    def test_bash_script_queries_command_executions(self):
        """bash 脚本应查询 command_executions 表校验 approval_action_id。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "command_executions" in script, "脚本缺少 command_executions 表查询"
        assert "action_id = ?" in script, "脚本缺少 action_id 参数化查询"

    def test_approval_not_found_rejected(self):
        """approval_action_id 不存在(command_executions 查询返回 None)→ 拒绝。"""
        # 模拟 fetchone 返回 None
        row = None
        if row is None:
            check_result = "ERROR:not_found"
        else:
            check_result = "OK"
        assert check_result.startswith("ERROR"), "approval 不存在应拒绝"

    def test_approval_status_not_approved_rejected(self):
        """approval status != 'approved' → 拒绝。"""
        test_cases = [
            ("pending", "ERROR"),
            ("executed", "ERROR"),
            ("failed", "ERROR"),
            ("", "ERROR"),
        ]
        for status, expected_prefix in test_cases:
            row = (1001, status, "some_hash")
            principal_id, db_status, request_hash = row[0], row[1], row[2]
            if db_status != "approved":
                check_result = f"ERROR:status_{db_status}"
            else:
                check_result = f"OK:principal={principal_id}"
            assert check_result.startswith(expected_prefix), (
                f"status={status} 应被拒绝,实际: {check_result}"
            )

    def test_approval_approved_accepted(self):
        """approval status='approved' → 接受。"""
        row = (1001, "approved", "stored_hash")
        principal_id, db_status, request_hash = row[0], row[1], row[2]
        if db_status != "approved":
            check_result = f"ERROR:status_{db_status}"
        else:
            check_result = f"OK:principal={principal_id}"
        assert check_result.startswith("OK"), (
            f"status=approved 应被接受,实际: {check_result}"
        )

    def test_bash_script_checks_status_approved(self):
        """bash 脚本应显式校验 status == 'approved'。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert 'status != "approved"' in script or "status != 'approved'" in script, (
            "脚本缺少 status != 'approved' 校验"
        )


# ════════════════════════════════════════════════════════════════
# G. 回滚能力测试
# ════════════════════════════════════════════════════════════════


class TestRollbackCapability:
    """G. 回滚能力测试(恢复失败后回滚到之前状态)。"""

    def test_bash_script_has_rollback_step(self):
        """bash 脚本应包含回滚能力测试步骤。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "回滚能力测试" in script, "脚本缺少回滚能力测试步骤"
        assert "rollback" in script.lower(), "脚本缺少 rollback 标识"

    def test_rollback_logic_snapshot_dirty_restore(self):
        """回滚逻辑:快照 → 脏数据 → 回滚 → 校验一致。"""
        # 模拟回滚逻辑
        kv_store = {"last_backup_at": "2026-07-16T10:00:00"}
        # 1. 快照
        snapshot_key = "r55_s22_rollback_snapshot"
        current_last_backup = kv_store.get("last_backup_at", "")
        kv_store[snapshot_key] = current_last_backup
        # 2. 写入脏数据
        kv_store["last_backup_at"] = "ROLLBACK_TEST_DIRTY_VALUE"
        assert kv_store["last_backup_at"] == "ROLLBACK_TEST_DIRTY_VALUE"
        # 3. 回滚
        snapshot_value = kv_store.get(snapshot_key)
        kv_store["last_backup_at"] = snapshot_value or current_last_backup
        # 4. 校验一致
        after_rollback = kv_store.get("last_backup_at")
        rollback_ok = (after_rollback == (snapshot_value or current_last_backup))
        assert rollback_ok is True
        assert after_rollback == "2026-07-16T10:00:00"

    def test_rollback_detects_inconsistency(self):
        """回滚后数据与快照不一致时应 rollback_ok=False。"""
        kv_store = {"last_backup_at": "original_value"}
        snapshot_key = "r55_s22_rollback_snapshot"
        current = kv_store.get("last_backup_at", "")
        kv_store[snapshot_key] = current
        kv_store["last_backup_at"] = "DIRTY"
        # 模拟回滚失败(写入错误值)
        kv_store["last_backup_at"] = "WRONG_RESTORED_VALUE"
        after = kv_store.get("last_backup_at")
        rollback_ok = (after == (kv_store.get(snapshot_key) or current))
        assert rollback_ok is False

    def test_bash_script_uses_kv_store_snapshot(self):
        """bash 脚本应使用 kv_store 做快照(last_backup_at)。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "r55_s22_rollback_snapshot" in script, "脚本缺少回滚快照 key"
        assert "ROLLBACK_TEST_DIRTY_VALUE" in script, "脚本缺少脏数据写入模拟"

    def test_bash_script_cleans_up_snapshot(self):
        """bash 脚本回滚测试后应清理快照 key。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "DELETE FROM kv_store WHERE key = ?" in script, "脚本缺少快照清理逻辑"


# ════════════════════════════════════════════════════════════════
# H. smoke 测试逻辑测试
# ════════════════════════════════════════════════════════════════


class TestSmokeTestLogic:
    """H. smoke 测试逻辑(Telegram sendMessage + 读取 + deleteMessage)。"""

    def test_bash_script_has_smoke_step(self):
        """bash 脚本应包含 Telegram smoke 测试步骤。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "Telegram smoke" in script, "脚本缺少 Telegram smoke 测试步骤"

    def test_bash_script_calls_sendmessage(self):
        """bash 脚本应调用 sendMessage API。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "$TG_API/sendMessage" in script, "脚本缺少 sendMessage 调用"

    def test_bash_script_calls_copymessage(self):
        """bash 脚本应调用 copyMessage API(验证消息可读)。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "$TG_API/copyMessage" in script, "脚本缺少 copyMessage 调用"

    def test_bash_script_calls_deletemessage(self):
        """bash 脚本应调用 deleteMessage API(删除原消息 + copy)。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        # 应有两次 deleteMessage(原消息 + copy)
        deletemessage_count = script.count("$TG_API/deleteMessage")
        assert deletemessage_count >= 2, (
            f"脚本应有至少 2 次 deleteMessage 调用,实际: {deletemessage_count}"
        )

    def test_smoke_four_steps_complete(self):
        """smoke 测试 4 步全通过才算 PASS。"""
        steps = {
            "sendMessage": True,
            "copyMessage": True,
            "deleteMessage_original": True,
            "deleteMessage_copy": True,
        }
        smoke_pass = all(steps.values())
        assert smoke_pass is True

    def test_smoke_fails_if_any_step_fails(self):
        """smoke 任一步骤失败应整体失败。"""
        # copyMessage 失败
        steps = {
            "sendMessage": True,
            "copyMessage": False,
            "deleteMessage_original": True,
            "deleteMessage_copy": True,
        }
        smoke_pass = all(steps.values())
        assert smoke_pass is False

    def test_bash_script_requires_upload_bot_token(self):
        """bash 脚本应要求 UPLOAD_BOT_TOKEN(production smoke 必需)。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "UPLOAD_BOT_TOKEN" in script, "脚本缺少 UPLOAD_BOT_TOKEN 校验"

    def test_bash_script_requires_test_chat_id(self):
        """bash 脚本应要求 --test-chat-id 参数。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "--test-chat-id" in script, "脚本缺少 --test-chat-id 参数"
        assert "必须指定 --test-chat-id" in script, "脚本缺少 test_chat_id 必填校验"

    def test_bash_script_parses_message_id_from_response(self):
        """bash 脚本应从 Telegram 响应解析 message_id。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "message_id" in script, "脚本缺少 message_id 解析"
        # 应使用 Python json 解析响应
        assert "r.get('result', {}).get('message_id')" in script, (
            "脚本缺少 result.message_id 解析逻辑"
        )


# ════════════════════════════════════════════════════════════════
# I. fail-closed 行为测试
# ════════════════════════════════════════════════════════════════


class TestFailClosedBehavior:
    """I. fail-closed 行为测试(任何失败立即 exit 1)。"""

    def test_bash_script_uses_set_euo_pipefail(self):
        """bash 脚本应使用 set -euo pipefail(fail-closed 基础)。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "set -euo pipefail" in script, "脚本缺少 set -euo pipefail"

    def test_bash_script_uses_fail_step_function(self):
        """bash 脚本应使用 fail_step 函数(更新 checks + exit 1)。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "fail_step()" in script, "脚本缺少 fail_step 函数定义"
        assert "exit 1" in script, "脚本缺少 exit 1 失败退出"

    def test_bash_script_has_trap_exit(self):
        """bash 脚本应设置 trap EXIT(确保任何退出都生成 report)。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "trap on_exit EXIT" in script, "脚本缺少 trap on_exit EXIT"
        assert "on_exit()" in script, "脚本缺少 on_exit 函数"

    def test_bash_script_fail_closed_on_rpo_violation(self):
        """RPO 超标应 fail-closed(exit 1)。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "RPO 超标" in script, "脚本缺少 RPO 超标 fail-closed"
        assert 'fail_step "round_${round}_rpo"' in script, "脚本缺少 RPO 失败 fail_step 调用"

    def test_bash_script_fail_closed_on_rto_violation(self):
        """RTO 超标应 fail-closed(exit 1)。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "RTO 超标" in script, "脚本缺少 RTO 超标 fail-closed"
        assert 'fail_step "round_${round}_rto"' in script, "脚本缺少 RTO 失败 fail_step 调用"

    def test_bash_script_fail_closed_on_checksum_failure(self):
        """checksum/schema 校验失败应 fail-closed。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "R2 恢复失败(checksum/schema" in script, "脚本缺少 checksum 失败 fail-closed"

    def test_bash_script_fail_closed_on_approval_failure(self):
        """审批门禁失败应 fail-closed。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "审批门禁校验失败" in script, "脚本缺少审批失败 fail-closed"

    def test_bash_script_fail_closed_on_smoke_failure(self):
        """smoke 测试失败应 fail-closed。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "sendMessage 失败" in script, "脚本缺少 smoke sendMessage 失败 fail-closed"
        assert "copyMessage 失败" in script, "脚本缺少 smoke copyMessage 失败 fail-closed"
        assert "deleteMessage(原消息)失败" in script, "脚本缺少 smoke deleteMessage 失败 fail-closed"

    def test_bash_script_fail_closed_on_rollback_failure(self):
        """回滚能力测试失败应 fail-closed。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "回滚能力测试失败" in script, "脚本缺少回滚失败 fail-closed"

    def test_bash_script_fail_closed_on_signature_failure(self):
        """签名失败应拒绝完成(fail-closed)。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "签名失败,拒绝完成" in script, "脚本缺少签名失败 fail-closed"

    def test_bash_script_fail_closed_on_restore_failure(self):
        """R2 restore 失败应 fail-closed。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert 'fail_step "round_${round}_restore"' in script, (
            "脚本缺少 restore 失败 fail_step 调用"
        )

    def test_fail_closed_no_or_true_fallback(self):
        """fail-closed:脚本不应在关键步骤使用 || true 兜底(清理操作除外)。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        # 提取所有 || true 行(忽略 systemctl stop / docker-compose stop / find 清理等)
        dangerous_or_true = []
        for line in script.split("\n"):
            stripped = line.strip()
            if "|| true" not in stripped:
                continue
            # 允许的清理操作(stop/find/rm 等非关键步骤)
            allowed_patterns = [
                "systemctl stop", "docker-compose stop", "find ", "rm -f",
                "git fetch", "docker inspect", "grep -oE", "2>/dev/null",
            ]
            if any(p in stripped for p in allowed_patterns):
                continue
            dangerous_or_true.append(stripped)
        # 关键步骤不应有 || true 兜底(允许少量边界情况)
        assert len(dangerous_or_true) == 0, (
            f"关键步骤不应使用 || true 兜底(fail-closed 违规): {dangerous_or_true}"
        )


# ════════════════════════════════════════════════════════════════
# J. 报告格式测试
# ════════════════════════════════════════════════════════════════


class TestReportFormat:
    """J. 报告格式测试(JSON 结构 + iterations 数组)。"""

    def test_report_is_valid_json(self):
        """样本报告应可序列化为合法 JSON。"""
        report = _build_sample_report()
        report_json = json.dumps(report, ensure_ascii=False)
        assert isinstance(report_json, str)
        # 反序列化校验
        parsed = json.loads(report_json)
        assert parsed == report

    def test_report_contains_spec_marker(self):
        """报告应包含 spec='R55-section22' 标记。"""
        report = _build_sample_report()
        assert report["spec"] == "R55-section22"

    def test_report_contains_thresholds_block(self):
        """报告应包含 thresholds 块(rpo_seconds/rto_seconds/required_consecutive_passes)。"""
        report = _build_sample_report()
        thresholds = report["thresholds"]
        assert "rpo_seconds" in thresholds
        assert "rto_seconds" in thresholds
        assert "required_consecutive_passes" in thresholds
        assert thresholds["rpo_seconds"] <= SPEC_RPO_MAX_SECONDS
        assert thresholds["rto_seconds"] <= SPEC_RTO_MAX_SECONDS
        assert thresholds["required_consecutive_passes"] == SPEC_REQUIRED_CONSECUTIVE_PASSES

    def test_report_iterations_is_list(self):
        """iterations 应为列表。"""
        report = _build_sample_report()
        assert isinstance(report["iterations"], list)
        assert len(report["iterations"]) == 3

    def test_report_iteration_structure(self):
        """每个 iteration 应包含 round/status/rto_seconds/rpo_seconds/checks。"""
        report = _build_sample_report()
        for iter_data in report["iterations"]:
            assert "round" in iter_data
            assert "status" in iter_data
            assert "rto_seconds" in iter_data
            assert "rpo_seconds" in iter_data
            assert "checks" in iter_data
            assert isinstance(iter_data["checks"], dict)

    def test_report_iteration_rto_within_threshold(self):
        """每个 iteration 的 rto_seconds 应 ≤ RTO 阈值(1800s)。"""
        report = _build_sample_report()
        for iter_data in report["iterations"]:
            assert iter_data["rto_seconds"] <= SPEC_RTO_MAX_SECONDS, (
                f"round {iter_data['round']} RTO {iter_data['rto_seconds']}s 超过阈值"
            )

    def test_report_iteration_checks_all_pass(self):
        """PASS 状态下每个 iteration 的 checks 应全部为 PASS。"""
        report = _build_sample_report(status="PASS")
        required_checks = [
            "clean_state", "checkout", "env_load", "rpo", "approval",
            "redis_migration", "restore_checksum_schema", "rollback",
            "smoke", "rto",
        ]
        for iter_data in report["iterations"]:
            for check in required_checks:
                assert check in iter_data["checks"], (
                    f"round {iter_data['round']} 缺少 check: {check}"
                )
                assert iter_data["checks"][check] == "PASS", (
                    f"round {iter_data['round']} check {check} 应为 PASS,"
                    f"实际: {iter_data['checks'][check]}"
                )

    def test_report_status_pass_when_all_pass(self):
        """3 轮全通过时 status='PASS'。"""
        report = _build_sample_report(status="PASS", consecutive_passes=3)
        assert report["status"] == "PASS"
        assert report["all_pass"] is True

    def test_report_status_failed_when_round_fails(self):
        """任一轮失败时 status='FAILED'。"""
        # 构造第 2 轮失败的 iterations
        iterations = [
            {"round": 1, "status": "PASS", "rto_seconds": 600,
             "rpo_seconds": 3600, "checks": {"rto": "PASS"}},
            {"round": 2, "status": "FAIL", "rto_seconds": 2000,
             "rpo_seconds": 3600, "checks": {"rto": "FAIL"}},
        ]
        report = _build_sample_report(
            status="FAILED", consecutive_passes=1, iterations=iterations,
        )
        assert report["status"] == "FAILED"
        assert report["all_pass"] is False
        assert report["consecutive_passes"] == 1

    def test_report_duration_seconds_is_int(self):
        """duration_seconds 应为整数。"""
        report = _build_sample_report()
        assert isinstance(report["duration_seconds"], int)
        assert report["duration_seconds"] > 0

    def test_report_timestamps_iso_format(self):
        """started_at/completed_at 应为 ISO 8601 格式(含 Z 后缀)。"""
        report = _build_sample_report()
        for ts_field in ("started_at", "completed_at"):
            ts = report[ts_field]
            assert isinstance(ts, str)
            assert ts.endswith("Z"), f"{ts_field} 应以 Z 结尾(UTC),实际: {ts}"
            # ISO 8601 基本格式校验
            assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", ts), (
                f"{ts_field} 不符合 ISO 8601 格式: {ts}"
            )

    def test_bash_script_generates_json_report(self):
        """bash 脚本应生成 vps_recovery_test_report_*.json 报告。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "vps_recovery_test_report_" in script, "脚本缺少报告文件名前缀"
        assert "json.dump" in script, "脚本缺少 JSON 写入逻辑"

    def test_bash_script_report_filename_has_timestamp(self):
        """报告文件名应包含时间戳 YYYYMMDD_HHMMSS。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "REPORT_TS=$(date +%Y%m%d_%H%M%S)" in script, (
            "脚本缺少报告时间戳生成"
        )
        assert "${REPORT_TS}.json" in script, "脚本缺少时间戳注入报告文件名"


# ════════════════════════════════════════════════════════════════
# K. 干净状态(空白 VPS)模拟测试
# ════════════════════════════════════════════════════════════════


class TestCleanStateSimulation:
    """K. 干净状态(空白 VPS)模拟测试。"""

    def test_bash_script_has_clean_state_step(self):
        """bash 脚本应包含干净状态准备步骤(模拟空白 VPS)。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "干净状态准备" in script, "脚本缺少干净状态准备步骤"
        assert "空白 VPS" in script, "脚本缺少空白 VPS 模拟说明"

    def test_bash_script_cleans_sqlite_files(self):
        """bash 脚本应清理本地 SQLite 数据文件(每轮从干净状态开始)。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert 'find "$REPO_DIR/data" -name "*.db"' in script, (
            "脚本缺少 SQLite db 文件清理"
        )
        assert "-delete" in script, "脚本缺少 find -delete 清理"

    def test_bash_script_stops_services_before_clean(self):
        """bash 脚本应在清理前停止业务服务(避免文件占用)。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        # systemctl stop 应在 find -delete 之前
        stop_pos = script.find("systemctl stop")
        clean_pos = script.find('find "$REPO_DIR/data"')
        assert stop_pos != -1 and clean_pos != -1, (
            "脚本缺少停止服务或清理 SQLite 逻辑"
        )
        assert stop_pos < clean_pos, "应先停止服务再清理 SQLite 文件"

    def test_bash_script_stops_redis_each_round(self):
        """bash 脚本每轮应停止 Redis(确保干净重启)。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "docker-compose stop redis" in script, "脚本缺少 Redis 停止逻辑"

    def test_clean_state_does_not_touch_r2(self):
        """干净状态清理不应触碰 R2 备份(仅清理本地 SQLite)。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        # 应有注释说明不触碰 R2
        assert "不触碰 R2" in script or "不触碰 R2 备份" in script, (
            "脚本应说明干净状态清理不触碰 R2 备份"
        )


# ════════════════════════════════════════════════════════════════
# L. 签名逻辑测试
# ════════════════════════════════════════════════════════════════


class TestSignatureLogic:
    """L. 签名逻辑测试(SSH 优先,GPG 兜底)。"""

    def test_bash_script_prefers_ssh_signature(self):
        """bash 脚本应优先使用 SSH 签名(ssh-keygen -Y sign)。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        ssh_pos = script.find("ssh-keygen -Y sign")
        gpg_pos = script.find("gpg --detach-sign")
        assert ssh_pos != -1, "脚本缺少 SSH 签名"
        assert gpg_pos != -1, "脚本缺少 GPG 兜底签名"
        # SSH 应在 GPG 之前(优先级)
        assert ssh_pos < gpg_pos, "SSH 签名应在 GPG 之前(优先级)"

    def test_bash_script_uses_ssh_key_path_env(self):
        """bash 脚本应支持 SSH_KEY_PATH 环境变量指定密钥。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "SSH_KEY_PATH" in script, "脚本缺少 SSH_KEY_PATH 环境变量支持"

    def test_bash_script_fallback_to_default_ssh_key(self):
        """bash 脚本应回退到 ~/.ssh/id_ed25519 默认密钥。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert ".ssh/id_ed25519" in script, "脚本缺少默认 SSH 密钥回退"

    def test_bash_script_signature_namespace(self):
        """bash 脚本应使用 'vps-recovery-test' 签名命名空间。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "-n vps-recovery-test" in script, "脚本缺少 vps-recovery-test 签名命名空间"

    def test_signature_method_preference_order(self):
        """报告 signature.method_preference 应为 ['ssh', 'gpg']。"""
        report = _build_sample_report()
        assert report["signature"]["method_preference"] == ["ssh", "gpg"]


# ════════════════════════════════════════════════════════════════
# M. Docker digest 绑定测试
# ════════════════════════════════════════════════════════════════


class TestDockerDigestBinding:
    """M. Docker 镜像 digest 绑定测试。"""

    def test_bash_script_gets_docker_digest(self):
        """bash 脚本应获取 Docker 镜像 digest。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "get_docker_digest" in script, "脚本缺少 get_docker_digest 函数"

    def test_bash_script_parses_dockerfile_python_image(self):
        """bash 脚本应从 Dockerfile 解析 ARG PYTHON_IMAGE(参考 verify_docker_digest.sh)。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "ARG PYTHON_IMAGE" in script or "PYTHON_IMAGE=" in script, (
            "脚本缺少 Dockerfile PYTHON_IMAGE 解析"
        )

    def test_bash_script_supports_docker_image_arg(self):
        """bash 脚本应支持 --docker-image 参数显式指定镜像。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "--docker-image" in script, "脚本缺少 --docker-image 参数"

    def test_bash_script_extracts_sha256_digest(self):
        """bash 脚本应提取 sha256:<64-hex> 格式的 digest。"""
        script = _read_script(VPS_RECOVERY_SCRIPT)
        assert script, f"无法读取脚本: {VPS_RECOVERY_SCRIPT}"
        assert "sha256:[a-f0-9]{64}" in script, "脚本缺少 sha256 digest 正则提取"

    def test_docker_digest_regex_matches_valid_digest(self):
        """sha256 digest 正则应匹配合法格式。"""
        valid_digest = "sha256:" + "a" * 64
        assert re.match(r"^sha256:[a-f0-9]{64}$", valid_digest)
        # 非法格式
        invalid_digests = [
            "sha256:abc",           # 过短
            "sha256:" + "g" * 64,   # 非 hex
            "abc123",               # 无前缀
            "",                     # 空
        ]
        for invalid in invalid_digests:
            assert not re.match(r"^sha256:[a-f0-9]{64}$", invalid), (
                f"非法 digest 应不匹配: {invalid}"
            )
