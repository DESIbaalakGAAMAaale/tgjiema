"""R37 Batch 3 P1 测试覆盖

测试覆盖:
- P1-6: 密钥管理增强(KEK provider 抽象 + key_id + 双 key 解密窗口)
- P1-8: 迁移客户端拆分(migration_runner / bootstrap_runner / runtime_client)
"""
import os
import sys
import base64
import hashlib
import inspect
from pathlib import Path
from unittest.mock import patch

import pytest

# 检查 cryptography 是否可用(本机 Python 3.9 可能 DLL 加载失败)
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
    _CRYPTO_AVAILABLE = True
except Exception:
    _CRYPTO_AVAILABLE = False

_CRYPTO_REASON = "cryptography 不可用(本机环境限制,CI 矩阵 3.10+ 可用)"


# ─────────────────────────────────────────────────────────────
#  P1-6: 密钥管理增强
# ─────────────────────────────────────────────────────────────

class TestP16KekProviderAbstraction:
    """P1-6: KEK provider 抽象 — systemd credentials 文件优先于环境变量。"""

    def test_read_kek_from_file_returns_content(self, tmp_path):
        """_read_kek_from_file 读取文件内容并去除空白。"""
        from services.backup_crypto import _read_kek_from_file
        kek_file = tmp_path / "kek.b64"
        kek_file.write_text("  dGhpcyBpcyBhIHRlc3Qga2V5\n", encoding="utf-8")
        result = _read_kek_from_file(str(kek_file))
        assert result == "dGhpcyBpcyBhIHRlc3Qga2V5"

    def test_read_kek_from_file_missing_returns_none(self, tmp_path):
        """文件不存在返回 None(不抛异常)。"""
        from services.backup_crypto import _read_kek_from_file
        result = _read_kek_from_file(str(tmp_path / "nonexistent.b64"))
        assert result is None

    def test_read_kek_from_file_empty_returns_none(self, tmp_path):
        """空文件返回 None。"""
        from services.backup_crypto import _read_kek_from_file
        kek_file = tmp_path / "empty.b64"
        kek_file.write_text("   \n  ", encoding="utf-8")
        result = _read_kek_from_file(str(kek_file))
        assert result is None

    def test_resolve_kek_b64_prioritizes_file_over_env(self, tmp_path, monkeypatch):
        """_resolve_kek_b64 优先读取 BACKUP_KEK_FILE,回退到 BACKUP_KEK。"""
        from services.backup_crypto import _resolve_kek_b64
        kek_file = tmp_path / "kek.b64"
        kek_file.write_text("file_kek_value", encoding="utf-8")
        monkeypatch.setenv("BACKUP_KEK_FILE", str(kek_file))
        monkeypatch.setenv("BACKUP_KEK", "env_kek_value")
        result = _resolve_kek_b64()
        assert result == "file_kek_value"

    def test_resolve_kek_b64_falls_back_to_env(self, monkeypatch):
        """BACKUP_KEK_FILE 未配置或读取失败时回退到 BACKUP_KEK。"""
        from services.backup_crypto import _resolve_kek_b64
        monkeypatch.delenv("BACKUP_KEK_FILE", raising=False)
        monkeypatch.setenv("BACKUP_KEK", "env_kek_value")
        result = _resolve_kek_b64()
        assert result == "env_kek_value"

    def test_resolve_kek_b64_falls_back_when_file_read_fails(self, monkeypatch):
        """BACKUP_KEK_FILE 指定但文件不存在时回退到 BACKUP_KEK。"""
        from services.backup_crypto import _resolve_kek_b64
        monkeypatch.setenv("BACKUP_KEK_FILE", "/nonexistent/path/kek.b64")
        monkeypatch.setenv("BACKUP_KEK", "fallback_env_value")
        result = _resolve_kek_b64()
        assert result == "fallback_env_value"

    def test_resolve_kek_b64_empty_when_neither_configured(self, monkeypatch):
        """BACKUP_KEK_FILE 和 BACKUP_KEK 均未配置时返回空字符串。"""
        from services.backup_crypto import _resolve_kek_b64
        monkeypatch.delenv("BACKUP_KEK_FILE", raising=False)
        monkeypatch.delenv("BACKUP_KEK", raising=False)
        result = _resolve_kek_b64()
        assert result == ""


