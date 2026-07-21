"""R70 Wave 7: restore writer 唯一化 — 薄 adapter + re-export 守卫测试。

R70 P0-07 整改背景:
    services/db_restore.py 曾经包含完整的本地 restore writer 实现
    (TABLE_PK, _restore_from_backup_data, _restore_crdb_tables,
    _restore_sqlite_tables_to_db, _safe_val, _sqlite_safe_val, etc.),
    与 services/restore_writer.py(R69 Wave 2 提取)形成双实现。

    违反 R70 P0-07「不得保留两份 restore writer」铁律:
      - 双实现可被绕过(攻击者 import db_restore 跳过 restore_writer 校验)
      - 维护漂移(两份实现可能不一致)
      - scanner 难以精确授权(同一函数名在两处定义)

R70 Wave 7 整改:
    1. services/db_restore.py 改为薄 CLI adapter:
       - 仅保留 CLI 入口(run_restore / main / _build_cli_decryptor)
       - 仅保留 deprecated legacy loader(get_latest_backup)
       - 删除所有本地 writer 实现
       - 通过 re-export 保持 ``from services.db_restore import TABLE_PK``
         等旧代码兼容
    2. services/restore_writer.py 成为唯一 writer 实现(单一事实源)
    3. backup_schema 符号(BACKUP_SCHEMA, validate_columns_for_table,
       get_table_source 等)也通过 re-export 暴露,保持测试访问路径
    4. scanner 白名单更新:移除 db_restore.py::_restore_from_backup_data
       条目,授权移至 restore_writer.py

测试覆盖矩阵:
    A. db_restore.py 不再定义 writer 实现(AST 扫描)— 5 个
       1. 不含 _restore_from_backup_data 定义
       2. 不含 _restore_crdb_tables 定义
       3. 不含 _restore_sqlite_tables_to_db 定义
       4. 不含 _restore_table 定义
       5. 不含 _safe_val / _sqlite_safe_val 定义
    B. db_restore.py re-export 完整性 — 5 个
       6. TABLE_PK 与 restore_writer.TABLE_PK 同一对象
       7. _restore_from_backup_data 与 restore_writer 同一对象
       8. _restore_crdb_tables 与 restore_writer 同一对象
       9. _restore_sqlite_tables_to_db 与 restore_writer 同一对象
       10. validate_columns_for_table 与 backup_schema 同一对象
    C. restore_writer.py 是唯一 writer 实现 — 3 个
       11. _restore_from_backup_data 定义在 restore_writer
       12. _restore_crdb_tables 定义在 restore_writer
       13. _restore_sqlite_tables_to_db 定义在 restore_writer
    D. .dockerignore 物理排除 db_restore.py — 2 个
       14. services/db_restore.py 被 .dockerignore 排除
       15. services/restore_writer.py 不被 .dockerignore 排除
    E. scanner 白名单更新正确 — 3 个
       16. PRECISE_WHITELIST 不含 db_restore.py::_restore_from_backup_data
       17. PRECISE_WHITELIST 含 restore_writer.py::_restore_from_backup_data
       18. PRECISE_WHITELIST 含 db_restore.py::run_restore(保留 CLI 入口授权)
    F. db_restore.py 保留 CLI / legacy loader 接口 — 2 个
       19. db_restore.run_restore 仍可被 import(委托 strict service)
       20. db_restore.main 仍可被 import(argparse 入口)
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# 测试环境兼容(mock telegram / asyncpg / httpx,避免环境依赖)
import sys
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ════════════════════════════════════════════════════════════════
# 辅助:解析模块 AST,提取顶层函数定义名
# ════════════════════════════════════════════════════════════════


def _get_module_func_defs(file_path: Path) -> set[str]:
    """解析 .py 文件 AST,返回所有顶层(async)函数定义名。"""
    src = file_path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(file_path))
    funcs: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.add(node.name)
    return funcs


def _get_module_imported_names(file_path: Path) -> dict[str, str]:
    """解析 .py 文件 AST,返回 {导入符号: 来源模块} 字典。

    覆盖 ``from X import a, b`` 与 ``from X import a as b``。
    """
    src = file_path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(file_path))
    imported: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                name = alias.asname or alias.name
                imported[name] = node.module
    return imported


# ════════════════════════════════════════════════════════════════
# A. db_restore.py 不再定义 writer 实现(AST 扫描)
# ════════════════════════════════════════════════════════════════


class TestDbRestoreNoLocalWriterDefs:
    """R70 Wave 7: db_restore.py AST 不得含本地 writer 实现。"""

    def test_no_local_restore_from_backup_data_definition(self):
        """db_restore.py 不应含 ``_restore_from_backup_data`` 的本地定义。

        R70 Wave 7: 该函数已迁移至 services/restore_writer.py,
        本模块通过 re-export 暴露以保持向后兼容。
        """
        funcs = _get_module_func_defs(REPO_ROOT / "services" / "db_restore.py")
        assert "_restore_from_backup_data" not in funcs, (
            "R70 Wave 7: db_restore.py 不应定义 _restore_from_backup_data"
            "(应通过 re-export 从 services.restore_writer 导入)"
        )

    def test_no_local_restore_crdb_tables_definition(self):
        """db_restore.py 不应含 ``_restore_crdb_tables`` 的本地定义。"""
        funcs = _get_module_func_defs(REPO_ROOT / "services" / "db_restore.py")
        assert "_restore_crdb_tables" not in funcs, (
            "R70 Wave 7: db_restore.py 不应定义 _restore_crdb_tables"
            "(应通过 re-export 从 services.restore_writer 导入)"
        )

    def test_no_local_restore_sqlite_tables_to_db_definition(self):
        """db_restore.py 不应含 ``_restore_sqlite_tables_to_db`` 的本地定义。"""
        funcs = _get_module_func_defs(REPO_ROOT / "services" / "db_restore.py")
        assert "_restore_sqlite_tables_to_db" not in funcs, (
            "R70 Wave 7: db_restore.py 不应定义 _restore_sqlite_tables_to_db"
            "(应通过 re-export 从 services.restore_writer 导入)"
        )

    def test_no_local_restore_table_definition(self):
        """db_restore.py 不应含 ``_restore_table`` 的本地定义(私有表级写入器)。"""
        funcs = _get_module_func_defs(REPO_ROOT / "services" / "db_restore.py")
        assert "_restore_table" not in funcs, (
            "R70 Wave 7: db_restore.py 不应定义 _restore_table"
            "(应通过 re-export 从 services.restore_writer 导入)"
        )

    def test_no_local_safe_val_definitions(self):
        """db_restore.py 不应含 ``_safe_val`` / ``_sqlite_safe_val`` 本地定义。"""
        funcs = _get_module_func_defs(REPO_ROOT / "services" / "db_restore.py")
        assert "_safe_val" not in funcs, (
            "R70 Wave 7: db_restore.py 不应定义 _safe_val"
        )
        assert "_sqlite_safe_val" not in funcs, (
            "R70 Wave 7: db_restore.py 不应定义 _sqlite_safe_val"
        )


# ════════════════════════════════════════════════════════════════
# B. db_restore.py re-export 完整性
# ════════════════════════════════════════════════════════════════


class TestDbRestoreReexportIntegrity:
    """R70 Wave 7: db_restore.py 通过 re-export 暴露 writer 符号,且指向同一对象。"""

    def test_table_pk_same_object_as_restore_writer(self):
        """db_restore.TABLE_PK 与 restore_writer.TABLE_PK 是同一对象(单一事实源)。"""
        from services.db_restore import TABLE_PK as db_table_pk
        from services.restore_writer import TABLE_PK as rw_table_pk
        assert db_table_pk is rw_table_pk, (
            "R70 Wave 7: db_restore.TABLE_PK 必须与 restore_writer.TABLE_PK 同一对象"
            "(re-export,而非复制)"
        )

    def test_restore_from_backup_data_same_object(self):
        """db_restore._restore_from_backup_data 与 restore_writer 同一对象。"""
        from services.db_restore import _restore_from_backup_data as db_fn
        from services.restore_writer import _restore_from_backup_data as rw_fn
        assert db_fn is rw_fn, (
            "R70 Wave 7: db_restore._restore_from_backup_data 必须与 "
            "restore_writer._restore_from_backup_data 同一对象"
        )

    def test_restore_crdb_tables_same_object(self):
        """db_restore._restore_crdb_tables 与 restore_writer 同一对象。"""
        from services.db_restore import _restore_crdb_tables as db_fn
        from services.restore_writer import _restore_crdb_tables as rw_fn
        assert db_fn is rw_fn, (
            "R70 Wave 7: db_restore._restore_crdb_tables 必须与 "
            "restore_writer._restore_crdb_tables 同一对象"
        )

    def test_restore_sqlite_tables_to_db_same_object(self):
        """db_restore._restore_sqlite_tables_to_db 与 restore_writer 同一对象。"""
        from services.db_restore import _restore_sqlite_tables_to_db as db_fn
        from services.restore_writer import _restore_sqlite_tables_to_db as rw_fn
        assert db_fn is rw_fn, (
            "R70 Wave 7: db_restore._restore_sqlite_tables_to_db 必须与 "
            "restore_writer._restore_sqlite_tables_to_db 同一对象"
        )

    def test_validate_columns_for_table_same_object_as_backup_schema(self):
        """db_restore.validate_columns_for_table 与 backup_schema 同一对象。"""
        from services.db_restore import validate_columns_for_table as db_fn
        from services.backup_schema import validate_columns_for_table as bs_fn
        assert db_fn is bs_fn, (
            "R70 Wave 7: db_restore.validate_columns_for_table 必须与 "
            "backup_schema.validate_columns_for_table 同一对象"
        )


# ════════════════════════════════════════════════════════════════
# C. restore_writer.py 是唯一 writer 实现
# ════════════════════════════════════════════════════════════════


class TestRestoreWriterIsSingleSourceOfTruth:
    """R70 Wave 7: restore_writer.py 必须是 writer 唯一定义点。"""

    def test_restore_writer_defines_restore_from_backup_data(self):
        """restore_writer.py 必须定义 _restore_from_backup_data(唯一实现)。"""
        funcs = _get_module_func_defs(REPO_ROOT / "services" / "restore_writer.py")
        assert "_restore_from_backup_data" in funcs, (
            "R70 Wave 7: restore_writer.py 必须定义 _restore_from_backup_data"
            "(单一事实源)"
        )

    def test_restore_writer_defines_restore_crdb_tables(self):
        """restore_writer.py 必须定义 _restore_crdb_tables。"""
        funcs = _get_module_func_defs(REPO_ROOT / "services" / "restore_writer.py")
        assert "_restore_crdb_tables" in funcs, (
            "R70 Wave 7: restore_writer.py 必须定义 _restore_crdb_tables"
        )

    def test_restore_writer_defines_restore_sqlite_tables_to_db(self):
        """restore_writer.py 必须定义 _restore_sqlite_tables_to_db。"""
        funcs = _get_module_func_defs(REPO_ROOT / "services" / "restore_writer.py")
        assert "_restore_sqlite_tables_to_db" in funcs, (
            "R70 Wave 7: restore_writer.py 必须定义 _restore_sqlite_tables_to_db"
        )


# ════════════════════════════════════════════════════════════════
# D. .dockerignore 物理排除 db_restore.py
# ════════════════════════════════════════════════════════════════


class TestDockerignoreExclusion:
    """R70 Wave 7: 生产镜像物理排除 db_restore.py(CLI 入口),
    但保留 restore_writer.py(必需的生产 runtime 模块)。"""

    def test_dockerignore_excludes_db_restore(self):
        """.dockerignore 必须排除 services/db_restore.py。"""
        content = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
        assert "services/db_restore.py" in content, (
            "R70 Wave 7: .dockerignore 必须排除 services/db_restore.py"
            "(CLI 入口不应进入生产镜像,生产恢复必须走 RestoreOrchestrator)"
        )

    def test_dockerignore_does_not_exclude_restore_writer(self):
        """.dockerignore 不得排除 services/restore_writer.py(必需的生产 runtime 模块)。"""
        content = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
        # 不得有排除 services/restore_writer.py 的规则
        # (排除 services/db_restore.py 不能误伤 restore_writer.py)
        for line in content.splitlines():
            line_clean = line.strip()
            if not line_clean or line_clean.startswith("#"):
                continue
            assert "services/restore_writer.py" not in line_clean, (
                f"R70 Wave 7: .dockerignore 不得排除 services/restore_writer.py"
                f"(必需的生产 runtime 模块),违规行: {line_clean}"
            )


# ════════════════════════════════════════════════════════════════
# E. scanner 白名单更新正确
# ════════════════════════════════════════════════════════════════


class TestScannerWhitelistUpdated:
    """R70 Wave 7: scanner PRECISE_WHITELIST 反映新架构。"""

    def test_whitelist_no_db_restore_restore_from_backup_data(self):
        """PRECISE_WHITELIST 不应含 db_restore.py::_restore_from_backup_data 条目。

        R70 Wave 7: 该函数已不在 db_restore.py 中定义,re-export 自 restore_writer。
        若仍保留旧条目,scanner 会因找不到定义而误报。
        """
        from scripts.check_restore_no_legacy_writer import PRECISE_WHITELIST
        for entry in PRECISE_WHITELIST:
            if entry["file"] == "services/db_restore.py":
                assert entry["function"] != "_restore_from_backup_data", (
                    "R70 Wave 7: PRECISE_WHITELIST 不应含 "
                    "db_restore.py::_restore_from_backup_data 条目"
                    "(该函数已 re-export,实际定义在 restore_writer.py)"
                )

    def test_whitelist_contains_restore_writer_restore_from_backup_data(self):
        """PRECISE_WHITELIST 必须含 restore_writer.py::_restore_from_backup_data。

        R70 Wave 7: 实际定义所在,授权条目应迁移至此。
        """
        from scripts.check_restore_no_legacy_writer import PRECISE_WHITELIST
        found = False
        for entry in PRECISE_WHITELIST:
            if (entry["file"] == "services/restore_writer.py"
                and entry["function"] == "_restore_from_backup_data"):
                found = True
                # 验证 allowed_callees 含子写入器
                assert "_restore_crdb_tables" in entry["allowed_callees"], (
                    "restore_writer.py::_restore_from_backup_data 应允许调用"
                    " _restore_crdb_tables"
                )
                assert "_restore_sqlite_tables_to_db" in entry["allowed_callees"], (
                    "restore_writer.py::_restore_from_backup_data 应允许调用"
                    " _restore_sqlite_tables_to_db"
                )
                break
        assert found, (
            "R70 Wave 7: PRECISE_WHITELIST 必须含 "
            "restore_writer.py::_restore_from_backup_data 条目"
        )

    def test_whitelist_retains_db_restore_run_restore(self):
        """PRECISE_WHITELIST 必须保留 db_restore.py::run_restore(CLI 入口授权)。

        R70 Wave 7: run_restore 仍在 db_restore.py 中定义(CLI 入口),
        其 allowed_callees 应为 validate_and_restore_backup_strict(委托 strict service)。
        """
        from scripts.check_restore_no_legacy_writer import PRECISE_WHITELIST
        found = False
        for entry in PRECISE_WHITELIST:
            if (entry["file"] == "services/db_restore.py"
                and entry["function"] == "run_restore"):
                found = True
                assert "validate_and_restore_backup_strict" in entry["allowed_callees"], (
                    "db_restore.py::run_restore 应允许调用 "
                    "validate_and_restore_backup_strict(strict service)"
                )
                break
        assert found, (
            "R70 Wave 7: PRECISE_WHITELIST 必须保留 "
            "db_restore.py::run_restore 条目(CLI 入口委托 strict service)"
        )


# ════════════════════════════════════════════════════════════════
# F. db_restore.py 保留 CLI / legacy loader 接口
# ════════════════════════════════════════════════════════════════


class TestDbRestoreRetainsCliInterface:
    """R70 Wave 7: db_restore.py 保留 CLI 入口(run_restore / main)
    与 deprecated legacy loader(get_latest_backup)。"""

    def test_db_restore_defines_run_restore(self):
        """db_restore.py 必须定义 run_restore(CLI 入口,委托 strict service)。"""
        funcs = _get_module_func_defs(REPO_ROOT / "services" / "db_restore.py")
        assert "run_restore" in funcs, (
            "R70 Wave 7: db_restore.py 必须保留 run_restore CLI 入口"
            "(委托 validate_and_restore_backup_strict,生产环境被 capability-sealed)"
        )

    def test_db_restore_defines_main(self):
        """db_restore.py 必须定义 main(argparse 入口)。"""
        funcs = _get_module_func_defs(REPO_ROOT / "services" / "db_restore.py")
        assert "main" in funcs, (
            "R70 Wave 7: db_restore.py 必须保留 main argparse 入口"
        )


# ════════════════════════════════════════════════════════════════
# G. scanner 整仓静态扫描通过(Wave 7 后无违规)
# ════════════════════════════════════════════════════════════════


class TestScannerPassesAfterWave7:
    """R70 Wave 7: scanner 在 Wave 7 重构后整仓静态扫描通过。"""

    def test_scanner_default_mode_passes(self):
        """scanner 默认模式(legacy writer 私有 + CLI 入口)无违规。"""
        import subprocess
        result = subprocess.run(
            ["python", "scripts/check_restore_no_legacy_writer.py"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, (
            f"R70 Wave 7: scanner 默认模式应有 0 违规,实际 returncode={result.returncode}\n"
            f"stdout: {result.stdout[-2000:]}\n"
            f"stderr: {result.stderr[-2000:]}"
        )
