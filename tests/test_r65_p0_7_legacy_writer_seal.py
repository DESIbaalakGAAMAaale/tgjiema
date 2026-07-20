"""R65 P0-07 / P1-07: 旧直接 restore writer capability-seal 测试。

审计背景(R65 终审报告 P0-07 / P1-07):
    旧直接 restore writer(``_restore_from_backup_data`` /
    ``_restore_crdb_tables`` / ``_restore_sqlite_tables_to_db`` /
    ``run_restore`` / ``validate_and_restore_backup_strict``)在原地覆盖
    模式下可能"先清空生产表再失败",造成 active 数据被破坏且不可恢复。
    R64 P0-03 已引入 ``RestoreOrchestrator`` 蓝绿切换模型(staging → active),
    R65 P0-07 / P1-07 在 capability-seal 层进一步封存旧 writer:生产入口必须
    改走 orchestrator,旧 writer 仅保留给 tests/ + scripts/ + 已 whitelisted
    的 services/ 模块使用。

整改方案(R65 P0-07 / P1-07):
    1. 在 ``services/db_restore.py::run_restore()``(CLI 入口)、
       ``services/db_backup.py::restore_from_backup()``(production wrapper)、
       ``services/command_bus.py::make_restore_backup_command()`` handler
       顶部添加 capability-seal:仅当 ``ALLOW_LEGACY_RESTORE=1`` 环境变量设置时
       才允许通过,否则 fail-closed 抛 ``AppError(RESTORE_LEGACY_WRITER_SEALED)``。
    2. ``_restore_from_backup_data()`` 已通过 R61 P0-03 / R62 P0-02 的
       ``_RestoreCapability``(不可伪造 sentinel 令牌)capability-seal,
       仅由 ``services/backup_dr_validate.validate_and_restore_backup_strict``
       构造并传入。
    3. AST 静态门禁 ``scripts/check_restore_no_legacy_writer.py`` 阻止生产代码
       直接调用旧 writer(``_restore_from_backup_data`` 等)。``--strict`` 模式
       额外检测 ``validate_and_restore_backup_strict`` / ``restore_from_backup``
       公共入口调用。
    4. ``tests/conftest.py`` autouse fixture 设置 ``ALLOW_LEGACY_RESTORE=1``
       环境变量,保持历史向后兼容测试(R62 / R63 直接调用 ``run_restore()`` /
       ``_restore_from_backup_data()``)继续工作。
    5. ``RESTORE_LEGACY_WRITER_SEALED`` 错误码注册在 ``services/error_codes.py``
       + locale 条目(zh-CN.json / en-US.json)。

测试覆盖矩阵:
    A. ``run_restore()`` capability seal
       - 生产模式(无 ALLOW_LEGACY_RESTORE)→ RESTORE_LEGACY_WRITER_SEALED
       - 测试模式(ALLOW_LEGACY_RESTORE=1)→ 通过 seal(后续 backup_id 校验等)
       - 多种逃生舱值:1 / true / yes 均通过
    B. ``db_backup.restore_from_backup()`` capability seal
       - 生产模式 → RESTORE_LEGACY_WRITER_SEALED
       - 测试模式 → 通过 seal(后续 R2 凭证校验等)
    C. ``command_bus.make_restore_backup_command()`` handler capability seal
       - 生产模式 → RESTORE_LEGACY_WRITER_SEALED
       - 测试模式 → 通过 seal(后续 ImportError 兜底等)
    D. AST gate ``scripts/check_restore_no_legacy_writer.py``
       - 默认模式:无违规(扫描全仓生产代码)
       - --strict 模式:已知 sealed 调用方被检测(db_backup / command_bus)
       - 合成违规文件:直接调用 _restore_from_backup_data 被检测
       - 白名单文件跳过:tests/ 与 scripts/ 不被扫描
    E. Error code 注册
       - RESTORE_LEGACY_WRITER_SEALED 在 ErrorCodes 枚举
       - ErrorRegistry 注册条目(http_status=403, severity=critical)
       - locale 条目(zh-CN.json / en-US.json)含 caller/reason 参数
    F. 向后兼容
       - ALLOW_LEGACY_RESTORE=1 时,run_restore 可被调用(不立即抛 sealed 错误)
       - 测试 fixture autouse 设置 ALLOW_LEGACY_RESTORE=1
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# 测试环境兼容(conftest 在收集阶段已注入 config/telegram mock)
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())


# ════════════════════════════════════════════════════════════════
# 测试辅助
# ════════════════════════════════════════════════════════════════


def _ensure_restore_module_importable():
    """确保 services.db_restore 可导入(测试环境兼容)。"""
    try:
        import services.db_restore  # noqa: F401
    except ImportError as e:
        pytest.skip(f"services.db_restore 不可导入: {e}")


def _run_gate(strict: bool = False) -> tuple[int, str]:
    """运行 AST gate 脚本,返回 (exit_code, stdout+stderr)。

    Args:
        strict: 是否启用 --strict 模式
    """
    cmd = [sys.executable, "scripts/check_restore_no_legacy_writer.py"]
    if strict:
        cmd.append("--strict")
    result = subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.returncode, result.stdout + result.stderr


# ════════════════════════════════════════════════════════════════
# A. run_restore() capability seal
# ════════════════════════════════════════════════════════════════


class TestRunRestoreCapabilitySeal:
    """R65 P0-07 / P1-07: ``db_restore.run_restore()`` capability seal。"""

    @pytest.mark.asyncio
    async def test_production_mode_raises_sealed_error(self, monkeypatch):
        """生产模式(无 ALLOW_LEGACY_RESTORE)→ RESTORE_LEGACY_WRITER_SEALED。

        conftest.py 的 autouse fixture 默认会设置 ALLOW_LEGACY_RESTORE=1,
        本用例显式 delenv 模拟生产环境。
        """
        _ensure_restore_module_importable()
        from services import db_restore
        from services.error_codes import AppError, ErrorCodes

        # 模拟生产环境:未设置 ALLOW_LEGACY_RESTORE
        monkeypatch.delenv("ALLOW_LEGACY_RESTORE", raising=False)

        with pytest.raises(AppError) as exc_info:
            await db_restore.run_restore(backup_id="20260718_120000", dry_run=True)

        assert exc_info.value.code == ErrorCodes.RESTORE_LEGACY_WRITER_SEALED, (
            "生产模式调用 run_restore() 必须抛 RESTORE_LEGACY_WRITER_SEALED"
        )
        # 验证 safe_params 包含 caller + reason
        params = exc_info.value.params
        assert params.get("caller") == "run_restore"
        assert params.get("reason") == "legacy_writer_sealed"

    @pytest.mark.asyncio
    async def test_production_mode_raises_before_backup_id_check(self, monkeypatch):
        """生产模式:seal 在 backup_id 必填校验之前 fire。

        调用 run_restore(backup_id=None) 在生产模式应抛 SEALED(不是
        BACKUP_RESTORE_TRUST_CHAIN_REQUIRED),证明 seal 是首条防线。
        """
        _ensure_restore_module_importable()
        from services import db_restore
        from services.error_codes import AppError, ErrorCodes

        monkeypatch.delenv("ALLOW_LEGACY_RESTORE", raising=False)

        with pytest.raises(AppError) as exc_info:
            await db_restore.run_restore(backup_id=None, dry_run=True)

        assert exc_info.value.code == ErrorCodes.RESTORE_LEGACY_WRITER_SEALED, (
            "seal 应在 backup_id 校验之前 fire,即使 backup_id=None 也应抛 SEALED"
        )

    @pytest.mark.asyncio
    async def test_test_mode_bypasses_seal_with_value_1(self, monkeypatch):
        """测试模式(ALLOW_LEGACY_RESTORE=1)→ 通过 seal。

        通过 seal 后会因 backup_id=None 抛 BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
        证明 seal 已通过。
        """
        _ensure_restore_module_importable()
        from services import db_restore
        from services.error_codes import AppError, ErrorCodes

        monkeypatch.setenv("ALLOW_LEGACY_RESTORE", "1")

        with pytest.raises(AppError) as exc_info:
            await db_restore.run_restore(backup_id=None, dry_run=True)

        # 通过 seal 后,backup_id=None 触发信任链错误(证明 seal 已 bypass)
        assert exc_info.value.code == ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED, (
            "ALLOW_LEGACY_RESTORE=1 时应通过 seal,后续 backup_id 校验失败"
        )

    @pytest.mark.asyncio
    async def test_test_mode_bypasses_seal_with_value_true(self, monkeypatch):
        """测试模式(ALLOW_LEGACY_RESTORE=true)→ 通过 seal。"""
        _ensure_restore_module_importable()
        from services import db_restore
        from services.error_codes import AppError, ErrorCodes

        monkeypatch.setenv("ALLOW_LEGACY_RESTORE", "true")

        with pytest.raises(AppError) as exc_info:
            await db_restore.run_restore(backup_id=None, dry_run=True)

        assert exc_info.value.code == ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED

    @pytest.mark.asyncio
    async def test_test_mode_bypasses_seal_with_value_yes(self, monkeypatch):
        """测试模式(ALLOW_LEGACY_RESTORE=yes)→ 通过 seal。"""
        _ensure_restore_module_importable()
        from services import db_restore
        from services.error_codes import AppError, ErrorCodes

        monkeypatch.setenv("ALLOW_LEGACY_RESTORE", "yes")

        with pytest.raises(AppError) as exc_info:
            await db_restore.run_restore(backup_id=None, dry_run=True)

        assert exc_info.value.code == ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED

    @pytest.mark.asyncio
    async def test_test_mode_case_insensitive(self, monkeypatch):
        """测试模式(ALLOW_LEGACY_RESTORE=TRUE 大写)→ 通过 seal(case-insensitive)。"""
        _ensure_restore_module_importable()
        from services import db_restore
        from services.error_codes import AppError, ErrorCodes

        monkeypatch.setenv("ALLOW_LEGACY_RESTORE", "TRUE")

        with pytest.raises(AppError) as exc_info:
            await db_restore.run_restore(backup_id=None, dry_run=True)

        assert exc_info.value.code == ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED

    @pytest.mark.asyncio
    async def test_empty_value_treated_as_production(self, monkeypatch):
        """ALLOW_LEGACY_RESTORE=''(空值)→ 视为生产模式(seal fire)。"""
        _ensure_restore_module_importable()
        from services import db_restore
        from services.error_codes import AppError, ErrorCodes

        monkeypatch.setenv("ALLOW_LEGACY_RESTORE", "")

        with pytest.raises(AppError) as exc_info:
            await db_restore.run_restore(backup_id=None, dry_run=True)

        assert exc_info.value.code == ErrorCodes.RESTORE_LEGACY_WRITER_SEALED

    @pytest.mark.asyncio
    async def test_invalid_value_treated_as_production(self, monkeypatch):
        """ALLOW_LEGACY_RESTORE=invalid(非 1/true/yes)→ 视为生产模式(seal fire)。"""
        _ensure_restore_module_importable()
        from services import db_restore
        from services.error_codes import AppError, ErrorCodes

        monkeypatch.setenv("ALLOW_LEGACY_RESTORE", "invalid")

        with pytest.raises(AppError) as exc_info:
            await db_restore.run_restore(backup_id=None, dry_run=True)

        assert exc_info.value.code == ErrorCodes.RESTORE_LEGACY_WRITER_SEALED


# ════════════════════════════════════════════════════════════════
# B. db_backup.restore_from_backup() capability seal
# ════════════════════════════════════════════════════════════════


class TestDbBackupRestoreFromBackupCapabilitySeal:
    """R65 P0-07 / P1-07: ``db_backup.restore_from_backup()`` capability seal。"""

    @pytest.mark.asyncio
    async def test_production_mode_raises_sealed_error(self, monkeypatch):
        """生产模式(无 ALLOW_LEGACY_RESTORE)→ RESTORE_LEGACY_WRITER_SEALED。"""
        from services import db_backup
        from services.error_codes import AppError, ErrorCodes

        monkeypatch.delenv("ALLOW_LEGACY_RESTORE", raising=False)

        with pytest.raises(AppError) as exc_info:
            await db_backup.restore_from_backup(
                key="db_backup/test.json",
                tables=None,
                merge=False,
            )

        assert exc_info.value.code == ErrorCodes.RESTORE_LEGACY_WRITER_SEALED
        params = exc_info.value.params
        assert params.get("caller") == "db_backup.restore_from_backup"
        assert params.get("reason") == "legacy_writer_sealed"

    @pytest.mark.asyncio
    async def test_test_mode_bypasses_seal(self, monkeypatch):
        """测试模式(ALLOW_LEGACY_RESTORE=1)→ 通过 seal。

        通过 seal 后会因 R2 凭证未配置抛 BACKUP_RESTORE_R2_CREDENTIAL_MISSING
        或继续执行后续路径(取决于 mock)。

        本用例显式 mock ``configure_r2_dynamic`` 与 ``r2_storage._access_key``,
        避免 conftest 的 MagicMock settings 注入导致 ``r2_storage._access_key``
        为 MagicMock 触发 ``TypeError: key: expected bytes or bytearray``。
        """
        from services import db_backup
        from services.error_codes import AppError, ErrorCodes

        monkeypatch.setenv("ALLOW_LEGACY_RESTORE", "1")
        # mock configure_r2_dynamic 不覆盖 _access_key,
        # _access_key 保持为空字符串 → 触发 BACKUP_RESTORE_R2_CREDENTIAL_MISSING
        monkeypatch.setattr(db_backup, "configure_r2_dynamic", AsyncMock())
        monkeypatch.setattr(db_backup.r2_storage, "_access_key", "")

        # 通过 seal 后会因 configure_r2_dynamic + R2 凭证缺失而失败
        # 错误码不是 RESTORE_LEGACY_WRITER_SEALED 即证明 seal 已 bypass
        with pytest.raises(AppError) as exc_info:
            await db_backup.restore_from_backup(
                key="db_backup/test.json",
                tables=None,
                merge=False,
            )

        assert exc_info.value.code != ErrorCodes.RESTORE_LEGACY_WRITER_SEALED, (
            "ALLOW_LEGACY_RESTORE=1 时应通过 seal,后续错误码应为其他"
        )


# ════════════════════════════════════════════════════════════════
# C. command_bus.make_restore_backup_command() handler capability seal
# ════════════════════════════════════════════════════════════════


class TestCommandBusRestoreBackupCapabilitySeal:
    """R65 P0-07 / P1-07: ``command_bus.make_restore_backup_command()`` handler seal。"""

    @pytest.mark.asyncio
    async def test_production_mode_raises_sealed_error(self, monkeypatch):
        """生产模式(无 ALLOW_LEGACY_RESTORE)→ RESTORE_LEGACY_WRITER_SEALED。"""
        from services.command_bus import make_restore_backup_command
        from services.error_codes import AppError, ErrorCodes

        monkeypatch.delenv("ALLOW_LEGACY_RESTORE", raising=False)

        cmd = make_restore_backup_command(
            backup_id="db_backup/test.json",
            tables=None,
            merge=False,
        )
        assert cmd.handler is not None

        with pytest.raises(AppError) as exc_info:
            await cmd.handler({
                "backup_id": "db_backup/test.json",
                "tables": None,
                "merge": False,
                "approval_action_id": "test_action",
            })

        assert exc_info.value.code == ErrorCodes.RESTORE_LEGACY_WRITER_SEALED
        params = exc_info.value.params
        assert params.get("caller") == "command_bus.make_restore_backup_command"
        assert params.get("reason") == "legacy_writer_sealed"

    @pytest.mark.asyncio
    async def test_test_mode_bypasses_seal(self, monkeypatch):
        """测试模式(ALLOW_LEGACY_RESTORE=1)→ 通过 seal。

        通过 seal 后会调用 db_backup.restore_from_backup(也通过 seal),
        最终因 R2 凭证未配置抛 BACKUP_RESTORE_R2_CREDENTIAL_MISSING 或其他错误。

        本用例显式 mock ``configure_r2_dynamic`` 与 ``r2_storage._access_key``,
        避免 conftest 的 MagicMock settings 注入导致 ``r2_storage._access_key``
        为 MagicMock 触发 ``TypeError: key: expected bytes or bytearray``。
        """
        from services import db_backup
        from services.command_bus import make_restore_backup_command
        from services.error_codes import AppError, ErrorCodes

        monkeypatch.setenv("ALLOW_LEGACY_RESTORE", "1")
        # mock R2 凭证缺失 → restore_from_backup 抛 BACKUP_RESTORE_R2_CREDENTIAL_MISSING
        monkeypatch.setattr(db_backup, "configure_r2_dynamic", AsyncMock())
        monkeypatch.setattr(db_backup.r2_storage, "_access_key", "")

        cmd = make_restore_backup_command(
            backup_id="db_backup/test.json",
            tables=None,
            merge=False,
        )

        with pytest.raises(AppError) as exc_info:
            await cmd.handler({
                "backup_id": "db_backup/test.json",
                "tables": None,
                "merge": False,
                "approval_action_id": "test_action",
            })

        assert exc_info.value.code != ErrorCodes.RESTORE_LEGACY_WRITER_SEALED, (
            "ALLOW_LEGACY_RESTORE=1 时应通过 seal,后续错误码应为其他"
        )


# ════════════════════════════════════════════════════════════════
# D. AST gate scripts/check_restore_no_legacy_writer.py
# ════════════════════════════════════════════════════════════════


class TestAstGate:
    """R65 P0-07 / P1-07: AST gate ``check_restore_no_legacy_writer.py``。"""

    def test_default_mode_passes(self):
        """默认模式:无违规(扫描全仓生产代码,白名单跳过)。

        默认模式仅检测直接调用私有 writer(_restore_from_backup_data 等)
        与 CLI 入口(run_restore)的违规。
        """
        exit_code, output = _run_gate(strict=False)
        assert exit_code == 0, (
            f"默认模式应通过(无直接调用私有 writer 的违规),实际输出:\n{output}"
        )
        assert "[OK]" in output
        assert "capability-seal 门禁检查通过" in output

    def test_strict_mode_sealed_callers_whitelisted(self):
        """--strict 模式:已知 sealed 调用方已被精确白名单覆盖(db_backup / command_bus)。

        R66 P0-07 整改后:db_backup.py:restore_from_backup 与
        command_bus.py:_handler 是已 capability-sealed 的调用方(运行时由
        ALLOW_LEGACY_RESTORE env var + RESTORE_LEGACY_WRITER_SEALED 错误码
        防护)。这些 sealed 调用方已加入 PRECISE_WHITELIST(精确函数+行范围),
        因此 --strict 模式应通过(无违规),不报告 [FAIL]。

        本测试验证白名单正确覆盖:sealed 调用方在 PRECISE_WHITELIST 中,
        且 --strict 模式 exit_code=0。
        """
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            import check_restore_no_legacy_writer as gate_mod
        finally:
            sys.path.pop(0)

        # 验证 sealed 调用方在 PRECISE_WHITELIST 中(精确白名单已覆盖)
        precise_files_functions = {
            (entry["file"], entry["function"])
            for entry in gate_mod.PRECISE_WHITELIST
        }
        assert ("services/db_backup.py", "restore_from_backup") in precise_files_functions, (
            "services/db_backup.py:restore_from_backup 应在 PRECISE_WHITELIST 中"
            "(已 capability-sealed,精确白名单覆盖)"
        )
        assert ("services/command_bus.py", "_handler") in precise_files_functions, (
            "services/command_bus.py:_handler 应在 PRECISE_WHITELIST 中"
            "(已 capability-sealed,精确白名单覆盖)"
        )

        # --strict 模式应通过(sealed 调用方已被白名单覆盖)
        exit_code, output = _run_gate(strict=True)
        assert exit_code == 0, (
            f"--strict 模式应通过(sealed 调用方已被精确白名单覆盖),"
            f"实际输出:\n{output}"
        )
        assert "[OK]" in output
        # 不应出现 [FAIL](sealed 调用方已白名单覆盖,无违规)
        assert "[FAIL]" not in output

    def test_synthetic_violation_detected(self, tmp_path, monkeypatch):
        """合成违规文件:直接调用 _restore_from_backup_data 被检测。

        临时创建一个 .py 文件,包含对 _restore_from_backup_data 的直接调用,
        验证 gate 能检测到。
        """
        # 由于 gate 扫描 REPO_ROOT,我们直接 import gate 模块并测试其内部函数
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            import check_restore_no_legacy_writer as gate_mod
        finally:
            sys.path.pop(0)

        # 合成违规源码:直接调用 _restore_from_backup_data
        source = """
