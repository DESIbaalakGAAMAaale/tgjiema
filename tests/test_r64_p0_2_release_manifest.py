"""R64 P0-02: Release artifact manifest(独立于提交树)测试。

审计背景(R64 终审报告 P0-02):
  旧实现问题:
    1. ``database/migrations/migration-manifest.json`` 中 ``release_commit`` /
       ``tree_sha`` 是旧 R63 HEAD,与当前 R64 HEAD 不一致。
    2. manifest **包含在提交树中**,``tree_sha`` 字段指向包含自身的 tree —
       自引用循环(任何 commit 都使旧 manifest 失效,而重生动作又改变 tree,
       无稳态)。
    3. CI 中"运行测试前重生工作区文件"只改 runner 未提交文件,不证明 Docker
       镜像内是同一份已签名 manifest。
    4. Dockerfile ``COPY . .`` 复制提交树版本(无 ``.sig``/``.pem``),运行时
       ``MIGRATION_MANIFEST_VERIFY`` 默认禁用。
    5. ``_verify_manifest_head_tree_binding`` 在 git 不可用时只 warning 不阻断。

  整改:
    1. 生成独立 release artifact manifest(不提交到 git),绑定 source commit、
       source tree、migration file digest 集合和 image digest。
    2. CI 在 docker build 之后生成 release manifest,cosign sign-blob 签名,
       verify-blob 验证后与镜像 attestation 一起上传。
    3. 镜像内 ENV ``MIGRATION_MANIFEST_VERIFY=1`` + ``APP_ENV=production``;
       staging/production 未启用验证拒绝启动。
    4. 非 git 部署环境从 ``RELEASE_SOURCE_COMMIT`` / ``RELEASE_SOURCE_TREE``
       环境变量获取预期值(由部署环境从签名 attestation 注入),未设置则
       raise AppError(fail-closed,不再 warning 后继续)。

测试覆盖矩阵:
  A. generate_release_manifest.py 脚本生成的 canonical 格式正确
  B. _is_manifest_verify_enabled() 在 APP_ENV=production 且未启用时 raise(fail-closed)
  C. _is_manifest_verify_enabled() 在 APP_ENV=local 且 MIGRATION_MANIFEST_VERIFY=0
     时不 raise
  D. _verify_manifest_head_tree_binding 在 git 不可用且 RELEASE_SOURCE_COMMIT 未设置时
     raise(fail-closed)
  E. _verify_manifest_head_tree_binding 在 git 不可用且 RELEASE_SOURCE_COMMIT 设置时
     正常验证
  F. _verify_release_manifest_consistency 一致性 / 不一致场景
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from services.error_codes import AppError, ErrorCodes

# 测试环境兼容 — conftest.py 在收集阶段已注入 config/telegram mock,
# 此处再注入一次以防本文件被单独运行(conftest 未加载场景)
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "database" / "migrations" / "migration-manifest.json"
MIGRATIONS_DIR = REPO_ROOT / "database" / "migrations"
GENERATE_RELEASE_MANIFEST_SCRIPT = REPO_ROOT / "scripts" / "generate_release_manifest.py"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"
DOCKERFILE_PATH = REPO_ROOT / "Dockerfile"


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════


def _load_manifest() -> dict:
    """加载 migration-manifest.json(直接读文件)。"""
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_rev_parse(rev: str) -> str | None:
    """执行 git rev-parse,失败返回 None。"""
    git_bin = shutil.which("git")
    if git_bin is None:
        return None
    result = subprocess.run(
        [git_bin, "rev-parse", rev],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    if len(sha) != 40 or not all(c in "0123456789abcdef" for c in sha.lower()):
        return None
    return sha


# ════════════════════════════════════════════════════════════════
# A. generate_release_manifest.py 脚本生成的 canonical 格式正确
# ════════════════════════════════════════════════════════════════


class TestGenerateReleaseManifestScript:
    """通过命令行调用 scripts/generate_release_manifest.py 验证生成的 JSON。"""

    def test_generates_canonical_release_manifest(self, tmp_path):
        """脚本生成的 release manifest 必须包含所有必填字段且格式正确。"""
        output_path = tmp_path / "release-manifest.json"
        image_digest = "sha256:abcdef0123456789" + "0" * (64 - 16)
        result = subprocess.run(
            [
                sys.executable,
                str(GENERATE_RELEASE_MANIFEST_SCRIPT),
                "--image-digest", image_digest,
                "--output", str(output_path),
                "--quiet",
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, (
            f"generate_release_manifest.py 失败: stdout={result.stdout}, "
            f"stderr={result.stderr}"
        )
        assert output_path.exists(), "release manifest 未写入输出文件"
        rm = json.loads(output_path.read_text(encoding="utf-8"))
        # 必填字段
        assert rm["version"] == 3, f"version 应为 3, 实际 {rm['version']}"
        assert rm["type"] == "release_artifact"
        assert rm["source_commit"] == _git_rev_parse("HEAD")
        assert rm["source_tree"] == _git_rev_parse("HEAD^{tree}")
        assert rm["image_digest"] == image_digest
        assert rm["image_name"] == "ghcr.io/maxiuquan/tgjiema"
        assert rm["tool_version"] == "R64-P0-02"
        assert rm["generated_at"]
        # migrations 集合与 migration-manifest.json 一致
        mm = _load_manifest()
        mm_map = {e["version"]: e["sha256"] for e in mm["migrations"]}
        rm_map = {e["version"]: e["sha256"] for e in rm["migrations"]}
        assert mm_map == rm_map, "release manifest migrations 与 migration-manifest.json 不一致"
        # migration_manifest_digest 必须等于当前 migration-manifest.json 实际 sha256
        assert rm["migration_manifest_digest"] == _file_sha256(MANIFEST_PATH)

    def test_normalizes_image_digest_without_prefix(self, tmp_path):
        """image_digest 不带 sha256: 前缀但为 64 字符 hex 时应自动补前缀。"""
        output_path = tmp_path / "release-manifest.json"
        raw_hex = "a" * 64
        result = subprocess.run(
            [
                sys.executable,
                str(GENERATE_RELEASE_MANIFEST_SCRIPT),
                "--image-digest", raw_hex,
                "--output", str(output_path),
                "--quiet",
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, f"脚本失败: {result.stderr}"
        rm = json.loads(output_path.read_text(encoding="utf-8"))
        assert rm["image_digest"] == f"sha256:{raw_hex}"

    def test_rejects_invalid_image_digest(self, tmp_path):
        """image_digest 非法(既无前缀又不为 64 字符 hex)应退出码 1。"""
        output_path = tmp_path / "release-manifest.json"
        result = subprocess.run(
            [
                sys.executable,
                str(GENERATE_RELEASE_MANIFEST_SCRIPT),
                "--image-digest", "not-a-real-digest",
                "--output", str(output_path),
                "--quiet",
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode != 0, "非法 digest 不应成功"
        assert "image_digest" in result.stderr or "digest" in result.stderr.lower()

    def test_release_manifest_is_deterministic_for_same_commit(self, tmp_path):
        """同一 commit + image_digest 重复生成的 release manifest 必须字节级一致(可复现)。

        R64 P0-02: generated_at 取 commit 时间(非 wall clock),保证可复现。
        """
        out1 = tmp_path / "rm1.json"
        out2 = tmp_path / "rm2.json"
        digest = "sha256:" + "b" * 64
        for out in (out1, out2):
            subprocess.run(
                [
                    sys.executable,
                    str(GENERATE_RELEASE_MANIFEST_SCRIPT),
                    "--image-digest", digest,
                    "--output", str(out),
                    "--quiet",
                ],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
        assert out1.read_bytes() == out2.read_bytes(), (
            "release manifest 在同一 commit + image_digest 下应字节级一致(可复现)"
        )


# ════════════════════════════════════════════════════════════════
# B/C. _is_manifest_verify_enabled() fail-closed 联动
# ════════════════════════════════════════════════════════════════


@pytest.fixture
def migrate_module():
    """加载 database.migrate 模块。"""
    import database.migrate as migrate
    return migrate


class TestIsManifestVerifyEnabledFailClosed:
    """_is_manifest_verify_enabled() 在 staging/production 必须 fail-closed。"""

    def test_raises_in_production_when_verify_unset(
        self, migrate_module, monkeypatch
    ):
        """APP_ENV=production 且 MIGRATION_MANIFEST_VERIFY 未设置 → raise AppError。"""
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.delenv("MIGRATION_MANIFEST_VERIFY", raising=False)
        with pytest.raises(AppError) as exc_info:
            migrate_module._is_manifest_verify_enabled()
        assert exc_info.value.code == ErrorCodes.MIGRATION_MANIFEST_VERIFY_REQUIRED
        assert "production" in str(exc_info.value).lower() or \
               "production" in str(exc_info.value.params.get("app_env", "")).lower()

    def test_raises_in_staging_when_verify_zero(self, migrate_module, monkeypatch):
        """APP_ENV=staging 且 MIGRATION_MANIFEST_VERIFY=0 → raise AppError。"""
        monkeypatch.setenv("APP_ENV", "staging")
        monkeypatch.setenv("MIGRATION_MANIFEST_VERIFY", "0")
        with pytest.raises(AppError) as exc_info:
            migrate_module._is_manifest_verify_enabled()
        assert exc_info.value.code == ErrorCodes.MIGRATION_MANIFEST_VERIFY_REQUIRED

    def test_raises_in_production_case_insensitive(self, migrate_module, monkeypatch):
        """APP_ENV 大小写不敏感(PRODUCTION / Production 都应触发)。"""
        for env_val in ("PRODUCTION", "Production", "Staging", "STAGING"):
            monkeypatch.setenv("APP_ENV", env_val)
            monkeypatch.delenv("MIGRATION_MANIFEST_VERIFY", raising=False)
            with pytest.raises(AppError) as exc_info:
                migrate_module._is_manifest_verify_enabled()
            assert exc_info.value.code == ErrorCodes.MIGRATION_MANIFEST_VERIFY_REQUIRED

    def test_passes_in_production_when_verify_enabled(
        self, migrate_module, monkeypatch
    ):
        """APP_ENV=production 且 MIGRATION_MANIFEST_VERIFY=1 → 通过,返回 True。"""
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("MIGRATION_MANIFEST_VERIFY", "1")
        assert migrate_module._is_manifest_verify_enabled() is True

    def test_passes_in_local_when_verify_zero(self, migrate_module, monkeypatch):
        """APP_ENV=local 且 MIGRATION_MANIFEST_VERIFY=0 → 不 raise,返回 False。

        R64 P0-02: 禁用验证只允许在 APP_ENV=local|test 中存在。
        """
        monkeypatch.setenv("APP_ENV", "local")
        monkeypatch.setenv("MIGRATION_MANIFEST_VERIFY", "0")
        assert migrate_module._is_manifest_verify_enabled() is False

    def test_passes_in_test_when_verify_unset(self, migrate_module, monkeypatch):
        """APP_ENV=test 且 MIGRATION_MANIFEST_VERIFY 未设置 → 不 raise,返回 False。"""
        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.delenv("MIGRATION_MANIFEST_VERIFY", raising=False)
        assert migrate_module._is_manifest_verify_enabled() is False

    def test_passes_when_app_env_unset(self, migrate_module, monkeypatch):
        """APP_ENV 未设置(本地开发)且 MIGRATION_MANIFEST_VERIFY 未设置 → 返回 False。"""
        monkeypatch.delenv("APP_ENV", raising=False)
        monkeypatch.delenv("MIGRATION_MANIFEST_VERIFY", raising=False)
        assert migrate_module._is_manifest_verify_enabled() is False


# ════════════════════════════════════════════════════════════════
# D/E. _verify_manifest_head_tree_binding 非 git 环境 fail-closed
# ════════════════════════════════════════════════════════════════


class TestVerifyManifestHeadTreeBindingNonGit:
    """_verify_manifest_head_tree_binding 在非 git 部署环境的行为。"""

    def test_raises_when_git_unavailable_and_release_source_unset(
        self, migrate_module, monkeypatch
    ):
        """git 不可用且 RELEASE_SOURCE_COMMIT/TREE 未设置 → raise(fail-closed)。

        R64 P0-02 核心整改:不再 warning 后继续,改为 fail-closed。
        """
        data = _load_manifest()
        # 模拟 git 不可用(返回 None)
        monkeypatch.setattr(migrate_module, "_git_rev_parse", lambda rev: None)
        # 清除 RELEASE_SOURCE_COMMIT/TREE 环境变量
        monkeypatch.delenv("RELEASE_SOURCE_COMMIT", raising=False)
        monkeypatch.delenv("RELEASE_SOURCE_TREE", raising=False)
        with pytest.raises(AppError) as exc_info:
            migrate_module._verify_manifest_head_tree_binding(data)
        assert exc_info.value.code == ErrorCodes.MIGRATION_MANIFEST_RELEASE_SOURCE_REQUIRED

    def test_passes_when_release_source_env_matches_manifest(
        self, migrate_module, monkeypatch
    ):
        """git 不可用但 RELEASE_SOURCE_COMMIT/TREE 与 manifest 一致 → 通过。"""
        data = _load_manifest()
        # 模拟 git 不可用
        monkeypatch.setattr(migrate_module, "_git_rev_parse", lambda rev: None)
        # 通过环境变量注入与 manifest 一致的 source commit/tree
        monkeypatch.setenv("RELEASE_SOURCE_COMMIT", data["release_commit"])
        monkeypatch.setenv("RELEASE_SOURCE_TREE", data["tree_sha"])
        # 不抛异常即通过
        migrate_module._verify_manifest_head_tree_binding(data)

    def test_raises_when_release_source_commit_mismatches_manifest(
        self, migrate_module, monkeypatch
    ):
        """git 不可用且 RELEASE_SOURCE_COMMIT 与 manifest 不一致 → raise。"""
        data = _load_manifest()
        monkeypatch.setattr(migrate_module, "_git_rev_parse", lambda rev: None)
        monkeypatch.setenv("RELEASE_SOURCE_COMMIT", "0" * 40)
        monkeypatch.setenv("RELEASE_SOURCE_TREE", data["tree_sha"])
        with pytest.raises(AppError) as exc_info:
            migrate_module._verify_manifest_head_tree_binding(data)
        assert exc_info.value.code == ErrorCodes.MIGRATION_MANIFEST_BINDING_MISMATCH

    def test_raises_when_release_source_tree_mismatches_manifest(
        self, migrate_module, monkeypatch
    ):
        """git 不可用且 RELEASE_SOURCE_TREE 与 manifest 不一致 → raise。"""
        data = _load_manifest()
        monkeypatch.setattr(migrate_module, "_git_rev_parse", lambda rev: None)
        monkeypatch.setenv("RELEASE_SOURCE_COMMIT", data["release_commit"])
        monkeypatch.setenv("RELEASE_SOURCE_TREE", "0" * 40)
        with pytest.raises(AppError) as exc_info:
            migrate_module._verify_manifest_head_tree_binding(data)
        assert exc_info.value.code == ErrorCodes.MIGRATION_MANIFEST_BINDING_MISMATCH

    def test_raises_when_only_release_source_commit_set(
        self, migrate_module, monkeypatch
    ):
        """git 不可用且只设置 RELEASE_SOURCE_COMMIT 未设置 RELEASE_SOURCE_TREE → raise。"""
        data = _load_manifest()
        monkeypatch.setattr(migrate_module, "_git_rev_parse", lambda rev: None)
        monkeypatch.setenv("RELEASE_SOURCE_COMMIT", data["release_commit"])
        monkeypatch.delenv("RELEASE_SOURCE_TREE", raising=False)
        with pytest.raises(AppError) as exc_info:
            migrate_module._verify_manifest_head_tree_binding(data)
        assert exc_info.value.code == ErrorCodes.MIGRATION_MANIFEST_RELEASE_SOURCE_REQUIRED


# ════════════════════════════════════════════════════════════════
# F. _verify_release_manifest_consistency 一致性验证
# ════════════════════════════════════════════════════════════════


class TestVerifyReleaseManifestConsistency:
    """_verify_release_manifest_consistency() 的一致性校验。"""

    def test_skips_when_release_manifest_missing(
        self, migrate_module, monkeypatch, tmp_path
    ):
        """RELEASE_MANIFEST_PATH 指向的文件不存在时应跳过(不 raise)。"""
        data = _load_manifest()
        # 指向不存在的文件
        monkeypatch.setattr(
            migrate_module, "_RELEASE_MANIFEST_PATH",
            tmp_path / "nonexistent-release-manifest.json"
        )
        # 不抛异常即通过(本地开发兼容)
        migrate_module._verify_release_manifest_consistency(data)

    def test_passes_when_release_manifest_consistent(
        self, migrate_module, monkeypatch, tmp_path
    ):
        """release-manifest.json 与 migration-manifest.json 集合/digest 一致 → 通过。"""
        data = _load_manifest()
        # 构造一致的 release manifest
        mm_digest = _file_sha256(MANIFEST_PATH)
        release_manifest = {
            "version": 3,
            "type": "release_artifact",
            "source_commit": data["release_commit"],
            "source_tree": data["tree_sha"],
            "image_digest": "sha256:" + "a" * 64,
            "image_name": "ghcr.io/maxiuquan/tgjiema",
            "migrations": [
                {"version": e["version"], "sha256": e["sha256"]}
                for e in data["migrations"]
            ],
            "migration_manifest_digest": mm_digest,
            "generated_at": "2026-07-18T00:00:00Z",
            "tool_version": "R64-P0-02",
        }
        rm_path = tmp_path / "release-manifest.json"
        rm_path.write_text(json.dumps(release_manifest), encoding="utf-8")
        monkeypatch.setattr(migrate_module, "_RELEASE_MANIFEST_PATH", rm_path)
        # 不抛异常即通过
        migrate_module._verify_release_manifest_consistency(data)

    def test_raises_when_migration_set_differs(
        self, migrate_module, monkeypatch, tmp_path
    ):
        """release-manifest.json.migrations 集合与 migration-manifest.json 不一致 → raise。"""
        data = _load_manifest()
        # 构造不一致的 release manifest:多一个不存在的 migration
        release_manifest = {
            "version": 3,
            "type": "release_artifact",
            "source_commit": data["release_commit"],
            "source_tree": data["tree_sha"],
            "image_digest": "sha256:" + "a" * 64,
            "image_name": "ghcr.io/maxiuquan/tgjiema",
            "migrations": [
                {"version": e["version"], "sha256": e["sha256"]}
                for e in data["migrations"]
            ] + [
                {"version": "999_nonexistent.sql", "sha256": "0" * 64}
            ],
            "migration_manifest_digest": _file_sha256(MANIFEST_PATH),
            "generated_at": "2026-07-18T00:00:00Z",
            "tool_version": "R64-P0-02",
        }
        rm_path = tmp_path / "release-manifest.json"
        rm_path.write_text(json.dumps(release_manifest), encoding="utf-8")
        monkeypatch.setattr(migrate_module, "_RELEASE_MANIFEST_PATH", rm_path)
        with pytest.raises(AppError) as exc_info:
            migrate_module._verify_release_manifest_consistency(data)
        assert exc_info.value.code == ErrorCodes.MIGRATION_MANIFEST_RELEASE_CONSISTENCY

    def test_raises_when_sha256_mismatches(
        self, migrate_module, monkeypatch, tmp_path
    ):
        """release-manifest.json 中某 migration 的 sha256 与 migration-manifest.json 不一致 → raise。"""
        data = _load_manifest()
        # 修改第一个 migration 的 sha256
        modified_migrations = [
            {"version": e["version"], "sha256": e["sha256"]}
            for e in data["migrations"]
        ]
        modified_migrations[0]["sha256"] = "0" * 64
        release_manifest = {
            "version": 3,
            "type": "release_artifact",
            "source_commit": data["release_commit"],
            "source_tree": data["tree_sha"],
            "image_digest": "sha256:" + "a" * 64,
            "image_name": "ghcr.io/maxiuquan/tgjiema",
            "migrations": modified_migrations,
            "migration_manifest_digest": _file_sha256(MANIFEST_PATH),
            "generated_at": "2026-07-18T00:00:00Z",
            "tool_version": "R64-P0-02",
        }
        rm_path = tmp_path / "release-manifest.json"
        rm_path.write_text(json.dumps(release_manifest), encoding="utf-8")
        monkeypatch.setattr(migrate_module, "_RELEASE_MANIFEST_PATH", rm_path)
        with pytest.raises(AppError) as exc_info:
            migrate_module._verify_release_manifest_consistency(data)
        assert exc_info.value.code == ErrorCodes.MIGRATION_MANIFEST_RELEASE_CONSISTENCY

    def test_raises_when_migration_manifest_digest_mismatches(
        self, migrate_module, monkeypatch, tmp_path
    ):
        """release-manifest.json.migration_manifest_digest 与当前 migration-manifest.json 实际 sha256 不一致 → raise。"""
        data = _load_manifest()
        release_manifest = {
            "version": 3,
            "type": "release_artifact",
            "source_commit": data["release_commit"],
            "source_tree": data["tree_sha"],
            "image_digest": "sha256:" + "a" * 64,
            "image_name": "ghcr.io/maxiuquan/tgjiema",
            "migrations": [
                {"version": e["version"], "sha256": e["sha256"]}
                for e in data["migrations"]
            ],
            "migration_manifest_digest": "0" * 64,  # 故意错误
            "generated_at": "2026-07-18T00:00:00Z",
            "tool_version": "R64-P0-02",
        }
        rm_path = tmp_path / "release-manifest.json"
        rm_path.write_text(json.dumps(release_manifest), encoding="utf-8")
        monkeypatch.setattr(migrate_module, "_RELEASE_MANIFEST_PATH", rm_path)
        with pytest.raises(AppError) as exc_info:
            migrate_module._verify_release_manifest_consistency(data)
        assert exc_info.value.code == ErrorCodes.MIGRATION_MANIFEST_RELEASE_CONSISTENCY


# ════════════════════════════════════════════════════════════════
# G. Dockerfile + release-gates.yml 结构校验
# ════════════════════════════════════════════════════════════════


class TestDockerfileAndWorkflowStructure:
    """R64 P0-02 整改的静态结构校验。"""

    def test_dockerfile_sets_migration_manifest_verify_enabled(self):
        """Dockerfile 必须设置 ENV MIGRATION_MANIFEST_VERIFY=1(生产默认启用)。"""
        content = DOCKERFILE_PATH.read_text(encoding="utf-8")
        assert "MIGRATION_MANIFEST_VERIFY=1" in content, (
            "R64 P0-02: Dockerfile 必须设置 ENV MIGRATION_MANIFEST_VERIFY=1"
        )

    def test_dockerfile_sets_app_env_production(self):
        """Dockerfile 必须设置 ENV APP_ENV=production(联动 fail-closed 检查)。"""
        content = DOCKERFILE_PATH.read_text(encoding="utf-8")
        assert "APP_ENV=production" in content, (
            "R64 P0-02: Dockerfile 必须设置 ENV APP_ENV=production"
        )

    def test_workflow_has_regenerate_migration_manifest_step(self):
        """release-gates.yml 必须有 'Regenerate migration manifest' 步骤。"""
        content = WORKFLOW_PATH.read_text(encoding="utf-8")
        assert "Regenerate migration manifest (bind to current HEAD/Tree)" in content, (
            "R64 P0-02: release-gates.yml 必须在 Sign migration manifest 之前 "
            "新增 Regenerate migration manifest 步骤"
        )

    def test_workflow_has_generate_release_manifest_step(self):
        """release-gates.yml 必须有 'Generate release artifact manifest' 步骤。"""
        content = WORKFLOW_PATH.read_text(encoding="utf-8")
        assert "Generate release artifact manifest" in content, (
            "R64 P0-02: release-gates.yml 必须在 docker build 之后 "
            "新增 Generate release artifact manifest 步骤"
        )

    def test_workflow_calls_generate_release_manifest_script(self):
        """release-gates.yml 必须调用 scripts/generate_release_manifest.py。"""
        content = WORKFLOW_PATH.read_text(encoding="utf-8")
        assert "scripts/generate_release_manifest.py" in content, (
            "R64 P0-02: release-gates.yml 必须调用 generate_release_manifest.py 脚本"
        )

    def test_workflow_signs_release_manifest(self):
        """release-gates.yml 必须对 release-manifest.json 执行 cosign sign-blob。"""
        content = WORKFLOW_PATH.read_text(encoding="utf-8")
        assert "Sign release manifest with cosign" in content, (
            "R64 P0-02: release-gates.yml 必须有 Sign release manifest 步骤"
        )
        assert "release-manifest.json.sig" in content
        assert "release-manifest.json.pem" in content

    def test_workflow_verifies_release_manifest_signature(self):
        """release-gates.yml 必须对 release-manifest.json 执行 cosign verify-blob。"""
        content = WORKFLOW_PATH.read_text(encoding="utf-8")
        assert "Verify release manifest signature" in content, (
            "R64 P0-02: release-gates.yml 必须有 Verify release manifest signature 步骤"
        )

    def test_workflow_uploads_release_manifest(self):
        """Upload signed artifacts 步骤必须包含 release-manifest.json 及其签名材料。"""
        content = WORKFLOW_PATH.read_text(encoding="utf-8")
        assert "release-artifacts/release-manifest.json" in content, (
            "R64 P0-02: Upload signed artifacts 必须包含 release-artifacts/release-manifest.json"
        )
        assert "release-manifest.json.pem" in content
        assert "release-manifest.json.sig" in content

    def test_workflow_extracts_release_manifest_identity(self):
        """release-gates.yml 必须有从 Fulcio cert 提取 release manifest identity 的步骤。"""
        content = WORKFLOW_PATH.read_text(encoding="utf-8")
        assert "extract_release_manifest_identity" in content, (
            "R64 P0-02: release-gates.yml 必须有 extract_release_manifest_identity 步骤"
        )


# ════════════════════════════════════════════════════════════════
# H. migration-manifest.json 已绑定到当前 HEAD(回归校验)
# ════════════════════════════════════════════════════════════════


class TestMigrationManifestBoundToCurrentHead:
    """重生后的 migration-manifest.json 必须严格绑定到当前 R64 HEAD/Tree。"""

    def test_release_commit_matches_current_head(self):
        """manifest.release_commit 必须等于当前 git HEAD。"""
        manifest = _load_manifest()
        head_sha = _git_rev_parse("HEAD")
        if head_sha is None:
            pytest.skip("git 不可用")
        assert manifest["release_commit"] == head_sha, (
            f"manifest release_commit ({manifest['release_commit'][:12]}...) "
            f"与当前 HEAD ({head_sha[:12]}...) 不一致 — R64 P0-02: manifest 必须重生"
        )

    def test_tree_sha_matches_current_head_tree(self):
        """manifest.tree_sha 必须等于当前 git HEAD^{tree}。"""
        manifest = _load_manifest()
        tree_sha = _git_rev_parse("HEAD^{tree}")
        if tree_sha is None:
            pytest.skip("git 不可用")
        assert manifest["tree_sha"] == tree_sha, (
            f"manifest tree_sha ({manifest['tree_sha'][:12]}...) "
            f"与当前 HEAD Tree ({tree_sha[:12]}...) 不一致 — R64 P0-02: manifest 必须重生"
        )

    def test_release_commit_not_stale_r63_value(self):
        """manifest.release_commit 不应是旧 R63 值 3513de9...。"""
        manifest = _load_manifest()
        stale_commit = "3513de91224844c83aa3370a8267ce7a6590ed5e"
        assert manifest["release_commit"] != stale_commit, (
            "R64 P0-02: manifest release_commit 仍是旧 R63 值 3513de9... — 必须重生为当前 HEAD"
        )

    def test_tree_sha_not_stale_r63_value(self):
        """manifest.tree_sha 不应是旧 R63 值 bd8337d...。"""
        manifest = _load_manifest()
        stale_tree = "bd8337d0f6e710f1dc734c1163eaa40b239924de"
        assert manifest["tree_sha"] != stale_tree, (
            "R64 P0-02: manifest tree_sha 仍是旧 R63 值 bd8337d... — 必须重生为当前 Tree"
        )
