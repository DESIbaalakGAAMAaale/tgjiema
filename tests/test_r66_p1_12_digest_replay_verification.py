"""R66 P1-12: 同候选不可变重验证 (3x digest replay) — 测试套件。

R66 终审报告 P1-12 整改要点:
  审计发现 docker-build job 仅做单次 pull 验证 digest,未做"同候选 3 次重放
  验证"。同候选(同一 commit + 同一 run)的 image digest 应通过 3 次独立
  replay(docker manifest inspect + docker pull + docker inspect)验证
  不可变性(immutability),而非 rebuild(重新 build 会产生新 digest)。

整改:
  在 docker-build job 的 "Verify image pull by digest" 步骤后新增:
    "R66 P1-12: Same-candidate immutable re-verification (3x digest replay)"
  该步骤对同一 EXPECTED_DIGEST 做 3 次独立 replay:
    1. docker manifest inspect 验证 digest 在 registry 中存在且可解析
    2. docker pull 验证 digest 可拉取(内容地址)
    3. docker inspect 验证拉取的镜像 RepoDigests 包含 EXPECTED_DIGEST
    4. 校验 EXPECTED_DIGEST 跨 3 次访问保持不变(immutability)
  任一次 replay 失败立即 exit 1(同候选不可变重验证失败)。

测试覆盖矩阵:
  A. docker-build job 包含 P1-12 步骤
  B. 步骤使用 EXPECTED_DIGEST 来自 steps.build.outputs.digest(frozen)
  C. 3 次 replay 循环(for i in 1 2 3)
  D. 每次 replay 包含 manifest inspect + pull + inspect
  E. 失败时 exit 1(fail-closed)
  F. 步骤语义为 replay 而非 rebuild
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


def _extract_docker_build_section(content: str) -> str:
    """提取 docker-build job 段落(到下一个顶级 job 为止)。"""
    start = content.find("  docker-build:")
    assert start != -1, "未找到 docker-build job"
    # 找到下一个顶级 job — "  # ───" 或 "  docker-digest-verify:"
    next_job = content.find("\n  # ─── 2.", start + 1)
    if next_job == -1:
        next_job = content.find("\n  docker-digest-verify:", start + 1)
    if next_job == -1:
        return content[start:]
    return content[start:next_job]


def _extract_p1_12_step(content: str) -> str:
    """提取 R66 P1-12 步骤段落。"""
    docker_build = _extract_docker_build_section(content)
    start = docker_build.find("R66 P1-12: Same-candidate immutable re-verification")
    if start == -1:
        # 也可能是 step name 行
        start = docker_build.find('name: "R66 P1-12')
    assert start != -1, "未找到 R66 P1-12 步骤"
    # 截到下一个 - name: 或 job 结束
    next_step = docker_build.find("\n    - name:", start + 10)
    if next_step == -1:
        next_step = docker_build.find("\n    - uses:", start + 10)
    if next_step == -1:
        return docker_build[start:]
    return docker_build[start:next_step]


# ════════════════════════════════════════════════════════════════
# A. docker-build job 包含 P1-12 步骤
# ════════════════════════════════════════════════════════════════


class TestDockerBuildHasP1_12Step:
    """docker-build job 必须包含 R66 P1-12 步骤。"""

    def test_docker_build_has_p1_12_step(self):
        """docker-build job 必须有 R66 P1-12 步骤。"""
        content = _read_workflow()
        docker_build = _extract_docker_build_section(content)
        assert "R66 P1-12" in docker_build, (
            "R66 P1-12: docker-build job 必须包含 R66 P1-12 标记"
        )
        assert "Same-candidate immutable re-verification" in docker_build, (
            "R66 P1-12: docker-build job 必须有 Same-candidate immutable re-verification 步骤"
        )

    def test_p1_12_step_has_name(self):
        """P1-12 步骤必须有 name 字段。"""
        content = _read_workflow()
        docker_build = _extract_docker_build_section(content)
        assert 'name: "R66 P1-12' in docker_build, (
            "R66 P1-12: 步骤必须有 name 字段(以 R66 P1-12 开头)"
        )

    def test_p1_12_step_after_verify_image_pull(self):
        """P1-12 步骤必须在 "Verify image pull by digest" 步骤之后。"""
        content = _read_workflow()
        docker_build = _extract_docker_build_section(content)
        verify_pull_idx = docker_build.find("Verify image pull by digest")
        assert verify_pull_idx != -1, "未找到 Verify image pull by digest 步骤"
        p1_12_idx = docker_build.find("R66 P1-12")
        assert p1_12_idx != -1, "未找到 R66 P1-12 步骤"
        assert p1_12_idx > verify_pull_idx, (
            "R66 P1-12: 步骤必须在 'Verify image pull by digest' 步骤之后"
        )

    def test_p1_12_step_before_upload_artifact(self):
        """P1-12 步骤必须在 "Upload build info artifact" 步骤之前。"""
        content = _read_workflow()
        docker_build = _extract_docker_build_section(content)
        upload_idx = docker_build.find("Upload build info artifact")
        assert upload_idx != -1, "未找到 Upload build info artifact 步骤"
        p1_12_idx = docker_build.find("R66 P1-12")
        assert p1_12_idx != -1
        assert p1_12_idx < upload_idx, (
            "R66 P1-12: 步骤必须在 'Upload build info artifact' 步骤之前"
        )


# ════════════════════════════════════════════════════════════════
# B. 步骤使用 EXPECTED_DIGEST 来自 steps.build.outputs.digest (frozen)
# ════════════════════════════════════════════════════════════════


class TestExpectedDigestFromBuildOutput:
    """EXPECTED_DIGEST 必须来自 steps.build.outputs.digest(frozen,不可变)。"""

    def test_expected_digest_references_build_output(self):
        """EXPECTED_DIGEST 必须引用 steps.build.outputs.digest。"""
        content = _read_workflow()
        p1_12_step = _extract_p1_12_step(content)
        assert "steps.build.outputs.digest" in p1_12_step, (
            "R66 P1-12: EXPECTED_DIGEST 必须来自 steps.build.outputs.digest (frozen)"
        )

    def test_expected_digest_variable_assignment(self):
        """EXPECTED_DIGEST 变量必须被赋值。"""
        content = _read_workflow()
        p1_12_step = _extract_p1_12_step(content)
        assert "EXPECTED_DIGEST=" in p1_12_step, (
            "R66 P1-12: EXPECTED_DIGEST 变量必须被赋值"
        )

    def test_image_name_references_meta_output(self):
        """IMAGE_NAME 必须来自 steps.meta.outputs.name。"""
        content = _read_workflow()
        p1_12_step = _extract_p1_12_step(content)
        assert "steps.meta.outputs.name" in p1_12_step, (
            "R66 P1-12: IMAGE_NAME 必须来自 steps.meta.outputs.name"
        )

    def test_image_ref_constructed_from_name_and_digest(self):
        """IMAGE_REF 必须由 IMAGE_NAME@EXPECTED_DIGEST 构成。"""
        content = _read_workflow()
        p1_12_step = _extract_p1_12_step(content)
        assert 'IMAGE_REF="${IMAGE_NAME}@${EXPECTED_DIGEST}"' in p1_12_step, (
            "R66 P1-12: IMAGE_REF 必须由 ${IMAGE_NAME}@${EXPECTED_DIGEST} 构成"
        )


# ════════════════════════════════════════════════════════════════
# C. 3 次 replay 循环 (for i in 1 2 3)
# ════════════════════════════════════════════════════════════════


class TestThreeReplayLoop:
    """3 次 replay 循环必须存在(for i in 1 2 3)。"""

    def test_loop_iterates_three_times(self):
        """循环必须迭代 3 次(for i in 1 2 3)。"""
        content = _read_workflow()
        p1_12_step = _extract_p1_12_step(content)
        assert "for i in 1 2 3" in p1_12_step, (
            "R66 P1-12: 必须有 3 次循环 (for i in 1 2 3)"
        )

    def test_loop_has_replay_counter_display(self):
        """循环必须显示 Replay #${i}/3 计数。"""
        content = _read_workflow()
        p1_12_step = _extract_p1_12_step(content)
        assert "Replay #${i}/3" in p1_12_step or 'Replay #${i}/3' in p1_12_step, (
            "R66 P1-12: 循环必须显示 Replay #${i}/3 计数"
        )

    def test_loop_has_pass_message_per_iteration(self):
        """每次循环必须有 PASS 消息。"""
        content = _read_workflow()
        p1_12_step = _extract_p1_12_step(content)
        assert "PASS: replay" in p1_12_step, (
            "R66 P1-12: 每次循环必须有 PASS: replay 消息"
        )

    def test_loop_has_final_summary(self):
        """3 次循环后必须有最终汇总消息。"""
        content = _read_workflow()
        p1_12_step = _extract_p1_12_step(content)
        assert "3x digest replay verification PASS" in p1_12_step, (
            "R66 P1-12: 3 次循环后必须有 '3x digest replay verification PASS' 汇总"
        )

    def test_loop_final_summary_mentions_immutability(self):
        """最终汇总必须提及 immutability(不可变性)。"""
        content = _read_workflow()
        p1_12_step = _extract_p1_12_step(content)
        assert "immutability" in p1_12_step.lower(), (
            "R66 P1-12: 最终汇总必须提及 immutability (不可变性验证)"
        )


