"""R66 P1-11: Tag Ruleset 不可变性配置验证测试。

审计背景(R66 终审报告 P1-11):
    为 tags 配置不可变、禁止删除/移动的 ruleset;
    生产 environment 需要独立审批、最小权限和 digest-pinned deploy。
    禁止用 master 的 required checks 推导 tag 已满足相同条件。

整改方案(R66 P1-11):
    1. 新增 ``scripts/configure_tag_ruleset.sh``:
       通过 GitHub REST API(POST /repos/{owner}/{repo}/rulesets
       或 PUT /repos/{owner}/{repo}/rulesets/{id})配置一个针对
       refs/tags/* 的 Repository Ruleset,强制以下规则:
         - creation          限制 tag 创建(仅 bypass_actors 可创建)
         - deletion          false — 禁止删除 tag
         - non_fast_forward  false — 禁止移动 / 更新 tag(不可变)
         - update            false — 禁止更新 tag
         - required_signatures  true — 强制 GPG 签名验证
       幂等性:若同名 ruleset 已存在(按 name 查找),则 PUT 更新;否则 POST 创建。
    2. 新增 ``scripts/verify_tag_ruleset.sh``:
       调用 GET /repos/{owner}/{repo}/rulesets,查找 target=tags 且
       conditions.ref_name.include 包含 refs/tags/* 的 ruleset,
       断言所有必需规则(deletion / non_fast_forward / update /
       creation / required_signatures)均已启用。
    3. 新增 ``.github/tag_ruleset.expected.json``:
       checked-in 基线配置,文档化期望的 tag ruleset 结构。

测试覆盖矩阵:
    A. 脚本文件存在且可执行(configure / verify)
    B. tag_ruleset.expected.json 是合法 JSON 且包含必需字段
    C. configure_tag_ruleset.sh 内容断言(gh api /rulesets / refs/tags/* / 幂等)
    D. verify_tag_ruleset.sh 内容断言(gh api /rulesets / deletion=false /
       non_fast_forward=false / update=false)
    E. --help 标志在两个脚本上均正常工作(subprocess)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="shell scripts require bash, not available on Windows")

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
CONFIGURE_SCRIPT = SCRIPTS_DIR / "configure_tag_ruleset.sh"
VERIFY_SCRIPT = SCRIPTS_DIR / "verify_tag_ruleset.sh"
EXPECTED_JSON = REPO_ROOT / ".github" / "tag_ruleset.expected.json"

# 测试环境兼容 — conftest.py 在收集阶段已注入 config/telegram mock,
# 此处再注入一次以防本文件被单独运行
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())


# ════════════════════════════════════════════════════════════════
# A. 脚本文件存在且可执行
# ════════════════════════════════════════════════════════════════


class TestScriptExistence:
    """R66 P1-11: 脚本文件存在且可执行。"""

    def test_configure_tag_ruleset_script_exists(self):
        """场景 A1: configure_tag_ruleset.sh 必须存在。"""
        assert CONFIGURE_SCRIPT.exists(), (
            f"R66 P1-11: {CONFIGURE_SCRIPT} 必须存在"
        )

    def test_verify_tag_ruleset_script_exists(self):
        """场景 A2: verify_tag_ruleset.sh 必须存在。"""
        assert VERIFY_SCRIPT.exists(), (
            f"R66 P1-11: {VERIFY_SCRIPT} 必须存在"
        )

    def test_configure_tag_ruleset_script_executable(self):
        """场景 A3: configure_tag_ruleset.sh 必须可执行。"""
        assert CONFIGURE_SCRIPT.exists(), "configure_tag_ruleset.sh 不存在"
        assert os.access(CONFIGURE_SCRIPT, os.X_OK), (
            f"R66 P1-11: {CONFIGURE_SCRIPT} 必须可执行(chmod +x)"
        )

    def test_verify_tag_ruleset_script_executable(self):
        """场景 A4: verify_tag_ruleset.sh 必须可执行。"""
        assert VERIFY_SCRIPT.exists(), "verify_tag_ruleset.sh 不存在"
        assert os.access(VERIFY_SCRIPT, os.X_OK), (
            f"R66 P1-11: {VERIFY_SCRIPT} 必须可执行(chmod +x)"
        )


# ════════════════════════════════════════════════════════════════
# B. tag_ruleset.expected.json 是合法 JSON 且包含必需字段
# ════════════════════════════════════════════════════════════════


class TestExpectedJson:
    """R66 P1-11: tag_ruleset.expected.json 是合法 JSON 且包含必需字段。"""

    @pytest.fixture
    def expected_data(self) -> dict:
        """加载 tag_ruleset.expected.json 为 dict。"""
        assert EXPECTED_JSON.exists(), (
            f"R66 P1-11: {EXPECTED_JSON} 必须存在"
        )
        with EXPECTED_JSON.open(encoding="utf-8") as f:
            return json.load(f)

    def test_json_is_valid(self):
        """场景 B1: tag_ruleset.expected.json 是合法 JSON。"""
        assert EXPECTED_JSON.exists()
        with EXPECTED_JSON.open(encoding="utf-8") as f:
            data = json.load(f)
        assert isinstance(data, dict), "顶层应为 JSON 对象"

    def test_json_has_required_fields(self, expected_data: dict):
        """场景 B2: 包含所有必需字段(name / target / source_type / enforcement /
        conditions / rules / description)。"""
        required_fields = [
            "name", "target", "source_type", "enforcement",
            "conditions", "rules", "description",
        ]
        missing = [f for f in required_fields if f not in expected_data]
        assert not missing, (
            f"R66 P1-11: tag_ruleset.expected.json 缺少必需字段: {missing}"
        )

    def test_json_target_is_tags(self, expected_data: dict):
        """场景 B3: target == "tags"。"""
        assert expected_data["target"] == "tags", (
            f"R66 P1-11: target 应为 'tags',实际 '{expected_data['target']}'"
        )

    def test_json_source_type_is_repository(self, expected_data: dict):
        """场景 B4: source_type == "Repository"。"""
        assert expected_data["source_type"] == "Repository", (
            f"R66 P1-11: source_type 应为 'Repository',"
            f"实际 '{expected_data['source_type']}'"
        )

    def test_json_enforcement_is_active(self, expected_data: dict):
        """场景 B5: enforcement == "active"。"""
        assert expected_data["enforcement"] == "active", (
            f"R66 P1-11: enforcement 应为 'active',"
            f"实际 '{expected_data['enforcement']}'"
        )

    def test_json_conditions_include_refs_tags(self, expected_data: dict):
        """场景 B6: conditions.ref_name.include 包含 "refs/tags/*"。"""
        include = expected_data["conditions"]["ref_name"]["include"]
        assert "refs/tags/*" in include, (
            f"R66 P1-11: conditions.ref_name.include 应包含 'refs/tags/*',"
            f"实际 {include}"
        )

    def test_json_rules_include_all_required(self, expected_data: dict):
        """场景 B7: rules 包含所有必需规则类型
        (creation / deletion / non_fast_forward / update / required_signatures)。"""
        rule_types = {r["type"] for r in expected_data["rules"]}
        required_rules = {
            "creation", "deletion", "non_fast_forward",
            "update", "required_signatures",
        }
        missing = required_rules - rule_types
        assert not missing, (
            f"R66 P1-11: rules 缺少必需规则类型: {missing}"
        )

    def test_json_has_description_field(self, expected_data: dict):
        """场景 B8: description 字段存在且解释 R66 P1-11 理由。"""
        desc = expected_data["description"]
        assert isinstance(desc, str) and len(desc) > 0, (
            "description 应为非空字符串"
        )
        assert "R66 P1-11" in desc, (
            "description 应提及 R66 P1-11(整改标识)"
        )


# ════════════════════════════════════════════════════════════════
# C. configure_tag_ruleset.sh 内容断言
# ════════════════════════════════════════════════════════════════


class TestConfigureScriptContent:
    """R66 P1-11: configure_tag_ruleset.sh 内容断言。"""

    @pytest.fixture
    def script_content(self) -> str:
        assert CONFIGURE_SCRIPT.exists(), "configure_tag_ruleset.sh 不存在"
        return CONFIGURE_SCRIPT.read_text(encoding="utf-8")

    def test_has_shebang(self, script_content: str):
        """场景 C1: 使用 #!/usr/bin/env bash shebang。"""
        assert script_content.startswith("#!/usr/bin/env bash"), (
            "R66 P1-11: 脚本应以 '#!/usr/bin/env bash' 开头"
        )

    def test_has_set_euo_pipefail(self, script_content: str):
        """场景 C2: 启用 set -euo pipefail(严格模式)。"""
        assert "set -euo pipefail" in script_content, (
            "R66 P1-11: 脚本必须启用 'set -euo pipefail'"
        )

    def test_uses_gh_api_rulesets(self, script_content: str):
        """场景 C3: 使用 gh api 调用 /rulesets 端点。"""
        assert "gh api" in script_content, (
            "R66 P1-11: 必须使用 gh api 调用 GitHub API"
        )
        assert "/rulesets" in script_content, (
            "R66 P1-11: 必须调用 /rulesets 端点(POST 创建 / PUT 更新 / GET 列出)"
        )

    def test_uses_refs_tags_target(self, script_content: str):
        """场景 C4: 使用 refs/tags/* 作为 ruleset 目标。"""
        assert "refs/tags/*" in script_content, (
            "R66 P1-11: ruleset 必须针对 refs/tags/*"
        )

    def test_is_idempotent(self, script_content: str):
        """场景 C5: 幂等性 — 检查现有 ruleset 后决定 PUT 更新或 POST 创建。"""
        # 必须有查找现有 ruleset 的逻辑(按 name 查找)
        assert "EXISTING_RULESET_ID" in script_content or "幂等" in script_content, (
            "R66 P1-11: 必须实现幂等性(查找现有 ruleset)"
        )
        # 必须同时支持 PUT(更新)与 POST(创建)两种路径
        assert "-X PUT" in script_content or "PUT" in script_content, (
            "R66 P1-11: 必须支持 PUT 更新现有 ruleset"
        )
        assert "-X POST" in script_content or "POST" in script_content, (
            "R66 P1-11: 必须支持 POST 创建新 ruleset"
        )

    def test_includes_all_required_rules(self, script_content: str):
        """场景 C6: payload 包含所有必需规则类型。"""
        required_rules = [
            "creation", "deletion", "non_fast_forward",
            "update", "required_signatures",
        ]
        missing = [r for r in required_rules if r not in script_content]
        assert not missing, (
            f"R66 P1-11: configure 脚本缺少规则类型: {missing}"
        )

    def test_has_r66_p1_11_prefix(self, script_content: str):
        """场景 C7: 错误消息包含 R66 P1-11 前缀(便于审计定位)。"""
        assert "R66 P1-11" in script_content, (
            "R66 P1-11: 脚本必须包含 'R66 P1-11' 整改标识"
        )

    def test_uses_owner_repo_from_env_or_gh(self, script_content: str):
        """场景 C8: 从环境变量或 gh repo view --json owner,name 读取 OWNER/REPO。"""
        assert "OWNER" in script_content and "REPO" in script_content, (
            "R66 P1-11: 必须支持 OWNER/REPO 环境变量"
        )
        assert "gh repo view --json owner,name" in script_content, (
            "R66 P1-11: 必须支持从 gh repo view --json owner,name 推断 OWNER/REPO"
        )


