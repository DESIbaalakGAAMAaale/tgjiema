"""R63 P0-01 / P0-04: Release supply-chain + migration manifest 一致性测试。

审计背景(R63 终审报告):
  P0-01 — Release 供应链失败:
    keyless image signing 成功, 但 image signature verification 失败,
    之后 provenance / source signature / migration manifest signature /
    manifest binding / artifact upload 全部被 skipped, attestation 未发布。
    根因: cosign verify 用 ``github.ref`` 拼接 certificate-identity,
          与实际 Fulcio cert SAN 存在细微差异导致 verify 失败。

    整改:
      - 从实际 Fulcio cert 提取 SAN (source of truth) 做 verify
      - sign-image / publish-attestation 复用同一 cert (跨 job artifact)
      - 每个 required job 只接受 success, skipped/failure 一律阻断
      - verification statement 作为后续强依赖输入

  P0-04 — migration manifest 与当前发布不一致:
    R66 P0-01 整改后:catalog 不再绑定 commit/tree(catalog-only 模型);
    HEAD 绑定由 release-artifacts/release-manifest.json 承担。
    004 migration 已存在但 manifest 只列 001–003;
    manifest 声称存在 .sig/.pem 但树中未见;
    ``_load_migration_manifest()`` 只解析 JSON 没有验签。

    整改(R66 P0-01 后):
      - catalog 只保存 migration 集合/顺序/sha256/DDL version/rollback strategy
      - catalog 不再保存 release_commit/tree_sha(自引用循环根因)
      - HEAD 绑定由 release-manifest.json (CI 产物)的 source_commit/source_tree 承担
      - 004 migration 加入 manifest 并记录 sha256
      - ``_load_migration_manifest()`` 增加 catalog-only 验证 + 磁盘集合一致性 +
        release-manifest 一致性 + cosign 验签
      - 验签 fail-closed, 本地通过 ``MIGRATION_MANIFEST_VERIFY=0`` 跳过(warning)

测试覆盖矩阵:
  A. manifest 一致性 (P0-04) — catalog-only 模型/004 条目/sha256 全量校验
  B. migrate.py 验签逻辑 (P0-04) — _is_manifest_verify_enabled / _verify_catalog_only_model
  C. release-gates.yml 结构 (P0-01) — cert 提取步骤 / 精确 identity / 上传 image-signing-cert.pem
  D. 跨步骤依赖 (P0-01) — sign-image / publish-attestation 使用同一 identity
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
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════


class _MockShutilWithCosign:
    """模拟 shutil 模块,但 which('cosign') 返回 fake path。

    用于测试 _verify_manifest_cosign_signature 时绕过"cosign 不在 PATH"检查,
    使代码进入后续的 identity_prefix / verify-blob 检查路径。

    注意: 不用 monkeypatch.setattr('shutil.which', lambda cmd: ...) 形式,
    因为 lambda 内部调用 shutil.which 会无限递归(lambda 替换了原函数)。
    改用完整 mock 对象,保留其他 shutil 方法原样委托。
    """

    def __init__(self):
        self._real_shutil = shutil

    def which(self, cmd: str):
        """cosign 返回 fake path,其他命令委托给真实 shutil.which。"""
        if cmd == "cosign":
            return "/usr/local/bin/cosign"  # fake path
        return self._real_shutil.which(cmd)

    def __getattr__(self, name):
        # 其他 shutil 属性/方法委托给真实 shutil(如 rmtree 等)
        return getattr(self._real_shutil, name)


def _git_rev_parse(rev: str) -> str | None:
    """执行 git rev-parse,失败返回 None(与 migrate.py 的 _git_rev_parse 行为一致)。"""
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


def _load_manifest() -> dict:
    """加载 manifest JSON(直接读文件,不走 migrate.py 的验证逻辑)。"""
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _file_sha256(path: Path) -> str:
    """计算文件内容的 sha256(十六进制小写)。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ════════════════════════════════════════════════════════════════
# A. manifest 一致性 (P0-04)
# ════════════════════════════════════════════════════════════════