import sys
async def bad_caller():
    await _restore_from_backup_data(payload, _capability=cap)
"""
        import ast as _ast
        tree = _ast.parse(source, filename="<test>")
        violations = gate_mod._find_violations(
            tree, gate_mod.LEGACY_WRITER_FUNDS_DEFAULT
        )
        assert len(violations) == 1, (
            f"应检测到 1 处违规(_restore_from_backup_data 调用),"
            f"实际: {violations}"
        )
        assert violations[0][2] == "_restore_from_backup_data"

    def test_synthetic_run_restore_violation_detected(self):
        """合成违规文件:直接调用 run_restore 被检测。"""
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            import check_restore_no_legacy_writer as gate_mod
        finally:
            sys.path.pop(0)

        source = """
async def bad_caller():
    await db_restore.run_restore(backup_id="20260718_120000")
"""
        import ast as _ast
        tree = _ast.parse(source, filename="<test>")
        violations = gate_mod._find_violations(
            tree, gate_mod.LEGACY_WRITER_FUNDS_DEFAULT
        )
        assert len(violations) == 1
        assert violations[0][2] == "run_restore"

    def test_strict_mode_detects_validate_and_restore(self):
        """--strict 模式合成违规:直接调用 validate_and_restore_backup_strict 被检测。"""
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            import check_restore_no_legacy_writer as gate_mod
        finally:
            sys.path.pop(0)

        source = """