# ════════════════════════════════════════════════════════════════
# D. verify_tag_ruleset.sh 内容断言
# ════════════════════════════════════════════════════════════════


class TestVerifyScriptContent:
    """R66 P1-11: verify_tag_ruleset.sh 内容断言。"""

    @pytest.fixture
    def script_content(self) -> str:
        assert VERIFY_SCRIPT.exists(), "verify_tag_ruleset.sh 不存在"
        return VERIFY_SCRIPT.read_text(encoding="utf-8")

    def test_has_shebang(self, script_content: str):
        """场景 D1: 使用 #!/usr/bin/env bash shebang。"""
        assert script_content.startswith("#!/usr/bin/env bash"), (
            "R66 P1-11: 脚本应以 '#!/usr/bin/env bash' 开头"
        )

    def test_has_set_euo_pipefail(self, script_content: str):
        """场景 D2: 启用 set -euo pipefail(严格模式)。"""
        assert "set -euo pipefail" in script_content, (
            "R66 P1-11: 脚本必须启用 'set -euo pipefail'"
        )

    def test_uses_gh_api_rulesets(self, script_content: str):
        """场景 D3: 使用 gh api 调用 /rulesets 端点(GET 列出)。"""
        assert "gh api" in script_content, (
            "R66 P1-11: 必须使用 gh api 调用 GitHub API"
        )
        assert "/rulesets" in script_content, (
            "R66 P1-11: 必须调用 GET /repos/{owner}/{repo}/rulesets"
        )

    def test_uses_refs_tags_target(self, script_content: str):
        """场景 D4: 查找目标为 refs/tags/* 的 ruleset。"""
        assert "refs/tags/*" in script_content, (
            "R66 P1-11: 必须查找 conditions.ref_name.include 包含 refs/tags/* 的 ruleset"
        )

    def test_asserts_deletion_false(self, script_content: str):
        """场景 D5: 断言 deletion=false(tags 不可删除)。"""
        assert "deletion" in script_content, (
            "R66 P1-11: verify 脚本必须检查 deletion 规则"
        )
        # 验证 deletion 与 false 在同一上下文出现(规则存在即表示 deletion=false)
        assert re.search(
            r"deletion[^a-z_]{0,30}false",
            script_content,
            re.IGNORECASE,
        ), (
            "R66 P1-11: verify 脚本必须断言 deletion=false (tags 不可删除)"
        )

    def test_asserts_non_fast_forward_false(self, script_content: str):
        """场景 D6: 断言 non_fast_forward=false(tags 不可移动)。"""
        assert "non_fast_forward" in script_content, (
            "R66 P1-11: verify 脚本必须检查 non_fast_forward 规则"
        )
        assert re.search(
            r"non_fast_forward[^a-z_]{0,30}false",
            script_content,
            re.IGNORECASE,
        ), (
            "R66 P1-11: verify 脚本必须断言 non_fast_forward=false (tags 不可移动)"
        )

    def test_asserts_update_false(self, script_content: str):
        """场景 D7: 断言 update=false(tags 不可更新)。"""
        assert "update" in script_content, (
            "R66 P1-11: verify 脚本必须检查 update 规则"
        )
        # 排除 "update_xxx" 形式的误匹配:要求 update 后紧跟非字母非下划线字符
        assert re.search(
            r"update[^a-z_]{0,30}false",
            script_content,
            re.IGNORECASE,
        ), (
            "R66 P1-11: verify 脚本必须断言 update=false (tags 不可更新)"
        )

    def test_asserts_required_signatures(self, script_content: str):
        """场景 D8: 断言 required_signatures 启用(强制 GPG 签名验证)。"""
        assert "required_signatures" in script_content, (
            "R66 P1-11: verify 脚本必须检查 required_signatures 规则"
        )

    def test_asserts_creation_rule(self, script_content: str):
        """场景 D9: 断言 creation 规则存在(创建限制)。"""
        assert "creation" in script_content, (
            "R66 P1-11: verify 脚本必须检查 creation 规则(创建限制)"
        )

    def test_has_r66_p1_11_prefix(self, script_content: str):
        """场景 D10: 错误消息包含 R66 P1-11 前缀(便于审计定位)。"""
        assert "R66 P1-11" in script_content, (
            "R66 P1-11: 脚本必须包含 'R66 P1-11' 整改标识"
        )

    def test_has_fail_diag_function(self, script_content: str):
        """场景 D11: 失败时输出完整诊断信息(fail_diag 函数)。"""
        assert "fail_diag" in script_content, (
            "R66 P1-11: verify 脚本必须有 fail_diag 函数(失败时输出诊断信息)"
        )


