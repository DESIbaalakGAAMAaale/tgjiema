"""R67 P1-12: 供应链 attestation 检查命名语义校正 — 单元测试。

R67 P1-12 整改要点:
    1. 旧检查 `predicate_materials_source_tree_sha` 实际验证的是 source_commit
       SHA(attestation materials[] 中的 commit digest 与 release_manifest.
       source_commit 一致),命名误导审计。
    2. 重命名为 `predicate_materials_source_commit`,反映真实语义。
    3. 新增独立检查 `predicate_materials_source_tree`,通过
       `git rev-parse <commit>^{tree}` 验证 release_manifest.source_commit
       派生的 tree SHA 与 release_manifest.source_tree_sha 一致。
    4. 两个检查互补:
       - source_commit:验证 attestation 内嵌证据(materials[] 中的 commit digest)
       - source_tree:验证本地 git 仓库证据(git rev-parse 派生的 tree SHA)

测试覆盖:
    A. 旧检查名不再存在(防止回归)
    B. 新检查名 predicate_materials_source_commit 存在并正确工作
    C. 新检查名 predicate_materials_source_tree 存在并正确工作
    D. _resolve_tree_sha_for_commit 辅助函数行为
    E. 真实 git 仓库场景(端到端验证)
    F. repo_root 参数支持
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# 测试环境兼容(mock telegram 库,避免 ImportError)
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "verify_attestation_semantics.py"
sys.path.insert(0, str(REPO_ROOT))


# ════════════════════════════════════════════════════════════════
# 模块加载
# ════════════════════════════════════════════════════════════════


def _load_verify_module():
    """通过 importlib 加载 verify_attestation_semantics 模块。"""
    spec = importlib.util.spec_from_file_location(
        "verify_attestation_semantics", SCRIPT_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_verify_mod = _load_verify_module()


# ════════════════════════════════════════════════════════════════
# Fixture 工厂与辅助
# ════════════════════════════════════════════════════════════════

# 使用真实 HEAD commit + tree SHA(确保 git rev-parse 可解析)
def _get_real_head_commit_and_tree() -> tuple[str, str]:
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(REPO_ROOT), text=True, stderr=subprocess.DEVNULL,
        ).strip()
        tree = subprocess.check_output(
            ["git", "rev-parse", "HEAD^{tree}"],
            cwd=str(REPO_ROOT), text=True, stderr=subprocess.DEVNULL,
        ).strip()
        return commit, tree
    except (OSError, subprocess.SubprocessError):
        pytest.skip("当前环境非 git 仓库,跳过 P1-12 真实 git 测试")


REAL_COMMIT_SHA, REAL_TREE_SHA = _get_real_head_commit_and_tree()

# 占位常量
IMAGE_SHA = "a" * 64
MIGRATION_MANIFEST_DIGEST = "d" * 64
IMAGE_REF = f"ghcr.io/example/app@sha256:{IMAGE_SHA}"
SUBJECT_NAME = "ghcr.io/example/app"
SOURCE_REPO = "example/app"


def _make_valid_statement() -> dict:
    """返回完整合法的 SLSA provenance v1 statement。"""
    return {
        "_type": "https://in-toto.io/Statement/v1",
        "predicateType": "https://slsa.dev/provenance/v1",
        "subject": [
            {
                "name": SUBJECT_NAME,
                "digest": {"sha256": IMAGE_SHA},
            }
        ],
        "predicate": {
            "builder": {
                "id": "https://github.com/actions/runner/github-hosted/ubuntu-22.04",
            },
            "buildType": "https://github.com/actions/buildtype/v1",
            "invocation": {
                "configSource": {
                    "uri": f"git+https://github.com/{SOURCE_REPO}",
                    "digest": {"sha1": REAL_COMMIT_SHA},
                },
            },
            "materials": [
                {
                    "uri": f"git+https://github.com/{SOURCE_REPO}",
                    "digest": {
                        "sha1": REAL_COMMIT_SHA,
                        "sha256": REAL_TREE_SHA,
                    },
                },
                {
                    "uri": "file://migration-manifest.json",
                    "digest": {"sha256": MIGRATION_MANIFEST_DIGEST},
                },
            ],
        },
    }


def _make_valid_manifest() -> dict:
    """返回完整合法的 release manifest(使用真实 git commit/tree SHA)。"""
    return {
        "image_digest": IMAGE_SHA,
        "image_ref": IMAGE_REF,
        "source_repository": SOURCE_REPO,
        "source_commit": REAL_COMMIT_SHA,
        "source_tree_sha": REAL_TREE_SHA,
        "migration_manifest_digest": MIGRATION_MANIFEST_DIGEST,
    }


def _check_named(result: dict, name: str) -> dict:
    """从 verify_attestation_semantics 返回结果中按 name 取出单条 check。"""
    for c in result.get("checks", []):
        if c["name"] == name:
            return c
    pytest.fail(f"未找到名为 {name!r} 的 check,实际 checks: "
                f"{[c['name'] for c in result.get('checks', [])]}")


# ════════════════════════════════════════════════════════════════
# A. 旧检查名不再存在(防止回归)
# ════════════════════════════════════════════════════════════════


class TestLegacyCheckNameRemoved:
    """R67 P1-12: 验证旧检查名 predicate_materials_source_tree_sha 不再存在。"""

    def test_legacy_check_name_not_in_checks(self):
        """旧检查名 predicate_materials_source_tree_sha 不应在 checks 列表中出现。"""
        result = _verify_mod.verify_attestation_semantics(
            _make_valid_statement(),
            _make_valid_manifest(),
        )
        check_names = [c["name"] for c in result["checks"]]
        assert "predicate_materials_source_tree_sha" not in check_names, (
            "R67 P1-12: 旧检查名 predicate_materials_source_tree_sha 应已重命名为 "
            "predicate_materials_source_commit"
        )

    def test_legacy_function_name_removed(self):
        """旧函数名 _check_predicate_materials_tree_sha 应已删除。"""
        assert not hasattr(_verify_mod, "_check_predicate_materials_tree_sha"), (
            "R67 P1-12: 旧函数 _check_predicate_materials_tree_sha 应已重命名为 "
            "_check_predicate_materials_source_commit"
        )


# ════════════════════════════════════════════════════════════════
# B. 新检查名 predicate_materials_source_commit 存在并正确工作
# ════════════════════════════════════════════════════════════════


class TestPredicateMaterialsSourceCommitCheck:
    """R67 P1-12: 验证 predicate_materials_source_commit 检查。"""

    def test_check_name_exists(self):
        """检查名 predicate_materials_source_commit 应存在于 checks 列表中。"""
        result = _verify_mod.verify_attestation_semantics(
            _make_valid_statement(),
            _make_valid_manifest(),
        )
        check_names = [c["name"] for c in result["checks"]]
        assert "predicate_materials_source_commit" in check_names

    def test_function_exists(self):
        """函数 _check_predicate_materials_source_commit 应存在。"""
        assert hasattr(_verify_mod, "_check_predicate_materials_source_commit")
        assert callable(_verify_mod._check_predicate_materials_source_commit)

    def test_check_passes_when_materials_match_source_commit(self):
        """materials[] 含 git source,commit digest 与 source_commit 一致 → 通过。"""
        result = _verify_mod.verify_attestation_semantics(
            _make_valid_statement(),
            _make_valid_manifest(),
        )
        check = _check_named(result, "predicate_materials_source_commit")
        assert check["passed"], (
            f"materials 含匹配 source_commit 的 git source 应通过: {check['message']}"
        )

    def test_check_fails_when_materials_missing_git_source(self):
        """materials[] 缺 git source 条目 → 失败。"""
        statement = _make_valid_statement()
        statement["predicate"]["materials"] = [
            {
                "uri": "file://other",
                "digest": {"sha256": "x" * 64},
            }
        ]
        result = _verify_mod.verify_attestation_semantics(
            statement, _make_valid_manifest(),
        )
        check = _check_named(result, "predicate_materials_source_commit")
        assert not check["passed"]

    def test_check_fails_when_commit_digest_mismatch(self):
        """materials[] git source 的 commit digest 与 source_commit 不一致 → 失败。"""
        statement = _make_valid_statement()
        # 修改 materials 中的 sha1 为不匹配的值
        statement["predicate"]["materials"][0]["digest"]["sha1"] = "0" * 40
        # 同时移除 sha256(tree)以防止回退匹配
        del statement["predicate"]["materials"][0]["digest"]["sha256"]
        result = _verify_mod.verify_attestation_semantics(
            statement, _make_valid_manifest(),
        )
        check = _check_named(result, "predicate_materials_source_commit")
        assert not check["passed"]


# ════════════════════════════════════════════════════════════════
# C. 新检查名 predicate_materials_source_tree 存在并正确工作
# ════════════════════════════════════════════════════════════════


class TestPredicateMaterialsSourceTreeCheck:
    """R67 P1-12: 验证 predicate_materials_source_tree 检查(git rev-parse <commit>^{tree})。"""

    def test_check_name_exists(self):
        """检查名 predicate_materials_source_tree 应存在于 checks 列表中。"""
        result = _verify_mod.verify_attestation_semantics(
            _make_valid_statement(),
            _make_valid_manifest(),
        )
        check_names = [c["name"] for c in result["checks"]]
        assert "predicate_materials_source_tree" in check_names

    def test_function_exists(self):
        """函数 _check_predicate_materials_source_tree 应存在。"""
        assert hasattr(_verify_mod, "_check_predicate_materials_source_tree")
        assert callable(_verify_mod._check_predicate_materials_source_tree)

    def test_check_passes_when_tree_sha_matches_git_rev_parse(self):
        """source_commit 派生的 tree SHA 与 source_tree_sha 一致 → 通过。"""
        result = _verify_mod.verify_attestation_semantics(
            _make_valid_statement(),
            _make_valid_manifest(),
        )
        check = _check_named(result, "predicate_materials_source_tree")
        assert check["passed"], (
            f"git rev-parse <commit>^{{tree}} 与 source_tree_sha 应一致: {check['message']}"
        )

    def test_check_fails_when_tree_sha_mismatches(self):
        """source_tree_sha 与 git rev-parse 派生值不一致 → 失败。"""
        manifest = _make_valid_manifest()
        # 故意使用错误的 tree SHA(40 字符 sha1,但与真实 tree 不匹配)
        manifest["source_tree_sha"] = "0" * 40
        result = _verify_mod.verify_attestation_semantics(
            _make_valid_statement(),
            manifest,
        )
        check = _check_named(result, "predicate_materials_source_tree")
        assert not check["passed"]
        assert "不一致" in check["message"]

    def test_check_not_applicable_when_source_commit_missing(self):
        """manifest.source_commit 缺失 → not_applicable。"""
        manifest = _make_valid_manifest()
        del manifest["source_commit"]
        result = _verify_mod.verify_attestation_semantics(
            _make_valid_statement(),
            manifest,
        )
        check = _check_named(result, "predicate_materials_source_tree")
        assert check["status"] == "not_applicable"
        # not_applicable 状态:passed=False 但不阻断 overall_passed
        # (status 字段而非 passed 字段决定是否阻断)

    def test_check_not_applicable_when_source_tree_missing(self):
        """manifest.source_tree_sha 缺失 → not_applicable。"""
        manifest = _make_valid_manifest()
        del manifest["source_tree_sha"]
        result = _verify_mod.verify_attestation_semantics(
            _make_valid_statement(),
            manifest,
        )
        check = _check_named(result, "predicate_materials_source_tree")
        assert check["status"] == "not_applicable"

    def test_check_fails_when_commit_not_in_repo(self):
        """source_commit 在 git 仓库中不存在 → 失败(git rev-parse 失败)。"""
        manifest = _make_valid_manifest()
        # 使用合法 40 字符 SHA-1 格式,但仓库中不存在此 commit
        manifest["source_commit"] = "f" * 40
        result = _verify_mod.verify_attestation_semantics(
            _make_valid_statement(),
            manifest,
        )
        check = _check_named(result, "predicate_materials_source_tree")
        assert not check["passed"]
        assert "git rev-parse" in check["message"]
        assert "失败" in check["message"]


# ════════════════════════════════════════════════════════════════
# D. _resolve_tree_sha_for_commit 辅助函数行为
# ════════════════════════════════════════════════════════════════


class TestResolveTreeShaForCommit:
    """R67 P1-12: 验证 _resolve_tree_sha_for_commit 辅助函数。"""

    def test_function_exists(self):
        """函数 _resolve_tree_sha_for_commit 应存在。"""
        assert hasattr(_verify_mod, "_resolve_tree_sha_for_commit")
        assert callable(_verify_mod._resolve_tree_sha_for_commit)

    def test_returns_tree_sha_for_real_commit(self):
        """对真实 HEAD commit 应返回真实 tree SHA。"""
        derived = _verify_mod._resolve_tree_sha_for_commit(REAL_COMMIT_SHA)
        assert derived is not None
        assert derived == REAL_TREE_SHA

    def test_returns_none_for_nonexistent_commit(self):
        """对仓库中不存在的 commit 应返回 None。"""
        derived = _verify_mod._resolve_tree_sha_for_commit("f" * 40)
        assert derived is None

    def test_returns_none_for_empty_input(self):
        """对空字符串应返回 None。"""
        assert _verify_mod._resolve_tree_sha_for_commit("") is None

    def test_uses_repo_root_parameter(self, tmp_path):
        """repo_root 参数应被使用 — 非 git 目录应返回 None。"""
        # tmp_path 通常不是 git 仓库
        derived = _verify_mod._resolve_tree_sha_for_commit(
            REAL_COMMIT_SHA, repo_root=tmp_path,
        )
        # tmp_path 不是 git 仓库,git rev-parse 失败 → None
        assert derived is None


# ════════════════════════════════════════════════════════════════
# E. 真实 git 仓库场景(端到端验证)
# ════════════════════════════════════════════════════════════════


class TestRealGitRepoEndToEnd:
    """R67 P1-12: 真实 git 仓库端到端验证。"""

    def test_full_scenario_passes_with_real_commit_and_tree(self):
        """完整合法场景(真实 commit + tree SHA)所有检查通过。"""
        result = _verify_mod.verify_attestation_semantics(
            _make_valid_statement(),
            _make_valid_manifest(),
        )
        assert result["overall_passed"], (
            f"完整合法场景应通过,errors: {result['errors']}, warnings: {result['warnings']}"
        )
        # 两个新检查均应通过
        commit_check = _check_named(result, "predicate_materials_source_commit")
        tree_check = _check_named(result, "predicate_materials_source_tree")
        assert commit_check["passed"]
        assert tree_check["passed"]

    def test_tree_check_only_fails_when_tree_mismatched(self):
        """source_commit 检查通过但 source_tree 检查失败(故意改坏 tree SHA)。"""
        manifest = _make_valid_manifest()
        manifest["source_tree_sha"] = "1" * 40  # 错误 tree SHA
        result = _verify_mod.verify_attestation_semantics(
            _make_valid_statement(),
            manifest,
        )
        assert not result["overall_passed"]
        commit_check = _check_named(result, "predicate_materials_source_commit")
        tree_check = _check_named(result, "predicate_materials_source_tree")
        # commit 检查仍通过(materials 含匹配 source_commit 的 git source)
        assert commit_check["passed"]
        # tree 检查失败(tree SHA 不匹配)
        assert not tree_check["passed"]

    def test_two_checks_are_independent(self):
        """两个检查独立运行 — 修改 materials 不影响 tree 检查,反之亦然。"""
        # 场景 1:materials 缺 git source,但 source_commit + source_tree 仍正确
        statement = _make_valid_statement()
        statement["predicate"]["materials"] = [
            {
                "uri": "file://other",
                "digest": {"sha256": "x" * 64},
            }
        ]
        result = _verify_mod.verify_attestation_semantics(
            statement, _make_valid_manifest(),
        )
        commit_check = _check_named(result, "predicate_materials_source_commit")
        tree_check = _check_named(result, "predicate_materials_source_tree")
        # commit 检查失败(materials 缺 git source)
        assert not commit_check["passed"]
        # tree 检查通过(git rev-parse 仍能解析真实 commit)
        assert tree_check["passed"]


# ════════════════════════════════════════════════════════════════
# F. repo_root 参数支持
# ════════════════════════════════════════════════════════════════


class TestRepoRootParameter:
    """R67 P1-12: 验证 verify_attestation_semantics 的 repo_root 参数。"""

    def test_repo_root_parameter_exists(self):
        """verify_attestation_semantics 应接受 repo_root 参数。"""
        import inspect
        sig = inspect.signature(_verify_mod.verify_attestation_semantics)
        assert "repo_root" in sig.parameters, (
            "R67 P1-12: verify_attestation_semantics 应接受 repo_root 参数"
        )

    def test_default_repo_root_uses_script_dir(self):
        """默认 repo_root 应为脚本所在仓库根目录。"""
        # 不传 repo_root,使用默认值 — 应能解析真实 commit
        result = _verify_mod.verify_attestation_semantics(
            _make_valid_statement(),
            _make_valid_manifest(),
        )
        tree_check = _check_named(result, "predicate_materials_source_tree")
        assert tree_check["passed"]

    def test_explicit_repo_root_works(self):
        """显式传入 REPO_ROOT 应与默认行为一致。"""
        result = _verify_mod.verify_attestation_semantics(
            _make_valid_statement(),
            _make_valid_manifest(),
            repo_root=REPO_ROOT,
        )
        tree_check = _check_named(result, "predicate_materials_source_tree")
        assert tree_check["passed"]

    def test_non_git_repo_root_fails_tree_check(self, tmp_path):
        """非 git 仓库的 repo_root 应使 tree 检查失败。"""
        result = _verify_mod.verify_attestation_semantics(
            _make_valid_statement(),
            _make_valid_manifest(),
            repo_root=tmp_path,  # tmp_path 不是 git 仓库
        )
        tree_check = _check_named(result, "predicate_materials_source_tree")
        assert not tree_check["passed"]