# ════════════════════════════════════════════════════════════════
# D. 每次 replay 包含 manifest inspect + pull + inspect
# ════════════════════════════════════════════════════════════════


class TestReplayOperations:
    """每次 replay 必须包含 docker manifest inspect + docker pull + docker inspect。"""

    def test_replay_has_manifest_inspect(self):
        """每次 replay 必须调用 docker manifest inspect。"""
        content = _read_workflow()
        p1_12_step = _extract_p1_12_step(content)
        assert "docker manifest inspect" in p1_12_step, (
            "R66 P1-12: 每次 replay 必须调用 docker manifest inspect"
        )

    def test_replay_has_docker_pull(self):
        """每次 replay 必须调用 docker pull(验证可拉取)。"""
        content = _read_workflow()
        p1_12_step = _extract_p1_12_step(content)
        assert "docker pull" in p1_12_step, (
            "R66 P1-12: 每次 replay 必须调用 docker pull (验证可拉取)"
        )

    def test_replay_has_docker_inspect(self):
        """每次 replay 必须调用 docker inspect(验证 RepoDigests)。"""
        content = _read_workflow()
        p1_12_step = _extract_p1_12_step(content)
        assert "docker inspect" in p1_12_step, (
            "R66 P1-12: 每次 replay 必须调用 docker inspect (验证 RepoDigests)"
        )

    def test_replay_checks_repo_digests(self):
        """每次 replay 必须校验 RepoDigests 包含 EXPECTED_DIGEST。"""
        content = _read_workflow()
        p1_12_step = _extract_p1_12_step(content)
        assert "RepoDigests" in p1_12_step, (
            "R66 P1-12: 每次 replay 必须校验 RepoDigests"
        )