# ════════════════════════════════════════════════════════════════
# E. --help 标志在两个脚本上均正常工作(subprocess)
# ════════════════════════════════════════════════════════════════


class TestHelpFlag:
    """R66 P1-11: 脚本支持 --help 标志(subprocess 实际执行)。"""

    def test_configure_script_help_exits_0(self):
        """场景 E1: configure_tag_ruleset.sh --help 退出码 0。"""
        result = subprocess.run(
            ["bash", str(CONFIGURE_SCRIPT), "--help"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=10,
        )
        assert result.returncode == 0, (
            f"R66 P1-11: --help 应 exit 0,实际 {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        # 输出应包含用法说明
        assert "用法" in result.stdout or "usage" in result.stdout.lower(), (
            f"R66 P1-11: --help 输出应包含用法说明\nstdout:\n{result.stdout}"
        )

    def test_verify_script_help_exits_0(self):
        """场景 E2: verify_tag_ruleset.sh --help 退出码 0。"""
        result = subprocess.run(
            ["bash", str(VERIFY_SCRIPT), "--help"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=10,
        )
        assert result.returncode == 0, (
            f"R66 P1-11: --help 应 exit 0,实际 {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        assert "用法" in result.stdout or "usage" in result.stdout.lower(), (
            f"R66 P1-11: --help 输出应包含用法说明\nstdout:\n{result.stdout}"
        )

    def test_configure_script_help_h_flag(self):
        """场景 E3: configure_tag_ruleset.sh -h(短标志)退出码 0。"""
        result = subprocess.run(
            ["bash", str(CONFIGURE_SCRIPT), "-h"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=10,
        )
        assert result.returncode == 0, (
            f"R66 P1-11: -h 应 exit 0,实际 {result.returncode}\n"
            f"stderr:\n{result.stderr}"
        )

    def test_verify_script_help_h_flag(self):
        """场景 E4: verify_tag_ruleset.sh -h(短标志)退出码 0。"""
        result = subprocess.run(
            ["bash", str(VERIFY_SCRIPT), "-h"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=10,
        )
        assert result.returncode == 0, (
            f"R66 P1-11: -h 应 exit 0,实际 {result.returncode}\n"
            f"stderr:\n{result.stderr}"
        )

    def test_configure_help_mentions_required_rules(self):
        """场景 E5: configure --help 输出提及所有必需规则。"""
        result = subprocess.run(
            ["bash", str(CONFIGURE_SCRIPT), "--help"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=10,
        )
        assert result.returncode == 0
        for rule in [
            "creation", "deletion", "non_fast_forward",
            "update", "required_signatures",
        ]:
            assert rule in result.stdout, (
                f"R66 P1-11: configure --help 应提及规则 '{rule}'\n"
                f"stdout:\n{result.stdout}"
            )

    def test_verify_help_mentions_required_assertions(self):
        """场景 E6: verify --help 输出提及所有必需断言。"""
        result = subprocess.run(
            ["bash", str(VERIFY_SCRIPT), "--help"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=10,
        )
        assert result.returncode == 0
        # 应提及 deletion / non_fast_forward / update 等断言
        for keyword in ["deletion", "non_fast_forward", "update"]:
            assert keyword in result.stdout, (
                f"R66 P1-11: verify --help 应提及断言 '{keyword}'\n"
                f"stdout:\n{result.stdout}"
            )
