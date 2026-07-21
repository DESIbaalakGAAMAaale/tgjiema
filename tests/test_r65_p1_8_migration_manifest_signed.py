"""R65 P1-08: migration manifest 001-007 全集合 + 签名链完整性测试。

审计背景(R65 终审报告 P1-08):
    Migration 005/006/007 需纳入签名 release manifest。
    本轮 Release 在生成 release manifest 前已经失败,因此新增迁移尚无已签名、
    同 digest 的生产证据。必须验证 001–007 全集合、顺序、hash、前驱、
    DDL version 和回滚策略。

测试覆盖矩阵(10 个场景):
  内容完整性(5 个):
    1. Manifest 包含 001-007 全集合
    2. predecessor 链完整(001 ← 002 ← ... ← 007)
    3. 每个 SQL 文件 SHA-256 与 manifest 一致
    4. 每个 migration 的 ddl_version 单调非递减
    5. 每个 migration 都有非空 rollback_strategy

  check_migration_manifest.py --strict 行为(5 个):
    6. 有效 manifest → exit 0
    7. 缺少 migration → exit 1
    8. SHA-256 不匹配 → exit 1
    9. predecessor 链断裂 → exit 1
    10. rollback_strategy 缺失 → exit 1

测试策略:
    - 内容完整性测试:直接读取真实 manifest,验证字段
    - 行为测试:通过 subprocess 调用 check 脚本,使用 --manifest 指向临时副本
    - 临时副本在 tmp_path 中创建,SQL 文件仍引用真实 database/migrations/
      目录(check 脚本默认从 REPO_ROOT/database/migrations/ 查找 SQL 文件)
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# 测试环境兼容(mock telegram 库,避免 ImportError)
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO_ROOT / "database" / "migrations"
MANIFEST_PATH = MIGRATIONS_DIR / "migration-manifest.json"
CHECK_SCRIPT = REPO_ROOT / "scripts" / "check_migration_manifest.py"

# R65 P1-08: 期望的 migration_id 集合(001-007 全集合)
EXPECTED_MIGRATION_IDS: list[str] = [f"{i:03d}" for i in range(1, 8)]


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════

def _load_manifest() -> dict:
    """加载真实 manifest。"""
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _file_sha256(path: Path) -> str:
    """计算文件 SHA-256(十六进制小写)。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_manifest_copy(manifest_data: dict, tmp_path: Path) -> Path:
    """将 manifest 数据写入临时文件,返回路径。

    用于 check 脚本行为测试 — 测试修改 manifest 副本后调用 check 脚本。
    """
    tmp_manifest = tmp_path / "migration-manifest.json"
    tmp_manifest.write_text(
        json.dumps(manifest_data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return tmp_manifest


def _run_check_strict(manifest_path: Path | None = None) -> subprocess.CompletedProcess:
    """调用 check_migration_manifest.py --strict,返回 CompletedProcess。

    Args:
        manifest_path: 指定 manifest 路径(None 表示使用默认真实 manifest)
    """
    cmd: list[str] = [sys.executable, str(CHECK_SCRIPT), "--strict"]
    if manifest_path is not None:
        cmd += ["--manifest", str(manifest_path)]
    return subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )


# ════════════════════════════════════════════════════════════════
# A. 内容完整性测试(5 个场景)
# ════════════════════════════════════════════════════════════════

class TestManifestContent:
    """manifest 内容完整性测试 — 验证 001-007 全集合字段。"""

    def test_manifest_includes_all_migrations_001_to_007(self):
        """场景 1: manifest 必须包含 001-007 全集合。"""
        data = _load_manifest()
        ids = [str(e.get("migration_id", "")).strip() for e in data["migrations"]]
        for expected in EXPECTED_MIGRATION_IDS:
            assert expected in ids, (
                f"manifest 缺少 migration_id={expected} "
                f"(期望 001-007 全集合,实际: {sorted(ids)})"
            )

    def test_predecessor_chain_correct(self):
        """场景 2: predecessor 链完整(001 ← 002 ← ... ← 007)。

        第一个 migration (001) 的 predecessor 必须为 null;
        其余 migration_id=N 的 predecessor 必须等于前一个 migration_id。
        """
        data = _load_manifest()
        # 按 migration_id 排序
        entries = sorted(
            (e for e in data["migrations"] if str(e.get("migration_id", "")).strip()),
            key=lambda e: str(e.get("migration_id", "")),
        )
        # 期望 8 个 migration (R67 P1-06 新增 008_restore_switch_reconciler.sql)
        assert len(entries) == 8, f"期望 8 个 migration,实际 {len(entries)}"
        # 001 是首个,predecessor 必须为 null
        assert entries[0]["migration_id"] == "001"
        first_pred = entries[0].get("predecessor")
        assert first_pred is None or (
            isinstance(first_pred, str) and first_pred.strip().lower() in ("", "null")
        ), f"001 的 predecessor 应为 null,实际: {first_pred!r}"
        # 其余 migration 的 predecessor 必须等于前一个的 migration_id
        for i in range(1, len(entries)):
            current = entries[i]
            previous = entries[i - 1]
            expected_pred = previous["migration_id"]
            actual_pred = current.get("predecessor")
            assert str(actual_pred) == str(expected_pred), (
                f"{current['migration_id']} 的 predecessor 应为 '{expected_pred}',"
                f"实际: '{actual_pred}' (predecessor 链断裂)"
            )

    def test_sha256_matches_disk_files(self):
        """场景 3: 每个 migration SQL 文件 SHA-256 与 manifest 一致。

        防止 manifest 被篡改后与磁盘文件不一致(fail-closed on tampering)。
        """
        data = _load_manifest()
        for entry in data["migrations"]:
            mid = entry.get("migration_id", "?")
            filename = entry.get("filename") or entry.get("version")
            assert filename, f"migration_id={mid} 缺少 filename / version 字段"
            sql_path = MIGRATIONS_DIR / filename
            assert sql_path.exists(), f"SQL 文件不存在: {sql_path}"
            expected_sha = str(entry["sha256"]).strip().lower()
            actual_sha = _file_sha256(sql_path)
            assert actual_sha == expected_sha, (
                f"{filename} (migration_id={mid}) SHA-256 不匹配: "
                f"manifest={expected_sha[:16]}... actual={actual_sha[:16]}..."
            )

    def test_ddl_version_monotonic_non_decreasing(self):
        """场景 4: 每个 migration 的 ddl_version 单调非递减。

        允许同值(列扩展/新表/数据补丁不 bump DDL_VERSION);
        禁止下降(DDL version 下降意味着 schema 退化)。
        """
        data = _load_manifest()
        entries = sorted(
            (e for e in data["migrations"] if str(e.get("migration_id", "")).strip()),
            key=lambda e: str(e.get("migration_id", "")),
        )
        last_ddl_version: int | None = None
        for entry in entries:
            mid = entry.get("migration_id", "?")
            ddl_version = entry.get("ddl_version")
            assert ddl_version is not None, (
                f"migration_id={mid} 缺少 ddl_version 字段"
            )
            ddl_version_int = int(ddl_version)
            if last_ddl_version is not None:
                assert ddl_version_int >= last_ddl_version, (
                    f"migration_id={mid} ddl_version={ddl_version_int} "
                    f"< 前一个 ddl_version={last_ddl_version} (非单调非递减)"
                )
            last_ddl_version = ddl_version_int

    def test_rollback_strategy_non_empty(self):
        """场景 5: 每个 migration 都有非空 rollback_strategy。

        回滚策略可以是 SQL 路径或描述性文本,但必须存在(不允许空)。
        """
        data = _load_manifest()
        for entry in data["migrations"]:
            mid = entry.get("migration_id", "?")
            rollback = entry.get("rollback_strategy")
            assert rollback is not None, (
                f"migration_id={mid} 缺少 rollback_strategy 字段"
            )
            assert isinstance(rollback, str), (
                f"migration_id={mid} rollback_strategy 不是字符串: "
                f"{type(rollback).__name__}"
            )
            assert rollback.strip(), (
                f"migration_id={mid} rollback_strategy 为空字符串"
            )


# ════════════════════════════════════════════════════════════════
# B. check_migration_manifest.py --strict 行为测试(5 个场景)
# ════════════════════════════════════════════════════════════════

class TestCheckScriptStrict:
    """check_migration_manifest.py --strict 严格模式行为测试。"""

    def test_strict_exits_0_on_valid_manifest(self):
        """场景 6: 有效 manifest → exit 0。

        真实 manifest 应通过 strict 校验(签名缺失仅 WARN 不阻断)。
        """
        result = _run_check_strict(MANIFEST_PATH)
        assert result.returncode == 0, (
            f"有效 manifest 应 exit 0,实际 {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_strict_exits_1_on_missing_migration(self, tmp_path):
        """场景 7: 缺少 migration → exit 1。

        移除 migration_id=007 后,strict 模式应检测到 001-007 全集合缺失。
        """
        data = _load_manifest()
        # 移除 007
        data["migrations"] = [
            e for e in data["migrations"]
            if str(e.get("migration_id", "")).strip() != "007"
        ]
        tmp_manifest = _write_manifest_copy(data, tmp_path)
        result = _run_check_strict(tmp_manifest)
        assert result.returncode == 1, (
            f"缺少 migration 应 exit 1,实际 {result.returncode}\n"
            f"stderr: {result.stderr}"
        )
        # stderr 应包含提示
        assert "007" in result.stderr or "migration_id" in result.stderr, (
            f"stderr 应提示缺少 007,实际: {result.stderr}"
        )

    def test_strict_exits_1_on_wrong_sha256(self, tmp_path):
        """场景 8: SHA-256 不匹配 → exit 1。

        修改 005 的 sha256 为错误值,strict 模式应检测到 SHA 不匹配。
        """
        data = _load_manifest()
        for entry in data["migrations"]:
            if str(entry.get("migration_id", "")).strip() == "005":
                entry["sha256"] = "0" * 64  # 故意错误的 SHA-256
                break
        tmp_manifest = _write_manifest_copy(data, tmp_path)
        result = _run_check_strict(tmp_manifest)
        assert result.returncode == 1, (
            f"SHA-256 不匹配应 exit 1,实际 {result.returncode}\n"
            f"stderr: {result.stderr}"
        )
        assert "SHA-256" in result.stderr or "sha256" in result.stderr.lower(), (
            f"stderr 应提示 SHA-256 不匹配,实际: {result.stderr}"
        )

    def test_strict_exits_1_on_broken_predecessor_chain(self, tmp_path):
        """场景 9: predecessor 链断裂 → exit 1。

        修改 005 的 predecessor 从 '004' 为 '002',strict 模式应检测到链断裂。
        """
        data = _load_manifest()
        for entry in data["migrations"]:
            if str(entry.get("migration_id", "")).strip() == "005":
                entry["predecessor"] = "002"  # 应为 "004"
                break
        tmp_manifest = _write_manifest_copy(data, tmp_path)
        result = _run_check_strict(tmp_manifest)
        assert result.returncode == 1, (
            f"predecessor 链断裂应 exit 1,实际 {result.returncode}\n"
            f"stderr: {result.stderr}"
        )
        assert "predecessor" in result.stderr.lower(), (
            f"stderr 应提示 predecessor 问题,实际: {result.stderr}"
        )

    def test_strict_exits_1_on_missing_rollback_strategy(self, tmp_path):
        """场景 10: rollback_strategy 缺失 → exit 1。

        清空 003 的 rollback_strategy,strict 模式应检测到缺失。
        """
        data = _load_manifest()
        for entry in data["migrations"]:
            if str(entry.get("migration_id", "")).strip() == "003":
                entry["rollback_strategy"] = ""  # 故意清空
                break
        tmp_manifest = _write_manifest_copy(data, tmp_path)
        result = _run_check_strict(tmp_manifest)
        assert result.returncode == 1, (
            f"rollback_strategy 缺失应 exit 1,实际 {result.returncode}\n"
            f"stderr: {result.stderr}"
        )
        assert "rollback_strategy" in result.stderr.lower(), (
            f"stderr 应提示 rollback_strategy 缺失,实际: {result.stderr}"
        )
