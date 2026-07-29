"""R81 §10.1 / §10.3 / §10.4 单元测试 — backup_id 跨进程持久化、endpoint URL parser、env flags。

测试覆盖:
  A. _persist_backup_id / _load_backup_id(§10.1)
    - 写入后跨进程读取
    - 空 backup_id 拒绝(fail-closed)
    - 状态文件缺失返回空
    - 环境变量优先级高于状态文件
    - 临时文件(.tmp)不得被当作有效状态
    - 旧运行残留状态不得被错误复用(覆盖写)

  B. _container_endpoint(§10.3)
    - http://localhost:9000 → http://minio:9000
    - http://127.0.0.1:9000 → http://minio:9000
    - 已是 http://minio:9000 原样返回
    - HTTPS endpoint
    - 带 path/query/fragment
    - path/query 中包含 localhost 不被误替换
    - 空字符串
    - 非 localhost endpoint 原样返回

  C. _env_to_compose_run_flags(§10.4)
    - flags 只包含变量名(-e KEY),不包含值
    - command repr 不包含 value
    - 稳定排序(保证 artifact 可复现)
    - 未声明变量不注入
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 将 scripts/ 加入 sys.path 以导入 compose_runtime_e2e 模块
REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


# ════════════════════════════════════════════════════════════════
# A. backup_id 跨进程持久化(§10.1)
# ════════════════════════════════════════════════════════════════


class TestPersistBackupState:
    """R82 §10.5: current-SHA 与三对象精确状态必须原子持久化并 fail-closed。"""

    @staticmethod
    def _objects(suffix: str = "one") -> dict[str, str]:
        return {
            "payload": f"db_backup/payload_{suffix}.enc",
            "manifest": f"db_backup/manifest_{suffix}.json",
            "COMPLETE": f"db_backup/COMPLETE_{suffix}.COMPLETE",
        }

    def _redirect(self, tmp_path, monkeypatch) -> Path:
        target = tmp_path / "backup-state.json"
        monkeypatch.setattr("compose_runtime_e2e._SECRETLESS_STATE_DIR", tmp_path)
        monkeypatch.setattr("compose_runtime_e2e._BACKUP_ID_FILE", target)
        return target

    def test_persist_and_load_roundtrip(self, tmp_path, monkeypatch):
        import compose_runtime_e2e as cre
        target = self._redirect(tmp_path, monkeypatch)
        cre._persist_backup_state(
            head_sha="sha-current", backup_id="bk-one", objects=self._objects(),
        )
        state = cre._load_backup_state(expected_head_sha="sha-current")
        assert state["backup_id"] == "bk-one"
        assert state["payload_key"] == "db_backup/payload_one.enc"
        assert target.is_file()
        assert not target.with_suffix(".tmp").exists()

    def test_wrong_sha_rejected(self, tmp_path, monkeypatch):
        import compose_runtime_e2e as cre
        self._redirect(tmp_path, monkeypatch)
        cre._persist_backup_state(
            head_sha="sha-old", backup_id="bk-old", objects=self._objects("old"),
        )
        assert cre._load_backup_state(expected_head_sha="sha-current") == {}

    @pytest.mark.parametrize("missing_key", ["payload", "manifest", "COMPLETE"])
    def test_missing_object_key_rejected(self, tmp_path, monkeypatch, missing_key):
        import compose_runtime_e2e as cre
        self._redirect(tmp_path, monkeypatch)
        objects = self._objects()
        objects.pop(missing_key)
        with pytest.raises(ValueError, match="backup state missing"):
            cre._persist_backup_state(
                head_sha="sha-current", backup_id="bk-one", objects=objects,
            )

    def test_duplicate_object_key_rejected(self, tmp_path, monkeypatch):
        import compose_runtime_e2e as cre
        self._redirect(tmp_path, monkeypatch)
        objects = self._objects()
        objects["manifest"] = objects["payload"]
        with pytest.raises(ValueError, match="must be unique"):
            cre._persist_backup_state(
                head_sha="sha-current", backup_id="bk-one", objects=objects,
            )

    def test_tmp_file_not_treated_as_valid_state(self, tmp_path, monkeypatch):
        import compose_runtime_e2e as cre
        target = self._redirect(tmp_path, monkeypatch)
        target.with_suffix(".tmp").write_text("{}", encoding="utf-8")
        assert cre._load_backup_state(expected_head_sha="sha-current") == {}

    def test_legacy_bare_backup_id_is_rejected(self, tmp_path, monkeypatch):
        import compose_runtime_e2e as cre
        self._redirect(tmp_path, monkeypatch)
        with pytest.raises(ValueError, match="requires _persist_backup_state"):
            cre._persist_backup_id("bk-legacy")


class TestPersistRestoreState:
    """R83 Step 12/13: restore target state 必须绑定 current SHA 与 exact keys。"""

    def _redirect(self, tmp_path, monkeypatch):
        import compose_runtime_e2e as cre

        monkeypatch.setattr(cre, "_SECRETLESS_STATE_DIR", tmp_path)
        monkeypatch.setattr(cre, "_BACKUP_STATE_FILE", tmp_path / "backup-state.json")
        monkeypatch.setattr(cre, "_BACKUP_ID_FILE", tmp_path / "backup-state.json")
        monkeypatch.setattr(cre, "_RESTORE_STATE_FILE", tmp_path / "restore-state.json")
        monkeypatch.setattr(cre, "_get_source_sha", lambda: "sha-current")
        return cre

    def test_restore_state_roundtrip_and_exact_binding(self, tmp_path, monkeypatch):
        cre = self._redirect(tmp_path, monkeypatch)
        objects = TestPersistBackupState._objects()
        cre._persist_backup_state(
            head_sha="sha-current",
            backup_id="bk-one",
            objects=objects,
        )
        backup_state = cre._load_backup_state(expected_head_sha="sha-current")
        cre._persist_restore_state(
            head_sha="sha-current",
            backup_state=backup_state,
            restore_evidence={
                "operation_id": "op-one",
                "source_identity": "source-id",
                "target_identity": "target-id",
                "source_database": "tgjiema",
                "target_database": "tgjiema_restore_run_one",
                "target_dsn_sha256": "a" * 64,
            },
        )
        state = cre._load_restore_state(expected_head_sha="sha-current")
        assert state["backup_id"] == "bk-one"
        assert state["target_database"] == "tgjiema_restore_run_one"
        assert state["source_identity"] != state["target_identity"]

    def test_restore_state_rejects_identity_collision(self, tmp_path, monkeypatch):
        cre = self._redirect(tmp_path, monkeypatch)
        objects = TestPersistBackupState._objects()
        cre._persist_backup_state(
            head_sha="sha-current",
            backup_id="bk-one",
            objects=objects,
        )
        backup_state = cre._load_backup_state(expected_head_sha="sha-current")
        with pytest.raises(ValueError, match="identities must differ"):
            cre._persist_restore_state(
                head_sha="sha-current",
                backup_state=backup_state,
                restore_evidence={
                    "operation_id": "op-one",
                    "source_identity": "same-id",
                    "target_identity": "same-id",
                    "source_database": "tgjiema",
                    "target_database": "tgjiema_restore_run_one",
                    "target_dsn_sha256": "a" * 64,
                },
            )


# ════════════════════════════════════════════════════════════════
# B. _container_endpoint URL parser(§10.3)
# ════════════════════════════════════════════════════════════════


class TestContainerEndpoint:
    """R81 §10.3: _container_endpoint 使用 URL parser 只替换 hostname。"""

    def test_localhost_translated_to_minio(self):
        """http://localhost:9000 → http://minio:9000"""
        from compose_runtime_e2e import _container_endpoint
        assert _container_endpoint("http://localhost:9000") == "http://minio:9000"

    def test_127_0_0_1_translated_to_minio(self):
        """http://127.0.0.1:9000 → http://minio:9000"""
        from compose_runtime_e2e import _container_endpoint
        assert _container_endpoint("http://127.0.0.1:9000") == "http://minio:9000"

    def test_already_minio_unchanged(self):
        """已经是 http://minio:9000 原样返回。"""
        from compose_runtime_e2e import _container_endpoint
        assert _container_endpoint("http://minio:9000") == "http://minio:9000"

    def test_https_endpoint(self):
        """HTTPS localhost → HTTPS minio"""
        from compose_runtime_e2e import _container_endpoint
        assert _container_endpoint("https://localhost:9000") == "https://minio:9000"

    def test_preserves_path(self):
        """带 path 的 endpoint 只替换 hostname。"""
        from compose_runtime_e2e import _container_endpoint
        result = _container_endpoint("http://localhost:9000/bucket/key")
        assert result == "http://minio:9000/bucket/key"

    def test_preserves_query(self):
        """带 query 的 endpoint 只替换 hostname。"""
        from compose_runtime_e2e import _container_endpoint
        result = _container_endpoint("http://localhost:9000/?list-type=2&prefix=backups/")
        assert result == "http://minio:9000/?list-type=2&prefix=backups/"

    def test_preserves_fragment(self):
        """带 fragment 的 endpoint 只替换 hostname。"""
        from compose_runtime_e2e import _container_endpoint
        result = _container_endpoint("http://localhost:9000/path#fragment")
        assert result == "http://minio:9000/path#fragment"

    def test_localhost_in_path_not_replaced(self):
        """path 中出现 localhost 不被替换(关键回归测试)。"""
        from compose_runtime_e2e import _container_endpoint
        result = _container_endpoint("http://localhost:9000/path/localhost/file")
        assert result == "http://minio:9000/path/localhost/file", (
            f"path 中的 localhost 不应被替换,实际: {result}"
        )

    def test_localhost_in_query_not_replaced(self):
        """query 中出现 localhost 不被替换。"""
        from compose_runtime_e2e import _container_endpoint
        result = _container_endpoint("http://localhost:9000/?host=localhost&port=9000")
        assert result == "http://minio:9000/?host=localhost&port=9000", (
            f"query 中的 localhost 不应被替换,实际: {result}"
        )

    def test_empty_string(self):
        """空字符串原样返回。"""
        from compose_runtime_e2e import _container_endpoint
        assert _container_endpoint("") == ""

    def test_non_localhost_unchanged(self):
        """非 localhost endpoint 原样返回。"""
        from compose_runtime_e2e import _container_endpoint
        assert _container_endpoint("http://example.com:9000") == "http://example.com:9000"

    def test_no_port(self):
        """无端口的 localhost → minio(无端口)。"""
        from compose_runtime_e2e import _container_endpoint
        assert _container_endpoint("http://localhost") == "http://minio"

    def test_with_userinfo(self):
        """带 userinfo 的 endpoint 保留 userinfo。"""
        from compose_runtime_e2e import _container_endpoint
        result = _container_endpoint("http://user:pass@localhost:9000")
        assert result == "http://user:pass@minio:9000"


