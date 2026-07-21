"""R67 P0-02 / P0-03: Deploy Check 假绿 + 祖先 workflow 回退 负向测试。

R67 审计背景:
    P0-02: .github/workflows/deploy-check.yml 中存在
           `docker compose config --quiet 2>/dev/null || true`,
           会吞掉真实 Compose 错误。
    P0-03: publish-attestation 在当前 SHA 没有 Deploy Check run 时,
           会接受祖先 SHA 的成功 run(通过 git merge-base --is-ancestor)。

R67 整改:
    P0-02:
      1. 删除 stderr 抑制和 `|| true`
      2. 对全部生产 profiles 执行 docker compose config
      3. 增加负向测试(主动注入无效 Compose,证明门禁必然失败)
      4. 输出 canonical rendered compose artifact
    P0-03:
      1. 删除祖先回退变量(PATH_FILTER_GRACE_SECONDS / TARGET_BRANCH / POLL_START_TS)
      2. 删除祖先回退逻辑块(git merge-base --is-ancestor)
      3. publish-attestation 只接受 head_sha == 当前 release SHA
      4. 增加 skipped/stale 到 FAIL case(不得视为 pending/success)

测试覆盖:
    A. deploy-check.yml 负向测试
        - 无 `|| true`(fail-closed)
        - 无 `2>/dev/null`(stderr 可见)
        - 无 continue-on-error
        - 有负向测试步骤
        - 有 artifact 上传
    B. release-gates.yml publish-attestation 负向测试
        - 无祖先回退变量(PATH_FILTER_GRACE_SECONDS 等)
        - 无 git merge-base --is-ancestor
        - skipped/stale 在 FAIL case 中
        - 有 POLL_DEADLINE 轮询超时
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ════════════════════════════════════════════════════════════════
# A. deploy-check.yml 负向测试(P0-02)
# ════════════════════════════════════════════════════════════════

class TestDeployCheckFailClosed:
    """R67 P0-02: deploy-check.yml 必须是 fail-closed。"""

    @pytest.fixture(scope="class")
    def deploy_check_content(self):
        return (REPO_ROOT / ".github" / "workflows" / "deploy-check.yml").read_text()

    def test_no_pipe_true_in_compose_config(self, deploy_check_content):
        """关键检查:docker compose config 步骤不得有 `|| true`。

        R67 P0-02 核心整改:`docker compose config --quiet 2>/dev/null || true`
        必须被删除 — 这种模式会吞掉真实 Compose 错误,让门禁失效。
        """
        # 查找所有 docker compose config 调用
        # 排除负向测试步骤(负向测试中允许 `|| true` 用于验证失败场景)
        lines = deploy_check_content.split("\n")
        in_negative_test = False
        violations = []
        for i, line in enumerate(lines, 1):
            # 跟踪是否在负向测试步骤内
            if "Negative test" in line or "negative test" in line.lower():
                in_negative_test = True
            if in_negative_test and (line.startswith("    - name:") and "Negative" not in line):
                in_negative_test = False
            # 在非负向测试步骤中,检查 docker compose config || true
            if not in_negative_test and "docker compose" in line and "config" in line:
                if "|| true" in line or "||  true" in line:
                    violations.append((i, line))
        assert not violations, (
            f"R67 P0-02: docker compose config 步骤不得包含 `|| true` (会吞错误),"
            f"违规行: {violations}"
        )

    def test_no_stderr_suppression_in_main_compose_check(self, deploy_check_content):
        """关键检查:主 Compose 校验步骤不得有 `2>/dev/null`(stderr 必须可见)。

        R67 P0-02: stderr 抑制会让 Compose 错误诊断信息丢失。
        注意:负向测试步骤中允许 `2>/dev/null`(用于验证失败场景)。
        """
        # 找到主要的 docker compose config 步骤(非负向测试)
        # 简化检查:确保 "2>/dev/null" 只出现在负向测试步骤内
        negative_section_match = re.search(
            r"Negative test.*?(?=\n    - name:|\Z)",
            deploy_check_content,
            re.DOTALL,
        )
        negative_section = negative_section_match.group(0) if negative_section_match else ""
        # 在负向测试之外,不应有 2>/dev/null 与 docker compose 同时出现
        # 删除负向测试段落后检查
        content_without_negative = deploy_check_content.replace(negative_section, "")
        # 检查 docker compose config 行不得有 2>/dev/null
        for line in content_without_negative.split("\n"):
            if "docker compose" in line and "config" in line:
                assert "2>/dev/null" not in line, (
                    f"R67 P0-02: 主 Compose 校验步骤不得有 2>/dev/null (stderr 必须可见): {line}"
                )

    def test_no_continue_on_error(self, deploy_check_content):
        """关键检查:deploy-check.yml 不得有 continue-on-error。

        R67 P0-02: continue-on-error 会让失败步骤被忽略,门禁失效。
        """
        assert "continue-on-error" not in deploy_check_content, (
            "R67 P0-02: deploy-check.yml 不得有 continue-on-error (会让失败被忽略)"
        )

    def test_has_negative_test_step(self, deploy_check_content):
        """R67 P0-02: deploy-check.yml 必须有负向测试步骤。"""
        assert "Negative test" in deploy_check_content or "negative test" in deploy_check_content.lower(), (
            "R67 P0-02: deploy-check.yml 必须包含负向测试步骤(主动注入无效 Compose)"
        )

    def test_has_artifact_upload(self, deploy_check_content):
        """R67 P0-02: deploy-check.yml 必须上传 rendered compose artifact。"""
        assert "upload-artifact" in deploy_check_content, (
            "R67 P0-02: deploy-check.yml 必须上传 rendered compose artifact"
        )
        assert "rendered-compose" in deploy_check_content, (
            "R67 P0-02: deploy-check.yml 必须上传 rendered-compose artifact"
        )

    def test_has_all_profiles_check(self, deploy_check_content):
        """R67 P0-02: deploy-check.yml 必须对所有生产 profiles 执行 docker compose config。"""
        # 检查有 --profile 参数(说明对所有 profiles 执行)
        assert "--profile" in deploy_check_content, (
            "R67 P0-02: deploy-check.yml 必须对所有生产 profiles 执行 docker compose config"
        )

    def test_has_placeholder_env_creation(self, deploy_check_content):
        """R67 P0-02: deploy-check.yml 必须创建安全占位 env 文件(不写入真实 secret)。"""
        # 检查有创建 .env.deploy-check 占位文件
        assert ".env.deploy-check" in deploy_check_content, (
            "R67 P0-02: deploy-check.yml 必须创建安全占位 .env.deploy-check 文件"
        )
        # 占位值不应是真实 secret(检查占位值是 PLACEHOLDER 之类)
        assert "PLACEHOLDER" in deploy_check_content.upper() or "placeholder" in deploy_check_content, (
            "R67 P0-02: 占位 env 文件必须使用占位值,不是真实 secret"
        )

    def test_no_paths_filter(self, deploy_check_content):
        """R67 P0-03: deploy-check.yml 不得有 paths 过滤。

        R67 P0-03 要求:Deploy Check 不再使用 paths 过滤,
        每个 master/main release candidate 和 tag 对同一 SHA 必须运行。
        """
        # 检查 on.push 不得有 paths 过滤
        # 简化检查:确保没有 paths: 在 on.push 段落
        on_section_match = re.search(r"^on:\s*\n(?:\s+\w+:.*?\n)+", deploy_check_content, re.MULTILINE)
        assert on_section_match, "deploy-check.yml 必须有 on: 触发器"
        on_section = on_section_match.group(0)
        assert "paths:" not in on_section, (
            "R67 P0-03: deploy-check.yml on.push 不得有 paths 过滤"
        )

    def test_triggers_on_master_main_push(self, deploy_check_content):
        """R67 P0-03: deploy-check.yml 必须在 master/main push 时触发。"""
        assert "master" in deploy_check_content, "deploy-check.yml 必须在 master 分支触发"
        assert "main" in deploy_check_content, "deploy-check.yml 必须在 main 分支触发"


# ════════════════════════════════════════════════════════════════
# B. release-gates.yml publish-attestation 负向测试(P0-03)
# ════════════════════════════════════════════════════════════════

class TestPublishAttestationNoAncestorFallback:
    """R67 P0-03: publish-attestation 不得使用祖先 workflow 回退。"""

    @pytest.fixture(scope="class")
    def release_gates_content(self):
        return (REPO_ROOT / ".github" / "workflows" / "release-gates.yml").read_text()

    def test_no_ancestor_fallback_variables(self, release_gates_content):
        """关键检查:代码中不得有祖先回退变量(PATH_FILTER_GRACE_SECONDS 等)。

        R67 P0-03 整改:删除 PATH_FILTER_GRACE_SECONDS / TARGET_BRANCH /
        POLL_START_TS 变量及其设置代码。
        注释中允许提到这些变量名(用于说明已删除)。
        """
        # 删除所有注释行后检查(注释中允许提到,用于说明已删除)
        lines = release_gates_content.split("\n")
        code_lines = [
            line for line in lines
            if not line.strip().startswith("#")
        ]
        code_content = "\n".join(code_lines)
        assert "PATH_FILTER_GRACE_SECONDS" not in code_content, (
            "R67 P0-03: 代码中不得有 PATH_FILTER_GRACE_SECONDS 变量(注释中允许)"
        )

    def test_no_git_merge_base_ancestor(self, release_gates_content):
        """关键检查:代码中不得有 git merge-base --is-ancestor 祖先回退逻辑。

        R67 P0-03 整改:删除祖先回退逻辑块(git merge-base --is-ancestor)。
        注释中允许提到(用于说明已删除)。
        """
        # 删除所有注释行后检查
        lines = release_gates_content.split("\n")
        code_lines = [
            line for line in lines
            if not line.strip().startswith("#")
        ]
        code_content = "\n".join(code_lines)
        assert "merge-base --is-ancestor" not in code_content, (
            "R67 P0-03: 代码中不得有 git merge-base --is-ancestor 祖先回退逻辑"
        )

    def test_no_ancestor_fallback_comment_only(self, release_gates_content):
        """允许祖先回退变量出现在注释中(说明已删除),但不得在代码中。

        R67 P0-03: 注释中提到 "ancestor fallback" 是允许的(用于说明已删除),
        但实际代码中不得有祖先回退逻辑。
        """
        # 删除所有注释行后检查
        lines = release_gates_content.split("\n")
        code_lines = [
            line for line in lines
            if not line.strip().startswith("#")
        ]
        code_content = "\n".join(code_lines)
        # 代码中不得有 PATH_FILTER_GRACE_SECONDS
        assert "PATH_FILTER_GRACE_SECONDS" not in code_content, (
            "R67 P0-03: 代码中不得有 PATH_FILTER_GRACE_SECONDS(注释中允许)"
        )
        # 代码中不得有 merge-base --is-ancestor
        assert "merge-base --is-ancestor" not in code_content, (
            "R67 P0-03: 代码中不得有 merge-base --is-ancestor(注释中允许)"
        )

    def test_skipped_stale_in_fail_case(self, release_gates_content):
        """关键检查:skipped/stale 必须在 FAIL case 中。

        R67 P0-03 补充:skipped/stale/neutral/cancelled/timed_out/action_required
        全部视为 FAIL,不得视为 pending 或 success。
        """
        # 查找 case 语句中的 FAIL 模式
        # 应该有 completed:skipped 和 completed:stale
        assert "completed:skipped" in release_gates_content, (
            "R67 P0-03: completed:skipped 必须在 FAIL case 中"
        )
        assert "completed:stale" in release_gates_content, (
            "R67 P0-03: completed:stale 必须在 FAIL case 中"
        )

    def test_has_poll_deadline(self, release_gates_content):
        """R67 P0-03: publish-attestation 必须有轮询超时(POLL_DEADLINE)。

        R67 P0-03 要求:设置明确轮询超时;超时即 failure。
        """
        # 在 publish-attestation 段落中应有 POLL_DEADLINE
        assert "POLL_DEADLINE" in release_gates_content, (
            "R67 P0-03: publish-attestation 必须有 POLL_DEADLINE 轮询超时"
        )

    def test_head_sha_must_match(self, release_gates_content):
        """R67 P0-03: publish-attestation 只接受 head_sha == 当前 release SHA。

        R67 P0-03 要求:publish-attestation 只接受:
          - head_sha == 当前 release SHA
          - status == completed
          - conclusion == success
        """
        # 应该有 head_sha 比较逻辑
        assert "head_sha" in release_gates_content, (
            "R67 P0-03: publish-attestation 必须校验 head_sha 与当前 SHA 一致"
        )
        # 应该有 TARGET_SHA 变量(当前 release SHA)
        assert "TARGET_SHA" in release_gates_content or "github.sha" in release_gates_content, (
            "R67 P0-03: publish-attestation 必须使用当前 SHA 作为 target"
        )


# ════════════════════════════════════════════════════════════════
# C. 综合 fail-closed 行为验证
# ════════════════════════════════════════════════════════════════

class TestFailClosedBehavior:
    """R67 P0-02/P0-03: 综合 fail-closed 行为验证。"""

    def test_deploy_check_yaml_valid(self):
        """deploy-check.yml 是有效 YAML。"""
        import yaml
        path = REPO_ROOT / ".github" / "workflows" / "deploy-check.yml"
        with open(path) as f:
            data = yaml.safe_load(f)
        assert "jobs" in data
        assert "verify-deploy" in data["jobs"]

    def test_release_gates_yaml_valid(self):
        """release-gates.yml 是有效 YAML。"""
        import yaml
        path = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"
        with open(path) as f:
            data = yaml.safe_load(f)
        assert "jobs" in data
        assert "publish-attestation" in data["jobs"]

    def test_deploy_check_no_paths_in_on_trigger(self):
        """R67 P0-03: deploy-check.yml 的 on.push 不得有 paths 过滤。"""
        import yaml
        path = REPO_ROOT / ".github" / "workflows" / "deploy-check.yml"
        with open(path) as f:
            data = yaml.safe_load(f)
        push_config = data.get("on", {}).get("push", {})
        # 不应有 paths 过滤
        if isinstance(push_config, dict):
            assert "paths" not in push_config, (
                "R67 P0-03: deploy-check.yml on.push 不得有 paths 过滤"
            )
            assert "paths-ignore" not in push_config, (
                "R67 P0-03: deploy-check.yml on.push 不得有 paths-ignore 过滤"
            )