class TestManifestConsistency:
    """manifest 与当前 release HEAD/Tree 的一致性校验。

    R66 P0-01: catalog 不再绑定 HEAD/Tree(catalog-only 模型)。
    HEAD 绑定移到 release-artifacts/release-manifest.json(CI 产物)。
    """

    def test_catalog_does_not_contain_release_commit(self):
        """R66 P0-01: catalog 不得包含 release_commit 字段(自引用循环根因)。

        旧版 catalog 含 release_commit 字段,但 catalog 自身被提交到 git,
        任何 commit 都使其失效(无稳态)。R66 P0-01 后移除该字段。
        """
        manifest = _load_manifest()
        assert "release_commit" not in manifest, (
            "R66 P0-01: catalog 禁止包含 release_commit 字段 "
            "(自引用循环根因 — HEAD 绑定移至 release-manifest.json)"
        )

    def test_catalog_does_not_contain_tree_sha(self):
        """R66 P0-01: catalog 不得包含 tree_sha 字段(自引用循环根因)。

        旧版 catalog 含 tree_sha 字段,但 catalog 自身被提交到 git tree,
        修改 catalog 使 tree 失效,自引用循环。R66 P0-01 后移除该字段。
        """
        manifest = _load_manifest()
        assert "tree_sha" not in manifest, (
            "R66 P0-01: catalog 禁止包含 tree_sha 字段 "
            "(自引用循环根因 — Tree 绑定移至 release-manifest.json)"
        )

    def test_manifest_lists_all_four_migrations(self):
        """manifest 必须枚举 001/002/003/004/005/006/007 全部 7 个 migration。

        P0-04: 旧 manifest 只列 001–003,004 已存在但未列入 manifest。
        R64 P1-02: 新增 005_restore_capability_nonce_ledger.sql(nonce 状态机)。
        R64 P0-04: 新增 006_outbox_lease_version.sql(lease fencing token + DLQ 审计)。
        R64 P0-03: 新增 007_restore_operations_ledger.sql(蓝绿切换 + 操作账本)。
        """
        manifest = _load_manifest()
        versions = [entry["version"] for entry in manifest["migrations"]]
        expected = [
            "001_initial_schema.sql",
            "002_r56_command_approvals_backfill.sql",
            "003_rebuild_command_approvals.sql",
            "004_effect_receipts_request_hash_unique.sql",
            "005_restore_capability_nonce_ledger.sql",
            "006_outbox_lease_version.sql",
            "007_restore_operations_ledger.sql",
        ]
        assert versions == expected, (
            f"manifest migrations 列表不匹配 — 期望 {expected},实际 {versions}"
        )

    def test_004_migration_entry_exists(self):
        """004 migration 条目必须存在于 manifest 中。

        P0-04: 004 已存在(R62 P1-01)但 manifest 只列 001–003,必须补全。
        """
        manifest = _load_manifest()
        versions = {entry["version"] for entry in manifest["migrations"]}
        assert "004_effect_receipts_request_hash_unique.sql" in versions, (
            "P0-04: manifest 缺少 004_effect_receipts_request_hash_unique.sql 条目"
        )

    def test_005_migration_entry_exists(self):
        """R64 P1-02: 005 migration 条目必须存在于 manifest 中。

        005_restore_capability_nonce_ledger.sql 为 nonce ledger 状态机迁移
        (reserved→consumed|failed),manifest 必须枚举。
        """
        manifest = _load_manifest()
        versions = {entry["version"] for entry in manifest["migrations"]}
        assert "005_restore_capability_nonce_ledger.sql" in versions, (
            "R64 P1-02: manifest 缺少 005_restore_capability_nonce_ledger.sql 条目"
        )

    def test_004_migration_sha256_matches_disk_file(self):
        """manifest 中 004 的 sha256 必须与磁盘文件实际 sha256 一致。

        P0-04: 004 sha256 必须实际计算(不能用占位值)。
        R63 P1-03: 004 文件内容可能因 P1-03 整改而变化,sha256 必须重新计算。
        """
        manifest = _load_manifest()
        entry_004 = next(
            (e for e in manifest["migrations"]
             if e["version"] == "004_effect_receipts_request_hash_unique.sql"),
            None,
        )
        assert entry_004 is not None, "manifest 缺少 004 条目"
        disk_path = MIGRATIONS_DIR / "004_effect_receipts_request_hash_unique.sql"
        assert disk_path.exists(), f"004 SQL 文件不存在: {disk_path}"
        actual_sha = _file_sha256(disk_path)
        assert entry_004["sha256"] == actual_sha, (
            f"004 sha256 不匹配 — manifest={entry_004['sha256']}, "
            f"磁盘={actual_sha} — P0-04: sha256 必须实际计算并与磁盘一致"
        )

    def test_all_migration_sha256_match_disk(self):
        """manifest 中每个 migration 的 sha256 必须与磁盘文件实际 sha256 一致。

        P0-04: 防止 manifest 与磁盘漂移(任意 migration 篡改都能被检测到)。
        """
        manifest = _load_manifest()
        for entry in manifest["migrations"]:
            version = entry["version"]
            expected_sha = entry["sha256"]
            disk_path = MIGRATIONS_DIR / version
            assert disk_path.exists(), f"migration 文件不存在: {disk_path}"
            actual_sha = _file_sha256(disk_path)
            assert actual_sha == expected_sha, (
                f"{version} sha256 不匹配 — manifest={expected_sha}, 磁盘={actual_sha}"
            )

    def test_manifest_migration_set_equals_disk_set(self):
        """磁盘 .sql 文件集合必须严格等于 manifest 声明集合(不漏不多)。

        P0-04: 磁盘有但 manifest 没列出 = 漏项(可能跳过验签);
              manifest 列出但磁盘不存在 = 多项(可能引用旧 manifest)。
        """
        manifest = _load_manifest()
        manifest_versions = {
            entry["version"] for entry in manifest["migrations"]
        }
        disk_versions = {
            f.name for f in MIGRATIONS_DIR.glob("*.sql")
        }
        missing_in_manifest = disk_versions - manifest_versions
        missing_on_disk = manifest_versions - disk_versions
        assert not missing_in_manifest, (
            f"磁盘存在但 manifest 未列出: {sorted(missing_in_manifest)} — "
            f"P0-04: manifest 必须枚举全部 migration"
        )
        assert not missing_on_disk, (
            f"manifest 列出但磁盘不存在: {sorted(missing_on_disk)} — "
            f"P0-04: manifest 与磁盘不一致"
        )

    def test_manifest_verification_fields_present(self):
        """manifest.verification 必须包含签名/证书路径 + OIDC issuer + identity prefix。

        P0-04: 验签逻辑需要这些字段构造 cosign verify-blob 命令。
        """
        manifest = _load_manifest()
        verification = manifest.get("verification", {})
        assert isinstance(verification, dict), "manifest 缺少 verification 字段"
        assert verification.get("signature_file"), (
            "verification.signature_file 缺失 — cosign 验签需要 detached signature 路径"
        )
        assert verification.get("certificate_file"), (
            "verification.certificate_file 缺失 — cosign 验签需要 certificate 路径"
        )
        assert verification.get("certificate_oidc_issuer"), (
            "verification.certificate_oidc_issuer 缺失"
        )
        assert verification.get("certificate_identity_prefix"), (
            "verification.certificate_identity_prefix 缺失 — "
            "用于构造精确 certificate-identity (prefix + ref)"
        )

    def test_catalog_version_is_at_least_3(self):
        """R66 P0-01: catalog version 必须 >= 3(catalog-only 模型版本)。

        旧版 catalog version=2 含 release_commit/tree_sha;
        R66 P0-01 后 version=3 移除自引用字段(catalog-only)。
        """
        manifest = _load_manifest()
        assert manifest.get("version", 0) >= 3, (
            f"R66 P0-01: catalog version 必须 >= 3(catalog-only 模型),"
            f"实际为 {manifest.get('version')} — version 2 含自引用字段已废弃"
        )