# ════════════════════════════════════════════════════════════════
# E. 失败时 exit 1 (fail-closed)
# ════════════════════════════════════════════════════════════════


class TestFailClosedBehavior:
    """任一 replay 失败必须 exit 1(fail-closed)。"""

    def test_manifest_inspect_failure_exits(self):
        """manifest inspect 失败必须 exit 1。"""
        content = _read_workflow()
        p1_12_step = _extract_p1_12_step(content)
        # 找到 manifest inspect 失败分支
        idx = p1_12_step.find("docker manifest inspect 未返回 digest")
        assert idx != -1, "未找到 manifest inspect 失败分支"
        after = p1_12_step[idx:idx + 300]
        assert "exit 1" in after, (
            "R66 P1-12: manifest inspect 失败必须 exit 1"
        )

    def test_docker_pull_failure_exits(self):
        """docker pull 失败必须 exit 1。"""
        content = _read_workflow()
        p1_12_step = _extract_p1_12_step(content)
        idx = p1_12_step.find("docker pull 失败")
        assert idx != -1, "未找到 docker pull 失败分支"
        after = p1_12_step[idx:idx + 300]
        assert "exit 1" in after, (
            "R66 P1-12: docker pull 失败必须 exit 1"
        )

    def test_repo_digests_mismatch_exits(self):
        """RepoDigests 不匹配必须 exit 1。"""
        content = _read_workflow()
        p1_12_step = _extract_p1_12_step(content)
        idx = p1_12_step.find("RepoDigests 不包含")
        assert idx != -1, "未找到 RepoDigests 不匹配分支"
        after = p1_12_step[idx:idx + 300]
        assert "exit 1" in after, (
            "R66 P1-12: RepoDigests 不匹配必须 exit 1"
        )

    def test_digest_modification_exits(self):
        """digest 在循环中被修改必须 exit 1(防御性)。"""
        content = _read_workflow()
        p1_12_step = _extract_p1_12_step(content)
        idx = p1_12_step.find("digest 在循环中被修改")
        assert idx != -1, "未找到 digest 修改检测分支"
        after = p1_12_step[idx:idx + 300]
        assert "exit 1" in after, (
            "R66 P1-12: digest 在循环中被修改必须 exit 1 (防御性)"
        )

    def test_set_euo_pipefail(self):
        """步骤必须 set -euo pipefail(任何命令失败立即退出)。"""
        content = _read_workflow()
        p1_12_step = _extract_p1_12_step(content)
        assert "set -euo pipefail" in p1_12_step, (
            "R66 P1-12: 步骤必须 set -euo pipefail (fail-closed)"
        )