class TestP16KeyId:
    """P1-6: key_id 生成 — KEK 的 SHA-256 前 16 字符 hex(不可逆)。"""

    def test_get_key_id_returns_sha256_prefix(self, monkeypatch):
        """get_key_id 返回 sha256(kek)[:16] hex。"""
        from services.backup_crypto import get_kek, get_key_id
        kek_bytes = b"a" * 32
        kek_b64 = base64.b64encode(kek_bytes).decode("ascii")
        monkeypatch.setenv("BACKUP_KEK", kek_b64)
        monkeypatch.delenv("BACKUP_KEK_FILE", raising=False)
        expected = hashlib.sha256(kek_bytes).hexdigest()[:16]
        result = get_key_id()
        assert result == expected
        assert len(result) == 16
        assert all(c in "0123456789abcdef" for c in result)

    def test_get_key_id_empty_when_kek_not_configured(self, monkeypatch):
        """KEK 未配置时返回空字符串。"""
        from services.backup_crypto import get_key_id
        monkeypatch.delenv("BACKUP_KEK_FILE", raising=False)
        monkeypatch.delenv("BACKUP_KEK", raising=False)
        result = get_key_id()
        assert result == ""

    def test_get_key_id_not_reversible(self, monkeypatch):
        """key_id 是 hash 摘要,无法反推 KEK 原文。"""
        from services.backup_crypto import get_key_id
        kek_bytes = b"my-secret-kek-32-bytes-long!!!"  # 32 字节
        kek_b64 = base64.b64encode(kek_bytes).decode("ascii")
        monkeypatch.setenv("BACKUP_KEK", kek_b64)
        monkeypatch.delenv("BACKUP_KEK_FILE", raising=False)
        key_id = get_key_id()
        # key_id 不包含 KEK 任何片段
        assert kek_bytes.decode("ascii") not in key_id
        assert kek_b64 not in key_id