# ════════════════════════════════════════════════════════════════
# B. migrate.py 验签逻辑 (P0-04)
# ════════════════════════════════════════════════════════════════


@pytest.fixture
def migrate_module():
    """加载 database.migrate 模块(conftest 已注入 config/telegram mock)。"""
    import database.migrate as migrate
    return migrate


@pytest.fixture
def clean_verify_env(monkeypatch):
    """清除 MIGRATION_MANIFEST_VERIFY 环境变量(默认禁用验签)。"""
    monkeypatch.delenv("MIGRATION_MANIFEST_VERIFY", raising=False)


class TestManifestVerifyEnabled:
    """_is_manifest_verify_enabled() 的环境变量解析。"""

    @pytest.mark.parametrize("value", ["1", "true", "yes", "TRUE", "Yes", "1"])
    def test_enabled_when_env_set_to_truthy(self, migrate_module, monkeypatch, value):
        """MIGRATION_MANIFEST_VERIFY=1/true/yes (大小写不敏感) 启用验签。"""
        monkeypatch.setenv("MIGRATION_MANIFEST_VERIFY", value)
        assert migrate_module._is_manifest_verify_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "False", "NO", ""])
    def test_disabled_when_env_set_to_falsy(self, migrate_module, monkeypatch, value):
        """MIGRATION_MANIFEST_VERIFY=0/false/no/空 禁用验签。"""
        monkeypatch.setenv("MIGRATION_MANIFEST_VERIFY", value)
        assert migrate_module._is_manifest_verify_enabled() is False

    def test_disabled_when_env_unset(self, migrate_module, monkeypatch):
        """未设置 MIGRATION_MANIFEST_VERIFY 时禁用验签(本地默认)。"""
        monkeypatch.delenv("MIGRATION_MANIFEST_VERIFY", raising=False)
        assert migrate_module._is_manifest_verify_enabled() is False


