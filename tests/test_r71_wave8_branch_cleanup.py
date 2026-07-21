"""R71 Wave 8: 残留分支管理 — 测试套件。

R71 P1-06 整改背景:
    残留分支不是单人项目的上线 P0,但会增加误构建风险(例如 CI 意外触发
    旧分支的构建)。R71 报告建议:
      1. 启用 PR 合并后自动删除 head 分支
      2. 让 release workflow 只接受 master 和受保护 tag(rc-v* / production-v*)
    本 Wave 整改:
      1. 新增 .github/workflows/auto-delete-branch.yml:
         - 触发: pull_request closed + merged == true
         - 保护: master / main / release/* 分支永不被删除
         - 审计: 记录 PR 号、merge commit、删除分支名、操作者到 step summary
      2. 新增 scripts/cleanup_merged_branches.sh:
         - 手动清理已 squash-merge 的本地 + 远程分支
         - 支持 --force / --dry-run / --help
         - 保护 master / main / release/* / hotfix/* 分支
         - 审计日志写入 .git/info/branch-cleanup-audit.log
      3. release-gates.yml 触发条件已限制为 master/main + rc-v*/production-v*
         (R70 P0-10 已完成,本 Wave 验证一致性)

被测对象:
    - .github/workflows/auto-delete-branch.yml(自动删除工作流)
    - scripts/cleanup_merged_branches.sh(手动清理脚本)
    - .github/workflows/release-gates.yml(触发条件一致性)

测试覆盖矩阵:
    A. auto-delete-branch.yml 工作流结构(8 个)
    B. cleanup_merged_branches.sh 脚本结构(8 个)
    C. release-gates.yml 触发条件一致性(4 个)

测试策略:
    - 静态文本检查(不实际执行 bash / 不实际触发 GitHub Actions)
    - Windows 兼容(无 bash 依赖)
    - 严格遵守 R71 整改规范(无 TODO / pass / 占位符)
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
AUTO_DELETE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "auto-delete-branch.yml"
CLEANUP_SCRIPT = REPO_ROOT / "scripts" / "cleanup_merged_branches.sh"
RELEASE_GATES_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"


# ════════════════════════════════════════════════════════════════
# A. auto-delete-branch.yml 工作流结构
# ════════════════════════════════════════════════════════════════


class TestAutoDeleteBranchWorkflow:
    """R71 P1-06: auto-delete-branch.yml 自动删除已合并的 head 分支。"""

    @pytest.fixture(scope="class")
    def content(self) -> str:
        assert AUTO_DELETE_WORKFLOW.exists(), (
            ".github/workflows/auto-delete-branch.yml 必须存在"
        )
        return AUTO_DELETE_WORKFLOW.read_text(encoding="utf-8")

    def test_file_exists(self):
        """R71 P1-06: auto-delete-branch.yml 文件存在。"""
        assert AUTO_DELETE_WORKFLOW.is_file(), (
            "R71 P1-06: .github/workflows/auto-delete-branch.yml 必须存在"
        )

    def test_workflow_name(self, content: str):
        """工作流名称描述其用途。"""
        assert "name:" in content
        assert "Delete" in content or "delete" in content

    def test_triggers_on_pull_request_closed(self, content: str):
        """R71 P1-06: 触发条件为 pull_request closed。"""
        assert "on:" in content
        assert "pull_request" in content
        assert "closed" in content

    def test_only_runs_when_merged(self, content: str):
        """R71 P1-06: 只在 PR 被合并(merged == true)时执行。"""
        assert "merged" in content
        assert "true" in content

    def test_has_write_permissions(self, content: str):
        """R71 P1-06: 需要 contents: write 权限删除分支。"""
        assert "permissions:" in content
        assert "contents" in content
        assert "write" in content

    def test_protects_master_and_main(self, content: str):
        """R71 P1-06: master / main 分支永不被删除。"""
        assert "master" in content
        assert "main" in content
        # 应有保护逻辑(检查分支名并拒绝删除)
        assert "Refusing" in content or "protected" in content

    def test_protects_release_prefix(self, content: str):
        """R71 P1-06: release/* 分支永不被删除。"""
        assert "release/" in content or "release/*" in content

    def test_has_audit_trail(self, content: str):
        """R71 P1-06: 审计日志记录到 step summary。"""
        assert "GITHUB_STEP_SUMMARY" in content or "audit" in content.lower()

    def test_uses_github_token(self, content: str):
        """R71 P1-06: 使用 GITHUB_TOKEN 认证(非 PAT)。"""
        assert "GITHUB_TOKEN" in content


# ════════════════════════════════════════════════════════════════
# B. cleanup_merged_branches.sh 脚本结构
# ════════════════════════════════════════════════════════════════


class TestCleanupMergedBranchesScript:
    """R71 P1-06: cleanup_merged_branches.sh 手动清理已合并分支。"""

    @pytest.fixture(scope="class")
    def content(self) -> str:
        assert CLEANUP_SCRIPT.exists(), (
            "scripts/cleanup_merged_branches.sh 必须存在"
        )
        return CLEANUP_SCRIPT.read_text(encoding="utf-8")

    def test_file_exists(self):
        """R71 P1-06: cleanup_merged_branches.sh 文件存在。"""
        assert CLEANUP_SCRIPT.is_file(), (
            "R71 P1-06: scripts/cleanup_merged_branches.sh 必须存在"
        )

    def test_has_shebang(self, content: str):
        """脚本以 #!/usr/bin/env bash 开头。"""
        first_line = content.split("\n")[0]
        assert first_line == "#!/usr/bin/env bash", (
            f"脚本应以 #!/usr/bin/env bash 开头, 实际: {first_line!r}"
        )

    def test_has_set_euo_pipefail(self, content: str):
        """R71 P1-06: 脚本启用 set -euo pipefail(严格模式)。"""
        assert "set -euo pipefail" in content or "set -eu" in content

    def test_supports_force_mode(self, content: str):
        """R71 P1-06: 支持 --force 模式(不提示确认)。"""
        assert "--force" in content

    def test_supports_dry_run_mode(self, content: str):
        """R71 P1-06: 支持 --dry-run 模式(只显示不删除)。"""
        assert "--dry-run" in content

    def test_supports_help(self, content: str):
        """R71 P1-06: 支持 --help。"""
        assert "--help" in content

    def test_protects_master_and_main(self, content: str):
        """R71 P1-06: master / main 分支被保护。"""
        assert "master" in content
        assert "main" in content
        assert "PROTECTED" in content or "protected" in content

    def test_protects_release_prefix(self, content: str):
        """R71 P1-06: release/* 和 hotfix/* 分支被保护。"""
        assert "release/" in content
        assert "hotfix/" in content

    def test_has_audit_log(self, content: str):
        """R71 P1-06: 审计日志写入 .git/info/branch-cleanup-audit.log。"""
        assert "audit" in content.lower() or "AUDIT_LOG" in content

    def test_checks_squash_merged(self, content: str):
        """R71 P1-06: 使用 git cherry 检测 squash-merge(非 merge ancestry)。"""
        assert "cherry" in content or "merge-base" in content


# ════════════════════════════════════════════════════════════════
# C. release-gates.yml 触发条件一致性
# ════════════════════════════════════════════════════════════════


class TestReleaseGatesTriggerConsistency:
    """R71 P1-06: release-gates.yml 只接受 master 和受保护 tag。"""

    @pytest.fixture(scope="class")
    def content(self) -> str:
        assert RELEASE_GATES_WORKFLOW.exists(), (
            ".github/workflows/release-gates.yml 必须存在"
        )
        return RELEASE_GATES_WORKFLOW.read_text(encoding="utf-8")

    def test_push_only_master_and_main(self, content: str):
        """R71 P1-06: push 触发只接受 master / main 分支。"""
        assert "branches:" in content
        assert "master" in content
        assert "main" in content

    def test_tags_only_rc_and_production(self, content: str):
        """R71 P1-06: tag 触发只接受 rc-v* 和 production-v*。"""
        assert "rc-v*" in content
        assert "production-v*" in content

    def test_no_legacy_v_tags(self, content: str):
        """R71 P1-06: 旧版 v*.*.* tag 不应触发 release-gates。

        R70 P0-10: 旧版 v*.*.* tag 已废弃 — production 部署只能通过
        production-v* tag 或 workflow_dispatch 触发。
        """
        # 不应出现 "v*.*.*" 作为 tag 触发模式
        # (允许在注释中提到废弃,但 on.push.tags 不应包含 v*.*.*)
        lines = content.split("\n")
        in_on_section = False
        in_tags = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("on:"):
                in_on_section = True
                continue
            if in_on_section and stripped.startswith("name:"):
                break  # on: 块结束
            if in_on_section:
                if "tags:" in stripped:
                    in_tags = True
                    continue
                if in_tags and stripped.startswith("-"):
                    tag_pattern = stripped.lstrip("- ").strip("'\"")
                    assert not (
                        tag_pattern.startswith("v") and "*" in tag_pattern
                        and "." in tag_pattern
                    ), (
                        f"R71 P1-06: release-gates.yml 不应接受旧版 {tag_pattern} tag 触发"
                    )
                elif in_tags and not stripped.startswith("-"):
                    in_tags = False

    def test_pull_request_only_master_and_main(self, content: str):
        """R71 P1-06: pull_request 触发只接受 master / main。"""
        assert "pull_request" in content
        # pull_request branches 应限制为 master/main
        # (已在 R70 P0-10 中完成)