class TestP16DualKeyDecryptWindow:
    """P1-6: 双 key 解密窗口 — current KEK → previous KEK 轮转支持。"""

    def test_get_previous_kek_from_env(self, monkeypatch):
        """get_previous_kek 从 BACKUP_KEK_PREVIOUS 读取旧 KEK。"""
        from services.backup_crypto import get_previous_kek
        kek_bytes = b"b" * 32
        kek_b64 = base64.b64encode(kek_bytes).decode("ascii")
        monkeypatch.setenv("BACKUP_KEK_PREVIOUS", kek_b64)
        monkeypatch.delenv("BACKUP_KEK_PREVIOUS_FILE", raising=False)
        result = get_previous_kek()
        assert result == kek_bytes

    def test_get_previous_kek_from_file(self, tmp_path, monkeypatch):
        """get_previous_kek 优先从 BACKUP_KEK_PREVIOUS_FILE 读取。"""
        from services.backup_crypto import get_previous_kek
        kek_bytes = b"c" * 32
        kek_b64 = base64.b64encode(kek_bytes).decode("ascii")
        prev_file = tmp_path / "prev_kek.b64"
        prev_file.write_text(kek_b64, encoding="utf-8")
        monkeypatch.setenv("BACKUP_KEK_PREVIOUS_FILE", str(prev_file))
        monkeypatch.delenv("BACKUP_KEK_PREVIOUS", raising=False)
        result = get_previous_kek()
        assert result == kek_bytes

    def test_get_previous_kek_none_when_not_configured(self, monkeypatch):
        """未配置 previous KEK 返回 None。"""
        from services.backup_crypto import get_previous_kek
        monkeypatch.delenv("BACKUP_KEK_PREVIOUS_FILE", raising=False)
        monkeypatch.delenv("BACKUP_KEK_PREVIOUS", raising=False)
        result = get_previous_kek()
        assert result is None

    def test_decrypt_with_previous_kek_after_rotation(self, monkeypatch):
        """密钥轮转后,旧 KEK 加密的备份可用 BACKUP_KEK_PREVIOUS 解密。"""
        if not _CRYPTO_AVAILABLE:
            pytest.skip(_CRYPTO_REASON)
        from services.backup_crypto import (
            encrypt_payload, decrypt_payload, get_kek, get_previous_kek,
        )
        # 场景:旧 KEK 加密的备份,新 KEK 配置为 BACKUP_KEK,旧 KEK 配置为 PREVIOUS
        old_kek = b"old-kek-32-bytes-pad!!!!!!!!!!"[:32]
        new_kek = b"new-kek-32-bytes-pad!!!!!!!!!!"[:32]
        # 用旧 KEK 加密
        plaintext = b'{"backup": "data", "version": 3}'
        enc = encrypt_payload(plaintext, kek=old_kek)
        assert enc["encrypted"] is True
        assert "key_id" in enc
        # 配置:当前 KEK = new,previous KEK = old
        monkeypatch.setenv("BACKUP_KEK", base64.b64encode(new_kek).decode("ascii"))
        monkeypatch.delenv("BACKUP_KEK_FILE", raising=False)
        monkeypatch.setenv("BACKUP_KEK_PREVIOUS", base64.b64encode(old_kek).decode("ascii"))
        monkeypatch.delenv("BACKUP_KEK_PREVIOUS_FILE", raising=False)
        # 解密:应尝试 current(失败)→ previous(成功)
        dec = decrypt_payload(enc["ciphertext"], enc["wrapped_dek"], enc["nonce"])
        assert dec == plaintext

    def test_decrypt_fails_when_no_kek_matches(self, monkeypatch):
        """所有候选 KEK 均不匹配时抛 ValueError。"""
        if not _CRYPTO_AVAILABLE:
            pytest.skip(_CRYPTO_REASON)
        from services.backup_crypto import encrypt_payload, decrypt_payload
        kek = b"a" * 32
        plaintext = b"test data"
        enc = encrypt_payload(plaintext, kek=kek)
        # 配置不同的 KEK
        wrong_kek = b"z" * 32
        monkeypatch.setenv("BACKUP_KEK", base64.b64encode(wrong_kek).decode("ascii"))
        monkeypatch.delenv("BACKUP_KEK_FILE", raising=False)
        monkeypatch.delenv("BACKUP_KEK_PREVIOUS", raising=False)
        monkeypatch.delenv("BACKUP_KEK_PREVIOUS_FILE", raising=False)
        with pytest.raises(ValueError, match="解密失败"):
            decrypt_payload(enc["ciphertext"], enc["wrapped_dek"], enc["nonce"])

    def test_encrypt_payload_returns_key_id(self, monkeypatch):
        """encrypt_payload 返回值包含 key_id 字段。"""
        if not _CRYPTO_AVAILABLE:
            pytest.skip(_CRYPTO_REASON)
        from services.backup_crypto import encrypt_payload
        kek = b"x" * 32
        enc = encrypt_payload(b"test", kek=kek)
        assert enc["encrypted"] is True
        assert "key_id" in enc
        assert len(enc["key_id"]) == 16
        expected = hashlib.sha256(kek).hexdigest()[:16]
        assert enc["key_id"] == expected


class TestP16ManifestKeyId:
    """P1-6: db_backup.py manifest 记录 key_id。"""

    def test_db_backup_manifest_records_key_id(self):
        """db_backup.py 在 manifest encryption 信息中包含 key_id 字段。"""
        backup_path = Path(__file__).resolve().parent.parent / "services" / "db_backup.py"
        content = backup_path.read_text(encoding="utf-8")
        # 验证 manifest encryption 信息包含 key_id
        assert "key_id" in content
        assert "get_key_id" in content or 'enc_result.get("key_id")' in content


# ─────────────────────────────────────────────────────────────
#  P1-8: 迁移客户端拆分
# ─────────────────────────────────────────────────────────────

class TestP18MigrationRunner:
    """P1-8: migration_runner — 唯一允许 DDL/TTL/版本写入。"""

    def test_migration_runner_module_exists(self):
        """services.migration_runner 模块存在且可导入。"""
        from services import migration_runner
        assert hasattr(migration_runner, "run_migration")
        assert hasattr(migration_runner, "main")

    def test_migration_runner_has_run_migration_async(self):
        """run_migration 是 async 函数。"""
        from services.migration_runner import run_migration
        assert inspect.iscoroutinefunction(run_migration)

    def test_migration_runner_docstring_mentions_p18(self):
        """模块 docstring 标注 R37 P1-8 职责边界。"""
        from services import migration_runner
        assert migration_runner.__doc__ is not None
        assert "P1-8" in migration_runner.__doc__
        assert "migration_runner" in migration_runner.__doc__

    def test_migration_runner_does_not_bootstrap(self):
        """migration_runner 不执行全表 bootstrap(那是 bootstrap_runner 的职责)。"""
        mr_path = Path(__file__).resolve().parent.parent / "services" / "migration_runner.py"
        content = mr_path.read_text(encoding="utf-8")
        # 不应调用 bootstrap_users / bootstrap_codes / bootstrap_file_records
        assert "bootstrap_users" not in content
        assert "bootstrap_codes" not in content
        assert "bootstrap_file_records" not in content