class TestVerifyCatalogOnlyModel:
    """_verify_catalog_only_model() 的 catalog-only 模型校验 (R66 P0-01)。

    R66 P0-01: catalog 不得包含 release_commit / tree_sha 字段
    (自引用循环根因 — 任何 commit 都使 catalog 自身的 tree_sha 失效)。
    HEAD 绑定由 release-artifacts/release-manifest.json 承担。
    """

    def test_passes_with_catalog_only_manifest(self, migrate_module, clean_verify_env):
        """使用当前 catalog(无 release_commit/tree_sha)应通过。"""
        data = _load_manifest()
        # 不抛异常即通过
        migrate_module._verify_catalog_only_model(data)

    def test_raises_when_release_commit_present(self, migrate_module, clean_verify_env):
        """catalog 包含 release_commit 字段 → AppError (fail-closed)。"""
        data = _load_manifest()
        data["release_commit"] = "0" * 40  # 旧版字段不应再存在
        with pytest.raises(AppError) as exc_info:
            migrate_module._verify_catalog_only_model(data)
        assert exc_info.value.code == ErrorCodes.MIGRATION_MANIFEST_FIELD_MISSING
        # R66 P0-01: params 应包含 forbidden field 名(release_commit)
        assert exc_info.value.params.get("field") == "release_commit"

    def test_raises_when_tree_sha_present(self, migrate_module, clean_verify_env):
        """catalog 包含 tree_sha 字段 → AppError (fail-closed)。"""
        data = _load_manifest()
        data["tree_sha"] = "0" * 40  # 旧版字段不应再存在
        with pytest.raises(AppError) as exc_info:
            migrate_module._verify_catalog_only_model(data)
        assert exc_info.value.code == ErrorCodes.MIGRATION_MANIFEST_FIELD_MISSING
        # R66 P0-01: params 应包含 forbidden field 名(tree_sha)
        assert exc_info.value.params.get("field") == "tree_sha"

    def test_passes_when_both_fields_absent(self, migrate_module, clean_verify_env):
        """catalog 既无 release_commit 也无 tree_sha → 通过(R66 P0-01 默认状态)。"""
        data = _load_manifest()
        data.pop("release_commit", None)
        data.pop("tree_sha", None)
        migrate_module._verify_catalog_only_model(data)


class TestVerifyManifestMigrationSet:
    """_verify_manifest_migration_set() 的磁盘集合一致性校验。"""

    def test_passes_with_current_manifest(self, migrate_module, clean_verify_env):
        """磁盘 4 个 .sql 文件与 manifest 4 条目一致时应通过。"""
        data = _load_manifest()
        migrate_module._verify_manifest_migration_set(data)

    def test_raises_when_disk_has_extra_migration(
        self, migrate_module, clean_verify_env, tmp_path, monkeypatch
    ):
        """磁盘有 manifest 未列出的 migration → AppError。

        P0-04: 不允许漏项(磁盘有但 manifest 没列出)。
        """
        data = _load_manifest()
        # 模拟磁盘多出一个 005 文件 — 通过 monkeypatch _MIGRATIONS_DIR
        # 指向一个临时目录,复制现有 4 个文件 + 新增一个
        tmp_migrations = tmp_path / "migrations"
        tmp_migrations.mkdir()
        for sql_file in MIGRATIONS_DIR.glob("*.sql"):
            (tmp_migrations / sql_file.name).write_bytes(sql_file.read_bytes())
        # 新增一个 manifest 没有的 migration
        (tmp_migrations / "005_extra_unlisted.sql").write_text(
            "-- fake migration not in manifest\n"
        )
        monkeypatch.setattr(migrate_module, "_MIGRATIONS_DIR", tmp_migrations)
        with pytest.raises(AppError, match="missing_in_manifest"):
            migrate_module._verify_manifest_migration_set(data)

    def test_raises_when_manifest_lists_extra_migration(
        self, migrate_module, clean_verify_env, tmp_path, monkeypatch
    ):
        """manifest 列出磁盘不存在的 migration → AppError。

        P0-04: 不允许多项(manifest 列出但磁盘不存在)。
        """
        data = _load_manifest()
        data["migrations"].append({
            "order": 99,
            "version": "999_nonexistent.sql",
            "sha256": "0" * 64,
            "predecessor": "004_effect_receipts_request_hash_unique.sql",
        })
        with pytest.raises(AppError, match="missing_on_disk"):
            migrate_module._verify_manifest_migration_set(data)