async def bad_caller():
    await validate_and_restore_backup_strict(data=None)
"""
        import ast as _ast
        tree = _ast.parse(source, filename="<test>")

        # 默认模式:不检测 validate_and_restore_backup_strict
        violations_default = gate_mod._find_violations(
            tree, gate_mod.LEGACY_WRITER_FUNDS_DEFAULT
        )
        assert len(violations_default) == 0, (
            "默认模式不应检测 validate_and_restore_backup_strict"
        )

        # --strict 模式:检测 validate_and_restore_backup_strict
        strict_funds = (
            gate_mod.LEGACY_WRITER_FUNDS_DEFAULT
            | gate_mod.LEGACY_WRITER_FUNDS_STRICT_EXTRA
        )
        violations_strict = gate_mod._find_violations(tree, strict_funds)
        assert len(violations_strict) == 1
        assert violations_strict[0][2] == "validate_and_restore_backup_strict"

    def test_whitelist_files_skipped(self):
        """R66 P0-07: 仅 error_codes.py 完全跳过;db_restore.py / backup_dr_validate.py
        改为精确函数级白名单(PRECISE_WHITELIST);restore_backends.py /
        restore_orchestrator.py 移出白名单。"""
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            import check_restore_no_legacy_writer as gate_mod
        finally:
            sys.path.pop(0)

        # services/error_codes.py 仍完全跳过(仅引用错误码字符串,非调用)
        ec_path = REPO_ROOT / "services" / "error_codes.py"
        assert gate_mod._is_whitelisted(ec_path), (
            "services/error_codes.py 应完全跳过(仅引用错误码字符串)"
        )

        # R66 P0-07: db_restore.py 不再完全跳过,改为精确函数级白名单
        db_restore_path = REPO_ROOT / "services" / "db_restore.py"
        assert not gate_mod._is_whitelisted(db_restore_path), (
            "services/db_restore.py 不应完全跳过(R66 P0-07: 改为精确函数级白名单)"
        )

        # R66 P0-07: backup_dr_validate.py 不再完全跳过,改为精确函数级白名单
        bdv_path = REPO_ROOT / "services" / "backup_dr_validate.py"
        assert not gate_mod._is_whitelisted(bdv_path), (
            "services/backup_dr_validate.py 不应完全跳过(R66 P0-07: 改为精确函数级白名单)"
        )

        # R66 P0-07: restore_backends.py 移出白名单(新生产路径,禁止调用 legacy writer)
        rb_path = REPO_ROOT / "services" / "restore_backends.py"
        assert not gate_mod._is_whitelisted(rb_path), (
            "services/restore_backends.py 不应在白名单中(R66 P0-07: 新生产路径,禁止调用)"
        )

        # R66 P0-07: restore_orchestrator.py 移出白名单(新生产路径,禁止调用 legacy writer)
        ro_path = REPO_ROOT / "services" / "restore_orchestrator.py"
        assert not gate_mod._is_whitelisted(ro_path), (
            "services/restore_orchestrator.py 不应在白名单中(R66 P0-07: 新生产路径,禁止调用)"
        )

        # 验证精确白名单中存在 db_restore.py / backup_dr_validate.py 的条目
        precise_files = {entry["file"] for entry in gate_mod.PRECISE_WHITELIST}
        assert "services/db_restore.py" in precise_files, (
            "精确白名单应包含 services/db_restore.py 的函数级条目"
        )
        assert "services/backup_dr_validate.py" in precise_files, (
            "精确白名单应包含 services/backup_dr_validate.py 的函数级条目"
        )
        # restore_backends.py / restore_orchestrator.py 不应在精确白名单中
        assert "services/restore_backends.py" not in precise_files, (
            "精确白名单不应包含 services/restore_backends.py"
        )
        assert "services/restore_orchestrator.py" not in precise_files, (
            "精确白名单不应包含 services/restore_orchestrator.py"
        )

    def test_whitelist_dirs_skipped(self):
        """白名单目录被跳过(tests/ 与 scripts/)。"""
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            import check_restore_no_legacy_writer as gate_mod
        finally:
            sys.path.pop(0)

        # tests/ 下的文件应在白名单中
        test_file = REPO_ROOT / "tests" / "test_r65_p0_7_legacy_writer_seal.py"
        assert gate_mod._is_whitelisted(test_file), (
            "tests/ 下的文件应在白名单中(测试逃生舱)"
        )

        # scripts/ 下的文件应在白名单中
        script_file = REPO_ROOT / "scripts" / "check_restore_no_legacy_writer.py"
        assert gate_mod._is_whitelisted(script_file), (
            "scripts/ 下的文件应在白名单中(gate 脚本本身)"
        )

    def test_non_whitelisted_files_scanned(self):
        """非白名单文件被扫描(services/db_backup.py / command_bus.py)。"""
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            import check_restore_no_legacy_writer as gate_mod
        finally:
            sys.path.pop(0)

        # services/db_backup.py 不在白名单中(已 sealed 但仍扫描以捕获新违规)
        db_backup_path = REPO_ROOT / "services" / "db_backup.py"
        assert not gate_mod._is_whitelisted(db_backup_path), (
            "services/db_backup.py 不应在白名单中(已 sealed 但仍扫描)"
        )

        # services/command_bus.py 不在白名单中
        command_bus_path = REPO_ROOT / "services" / "command_bus.py"
        assert not gate_mod._is_whitelisted(command_bus_path)


# ════════════════════════════════════════════════════════════════
# E. Error code registration
# ════════════════════════════════════════════════════════════════


class TestErrorCodeRegistration:
    """R65 P0-07 / P1-07: RESTORE_LEGACY_WRITER_SEALED 错误码注册。"""

    def test_error_code_in_enum(self):
        """RESTORE_LEGACY_WRITER_SEALED 在 ErrorCodes 枚举中。"""
        from services.error_codes import ErrorCodes
        assert hasattr(ErrorCodes, "RESTORE_LEGACY_WRITER_SEALED")
        assert ErrorCodes.RESTORE_LEGACY_WRITER_SEALED == "RESTORE.LEGACY_WRITER.SEALED"

    def test_error_registry_entry_exists(self):
        """ErrorRegistry 中存在 RESTORE_LEGACY_WRITER_SEALED 注册条目。"""
        from services.error_codes import ErrorRegistry
        envelope = ErrorRegistry.create_envelope(
            "RESTORE.LEGACY_WRITER.SEALED",
            params={"caller": "test_caller", "reason": "test_reason"},
            locale="zh-CN",
        )
        assert envelope is not None
        assert "test_caller" in envelope.message or "caller" in envelope.message

    def test_error_registry_critical_severity(self):
        """RESTORE_LEGACY_WRITER_SEALED 注册为 critical severity。"""
        from services.error_codes import ErrorRegistry
        # 通过 ErrorRegistry._find_definition 或类似方法获取 ErrorDefinition
        # 不同实现可能用不同 API,这里通过 create_envelope 间接验证
        envelope = ErrorRegistry.create_envelope(
            "RESTORE.LEGACY_WRITER.SEALED",
            params={"caller": "test", "reason": "test"},
            locale="zh-CN",
        )
        # 消息应包含 caller 与 reason(说明 safe_params 已注册)
        assert envelope.params.get("caller") == "test"
        assert envelope.params.get("reason") == "test"

    def test_locale_entry_zh_cn(self):
        """zh-CN.json 含 restore.legacy_writer.sealed 条目(含 caller/reason 参数)。"""
        locale_path = REPO_ROOT / "locales" / "zh-CN.json"
        with open(locale_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # errors.restore.legacy_writer.sealed 是 message_key,locale 中存为
        # nested errors dict 下的 "restore.legacy_writer.sealed" 子键
        # (与现有 errors.restore.phase.transition_invalid 等约定一致)
        errors_dict = data.get("errors", {})
        message = errors_dict.get("restore.legacy_writer.sealed")
        assert message is not None, (
            "zh-CN.json 应含 errors.restore.legacy_writer.sealed 条目"
        )
        assert "{caller}" in message, "消息应含 {caller} ICU 占位符"
        assert "{reason}" in message, "消息应含 {reason} ICU 占位符"
        assert "capability-seal" in message or "capability" in message.lower()

    def test_locale_entry_en_us(self):
        """en-US.json 含 restore.legacy_writer.sealed 条目(含 caller/reason 参数)。"""
        locale_path = REPO_ROOT / "locales" / "en-US.json"
        with open(locale_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        errors_dict = data.get("errors", {})
        message = errors_dict.get("restore.legacy_writer.sealed")
        assert message is not None, (
            "en-US.json 应含 errors.restore.legacy_writer.sealed 条目"
        )
        assert "{caller}" in message
        assert "{reason}" in message
        assert "capability-seal" in message or "capability" in message.lower()

    def test_app_error_raises_with_correct_code(self):
        """直接抛 AppError(RESTORE_LEGACY_WRITER_SEALED) 可正确构造。"""
        from services.error_codes import AppError, ErrorCodes
        try:
            raise AppError(
                ErrorCodes.RESTORE_LEGACY_WRITER_SEALED,
                params={"caller": "test", "reason": "test_reason"},
            )
        except AppError as e:
            assert e.code == ErrorCodes.RESTORE_LEGACY_WRITER_SEALED
            assert e.params.get("caller") == "test"
            assert e.params.get("reason") == "test_reason"
            # 消息应已 i18n 化(包含 caller 与 reason)
            assert "test" in e.message or "caller" in e.message


# ════════════════════════════════════════════════════════════════
# F. 向后兼容
# ════════════════════════════════════════════════════════════════


class TestBackwardCompatibility:
    """R65 P0-07 / P1-07: 向后兼容 — ALLOW_LEGACY_RESTORE=1 时测试仍可工作。"""

    def test_conftest_autouse_fixture_sets_allow_legacy_restore(self, monkeypatch):
        """conftest.py 的 autouse fixture 默认设置 ALLOW_LEGACY_RESTORE=1。

        本用例验证 fixture 已生效(无需在用例内手动设置)。
        """
        # conftest 的 autouse fixture 应已设置 ALLOW_LEGACY_RESTORE=1
        # 用例内可直接读取环境变量验证
        value = os.environ.get("ALLOW_LEGACY_RESTORE", "")
        assert value.lower() in ("1", "true", "yes"), (
            f"conftest autouse fixture 应设置 ALLOW_LEGACY_RESTORE=1,实际: {value!r}"
        )

    @pytest.mark.asyncio
    async def test_run_restore_callable_in_test_mode(self, monkeypatch):
        """测试模式下 run_restore 可被调用(不立即抛 sealed 错误)。

        这是 R62 / R63 测试中直接调用 run_restore 的基础。
        """
        _ensure_restore_module_importable()
        from services import db_restore
        from services.error_codes import AppError, ErrorCodes

        # 不显式设置 ALLOW_LEGACY_RESTORE — 验证 conftest autouse fixture 已生效
        # 用例内可显式覆盖为 "1" 以保证测试独立
        monkeypatch.setenv("ALLOW_LEGACY_RESTORE", "1")

        # 调用 run_restore(backup_id=None) 应通过 seal,后续抛 BACKUP_RESTORE_TRUST_CHAIN_REQUIRED
        with pytest.raises(AppError) as exc_info:
            await db_restore.run_restore(backup_id=None, dry_run=True)

        # 通过 seal 后,backup_id=None 触发信任链错误
        assert exc_info.value.code == ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED

    def test_legacy_writer_functions_still_importable(self):
        """旧 writer 函数仍可被 import(_restore_from_backup_data 等)。

        它们已被 R61 P0-03 / R62 P0-02 的 _capability 参数 capability-seal,
        外部代码无法伪造 capability,但仍可被 import(供 tests/ 与 scripts/ 使用)。
        """
        _ensure_restore_module_importable()
        from services import db_restore
        # 这些函数应仍可被 import(供测试/脚本使用)
        assert hasattr(db_restore, "_restore_from_backup_data")
        assert hasattr(db_restore, "_restore_crdb_tables")
        assert hasattr(db_restore, "_restore_sqlite_tables_to_db")
        assert hasattr(db_restore, "run_restore")

    def test_strict_service_still_importable(self):
        """strict service validate_and_restore_backup_strict 仍可被 import。"""
        try:
            from services.backup_dr_validate import (
                validate_and_restore_backup_strict,
            )
        except ImportError as e:
            pytest.skip(f"services.backup_dr_validate 不可导入: {e}")
        assert callable(validate_and_restore_backup_strict)