class TestP18BootstrapRunner:
    """P1-8: bootstrap_runner — 显式人工或恢复任务,限速且可观测。"""

    def test_bootstrap_runner_module_exists(self):
        """services.bootstrap_runner 模块存在且可导入。"""
        from services import bootstrap_runner
        assert hasattr(bootstrap_runner, "run_bootstrap")
        assert hasattr(bootstrap_runner, "main")

    def test_bootstrap_runner_has_run_bootstrap_async(self):
        """run_bootstrap 是 async 函数。"""
        from services.bootstrap_runner import run_bootstrap
        assert inspect.iscoroutinefunction(run_bootstrap)

    def test_bootstrap_runner_docstring_mentions_p18(self):
        """模块 docstring 标注 R37 P1-8 职责边界。"""
        from services import bootstrap_runner
        assert bootstrap_runner.__doc__ is not None
        assert "P1-8" in bootstrap_runner.__doc__
        assert "bootstrap_runner" in bootstrap_runner.__doc__

    def test_bootstrap_runner_does_not_execute_ddl(self):
        """bootstrap_runner 不执行 DDL(那是 migration_runner 的职责)。"""
        br_path = Path(__file__).resolve().parent.parent / "services" / "bootstrap_runner.py"
        content = br_path.read_text(encoding="utf-8")
        # 不应执行 DDL_STATEMENTS / MIGRATION_STATEMENTS
        assert "DDL_STATEMENTS" not in content
        assert "MIGRATION_STATEMENTS" not in content
        assert "CREATE TABLE" not in content
        assert "ALTER TABLE" not in content


class TestP18RuntimeClientSplit:
    """P1-8: runtime_client — 只连接/查询,不导入 DDL,不 bootstrap。

    注意: 用文件内容检查代替直接导入,避免 Python 3.9 类型注解兼容问题
    (session.py 的 D1Collection 使用 list[str] | None 语法,Python 3.10+ 才支持)。
    """

    def test_connect_runtime_only_exists(self):
        """CockroachDBClient.connect_runtime_only 方法定义存在。"""
        session_path = Path(__file__).resolve().parent.parent / "database" / "session.py"
        content = session_path.read_text(encoding="utf-8")
        assert "async def connect_runtime_only(self):" in content

    def test_connect_runtime_only_docstring_mentions_no_ddl(self):
        """connect_runtime_only docstring 明确不执行 DDL/bootstrap。"""
        session_path = Path(__file__).resolve().parent.parent / "database" / "session.py"
        content = session_path.read_text(encoding="utf-8")
        idx = content.find("async def connect_runtime_only")
        assert idx > 0
        # 在 connect_runtime_only 定义后 800 字符内查找 docstring 内容
        snippet = content[idx:idx + 800]
        assert "P1-8" in snippet
        assert "DDL" in snippet or "ddl" in snippet.lower()
        assert "bootstrap" in snippet.lower()

    def test_connect_delegates_to_connect_runtime_only(self):
        """connect() 委托 connect_runtime_only()。"""
        session_path = Path(__file__).resolve().parent.parent / "database" / "session.py"
        content = session_path.read_text(encoding="utf-8")
        idx = content.find("async def connect(self):")
        assert idx > 0
        snippet = content[idx:idx + 500]
        assert "connect_runtime_only" in snippet

    def test_connect_supports_db_auto_migrate(self):
        """connect() 在 DB_AUTO_MIGRATE=true 时调用 _legacy_run_ddl_and_bootstrap。"""
        session_path = Path(__file__).resolve().parent.parent / "database" / "session.py"
        content = session_path.read_text(encoding="utf-8")
        idx = content.find("async def connect(self):")
        assert idx > 0
        snippet = content[idx:idx + 1000]
        assert "DB_AUTO_MIGRATE" in snippet
        assert "_legacy_run_ddl_and_bootstrap" in snippet

    def test_legacy_run_ddl_and_bootstrap_exists(self):
        """_legacy_run_ddl_and_bootstrap 函数定义存在(本地开发兼容)。"""
        session_path = Path(__file__).resolve().parent.parent / "database" / "session.py"
        content = session_path.read_text(encoding="utf-8")
        assert "async def _legacy_run_ddl_and_bootstrap" in content

    def test_init_db_does_not_mention_ddl_execution(self):
        """init_db() docstring 明确不再执行 DDL/bootstrap。"""
        session_path = Path(__file__).resolve().parent.parent / "database" / "session.py"
        content = session_path.read_text(encoding="utf-8")
        idx = content.find("async def init_db():")
        assert idx > 0
        snippet = content[idx:idx + 600]
        assert "P1-8" in snippet
        assert "runtime_client" in snippet or "runtime" in snippet.lower()

    def test_init_db_with_migration_exists(self):
        """init_db_with_migration 便利函数定义存在(本地开发)。"""
        session_path = Path(__file__).resolve().parent.parent / "database" / "session.py"
        content = session_path.read_text(encoding="utf-8")
        assert "async def init_db_with_migration" in content