class TestVerifyManifestCosignSignature:
    """_verify_manifest_cosign_signature() 的签名验证逻辑。

    本地测试环境无 cosign 二进制 / .sig / .pem 文件 — 期望 fail-closed。
    """

    def test_raises_when_signature_file_missing(self, migrate_module, clean_verify_env):
        """签名文件不存在 → AppError (fail-closed)。

        P0-04: manifest 声称存在 .sig/.pem 但树中未见 — 拒绝加载未验签 manifest。
        """
        data = _load_manifest()
        # 当前 migrations 目录无 .sig/.pem 文件(CI 中生成,不在仓库)
        with pytest.raises(AppError, match="not_found"):
            migrate_module._verify_manifest_cosign_signature(data)

    def test_raises_when_verification_field_missing(self, migrate_module, clean_verify_env):
        """verification 字段非 dict(如 None/字符串) → AppError。

        注: 当 verification key 缺失时, data.get("verification", {}) 返回空 dict,
        isinstance 检查通过 → 进入 sig_rel/cert_rel 检查并报"缺少 signature_file /
        certificate_file"。要触发"缺少 verification 字段"错误,需将 verification
        设为非 dict 值(如 None / 字符串)。
        """
        data = _load_manifest()
        data["verification"] = None  # 非 dict
        with pytest.raises(AppError, match="verification"):
            migrate_module._verify_manifest_cosign_signature(data)

    def test_raises_when_verification_key_absent(self, migrate_module, clean_verify_env):
        """verification key 完全缺失 → 进入 sig_rel/cert_rel 检查并报错。

        data.get("verification", {}) 返回空 dict, isinstance 通过,
        但 sig_rel/cert_rel 为空 → 报"缺少 signature_file / certificate_file 字段"。
        """
        data = _load_manifest()
        del data["verification"]
        with pytest.raises(AppError, match="signature_file"):
            migrate_module._verify_manifest_cosign_signature(data)

    def test_raises_when_signature_path_missing(self, migrate_module, clean_verify_env):
        """verification.signature_file 字段缺失 → AppError。"""
        data = _load_manifest()
        data["verification"] = {"certificate_file": "x.pem"}
        with pytest.raises(AppError, match="signature_file"):
            migrate_module._verify_manifest_cosign_signature(data)

    def test_raises_when_certificate_path_missing(self, migrate_module, clean_verify_env):
        """verification.certificate_file 字段缺失 → AppError。"""
        data = _load_manifest()
        data["verification"] = {"signature_file": "x.sig"}
        with pytest.raises(AppError, match="signature_file"):
            migrate_module._verify_manifest_cosign_signature(data)

    def test_raises_when_identity_prefix_missing(
        self, migrate_module, clean_verify_env, tmp_path, monkeypatch
    ):
        """verification.certificate_identity_prefix 缺失 → AppError。

        需要模拟 cosign 二进制可用(否则会先在 cosign 检查处失败)。
        """
        data = _load_manifest()
        # 模拟有 .sig/.pem 文件但 identity_prefix 缺失
        tmp_migrations = tmp_path / "migrations"
        tmp_migrations.mkdir()
        for sql_file in MIGRATIONS_DIR.glob("*.sql"):
            (tmp_migrations / sql_file.name).write_bytes(sql_file.read_bytes())
        (tmp_migrations / "migration-manifest.json").write_text(
            json.dumps(data)
        )
        (tmp_migrations / "migration-manifest.json.sig").write_bytes(b"fake-sig")
        (tmp_migrations / "migration-manifest.json.pem").write_bytes(b"fake-cert")
        data["verification"] = {
            "signature_file": "migration-manifest.json.sig",
            "certificate_file": "migration-manifest.json.pem",
            "certificate_oidc_issuer": "https://token.actions.githubusercontent.com",
            # 缺 certificate_identity_prefix
        }
        monkeypatch.setattr(migrate_module, "_MIGRATIONS_DIR", tmp_migrations)
        monkeypatch.setattr(migrate_module, "_MANIFEST_PATH", tmp_migrations / "migration-manifest.json")
        # 模拟 cosign 二进制可用(返回 fake path,使代码跳过 cosign 不可用检查,
        # 进入 identity_prefix 检查)
        monkeypatch.setattr(migrate_module, "shutil", _MockShutilWithCosign())
        with pytest.raises(AppError, match="certificate_identity_prefix"):
            migrate_module._verify_manifest_cosign_signature(data)


