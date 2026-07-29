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
    5. ``_verify_release_manifest_consistency`` 在 git 不可用时只 warning 不阻断(R66 P0-01 后改为 fail-closed)。

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
  D. _verify_release_manifest_consistency 在 git 不可用且 RELEASE_SOURCE_COMMIT 未设置时
     raise(fail-closed) — R66 P0-01 后从 catalog HEAD/Tree 绑定改为 release-manifest.json 绑定
  E. _verify_release_manifest_consistency 在 git 不可用且 RELEASE_SOURCE_COMMIT 设置时
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
    # RC58 fix: 规范化 CRLF→LF,与 database.migrate._compute_sha256 一致(跨平台)
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


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
# D/E. _verify_release_manifest_consistency HEAD/Tree 绑定非 git 环境 fail-closed
#     (R66 P0-01: HEAD 绑定从 catalog 移到 release-manifest.json)
# ════════════════════════════════════════════════════════════════


class TestVerifyReleaseManifestHeadTreeBindingNonGit:
    """_verify_release_manifest_consistency 在非 git 部署环境的行为 (R66 P0-01)。

    R66 P0-01: HEAD/Tree 绑定从 migration-manifest.json(catalog)移到
    release-manifest.json(CI 产物)。catalog 不再保存 release_commit/tree_sha。
    """

    def test_raises_when_git_unavailable_and_release_source_unset(
        self, migrate_module, monkeypatch, tmp_path
    ):
        """git 不可用且 RELEASE_SOURCE_COMMIT/TREE 未设置 → raise(fail-closed)。

        R64 P0-02 / R66 P0-01 核心整改:不再 warning 后继续,改为 fail-closed。
        """
        data = _load_manifest()
        # 构造 release-manifest.json(含 source_commit/source_tree)
        release_manifest = {
            "version": 3,
            "type": "release_artifact",
            "source_commit": "a" * 40,
            "source_tree": "b" * 40,
            "image_digest": "sha256:" + "a" * 64,
            "image_name": "ghcr.io/maxiuquan/tgjiema",
            "migrations": [
                {"version": e["version"], "sha256": e["sha256"]}
                for e in data["migrations"]
            ],
            "migration_manifest_digest": _file_sha256(MANIFEST_PATH),
            "generated_at": "2026-07-18T00:00:00Z",
            "tool_version": "R64-P0-02",
        }
        rm_path = tmp_path / "release-manifest.json"
        rm_path.write_text(json.dumps(release_manifest), encoding="utf-8")
        monkeypatch.setattr(migrate_module, "_RELEASE_MANIFEST_PATH", rm_path)
        # 模拟 git 不可用(返回 None)
        monkeypatch.setattr(migrate_module, "_git_rev_parse", lambda rev: None)
        # 清除 RELEASE_SOURCE_COMMIT/TREE 环境变量
        monkeypatch.delenv("RELEASE_SOURCE_COMMIT", raising=False)
        monkeypatch.delenv("RELEASE_SOURCE_TREE", raising=False)
        with pytest.raises(AppError) as exc_info:
            migrate_module._verify_release_manifest_consistency(data)
        assert exc_info.value.code == ErrorCodes.MIGRATION_MANIFEST_RELEASE_SOURCE_REQUIRED

    def test_passes_when_release_source_env_matches_release_manifest(
        self, migrate_module, monkeypatch, tmp_path
    ):
        """git 不可用但 RELEASE_SOURCE_COMMIT/TREE 与 release-manifest.json 一致 → 通过。"""
        data = _load_manifest()
        rm_commit = "a" * 40
        rm_tree = "b" * 40
        release_manifest = {
            "version": 3,
            "type": "release_artifact",
            "source_commit": rm_commit,
            "source_tree": rm_tree,
            "image_digest": "sha256:" + "a" * 64,
            "image_name": "ghcr.io/maxiuquan/tgjiema",
            "migrations": [
                {"version": e["version"], "sha256": e["sha256"]}
                for e in data["migrations"]
            ],
            "migration_manifest_digest": _file_sha256(MANIFEST_PATH),
            "generated_at": "2026-07-18T00:00:00Z",
            "tool_version": "R64-P0-02",
        }
        rm_path = tmp_path / "release-manifest.json"
        rm_path.write_text(json.dumps(release_manifest), encoding="utf-8")
        monkeypatch.setattr(migrate_module, "_RELEASE_MANIFEST_PATH", rm_path)
        monkeypatch.setattr(migrate_module, "_git_rev_parse", lambda rev: None)
        monkeypatch.setenv("RELEASE_SOURCE_COMMIT", rm_commit)
        monkeypatch.setenv("RELEASE_SOURCE_TREE", rm_tree)
        migrate_module._verify_release_manifest_consistency(data)

    def test_raises_when_release_source_commit_mismatches_release_manifest(
        self, migrate_module, monkeypatch, tmp_path
    ):
        """git 不可用且 RELEASE_SOURCE_COMMIT 与 release-manifest 不一致 → raise。"""
        data = _load_manifest()
        release_manifest = {
            "version": 3,
            "type": "release_artifact",
            "source_commit": "a" * 40,
            "source_tree": "b" * 40,
            "image_digest": "sha256:" + "a" * 64,
            "image_name": "ghcr.io/maxiuquan/tgjiema",
            "migrations": [
                {"version": e["version"], "sha256": e["sha256"]}
                for e in data["migrations"]
            ],
            "migration_manifest_digest": _file_sha256(MANIFEST_PATH),
            "generated_at": "2026-07-18T00:00:00Z",
            "tool_version": "R64-P0-02",
        }
        rm_path = tmp_path / "release-manifest.json"
        rm_path.write_text(json.dumps(release_manifest), encoding="utf-8")
        monkeypatch.setattr(migrate_module, "_RELEASE_MANIFEST_PATH", rm_path)
        monkeypatch.setattr(migrate_module, "_git_rev_parse", lambda rev: None)
        monkeypatch.setenv("RELEASE_SOURCE_COMMIT", "0" * 40)
        monkeypatch.setenv("RELEASE_SOURCE_TREE", "b" * 40)
        with pytest.raises(AppError) as exc_info:
            migrate_module._verify_release_manifest_consistency(data)
        assert exc_info.value.code == ErrorCodes.MIGRATION_MANIFEST_BINDING_MISMATCH

    def test_raises_when_release_source_tree_mismatches_release_manifest(
        self, migrate_module, monkeypatch, tmp_path
    ):
        """git 不可用且 RELEASE_SOURCE_TREE 与 release-manifest 不一致 → raise。"""
        data = _load_manifest()
        release_manifest = {
            "version": 3,
            "type": "release_artifact",
            "source_commit": "a" * 40,
            "source_tree": "b" * 40,
            "image_digest": "sha256:" + "a" * 64,
            "image_name": "ghcr.io/maxiuquan/tgjiema",
            "migrations": [
                {"version": e["version"], "sha256": e["sha256"]}
                for e in data["migrations"]
            ],
            "migration_manifest_digest": _file_sha256(MANIFEST_PATH),
            "generated_at": "2026-07-18T00:00:00Z",
            "tool_version": "R64-P0-02",
        }
        rm_path = tmp_path / "release-manifest.json"
        rm_path.write_text(json.dumps(release_manifest), encoding="utf-8")
        monkeypatch.setattr(migrate_module, "_RELEASE_MANIFEST_PATH", rm_path)
        monkeypatch.setattr(migrate_module, "_git_rev_parse", lambda rev: None)
        monkeypatch.setenv("RELEASE_SOURCE_COMMIT", "a" * 40)
        monkeypatch.setenv("RELEASE_SOURCE_TREE", "0" * 40)
        with pytest.raises(AppError) as exc_info:
            migrate_module._verify_release_manifest_consistency(data)
        assert exc_info.value.code == ErrorCodes.MIGRATION_MANIFEST_BINDING_MISMATCH

    def test_raises_when_only_release_source_commit_set(
        self, migrate_module, monkeypatch, tmp_path
    ):
        """git 不可用且只设置 RELEASE_SOURCE_COMMIT 未设置 RELEASE_SOURCE_TREE → raise。"""
        data = _load_manifest()
        release_manifest = {
            "version": 3,
            "type": "release_artifact",
            "source_commit": "a" * 40,
            "source_tree": "b" * 40,
            "image_digest": "sha256:" + "a" * 64,
            "image_name": "ghcr.io/maxiuquan/tgjiema",
            "migrations": [
                {"version": e["version"], "sha256": e["sha256"]}
                for e in data["migrations"]
            ],
            "migration_manifest_digest": _file_sha256(MANIFEST_PATH),
            "generated_at": "2026-07-18T00:00:00Z",
            "tool_version": "R64-P0-02",
        }
        rm_path = tmp_path / "release-manifest.json"
        rm_path.write_text(json.dumps(release_manifest), encoding="utf-8")
        monkeypatch.setattr(migrate_module, "_RELEASE_MANIFEST_PATH", rm_path)
        monkeypatch.setattr(migrate_module, "_git_rev_parse", lambda rev: None)
        monkeypatch.setenv("RELEASE_SOURCE_COMMIT", "a" * 40)
        monkeypatch.delenv("RELEASE_SOURCE_TREE", raising=False)
        with pytest.raises(AppError) as exc_info:
            migrate_module._verify_release_manifest_consistency(data)
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
        # R66 P0-01: source_commit/source_tree 必须与 git HEAD/Tree 一致
        # 用 monkeypatch 固定 _git_rev_parse 返回值,使测试不依赖真实 git 状态
        test_commit = "a" * 40
        test_tree = "b" * 40
        monkeypatch.setattr(
            migrate_module, "_git_rev_parse",
            lambda rev: test_commit if rev == "HEAD" else test_tree
        )
        # 构造一致的 release manifest
        mm_digest = _file_sha256(MANIFEST_PATH)
        release_manifest = {
            "version": 3,
            "type": "release_artifact",
            "source_commit": test_commit,
            "source_tree": test_tree,
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
        # R66 P0-01: source_commit/source_tree 必须与 git HEAD/Tree 一致
        test_commit = "a" * 40
        test_tree = "b" * 40
        monkeypatch.setattr(
            migrate_module, "_git_rev_parse",
            lambda rev: test_commit if rev == "HEAD" else test_tree
        )
        # 构造不一致的 release manifest:多一个不存在的 migration
        release_manifest = {
            "version": 3,
            "type": "release_artifact",
            "source_commit": test_commit,
            "source_tree": test_tree,
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
        # R66 P0-01: source_commit/source_tree 必须与 git HEAD/Tree 一致
        test_commit = "a" * 40
        test_tree = "b" * 40
        monkeypatch.setattr(
            migrate_module, "_git_rev_parse",
            lambda rev: test_commit if rev == "HEAD" else test_tree
        )
        # 修改第一个 migration 的 sha256
        modified_migrations = [
            {"version": e["version"], "sha256": e["sha256"]}
            for e in data["migrations"]
        ]
        modified_migrations[0]["sha256"] = "0" * 64
        release_manifest = {
            "version": 3,
            "type": "release_artifact",
            "source_commit": test_commit,
            "source_tree": test_tree,
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
        # R66 P0-01: source_commit/source_tree 必须与 git HEAD/Tree 一致
        test_commit = "a" * 40
        test_tree = "b" * 40
        monkeypatch.setattr(
            migrate_module, "_git_rev_parse",
            lambda rev: test_commit if rev == "HEAD" else test_tree
        )
        release_manifest = {
            "version": 3,
            "type": "release_artifact",
            "source_commit": test_commit,
            "source_tree": test_tree,
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
        """release-gates.yml 必须有 'Regenerate migration manifest' 步骤。

        R66 P0-01: 步骤名从 'Regenerate migration manifest (bind to current HEAD/Tree)'
        改为 'Regenerate migration manifest (catalog-only, no HEAD/Tree binding)'
        (catalog-only 模型,不再绑定 HEAD/Tree)。
        R66 P0-02: 步骤从 sign-image job(build 之后)移到 docker-build job
        (build 之前),确保镜像内 catalog 与签名对象一致。步骤名改为
        'Regenerate migration manifest (catalog-only, before docker build)'。
        """
        content = WORKFLOW_PATH.read_text(encoding="utf-8")
        # R66 P0-02: 新步骤名(在 docker-build job 中, build 之前)
        assert "Regenerate migration manifest (catalog-only, before docker build)" in content, (
            "R66 P0-02: release-gates.yml 必须在 docker-build job 的 build 步骤之前 "
            "新增 Regenerate migration manifest (catalog-only, before docker build) 步骤"
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
# G2. R66 P0-02: build-once 后才重生 manifest 整改校验
# ════════════════════════════════════════════════════════════════


class TestR66P0_02BuildOnceManifestFrozen:
    """R66 P0-02: build-once 后才重生 manifest, 签名对象不在已构建镜像内 — 整改校验。

    审计背景(R66 P0-02):
      旧实现: sign-image job 在 docker-build 完成后才执行 'Regenerate migration
      manifest' 步骤, 仅修改 runner 工作区文件, 已 push 的 OCI image 仍含
      checkout 时的旧 manifest。签名对象(新 manifest)与镜像内实际内容
      (旧 manifest)不一致 — 签名材料无法证明镜像内 catalog 完整性。

    整改:
      1. catalog 重生移到 docker-build job (build 之前), 确保镜像内 catalog
         与 runner 工作区一致。
      2. docker-build job 输出 catalog_digest (build 前冻结的 catalog sha256)。
      3. sign-image job 不再重生 catalog (禁止 build 后修改任何声称位于镜像内
         的发布输入)。
      4. sign-image job 新增 'Verify image catalog digest matches source' 步骤,
         从镜像内读取 /app/database/migrations/migration-manifest.json 计算
         sha256, 与 docker-build 输出的 catalog_digest + runner 工作区 source
         catalog sha256 双重比对。
    """

    @pytest.fixture
    def workflow_yaml(self):
        """解析 release-gates.yml 为 dict。"""
        import yaml
        return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))

    @pytest.fixture
    def workflow_content(self):
        """读取 release-gates.yml 完整内容。"""
        return WORKFLOW_PATH.read_text(encoding="utf-8")

    def test_docker_build_has_regenerate_step_before_build(self, workflow_yaml):
        """docker-build job 必须在 build 步骤之前重生 catalog。

        R66 P0-02: 'Regenerate migration manifest (catalog-only, before docker build)'
        步骤必须出现在 'Build and push OCI image (build once)' 步骤之前。
        """
        docker_build_steps = workflow_yaml["jobs"]["docker-build"]["steps"]
        regenerate_idx = None
        build_idx = None
        for i, step in enumerate(docker_build_steps):
            name = step.get("name", "")
            if "Regenerate migration manifest" in name and "before docker build" in name:
                regenerate_idx = i
            if "Build and push OCI image" in name:
                build_idx = i
        assert regenerate_idx is not None, (
            "R66 P0-02: docker-build job 必须有 'Regenerate migration manifest "
            "(catalog-only, before docker build)' 步骤"
        )
        assert build_idx is not None, (
            "R66 P0-02: docker-build job 必须有 'Build and push OCI image' 步骤"
        )
        assert regenerate_idx < build_idx, (
            f"R66 P0-02: Regenerate 步骤 (index={regenerate_idx}) 必须在 "
            f"Build 步骤 (index={build_idx}) 之前 — manifest 在 docker build "
            f"前已冻结, build 后禁止修改"
        )

    def test_docker_build_outputs_catalog_digest(self, workflow_yaml):
        """docker-build job 必须输出 catalog_digest (build 前冻结的 catalog sha256)。

        R66 P0-02: catalog_digest 作为 sign-image job 校验镜像内 catalog 的 baseline。
        """
        docker_build_outputs = workflow_yaml["jobs"]["docker-build"].get("outputs", {})
        assert "catalog_digest" in docker_build_outputs, (
            "R66 P0-02: docker-build job 必须输出 catalog_digest "
            "(build 前冻结的 catalog sha256, 供 sign-image job 校验镜像内 catalog)"
        )

    def test_load_only_build_disables_attestation_manifest_lists(self, workflow_yaml):
        """Master load-only build 不得向 docker exporter 输出 attestation manifest list。

        BuildKit 的 docker exporter 无法加载 provenance/SBOM 产生的 manifest list。
        Attestations 仅在 should_push=true 的 PR/RC 发布路径启用；master/main 仍执行
        完整 OCI build + load 验证，但不伪造或丢弃发布证据。
        """
        build_step = next(
            step
            for step in workflow_yaml["jobs"]["docker-build"]["steps"]
            if "Build and push OCI image" in step.get("name", "")
        )
        with_config = build_step["with"]
        should_push = "steps.meta.outputs.should_push == 'true'"
        assert should_push in str(with_config["push"])
        assert "steps.meta.outputs.should_push == 'false'" in str(with_config["load"])
        assert should_push in str(with_config["provenance"])
        assert should_push in str(with_config["sbom"])

    def test_release_summary_accepts_only_expected_master_load_only_skips(
        self,
        workflow_yaml,
    ):
        """Master build-validation 的汇总语义必须匹配 should_push job 合同。

        Master/main 不推送镜像，因此 RepoDigest 消费者和 runtime-config 绑定会
        按 job-level if 精确跳过。release-summary 只能允许这些预期 skipped，仍须
        阻断 failure/cancelled；PR/RC 发布路径仍必须要求这些 job 成功。
        """
        summary_script = workflow_yaml["jobs"]["release-summary"]["steps"][0]["run"]
        for job_name in (
            "oci-allowlist-verify",
            "validate-oci-rootfs",
            "runtime-smoke-compose",
        ):
            assert f'[ "${{job}}" = "{job_name}" ]' in summary_script
        assert 'if [ "${RELEASE_TARGET}" = "true" ]; then' in summary_script
        assert (
            '[ "${conclusion}" != "success" ] && '
            '[ "${conclusion}" != "skipped" ]'
        ) in summary_script
        bind_block = summary_script.split(
            'elif [ "${job}" = "bind-runtime-config" ]; then',
            maxsplit=1,
        )[1].split("else", maxsplit=1)[0]
        assert '"${RELEASE_TARGET}" = "true"' not in bind_block
        assert '"${RC_TAG}" = "true"' in bind_block
        assert '"${PRODUCTION_TAG}" = "true"' in bind_block

    def test_sign_image_does_not_regenerate_manifest(self, workflow_yaml):
        """sign-image job 不得包含 'Regenerate migration manifest' 步骤。

        R66 P0-02: 禁止 build 后修改任何声称位于镜像内的发布输入。
        catalog 重生已移到 docker-build job (build 之前), sign-image job
        只能校验镜像内 catalog digest, 不得重生 catalog。
        """
        sign_image_steps = workflow_yaml["jobs"]["sign-image"]["steps"]
        regenerate_steps = [
            s for s in sign_image_steps
            if "Regenerate migration manifest" in s.get("name", "")
        ]
        assert not regenerate_steps, (
            "R66 P0-02: sign-image job 不得包含 'Regenerate migration manifest' 步骤 "
            "(禁止 build 后修改任何声称位于镜像内的发布输入) — "
            f"发现 {len(regenerate_steps)} 个: {[s.get('name') for s in regenerate_steps]}"
        )

    def test_sign_image_does_not_call_generate_migration_manifest_script(
        self, workflow_yaml
    ):
        """sign-image job 不得调用 scripts/generate_migration_manifest.py。

        R66 P0-02: sign-image job 禁止重生 catalog (build 后修改声称位于镜像内的
        发布输入)。generate_migration_manifest.py 只能在 docker-build job
        (build 之前) 或 ci.yml (测试之前) 中调用。
        """
        sign_image_steps = workflow_yaml["jobs"]["sign-image"]["steps"]
        for step in sign_image_steps:
            run_script = step.get("run", "")
            assert "generate_migration_manifest.py" not in run_script, (
                f"R66 P0-02: sign-image job 的步骤 '{step.get('name', '?')}' "
                f"不得调用 generate_migration_manifest.py (禁止 build 后重生 catalog)"
            )

    def test_sign_image_has_image_catalog_digest_verification_step(self, workflow_yaml):
        """sign-image job 必须有 'Verify image catalog digest matches source' 步骤。

        R66 P0-02: 镜像内 catalog digest 校验(image catalog digest verification)。
        从已 push 的 OCI image 中读取 /app/database/migrations/migration-manifest.json,
        计算 sha256, 与 docker-build 输出的 catalog_digest + runner 工作区 source
        catalog sha256 双重比对。
        """
        sign_image_steps = workflow_yaml["jobs"]["sign-image"]["steps"]
        verify_step = next(
            (s for s in sign_image_steps
             if "Verify image catalog digest" in s.get("name", "")),
            None,
        )
        assert verify_step is not None, (
            "R66 P0-02: sign-image job 必须有 'Verify image catalog digest matches source' 步骤"
        )

    def test_image_catalog_digest_verification_runs_image(self, workflow_yaml):
        """Verify image catalog digest 步骤必须 docker run 镜像读取 catalog 文件。

        R66 P0-02: 通过 docker run --rm --entrypoint cat <image> 读取镜像内
        /app/database/migrations/migration-manifest.json, 确保校验的是镜像内
        实际内容(而非 runner 工作区内容)。
        """
        sign_image_steps = workflow_yaml["jobs"]["sign-image"]["steps"]
        verify_step = next(
            (s for s in sign_image_steps
             if "Verify image catalog digest" in s.get("name", "")),
            None,
        )
        assert verify_step is not None, (
            "R66 P0-02: sign-image job 必须有 'Verify image catalog digest' 步骤"
        )
        run_script = verify_step.get("run", "")
        assert "docker run" in run_script, (
            "R66 P0-02: Verify image catalog digest 步骤必须 docker run 镜像读取 catalog"
        )
        assert "migration-manifest.json" in run_script, (
            "R66 P0-02: Verify image catalog digest 步骤必须读取 /app/database/migrations/"
            "migration-manifest.json"
        )

    def test_image_catalog_digest_verification_uses_docker_build_output(
        self, workflow_yaml
    ):
        """Verify image catalog digest 步骤必须引用 docker-build 输出的 catalog_digest。

        R66 P0-02: catalog_digest 是 build 前冻结的 catalog sha256, 作为校验
        镜像内 catalog 的 baseline (禁止 build 后修改)。
        """
        sign_image_steps = workflow_yaml["jobs"]["sign-image"]["steps"]
        verify_step = next(
            (s for s in sign_image_steps
             if "Verify image catalog digest" in s.get("name", "")),
            None,
        )
        assert verify_step is not None
        run_script = verify_step.get("run", "")
        assert "needs.docker-build.outputs.catalog_digest" in run_script, (
            "R66 P0-02: Verify image catalog digest 步骤必须引用 "
            "needs.docker-build.outputs.catalog_digest (build 前冻结的 baseline)"
        )

    def test_image_catalog_digest_verification_before_sign(self, workflow_yaml):
        """Verify image catalog digest 步骤必须在 Sign OCI image 步骤之前。

        R66 P0-02: 先校验镜像内 catalog digest, 再签名镜像 — 若镜像内 catalog
        与 baseline 不一致, 拒绝签名 (fail-fast, 不浪费 cosign sign 调用)。
        """
        sign_image_steps = workflow_yaml["jobs"]["sign-image"]["steps"]
        verify_idx = None
        sign_idx = None
        for i, step in enumerate(sign_image_steps):
            name = step.get("name", "")
            if "Verify image catalog digest" in name:
                verify_idx = i
            if "Sign OCI image" in name:
                sign_idx = i
        assert verify_idx is not None, (
            "R66 P0-02: sign-image job 必须有 'Verify image catalog digest' 步骤"
        )
        assert sign_idx is not None, (
            "R66 P0-02: sign-image job 必须有 'Sign OCI image' 步骤"
        )
        assert verify_idx < sign_idx, (
            f"R66 P0-02: Verify image catalog digest (index={verify_idx}) "
            f"必须在 Sign OCI image (index={sign_idx}) 之前 — 先校验镜像内 catalog, 再签名"
        )

    def test_workflow_has_r66_p0_02_comment(self, workflow_content):
        """release-gates.yml 必须包含 R66 P0-02 整改说明注释。

        R66 P0-02: 'manifest 在 docker build 前已冻结, build 后禁止修改'
        注释必须出现在 workflow 中, 解释为何 catalog 重生移到 build 之前。
        """
        assert "R66 P0-02" in workflow_content, (
            "R66 P0-02: release-gates.yml 必须包含 R66 P0-02 整改说明注释"
        )
        assert "manifest 在 docker build 前已冻结" in workflow_content, (
            "R66 P0-02: release-gates.yml 必须包含 'manifest 在 docker build 前已冻结' 注释"
        )
        assert "build 后禁止修改" in workflow_content, (
            "R66 P0-02: release-gates.yml 必须包含 'build 后禁止修改' 注释"
        )


# ════════════════════════════════════════════════════════════════
# H. migration-manifest.json 已绑定到当前 HEAD(回归校验)
# ════════════════════════════════════════════════════════════════


class TestCatalogDoesNotBindToHead:
    """R66 P0-01: catalog(migration-manifest.json)不再绑定到 HEAD/Tree。

    R66 P0-01 整改:catalog 不再包含 release_commit/tree_sha 字段。
    HEAD/Tree 绑定由 release-manifest.json(CI 产物,不提交 git)承担。
    """

    def test_catalog_does_not_contain_release_commit(self):
        """R66 P0-01: catalog 不得包含 release_commit 字段(自引用循环根因)。"""
        manifest = _load_manifest()
        assert "release_commit" not in manifest, (
            "R66 P0-01: catalog 禁止包含 release_commit 字段 "
            "(自引用循环根因 — HEAD 绑定移至 release-manifest.json)"
        )

    def test_catalog_does_not_contain_tree_sha(self):
        """R66 P0-01: catalog 不得包含 tree_sha 字段(自引用循环根因)。"""
        manifest = _load_manifest()
        assert "tree_sha" not in manifest, (
            "R66 P0-01: catalog 禁止包含 tree_sha 字段 "
            "(自引用循环根因 — Tree 绑定移至 release-manifest.json)"
        )

    def test_catalog_version_is_at_least_3(self):
        """R66 P0-01: catalog version 必须 >= 3(catalog-only 模型版本)。"""
        manifest = _load_manifest()
        assert manifest.get("version", 0) >= 3, (
            f"R66 P0-01: catalog version 必须 >= 3(catalog-only 模型),"
            f"实际为 {manifest.get('version')}"
        )