# ════════════════════════════════════════════════════════════════
# F. 步骤语义为 replay 而非 rebuild
# ════════════════════════════════════════════════════════════════


class TestReplayNotRebuildSemantics:
    """步骤语义必须是 replay(重新 pull/inspect),不是 rebuild(重新 build)。

    R66 P1-12: rebuild 会产生新 digest,无法验证不可变性;
    replay 验证同 digest 跨多次访问的不变性。
    """

    def test_step_mentions_replay(self):
        """步骤必须明确提及 replay。"""
        content = _read_workflow()
        docker_build = _extract_docker_build_section(content)
        assert "replay" in docker_build.lower(), (
            "R66 P1-12: 步骤必须明确提及 replay (重放,非重建)"
        )

    def test_step_mentions_not_rebuild(self):
        """步骤必须明确说明 'not rebuild'。"""
        content = _read_workflow()
        docker_build = _extract_docker_build_section(content)
        assert "not rebuild" in docker_build.lower() or "不是 rebuild" in docker_build, (
            "R66 P1-12: 步骤必须明确说明 'not rebuild' (不是 rebuild)"
        )

    def test_step_does_not_call_docker_build(self):
        """P1-12 步骤不能调用 docker build(那会是 rebuild)。"""
        content = _read_workflow()
        p1_12_step = _extract_p1_12_step(content)
        # 禁止出现 docker build 命令(允许 docker buildx,但 P1-12 步骤不应有)
        # 注意:build-push-action 是 docker-build job 的 build 步骤,不是 P1-12
        assert "docker build" not in p1_12_step.lower(), (
            "R66 P1-12: 步骤不能调用 docker build (那会是 rebuild,违反 replay 语义)"
        )

    def test_step_mentions_immutability(self):
        """步骤必须提及 immutability(不可变性)。"""
        content = _read_workflow()
        docker_build = _extract_docker_build_section(content)
        assert "immutab" in docker_build.lower() or "不可变" in docker_build, (
            "R66 P1-12: 步骤必须提及 immutability / 不可变性"
        )

    def test_step_mentions_defense_in_depth(self):
        """步骤必须提及 defense in depth(防御纵深)。"""
        content = _read_workflow()
        docker_build = _extract_docker_build_section(content)
        assert "defense in depth" in docker_build.lower() or "防御纵深" in docker_build, (
            "R66 P1-12: 步骤必须提及 defense in depth (防御纵深)"
        )


# ════════════════════════════════════════════════════════════════
# G. YAML 完整性
# ════════════════════════════════════════════════════════════════


class TestYamlIntegrity:
    """YAML 完整性 + P1-12 整改一致性。"""

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
        assert "docker-build" in parsed["jobs"]

    def test_docker_build_has_p1_12_step_in_yaml(self):
        """YAML 解析后 docker-build job 必须包含 P1-12 步骤。"""
        try:
            import yaml
        except ImportError:
            pytest.skip("PyYAML 未安装")
        content = _read_workflow()
        parsed = yaml.safe_load(content)
        steps = parsed["jobs"]["docker-build"]["steps"]
        # 查找 name 包含 "R66 P1-12" 的步骤
        p1_12_steps = [s for s in steps if "name" in s and "R66 P1-12" in s["name"]]
        assert len(p1_12_steps) >= 1, (
            "R66 P1-12: YAML 解析后 docker-build job 必须包含 R66 P1-12 步骤"
        )

    def test_p1_12_step_has_run_command(self):
        """P1-12 步骤必须有 run 命令(不是 uses)。"""
        try:
            import yaml
        except ImportError:
            pytest.skip("PyYAML 未安装")
        content = _read_workflow()
        parsed = yaml.safe_load(content)
        steps = parsed["jobs"]["docker-build"]["steps"]
        p1_12_steps = [s for s in steps if "name" in s and "R66 P1-12" in s["name"]]
        assert len(p1_12_steps) >= 1
        assert "run" in p1_12_steps[0], (
            "R66 P1-12: 步骤必须有 run 命令(不是 uses action)"
        )