class TestLoadMigrationManifest:
    """_load_migration_manifest() 集成测试 — 完整加载流程。"""

    def test_loads_successfully_when_verify_disabled(self, migrate_module, clean_verify_env):
        """默认(verify 禁用)应成功加载并返回 7 个 migration 条目。

        本地模式: MIGRATION_MANIFEST_VERIFY 未设置 → 跳过 cosign 验签 (warning),
                  但 HEAD/Tree 绑定 + 集合一致性仍强制执行。

        R64 P1-02: 新增 005_restore_capability_nonce_ledger.sql,共 5 个 migration。
        R64 P0-04: 新增 006_outbox_lease_version.sql,共 6 个 migration。
        R64 P0-03: 新增 007_restore_operations_ledger.sql,共 7 个 migration。
        """
        result = migrate_module._load_migration_manifest()
        assert isinstance(result, dict)
        assert len(result) == 7, f"期望 7 个 migration,实际 {len(result)}"
        assert "001_initial_schema.sql" in result
        assert "002_r56_command_approvals_backfill.sql" in result
        assert "003_rebuild_command_approvals.sql" in result
        assert "004_effect_receipts_request_hash_unique.sql" in result
        assert "005_restore_capability_nonce_ledger.sql" in result
        assert "006_outbox_lease_version.sql" in result
        assert "007_restore_operations_ledger.sql" in result

    def test_raises_when_verify_enabled_and_cosign_unavailable(
        self, migrate_module, monkeypatch
    ):
        """MIGRATION_MANIFEST_VERIFY=1 且 cosign 不可用时 → AppError (fail-closed)。

        CI 模式: 必须通过 cosign verify-blob 验签,本地无 cosign 不应通过。
        注: 当前仓库无 .sig/.pem 文件(CI 中生成),所以 _verify_manifest_cosign_signature
        会先报"签名文件不存在"; 即使有 .sig/.pem 文件,cosign 不在 PATH 也会 fail-closed。
        """
        monkeypatch.setenv("MIGRATION_MANIFEST_VERIFY", "1")
        # 模拟 cosign 不在 PATH 中(用 mock shutil 对象避免递归)
        # 真实场景: 仓库无 .sig/.pem 文件 → 报"签名文件不存在"或"证书文件不存在"
        #         (在 cosign 检查之前就失败)
        # 若有 .sig/.pem 文件 → 报"cosign 二进制不在 PATH"
        with pytest.raises(AppError, match="cosign|not_found"):
            migrate_module._load_migration_manifest()

    def test_raises_when_manifest_file_missing(self, migrate_module, clean_verify_env, monkeypatch):
        """manifest 文件不存在 → RuntimeError。"""
        monkeypatch.setattr(
            migrate_module, "_MANIFEST_PATH",
            MIGRATIONS_DIR / "nonexistent-manifest.json"
        )
        with pytest.raises(RuntimeError, match="migration manifest not found"):
            migrate_module._load_migration_manifest()


# ════════════════════════════════════════════════════════════════
# C. release-gates.yml 结构 (P0-01)
# ════════════════════════════════════════════════════════════════