class TestP18DeployScriptMigrationRunner:
    """P1-8: 部署脚本调用 migration_runner 而非 init_db。"""

    def test_deploy_script_calls_migration_runner(self):
        """deploy_vps_per_bot.sh 的 migration 服务 ExecStart 调用 migration_runner。"""
        deploy_path = Path(__file__).resolve().parent.parent / "deploy_vps_per_bot.sh"
        content = deploy_path.read_text(encoding="utf-8")
        # 找到 migration service 定义部分
        assert "services.migration_runner" in content
        # 不应再调用 init_db() 做 DDL
        # (init_db 可能出现在其他注释中,但 ExecStart 应是 migration_runner)
        idx = content.find("tgjiema-migration.service")
        assert idx > 0
        # 在 migration service 定义附近 1500 字符内应有 migration_runner
        snippet = content[idx:idx + 1500]
        assert "services.migration_runner" in snippet

    def test_docker_compose_migration_uses_migration_runner(self):
        """docker-compose.yml 的 migration 服务 command 使用 migration_runner。"""
        compose_path = Path(__file__).resolve().parent.parent / "docker-compose.yml"
        content = compose_path.read_text(encoding="utf-8")
        assert "services.migration_runner" in content


# ─────────────────────────────────────────────────────────────
#  P1-7: CI/CD workflow 验证(结构性检查)
# ─────────────────────────────────────────────────────────────

class TestP17CICDWorkflow:
    """P1-7: CI/CD workflow 包含商用发布门禁。"""

    def test_ci_workflow_has_release_gates_job(self):
        """ci.yml 包含 release-gates job(Compose config + migration dry-run + SBOM + 依赖扫描)。"""
        ci_path = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"
        content = ci_path.read_text(encoding="utf-8")
        assert "release-gates:" in content
        assert "docker compose config" in content
        assert "Migration dry-run" in content
        assert "SBOM" in content or "sbom" in content.lower()
        assert "pip-audit" in content

    def test_ci_workflow_has_artifact_signing(self):
        """ci.yml 包含制品签名(Sigstore cosign)。"""
        ci_path = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"
        content = ci_path.read_text(encoding="utf-8")
        assert "sign-artifacts" in content or "cosign" in content
        assert "id-token: write" in content or "id_token: write" in content

    def test_deploy_check_has_cross_verification(self):
        """deploy-check.yml 包含三方一致性校验(services.yaml ↔ compose ↔ systemd)。"""
        dc_path = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "deploy-check.yml"
        content = dc_path.read_text(encoding="utf-8")
        assert "Cross-verify service consistency" in content or "cross-verify" in content.lower()
        assert "env_file_secrets" in content

    def test_deploy_check_documents_branch_protection(self):
        """deploy-check.yml 文档化 branch protection 必需检查。"""
        dc_path = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "deploy-check.yml"
        content = dc_path.read_text(encoding="utf-8")
        assert "branch protection" in content.lower() or "branch-protection" in content.lower()
        assert "required checks" in content.lower() or "required_status_checks" in content


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
