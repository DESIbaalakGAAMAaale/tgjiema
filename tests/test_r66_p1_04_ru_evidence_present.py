"""R66 P1-04: RU gate 显式 evidence_present 输出 — 测试套件。

R66 终审报告 P1-04 整改要点:
  审计发现 `crdb-ru-72h-attribution-gate` 仅输出 `evidence_status`,但下游
  release-summary / production-promotion-gate 在判断 "Production GO" 时
  无法区分 "无数据但 job success(dry-run)" 与 "真实证据通过"。
  若 evidence_status 字段缺失或被意外置空,下游可能误把 dry-run success
  计入 Production GO。

整改:
  1. `crdb-ru-72h-attribution-gate` job 增加 `evidence_present` 显式输出:
     - PR / push (dry-run,无真实数据): evidence_present=false
     - release tag + 数据文件存在: evidence_present=true
     - release tag + 数据缺失: evidence_present=false (strict 模式 exit 1)
  2. `release-summary` env 增加 `CRDB_RU_72H_EVIDENCE_PRESENT`,
     在 Production Promotion Summary 中显式展示,
     并要求 evidence_present=true 才允许 production promotion。
  3. `production-promotion-gate` 校验 evidence_status=production **AND**
     evidence_present=true,defense in depth 防止单点失效。

测试覆盖矩阵:
  A. crdb-ru-72h-attribution-gate outputs 包含 evidence_present
  B. gate 步骤脚本写入 evidence_present=false (dry-run) / true (release tag + data)
  C. release-summary env 包含 CRDB_RU_72H_EVIDENCE_PRESENT
  D. release-summary Production Promotion Summary 展示 evidence_present
  E. release-summary production_promotion_allowed 要求 evidence_present=true
  F. production-promotion-gate 校验 evidence_present=true (defense in depth)
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# 测试环境兼容
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"


def _read_workflow() -> str:
    """读取 release-gates.yml 完整内容。"""
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def _extract_gate_section(content: str) -> str:
    """提取 crdb-ru-72h-attribution-gate job 段落(到下一个顶级 job 为止)。"""
    start = content.find("  crdb-ru-72h-attribution-gate:")
    assert start != -1, "未找到 crdb-ru-72h-attribution-gate job"
    # 找到下一个顶级 job(2 空格缩进 + 名称 + 冒号)
    next_job = content.find("\n  # ───", start + 1)
    if next_job == -1:
        next_job = content.find("\n  production-evidence:", start + 1)
    if next_job == -1:
        return content[start:]
    return content[start:next_job]


def _extract_release_summary_section(content: str) -> str:
    """提取 release-summary job 段落。"""
    start = content.find("  release-summary:")
    assert start != -1, "未找到 release-summary job"
    return content[start:]


def _extract_production_promotion_gate_section(content: str) -> str:
    """提取 production-promotion-gate job 段落。"""
    start = content.find("  production-promotion-gate:")
    assert start != -1, "未找到 production-promotion-gate job"
    # 截到下一个 job
    next_marker = content.find("\n  # ───", start + 1)
    if next_marker == -1:
        next_marker = len(content)
    return content[start:next_marker]


# ════════════════════════════════════════════════════════════════
# A. crdb-ru-72h-attribution-gate outputs 包含 evidence_present
# ════════════════════════════════════════════════════════════════


class TestGateJobOutputsEvidencePresent:
    """crdb-ru-72h-attribution-gate job 必须在 outputs 中声明 evidence_present。

    R66 P1-04: 显式 evidence_present 输出,区分"无数据"与"通过"。
    """

    def test_gate_job_has_evidence_present_output(self):
        """outputs 段必须包含 evidence_present。"""
        content = _read_workflow()
        gate_section = _extract_gate_section(content)
        assert "evidence_present:" in gate_section, (
            "R66 P1-04: crdb-ru-72h-attribution-gate outputs 必须包含 evidence_present"
        )

    def test_evidence_present_output_references_steps_gate_outputs(self):
        """evidence_present 输出必须引用 steps.gate.outputs.evidence_present。"""
        content = _read_workflow()
        gate_section = _extract_gate_section(content)
        assert "steps.gate.outputs.evidence_present" in gate_section, (
            "R66 P1-04: evidence_present 输出必须引用 steps.gate.outputs.evidence_present"
        )

    def test_evidence_present_has_r66_p1_04_marker(self):
        """evidence_present 输出必须有 R66 P1-04 标记注释。"""
        content = _read_workflow()
        gate_section = _extract_gate_section(content)
        assert "R66 P1-04" in gate_section, (
            "R66 P1-04: crdb-ru-72h-attribution-gate 段落必须包含 'R66 P1-04' 标记"
        )

    def test_outputs_section_has_three_outputs(self):
        """outputs 段必须同时包含 evidence_status / evidence_mode / evidence_present。"""
        content = _read_workflow()
        gate_section = _extract_gate_section(content)
        assert "evidence_status:" in gate_section
        assert "evidence_mode:" in gate_section
        assert "evidence_present:" in gate_section, (
            "R66 P1-04: outputs 必须新增 evidence_present(原 evidence_status/evidence_mode 保留)"
        )


# ════════════════════════════════════════════════════════════════
# B. gate 步骤脚本写入 evidence_present
# ════════════════════════════════════════════════════════════════


class TestGateStepWritesEvidencePresent:
    """gate 步骤的 shell 脚本必须在所有路径上写入 evidence_present。"""

    def test_dry_run_path_writes_evidence_present_false(self):
        """PR / push dry-run 场景必须写入 evidence_present=false。"""
        content = _read_workflow()
        gate_section = _extract_gate_section(content)
        # dry-run 分支(not_applicable / dry_run)必须写 evidence_present=false
        # 在 else 分支(evidence_status=not_applicable)中
        assert 'echo "evidence_present=false" >> $GITHUB_OUTPUT' in gate_section, (
            "R66 P1-04: dry-run 分支必须写入 evidence_present=false"
        )

    def test_release_tag_with_data_writes_evidence_present_true(self):
        """release tag + 数据文件存在场景必须写入 evidence_present=true。"""
        content = _read_workflow()
        gate_section = _extract_gate_section(content)
        assert 'echo "evidence_present=true" >> $GITHUB_OUTPUT' in gate_section, (
            "R66 P1-04: release tag + 数据文件存在分支必须写入 evidence_present=true"
        )

    def test_release_tag_data_missing_writes_evidence_present_false(self):
        """release tag + 数据缺失场景必须写入 evidence_present=false(在 exit 1 之前)。"""
        content = _read_workflow()
        gate_section = _extract_gate_section(content)
        # 找到 "FAIL: release tag 场景要求" 附近,必须有 evidence_present=false
        fail_idx = gate_section.find("FAIL: release tag 场景要求")
        assert fail_idx != -1, "未找到 release tag 数据缺失的 FAIL 分支"
        # 在 FAIL 分支后查找 evidence_present=false(在 exit 1 之前)
        after_fail = gate_section[fail_idx:fail_idx + 800]
        assert 'evidence_present=false' in after_fail, (
            "R66 P1-04: release tag 数据缺失分支必须写入 evidence_present=false "
            "(在 exit 1 之前,防御性输出)"
        )

    def test_evidence_present_false_count_at_least_two(self):
        """evidence_present=false 至少出现 2 次(dry-run + 数据缺失)。"""
        content = _read_workflow()
        gate_section = _extract_gate_section(content)
        count = gate_section.count('echo "evidence_present=false" >> $GITHUB_OUTPUT')
        assert count >= 2, (
            f"R66 P1-04: evidence_present=false 至少出现 2 次(dry-run + 数据缺失),"
            f"实际 {count} 次"
        )

    def test_evidence_present_true_count_at_least_two(self):
        """evidence_present=true 至少出现 2 次(artifact 文件 + workspace 文件)。"""
        content = _read_workflow()
        gate_section = _extract_gate_section(content)
        count = gate_section.count('echo "evidence_present=true" >> $GITHUB_OUTPUT')
        assert count >= 2, (
            f"R66 P1-04: evidence_present=true 至少出现 2 次"
            f"(artifact 文件 + workspace 文件),实际 {count} 次"
        )

    def test_evidence_present_written_before_exit_in_missing_branch(self):
        """数据缺失分支中,evidence_present=false 必须在 exit 1 之前写入。"""
        content = _read_workflow()
        gate_section = _extract_gate_section(content)
        fail_idx = gate_section.find("FAIL: release tag 场景要求")
        assert fail_idx != -1
        after_fail = gate_section[fail_idx:fail_idx + 800]
        ep_idx = after_fail.find('evidence_present=false')
        exit_idx = after_fail.find("exit 1")
        assert ep_idx != -1 and exit_idx != -1, (
            "R66 P1-04: 数据缺失分支必须同时有 evidence_present=false 和 exit 1"
        )
        assert ep_idx < exit_idx, (
            "R66 P1-04: 数据缺失分支中 evidence_present=false 必须在 exit 1 之前写入"
        )


# ════════════════════════════════════════════════════════════════
# C. release-summary env 包含 CRDB_RU_72H_EVIDENCE_PRESENT
# ════════════════════════════════════════════════════════════════


class TestReleaseSummaryEnvIncludesEvidencePresent:
    """release-summary job 的 env 必须包含 CRDB_RU_72H_EVIDENCE_PRESENT。"""

    def test_env_has_crdb_ru_72h_evidence_present(self):
        """env 段必须包含 CRDB_RU_72H_EVIDENCE_PRESENT。"""
        content = _read_workflow()
        assert "CRDB_RU_72H_EVIDENCE_PRESENT:" in content, (
            "R66 P1-04: release-summary env 必须包含 CRDB_RU_72H_EVIDENCE_PRESENT"
        )

    def test_env_references_outputs_evidence_present(self):
        """CRDB_RU_72H_EVIDENCE_PRESENT 必须引用 outputs.evidence_present。"""
        content = _read_workflow()
        assert (
            "CRDB_RU_72H_EVIDENCE_PRESENT: ${{ needs.crdb-ru-72h-attribution-gate.outputs.evidence_present }}"
            in content
        ), (
            "R66 P1-04: CRDB_RU_72H_EVIDENCE_PRESENT 必须引用 "
            "needs.crdb-ru-72h-attribution-gate.outputs.evidence_present"
        )

    def test_env_has_r66_p1_04_marker(self):
        """env 段必须有 R66 P1-04 标记注释。"""
        content = _read_workflow()
        # 查找 CRDB_RU_72H_EVIDENCE_PRESENT 附近的 R66 P1-04 注释
        idx = content.find("CRDB_RU_72H_EVIDENCE_PRESENT:")
        assert idx != -1
        # 在前 200 字符内查找 R66 P1-04 标记
        before = content[max(0, idx - 200):idx]
        assert "R66 P1-04" in before, (
            "R66 P1-04: CRDB_RU_72H_EVIDENCE_PRESENT 环境变量必须有 R66 P1-04 标记注释"
        )

    def test_env_keeps_existing_evidence_status(self):
        """P1-04 新增 evidence_present 不应删除原有 evidence_status。"""
        content = _read_workflow()
        assert "CRDB_RU_72H_EVIDENCE_STATUS:" in content, (
            "R66 P1-04: 新增 evidence_present 不应删除原有 CRDB_RU_72H_EVIDENCE_STATUS"
        )


# ════════════════════════════════════════════════════════════════
# D. release-summary Production Promotion Summary 展示 evidence_present
# ════════════════════════════════════════════════════════════════


class TestReleaseSummaryDisplaysEvidencePresent:
    """release-summary 的 Production Promotion Summary 必须展示 evidence_present。"""

    def test_summary_displays_evidence_present(self):
        """Production Promotion Summary 必须输出 crdb-ru-72h-evidence-present。"""
        content = _read_workflow()
        assert "crdb-ru-72h-evidence-present:" in content, (
            "R66 P1-04: Production Promotion Summary 必须输出 crdb-ru-72h-evidence-present"
        )

    def test_summary_uses_env_var(self):
        """Summary 输出必须引用 ${CRDB_RU_72H_EVIDENCE_PRESENT} 环境变量。"""
        content = _read_workflow()
        assert "${CRDB_RU_72H_EVIDENCE_PRESENT}" in content, (
            "R66 P1-04: Summary 必须引用 ${CRDB_RU_72H_EVIDENCE_PRESENT} 环境变量"
        )

    def test_summary_header_includes_p1_04(self):
        """Summary 标题必须包含 R66 P1-04 标记。"""
        content = _read_workflow()
        # 查找 Production Promotion Summary 标题
        idx = content.find("Production Promotion Summary")
        assert idx != -1, "未找到 Production Promotion Summary"
        header = content[max(0, idx - 100):idx + 100]
        assert "R66 P1-04" in header, (
            "R66 P1-04: Production Promotion Summary 标题必须包含 R66 P1-04 标记"
        )


# ════════════════════════════════════════════════════════════════
# E. release-summary production_promotion_allowed 要求 evidence_present=true
# ════════════════════════════════════════════════════════════════


class TestReleaseSummaryRequiresEvidencePresentTrue:
    """release-summary 的 production_promotion_allowed 判断必须要求 evidence_present=true。"""

    def test_production_promotion_check_includes_evidence_present(self):
        """production_promotion_allowed 判断必须同时校验 evidence_status=production
        AND evidence_present=true。"""
        content = _read_workflow()
        # 查找 PRODUCTION_PROMOTION_ALLOWED 判断逻辑
        idx = content.find("PRODUCTION_PROMOTION_ALLOWED=false")
        assert idx != -1, "未找到 PRODUCTION_PROMOTION_ALLOWED 判断"
        # 在 PRODUCTION_PROMOTION_ALLOWED 判断的 if 块中必须有 evidence_present=true
        check_block = content[idx:idx + 800]
        assert "CRDB_RU_72H_EVIDENCE_PRESENT" in check_block, (
            "R66 P1-04: PRODUCTION_PROMOTION_ALLOWED 判断必须引用 CRDB_RU_72H_EVIDENCE_PRESENT"
        )
        assert '"true"' in check_block, (
            "R66 P1-04: PRODUCTION_PROMOTION_ALLOWED 判断必须校验 evidence_present=true"
        )

    def test_production_promotion_check_uses_and_logic(self):
        """校验必须是 evidence_status=production AND evidence_present=true(AND 逻辑)。"""
        content = _read_workflow()
        idx = content.find("PRODUCTION_PROMOTION_ALLOWED=false")
        assert idx != -1
        # 查找 if [... ] && [... ] 结构
        check_block = content[idx:idx + 800]
        # 必须有 && 连接 evidence_status 和 evidence_present
        assert "CRDB_RU_72H_EVIDENCE_STATUS" in check_block
        assert "CRDB_RU_72H_EVIDENCE_PRESENT" in check_block
        # 必须有 && 操作符(在同一 if 语句中)
        assert "&&" in check_block, (
            "R66 P1-04: production_promotion 判断必须用 && 同时校验 "
            "evidence_status=production AND evidence_present=true"
        )

    def test_failure_message_mentions_evidence_present(self):
        """失败消息必须提及 evidence_present。"""
        content = _read_workflow()
        idx = content.find("PRODUCTION_PROMOTION_ALLOWED=false")
        assert idx != -1
        # 查找 else 分支的失败消息
        check_block = content[idx:idx + 1500]
        assert "evidence_present" in check_block, (
            "R66 P1-04: 失败消息必须提及 evidence_present 字段名"
        )


# ════════════════════════════════════════════════════════════════
# F. production-promotion-gate 校验 evidence_present=true (defense in depth)
# ════════════════════════════════════════════════════════════════


class TestProductionPromotionGateVerifiesEvidencePresent:
    """production-promotion-gate (tag-only) 必须额外校验 evidence_present=true。

    R66 P1-04: defense in depth — release-summary 已校验,本 job 再校验一次,
    防止单点失效。
    """

    def test_ppg_reads_evidence_present_output(self):
        """production-promotion-gate 必须读取 needs.crdb-ru-72h-attribution-gate.outputs.evidence_present。"""
        content = _read_workflow()
        ppg_section = _extract_production_promotion_gate_section(content)
        assert (
            "needs.crdb-ru-72h-attribution-gate.outputs.evidence_present" in ppg_section
        ), (
            "R66 P1-04: production-promotion-gate 必须读取 "
            "needs.crdb-ru-72h-attribution-gate.outputs.evidence_present"
        )

    def test_ppg_has_evidence_present_check(self):
        """production-promotion-gate 必须有 evidence_present != true 的失败校验。"""
        content = _read_workflow()
        ppg_section = _extract_production_promotion_gate_section(content)
        assert "evidence_present" in ppg_section, (
            "R66 P1-04: production-promotion-gate 必须校验 evidence_present"
        )
        assert "true" in ppg_section, (
            "R66 P1-04: production-promotion-gate 必须校验 evidence_present=true"
        )

    def test_ppg_evidence_present_check_exits_on_failure(self):
        """evidence_present != true 时必须 exit 1。"""
        content = _read_workflow()
        ppg_section = _extract_production_promotion_gate_section(content)
        # 查找 evidence_present 失败分支
        ep_fail_idx = ppg_section.find("evidence_present != true")
        if ep_fail_idx == -1:
            # 也可能写成 [ "${EVIDENCE_PRESENT}" != "true" ]
            ep_fail_idx = ppg_section.find('"${EVIDENCE_PRESENT}" != "true"')
        assert ep_fail_idx != -1, (
            "R66 P1-04: production-promotion-gate 必须有 evidence_present != true 的判断"
        )
        # 在判断后 500 字符内必须有 exit 1
        after = ppg_section[ep_fail_idx:ep_fail_idx + 500]
        assert "exit 1" in after, (
            "R66 P1-04: production-promotion-gate evidence_present != true 时必须 exit 1"
        )

    def test_ppg_step_name_mentions_p1_04(self):
        """校验步骤名称必须提及 R66 P0-05/P1-04。"""
        content = _read_workflow()
        ppg_section = _extract_production_promotion_gate_section(content)
        assert "R66 P1-04" in ppg_section, (
            "R66 P1-04: production-promotion-gate 段落必须包含 R66 P1-04 标记"
        )

    def test_ppg_evidence_present_pass_message(self):
        """evidence_present 校验通过时必须有 PASS 消息。"""
        content = _read_workflow()
        ppg_section = _extract_production_promotion_gate_section(content)
        assert "PASS:" in ppg_section, (
            "R66 P1-04: production-promotion-gate evidence_present 校验通过必须有 PASS 消息"
        )
        assert "evidence_present=true" in ppg_section, (
            "R66 P1-04: PASS 消息必须提及 evidence_present=true"
        )


# ════════════════════════════════════════════════════════════════
# G. 综合 / YAML 完整性
# ════════════════════════════════════════════════════════════════


class TestYamlIntegrityAndCompleteness:
    """YAML 完整性 + P1-04 整改一致性。"""

    def test_workflow_yaml_is_valid(self):
        """release-gates.yml 必须是合法 YAML。"""
        try:
            import yaml
        except ImportError:
            pytest.skip("PyYAML 未安装")
        content = _read_workflow()
        parsed = yaml.safe_load(content)
        assert parsed is not None
        assert "jobs" in parsed
        assert "crdb-ru-72h-attribution-gate" in parsed["jobs"]
        assert "release-summary" in parsed["jobs"]
        assert "production-promotion-gate" in parsed["jobs"]

    def test_gate_outputs_evidence_present_in_yaml(self):
        """YAML 解析后 crdb-ru-72h-attribution-gate.outputs.evidence_present 必须存在。"""
        try:
            import yaml
        except ImportError:
            pytest.skip("PyYAML 未安装")
        content = _read_workflow()
        parsed = yaml.safe_load(content)
        gate_outputs = parsed["jobs"]["crdb-ru-72h-attribution-gate"]["outputs"]
        assert "evidence_status" in gate_outputs
        assert "evidence_mode" in gate_outputs
        assert "evidence_present" in gate_outputs, (
            "R66 P1-04: YAML 解析后 crdb-ru-72h-attribution-gate.outputs 必须包含 evidence_present"
        )

    def test_release_summary_env_has_evidence_present_in_yaml(self):
        """YAML 解析后 release-summary.env 必须包含 CRDB_RU_72H_EVIDENCE_PRESENT。"""
        try:
            import yaml
        except ImportError:
            pytest.skip("PyYAML 未安装")
        content = _read_workflow()
        parsed = yaml.safe_load(content)
        rs_env = parsed["jobs"]["release-summary"]["env"]
        assert "CRDB_RU_72H_EVIDENCE_STATUS" in rs_env
        assert "CRDB_RU_72H_EVIDENCE_PRESENT" in rs_env, (
            "R66 P1-04: YAML 解析后 release-summary.env 必须包含 CRDB_RU_72H_EVIDENCE_PRESENT"
        )