class TestReleaseGatesWorkflow:
    """release-gates.yml workflow 结构校验 — P0-01 整改。"""

    @pytest.fixture
    def workflow_content(self):
        """读取 release-gates.yml 完整内容。"""
        return WORKFLOW_PATH.read_text(encoding="utf-8")

    def test_sign_image_step_outputs_certificate(self, workflow_content):
        """sign-image job 的 Sign OCI image 步骤必须用 --output-certificate 保存 Fulcio cert。

        P0-01: 从实际 cert 提取 SAN 做 verify,而非 github.ref 拼接。
        """
        # 检查 Sign OCI image 步骤包含 --output-certificate
        assert "--output-certificate image-signing-cert.pem" in workflow_content, (
            "P0-01: Sign OCI image 步骤必须用 --output-certificate image-signing-cert.pem "
            "保存 Fulcio cert,供后续 verify 提取实际 SAN"
        )

    def test_has_image_identity_extraction_step(self, workflow_content):
        """sign-image job 必须有 Extract certificate identity 步骤(image signing)。

        P0-01: 从 cert 提取 SAN,作为 verify 的 certificate-identity。
        """
        assert "Extract certificate identity from Fulcio cert (image signing)" in workflow_content, (
            "P0-01: sign-image 必须有从 Fulcio cert 提取 certificate-identity 的步骤"
        )
        # 步骤必须用 openssl 提取 SAN
        assert "openssl x509" in workflow_content, (
            "P0-01: 必须用 openssl x509 从 cert 提取 subjectAltName (SAN URI)"
        )
        assert "subjectAltName" in workflow_content, (
            "P0-01: 必须读取 cert 的 subjectAltName 扩展提取 SAN URI"
        )

    def test_has_manifest_identity_extraction_step(self, workflow_content):
        """sign-image job 必须有 Extract certificate identity 步骤(manifest signing)。

        P0-01: migration manifest 验签也必须从实际 cert 提取 SAN。
        """
        assert "Extract certificate identity from Fulcio cert (manifest signing)" in workflow_content, (
            "P0-01: sign-image 必须有 manifest cert 的 identity 提取步骤"
        )

    def test_has_production_identity_extraction_step(self, workflow_content):
        """publish-attestation job 必须有 Extract certificate identity 步骤(production promote)。

        P0-01: publish-attestation 复用 sign-image 的 cert 提取 SAN,确保 verify identity 一致。
        """
        assert "Extract certificate identity from Fulcio cert (production promote)" in workflow_content, (
            "P0-01: publish-attestation 必须有 cert identity 提取步骤(复用 sign-image cert)"
        )

    def test_image_verify_uses_extracted_identity_not_github_ref(self, workflow_content):
        """Verify image signature 步骤必须用 steps.extract_image_identity.outputs,而非 github.ref 拼接。

        P0-01 根治: 不再用 CERT_IDENTITY="https://github.com/.../.github/workflows/release-gates.yml@${{ github.ref }}"
        """
        # 找到 Verify image signature 步骤 — 检查它引用 extract_image_identity step output
        assert "steps.extract_image_identity.outputs.certificate_identity" in workflow_content, (
            "P0-01: Verify image signature 步骤必须用从 Fulcio cert 提取的 identity, "
            "而非 github.ref 拼接的字符串"
        )

    def test_manifest_verify_uses_extracted_identity(self, workflow_content):
        """Verify migration manifest signature 步骤必须用从 cert 提取的 identity。

        P0-01: 不再硬编码 github.ref 拼接。
        """
        assert "steps.extract_manifest_identity.outputs.certificate_identity" in workflow_content, (
            "P0-01: Verify migration manifest signature 步骤必须用从 cert 提取的 identity"
        )

    def test_production_verify_uses_extracted_identity(self, workflow_content):
        """publish-attestation 的 cosign verify image digest 步骤必须用从 cert 提取的 identity。

        P0-01: production promote 也必须复用 sign-image 的 cert。
        """
        assert "steps.extract_prod_identity.outputs.certificate_identity" in workflow_content, (
            "P0-01: publish-attestation 的 cosign verify image digest 步骤 "
            "必须用从 sign-image cert 提取的 identity"
        )

    def test_image_signing_cert_uploaded_as_artifact(self, workflow_content):
        """Upload signed artifacts 步骤必须上传 image-signing-cert.pem。

        P0-01: publish-attestation 跨 job 复用此 cert 提取 SAN。
        """
        assert "image-signing-cert.pem" in workflow_content, (
            "P0-01: Upload signed artifacts 必须包含 image-signing-cert.pem, "
            "供 publish-attestation 跨 job 下载并提取 identity"
        )

    def test_publish_attestation_downloads_signed_artifact(self, workflow_content):
        """publish-attestation 必须有 download-artifact 步骤下载 sign-image 上传的 artifact。

        P0-01: 跨 job 共享 Fulcio cert。
        """
        assert "actions/download-artifact" in workflow_content, (
            "P0-01: publish-attestation 必须用 actions/download-artifact 下载 "
            "sign-image 上传的签名制品(含 image-signing-cert.pem)"
        )
        assert "release-gates-signed-" in workflow_content, (
            "P0-01: download-artifact 必须下载 release-gates-signed-${{ github.sha }} 制品"
        )

    def test_workflow_does_not_verify_release_commit_binding(self, workflow_content):
        """R66 P0-01: release-gates.yml 必须移除 catalog 的 release_commit/tree_sha 绑定校验。

        R66 P0-01: catalog 不再包含 release_commit/tree_sha 字段(catalog-only 模型),
        workflow 不应再验证 catalog 的 release_commit binding。HEAD 绑定由
        release-manifest.json 的 source_commit/source_tree 字段验证。
        """
        # 移除"Verify migration manifest release_commit binding"步骤
        assert "Verify migration manifest release_commit binding" not in workflow_content, (
            "R66 P0-01: release-gates.yml 必须移除 'Verify migration manifest "
            "release_commit binding' 步骤(catalog 不再包含 release_commit 字段)"
        )

    def test_workflow_verifies_release_manifest_source_binding(self, workflow_content):
        """R66 P0-01: workflow 必须验证 release-manifest.json 的 source_commit/source_tree。

        HEAD/Tree 绑定从 catalog 移到 release-manifest.json(R66 P0-01)。
        workflow 应验证 release-manifest.json 的 source_commit == 当前 HEAD。
        """
        # 必须有验证 release-manifest.json source_commit 的步骤
        assert "source_commit" in workflow_content, (
            "R66 P0-01: release-gates.yml 必须验证 release-manifest.json 的 source_commit 字段"
        )
        assert "source_tree" in workflow_content, (
            "R66 P0-01: release-gates.yml 必须验证 release-manifest.json 的 source_tree 字段"
        )

    def test_identity_form_check_enforced(self, workflow_content):
        """identity 形态校验:必须是本仓库 release-gates.yml 工作流。

        P0-01: 防止 cert 被替换为其他工作流的签名(跨工作流伪造防护)。
        """
        # 检查 grep -qE '^https://github\.com/...release-gates\.yml@' 形态校验
        assert "release-gates\\.yml@" in workflow_content, (
            "P0-01: 必须校验 certificate-identity 形态为本仓库 release-gates.yml 工作流"
        )

    def test_no_github_ref_hardcoded_in_verify_steps(self, workflow_content):
        """verify 步骤不应再用 github.ref 拼接 certificate-identity。

        P0-01 根治: 从实际 cert 提取 SAN,消除拼接字符串不匹配的根因。
        旧实现: CERT_IDENTITY="https://github.com/${{ github.repository }}/.github/workflows/release-gates.yml@${{ github.ref }}"
        新实现: CERT_IDENTITY="${{ steps.extract_*_identity.outputs.certificate_identity }}"
        """
        # 查找所有 "CERT_IDENTITY=...github.ref" 的硬编码模式
        # (排除注释行 — 注释中可能提到旧实现作为说明)
        lines = workflow_content.splitlines()
        hardcoded_lines = []
        for i, line in enumerate(lines):
            stripped = line.lstrip()
            # 跳过注释行
            if stripped.startswith("#"):
                continue
            # 检测 CERT_IDENTITY=...${{ github.ref }} 的硬编码模式
            if "CERT_IDENTITY=" in line and "${{ github.ref }}" in line:
                hardcoded_lines.append((i + 1, line.strip()))
        assert not hardcoded_lines, (
            "P0-01: verify 步骤中不应再用 github.ref 拼接 CERT_IDENTITY — "
            f"发现 {len(hardcoded_lines)} 处硬编码: {hardcoded_lines[:3]}"
        )