# ════════════════════════════════════════════════════════════════
# C. _env_to_compose_run_flags(§10.4)
# ════════════════════════════════════════════════════════════════


class TestEnvToComposeRunFlags:
    """R81 §10.4: _env_to_compose_run_flags 只传递变量名,不传递值。"""

    def test_flags_contain_only_names(self):
        """flags 只包含 -e KEY,不包含 -e KEY=VALUE。"""
        from compose_runtime_e2e import _env_to_compose_run_flags
        env = {"ACCESS_KEY": "secret123", "BUCKET": "test-bucket"}
        flags = _env_to_compose_run_flags(env)
        # 每个 flag 应是 -e 或变量名,不应包含 = value
        for flag in flags:
            if flag != "-e":
                assert "=" not in flag, (
                    f"flag 不应包含 = value,实际: {flag}"
                )
        # 应包含变量名
        assert "ACCESS_KEY" in flags
        assert "BUCKET" in flags

    def test_command_repr_no_value(self):
        """flags 的字符串表示不包含 value。"""
        from compose_runtime_e2e import _env_to_compose_run_flags
        env = {"SECRET_KEY": "super_secret_value_12345"}
        flags = _env_to_compose_run_flags(env)
        flags_str = " ".join(flags)
        assert "super_secret_value_12345" not in flags_str, (
            f"flags 字符串不应包含 value,实际: {flags_str}"
        )

    def test_stable_sorting(self):
        """稳定排序保证 artifact 可复现。"""
        from compose_runtime_e2e import _env_to_compose_run_flags
        env = {
            "ZEBRA_KEY": "val_z",
            "ALPHA_KEY": "val_a",
            "MIDDLE_KEY": "val_m",
        }
        flags1 = _env_to_compose_run_flags(env)
        flags2 = _env_to_compose_run_flags(env)
        assert flags1 == flags2, "相同输入应产生相同输出(可复现)"
        # 验证排序:ALPHA_KEY < MIDDLE_KEY < ZEBRA_KEY
        names = [f for f in flags1 if f != "-e"]
        assert names == sorted(names), (
            f"flags 应按字母排序,实际: {names}"
        )

    def test_empty_env(self):
        """空环境变量字典返回空 flags。"""
        from compose_runtime_e2e import _env_to_compose_run_flags
        assert _env_to_compose_run_flags({}) == []

    def test_undeclared_not_injected(self):
        """未声明的变量不注入。"""
        from compose_runtime_e2e import _env_to_compose_run_flags
        env = {"DECLARED_VAR": "value"}
        flags = _env_to_compose_run_flags(env)
        names = [f for f in flags if f != "-e"]
        assert "UNDECLARED_VAR" not in names
        assert "DECLARED_VAR" in names

    def test_flag_structure(self):
        """flags 结构为 [-e, KEY, -e, KEY, ...]"""
        from compose_runtime_e2e import _env_to_compose_run_flags
        env = {"A": "1", "B": "2"}
        flags = _env_to_compose_run_flags(env)
        # 长度应为 2 * len(env)
        assert len(flags) == 4
        # 偶数索引为 -e
        for i in range(0, len(flags), 2):
            assert flags[i] == "-e"
        # 奇数索引为变量名
        for i in range(1, len(flags), 2):
            assert flags[i] in env