# ════════════════════════════════════════════════════════════════
# D. 跨步骤依赖 (P0-01)
# ════════════════════════════════════════════════════════════════


class TestWorkflowDependencyChain:
    """sign-image → publish-attestation 跨 job 依赖链校验。"""

    @pytest.fixture
    def workflow_yaml(self):
        """解析 release-gates.yml 为 dict。"""
        import yaml
        return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))

    def test_publish_attestation_needs_sign_image(self, workflow_yaml):
        """publish-attestation 必须通过 needs 依赖 sign-image。

        P0-01: sign-image 失败 → publish-attestation 自动 skipped(不强运行)。
        """
        pa_needs = workflow_yaml["jobs"]["publish-attestation"]["needs"]
        # needs 可能是 list 或 str
        if isinstance(pa_needs, str):
            pa_needs = [pa_needs]
        assert "sign-image" in pa_needs, (
            "P0-01: publish-attestation 必须在 needs 中依赖 sign-image, "
            "确保 sign-image 失败时 publish-attestation 自动跳过"
        )

    def test_sign_image_only_runs_on_master_push(self, workflow_yaml):
        """sign-image 必须只在 push 到 master/main 时运行(if 条件)。

        P0-01: PR 场景不应运行签名(无 OIDC keyless 上下文)。
        """
        sign_image = workflow_yaml["jobs"]["sign-image"]
        if_cond = sign_image.get("if", "")
        assert "push" in str(if_cond), (
            "P0-01: sign-image 的 if 条件必须限制为 push 事件"
        )
        assert "master" in str(if_cond) or "main" in str(if_cond), (
            "P0-01: sign-image 的 if 条件必须限制为 master/main 分支"
        )

    def test_release_summary_requires_sign_image_success_on_release_target(self, workflow_yaml):
        """release-summary 在 release target 场景下必须要求 sign-image == success。

        P0-01: skipped/neutral/cancelled 一律失败。
        """
        # release-summary 的 Verify all required jobs succeeded 步骤会检查
        # sign-image result,在 release target 场景下必须 success
        rs_steps = workflow_yaml["jobs"]["release-summary"]["steps"]
        assert len(rs_steps) > 0
        # 检查步骤的 run 脚本中包含 sign-image 的 result 检查
        run_script = rs_steps[0].get("run", "")
        assert "sign-image" in run_script, (
            "P0-01: release-summary 必须检查 sign-image 的 result"
        )
        assert "RELEASE_TARGET" in run_script, (
            "P0-01: release-summary 必须区分 release target (master/main push) 与 PR"
        )

    def test_sign_image_has_no_continue_on_error(self, workflow_yaml):
        """sign-image 不应有 continue-on-error: true。

        P0-01: 签名失败必须阻断,不允许 continue-on-error 掩盖供应链断裂。
        """
        sign_image = workflow_yaml["jobs"]["sign-image"]
        assert not sign_image.get("continue-on-error", False), (
            "P0-01: sign-image 不允许 continue-on-error — 签名失败必须阻断"
        )
        # 也检查每个 step
        for step in sign_image["steps"]:
            assert not step.get("continue-on-error", False), (
                f"P0-01: sign-image 的步骤 '{step.get('name', '?')}' "
                f"不允许 continue-on-error"
            )

    def test_upload_artifact_uses_if_success(self, workflow_yaml):
        """Upload signed artifacts 步骤必须用 if: success()。

        P0-01: 仅在所有前置步骤成功时才上传,避免上传部分/空 artifact 掩盖断裂。
        """
        sign_image_steps = workflow_yaml["jobs"]["sign-image"]["steps"]
        upload_step = next(
            (s for s in sign_image_steps
             if s.get("uses", "").startswith("actions/upload-artifact")),
            None,
        )
        assert upload_step is not None, "未找到 Upload signed artifacts 步骤"
        assert upload_step.get("if") == "success()", (
            "P0-01: Upload signed artifacts 必须用 if: success() — "
            "前置步骤失败时不上传,避免掩盖供应链断裂"
        )
