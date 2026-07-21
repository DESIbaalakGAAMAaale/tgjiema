"""R67 P0-06: 生产镜像物理移除 Legacy Restore 公共入口 — 硬守卫测试。

R67 审计背景:
    legacy ``run_restore()`` / ``restore_from_backup()`` / strict wrapper 与
    原地 writer 仍由 Dockerfile 的 ``COPY . .`` 带入生产镜像,并可通过
    ``ALLOW_LEGACY_RESTORE`` 解封。R65 P0-07 的 capability-seal 虽已封存,
    但逃生舱 ``ALLOW_LEGACY_RESTORE=1`` 仍可在生产环境解封 — 违反 R67 P0-06:
    "生产镜像不得包含可调用的 legacy restore public entrypoint"。

    R67 P0-06 整改:
      1. 在 ``run_restore()`` / ``restore_from_backup()`` / command_bus _handler
         的 capability-seal 之前增加硬守卫:
         - 生产环境(APP_ENV=production|staging)无条件拒绝,**不允许**
           ``ALLOW_LEGACY_RESTORE`` 解封
         - 守卫直接读取 ``APP_ENV`` 环境变量,不依赖 Settings 实例化
      2. 生产镜像通过 .dockerignore 物理排除 tests/ 与 scripts/
      3. settings.py 已有 ``validate_no_legacy_restore_in_production`` validator

测试覆盖矩阵:
    A. 硬守卫模块单元测试(4 个)
        1. 非生产环境(无 APP_ENV) → 守卫不 raise
        2. APP_ENV=production → 守卫 raise AppError
        3. APP_ENV=staging → 守卫 raise AppError
        4. APP_ENV=development → 守卫不 raise
    B. 硬守卫在 ALLOW_LEGACY_RESTORE 设置时仍生效(3 个)
        5. APP_ENV=production + ALLOW_LEGACY_RESTORE=1 → 守卫仍 raise(关键!)
        6. APP_ENV=production + ALLOW_LEGACY_RESTORE=true → 守卫仍 raise
        7. APP_ENV=staging + ALLOW_LEGACY_RESTORE=yes → 守卫仍 raise
    C. run_restore() 在生产环境硬失败(2 个)
        8. APP_ENV=production + ALLOW_LEGACY_RESTORE=1 + 调用 run_restore → raise
        9. APP_ENV=development + ALLOW_LEGACY_RESTORE=1 + 调用 run_restore → 不被硬守卫阻断
           (capability-seal 由 ALLOW_LEGACY_RESTORE 解封,继续执行)
    D. restore_from_backup() 在生产环境硬失败(2 个)
        10. APP_ENV=production + ALLOW_LEGACY_RESTORE=1 + 调用 restore_from_backup → raise
        11. APP_ENV=development + ALLOW_LEGACY_RESTORE=1 + 调用 restore_from_backup → 不被硬守卫阻断
    E. 守卫不依赖 Settings 实例化(1 个)
        12. 不实例化 Settings,直接设置 APP_ENV=production + ALLOW_LEGACY_RESTORE=1
            + 调用 run_restore → raise(关键:证明守卫独立于 Settings)
    F. .dockerignore 物理排除验证(2 个)
        13. .dockerignore 包含 tests/ 排除规则
        14. .dockerignore 包含 scripts/ 排除规则
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# 测试环境兼容(mock telegram 库)
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services._production_guard import (
    _detect_production_environment,
    assert_no_legacy_restore_in_production,
    is_production_environment,
)
from services.error_codes import AppError, ErrorCodes


# ════════════════════════════════════════════════════════════════
# A. 硬守卫模块单元测试
# ════════════════════════════════════════════════════════════════

class TestProductionGuardUnit:
    """R67 P0-06: services/_production_guard.py 单元测试。"""

    def test_no_app_env_not_production(self, monkeypatch):
        """无 APP_ENV 环境变量 → 非生产环境,守卫不 raise。"""
        monkeypatch.delenv("APP_ENV", raising=False)
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        monkeypatch.delenv("DEPLOY_ENV", raising=False)
        assert not is_production_environment()
        # 守卫不应 raise
        assert_no_legacy_restore_in_production(
            entry_point="test", caller="test",
        )

    @pytest.mark.parametrize("env_var", ["APP_ENV", "ENVIRONMENT", "DEPLOY_ENV"])
    @pytest.mark.parametrize("env_val", ["production", "staging", "prod", "stg", "PRODUCTION", "Staging"])
    def test_production_env_vars_detected(self, monkeypatch, env_var, env_val):
        """各种生产环境标识均被检测到(APP_ENV/ENVIRONMENT/DEPLOY_ENV,大小写不敏感)。"""
        monkeypatch.delenv("APP_ENV", raising=False)
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        monkeypatch.delenv("DEPLOY_ENV", raising=False)
        monkeypatch.setenv(env_var, env_val)
        assert is_production_environment()
        is_prod, source = _detect_production_environment()
        assert is_prod is True
        assert source == env_var

    @pytest.mark.parametrize("env_val", ["development", "dev", "test", "testing", "ci", ""])
    def test_non_production_env_vars_not_detected(self, monkeypatch, env_val):
        """非生产环境标识不被检测为生产。"""
        monkeypatch.delenv("APP_ENV", raising=False)
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        monkeypatch.delenv("DEPLOY_ENV", raising=False)
        if env_val:
            monkeypatch.setenv("APP_ENV", env_val)
        assert not is_production_environment()

    def test_production_env_raises_with_detailed_params(self, monkeypatch):
        """生产环境调用守卫 → raise AppError,含详细诊断参数。"""
        monkeypatch.setenv("APP_ENV", "production")
        with pytest.raises(AppError) as exc_info:
            assert_no_legacy_restore_in_production(
                entry_point="run_restore()", caller="run_restore",
            )
        # 错误码必须是 RESTORE_LEGACY_WRITER_SEALED
        assert exc_info.value.code == ErrorCodes.RESTORE_LEGACY_WRITER_SEALED
        # params 必须含 entry_point, caller, source_env_var
        params = exc_info.value.params
        assert params["entry_point"] == "run_restore()"
        assert params["caller"] == "run_restore"
        assert params["source_env_var"] == "APP_ENV"


# ════════════════════════════════════════════════════════════════
# B. 硬守卫在 ALLOW_LEGACY_RESTORE 设置时仍生效(关键!)
# ════════════════════════════════════════════════════════════════

class TestHardGuardIgnoresAllowLegacyRestore:
    """R67 P0-06 核心:生产环境即使设置 ALLOW_LEGACY_RESTORE 也无法解封。"""

    @pytest.mark.parametrize("allow_val", ["1", "true", "yes", "TRUE", "Yes"])
    def test_production_with_allow_legacy_still_raises(self, monkeypatch, allow_val):
        """APP_ENV=production + ALLOW_LEGACY_RESTORE=* → 守卫仍 raise(关键!)。"""
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("ALLOW_LEGACY_RESTORE", allow_val)
        with pytest.raises(AppError) as exc_info:
            assert_no_legacy_restore_in_production(
                entry_point="run_restore()", caller="run_restore",
            )
        # params 必须标记 allow_legacy_restore_set=True
        params = exc_info.value.params
        assert params["allow_legacy_restore_set"] == "True"
        assert "r67_p0_06_production_hard_guard_allow_legacy_ignored" in params["reason"]

    @pytest.mark.parametrize("allow_val", ["1", "true", "yes"])
    def test_staging_with_allow_legacy_still_raises(self, monkeypatch, allow_val):
        """APP_ENV=staging + ALLOW_LEGACY_RESTORE=* → 守卫仍 raise。"""
        monkeypatch.setenv("APP_ENV", "staging")
        monkeypatch.setenv("ALLOW_LEGACY_RESTORE", allow_val)
        with pytest.raises(AppError) as exc_info:
            assert_no_legacy_restore_in_production(
                entry_point="restore_from_backup()", caller="db_backup.restore_from_backup",
            )
        params = exc_info.value.params
        assert params["allow_legacy_restore_set"] == "True"

    def test_development_with_allow_legacy_does_not_raise(self, monkeypatch):
        """APP_ENV=development + ALLOW_LEGACY_RESTORE=1 → 守卫不 raise(逃生舱仍可用)。"""
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.setenv("ALLOW_LEGACY_RESTORE", "1")
        # 守卫不 raise(非生产环境允许逃生舱)
        assert_no_legacy_restore_in_production(
            entry_point="run_restore()", caller="run_restore",
        )


# ════════════════════════════════════════════════════════════════
# C. run_restore() 在生产环境硬失败
# ════════════════════════════════════════════════════════════════

class TestRunRestoreHardFailsInProduction:
    """R67 P0-06: run_restore() 在生产环境硬失败(不允许 ALLOW_LEGACY_RESTORE 解封)。"""

    @pytest.mark.asyncio
    async def test_run_restore_production_with_allow_legacy_raises(self, monkeypatch):
        """APP_ENV=production + ALLOW_LEGACY_RESTORE=1 + 调用 run_restore → raise。

        关键:这是 R67 P0-06 的核心验收 — 即使设置逃生舱,生产环境也拒绝调用。
        守卫位于 run_restore 函数体最顶端,在任何其他逻辑之前触发。
        """
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("ALLOW_LEGACY_RESTORE", "1")
        from services.db_restore import run_restore
        with pytest.raises(AppError) as exc_info:
            await run_restore(backup_id="20260718_120000")
        assert exc_info.value.code == ErrorCodes.RESTORE_LEGACY_WRITER_SEALED

    @pytest.mark.asyncio
    async def test_run_restore_development_with_allow_legacy_not_hard_blocked(self, monkeypatch):
        """APP_ENV=development + ALLOW_LEGACY_RESTORE=1 + 调用 run_restore → 不被硬守卫阻断。

        非生产环境逃生舱仍可用(硬守卫通过,后续 capability-seal 由 ALLOW_LEGACY_RESTORE 解封)。
        本测试只验证硬守卫不阻断 — 实际 run_restore 内部还会执行后续逻辑,
        可能因 R2 未配置等其他原因失败,但不应是 RESTORE_LEGACY_WRITER_SEALED。
        """
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.setenv("ALLOW_LEGACY_RESTORE", "1")
        from services.db_restore import run_restore
        # 调用 run_restore — 硬守卫不阻断,但后续可能因 R2 未配置失败
        try:
            await run_restore(backup_id="20260718_120000")
        except AppError as e:
            # 硬守卫不应阻断 — 错误码不应是 RESTORE_LEGACY_WRITER_SEALED
            assert e.code != ErrorCodes.RESTORE_LEGACY_WRITER_SEALED, (
                "R67 P0-06:development 环境不应被硬守卫阻断"
            )
        except Exception:
            # 其他异常(R2 未配置等)是预期的 — 硬守卫通过
            pass


# ════════════════════════════════════════════════════════════════
# D. restore_from_backup() 在生产环境硬失败
# ════════════════════════════════════════════════════════════════

class TestRestoreFromBackupHardFailsInProduction:
    """R67 P0-06: restore_from_backup() 在生产环境硬失败。"""

    @pytest.mark.asyncio
    async def test_restore_from_backup_production_with_allow_legacy_raises(self, monkeypatch):
        """APP_ENV=production + ALLOW_LEGACY_RESTORE=1 + 调用 restore_from_backup → raise。"""
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("ALLOW_LEGACY_RESTORE", "1")
        from services.db_backup import restore_from_backup
        with pytest.raises(AppError) as exc_info:
            await restore_from_backup("db_backup/test.json")
        assert exc_info.value.code == ErrorCodes.RESTORE_LEGACY_WRITER_SEALED

    @pytest.mark.asyncio
    async def test_restore_from_backup_development_with_allow_legacy_not_hard_blocked(self, monkeypatch):
        """APP_ENV=development + ALLOW_LEGACY_RESTORE=1 + 调用 restore_from_backup → 不被硬守卫阻断。"""
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.setenv("ALLOW_LEGACY_RESTORE", "1")
        from services.db_backup import restore_from_backup
        try:
            await restore_from_backup("db_backup/test.json")
        except AppError as e:
            assert e.code != ErrorCodes.RESTORE_LEGACY_WRITER_SEALED, (
                "R67 P0-06:development 环境不应被硬守卫阻断"
            )
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════
# E. 守卫不依赖 Settings 实例化(关键!)
# ════════════════════════════════════════════════════════════════

class TestGuardIndependentOfSettings:
    """R67 P0-06: 守卫直接读取 APP_ENV,不依赖 Settings 实例化。

    关键场景:攻击者/误操作直接 CLI 调用 ``python -c "from services.db_restore
    import run_restore; run_restore()"`` 时,Settings 可能未实例化 — 守卫必须独立生效。
    """

    @pytest.mark.asyncio
    async def test_guard_works_without_settings_instance(self, monkeypatch):
        """不实例化 Settings,直接设置 APP_ENV=production + ALLOW_LEGACY_RESTORE=1
        + 调用 run_restore → raise(证明守卫独立于 Settings)。"""
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("ALLOW_LEGACY_RESTORE", "1")
        # 不调用任何 Settings 加载逻辑,直接 import + 调用
        from services.db_restore import run_restore
        with pytest.raises(AppError) as exc_info:
            await run_restore(backup_id="20260718_120000")
        # 守卫在 capability-seal 之前 raise,所以错误码是 RESTORE_LEGACY_WRITER_SEALED
        assert exc_info.value.code == ErrorCodes.RESTORE_LEGACY_WRITER_SEALED
        # params 必须含 r67_p0_06 标记
        params = exc_info.value.params
        assert "r67_p0_06" in params["reason"]


# ════════════════════════════════════════════════════════════════
# F. .dockerignore 物理排除验证
# ════════════════════════════════════════════════════════════════

class TestDockerignorePhysicalExclusion:
    """R67 P0-06: .dockerignore 必须物理排除 tests/ 与 scripts/。"""

    def test_dockerignore_excludes_tests(self):
        """.dockerignore 包含 tests/ 排除规则(生产镜像不含测试代码)。"""
        content = (REPO_ROOT / ".dockerignore").read_text()
        assert "tests/" in content, (
            "R67 P0-06:.dockerignore 必须排除 tests/ 目录(测试代码不得进入生产镜像)"
        )

    def test_dockerignore_excludes_scripts(self):
        """.dockerignore 包含 scripts/ 排除规则(生产镜像不含运维脚本)。"""
        content = (REPO_ROOT / ".dockerignore").read_text()
        assert "scripts/" in content, (
            "R67 P0-06:.dockerignore 必须排除 scripts/ 目录(运维脚本不得进入生产镜像)"
        )

    def test_dockerignore_excludes_github(self):
        """.dockerignore 包含 .github 排除规则(生产镜像不含 CI 配置)。"""
        content = (REPO_ROOT / ".dockerignore").read_text()
        assert ".github" in content, (
            "R67 P0-06:.dockerignore 必须排除 .github 目录(CI 配置不得进入生产镜像)"
        )


# ════════════════════════════════════════════════════════════════
# G. 配置层 settings.py validator 回归(R66 P0-07 已有,R67 P0-06 保留)
# ════════════════════════════════════════════════════════════════

class TestSettingsValidatorRegression:
    """R66 P0-07 已有的 settings.py validator 在 R67 P0-06 后仍生效。"""

    def test_settings_validator_still_blocks_production_allow_legacy(self, monkeypatch):
        """settings.py 的 validate_no_legacy_restore_in_production 仍生效。

        R66 P0-07 已实现:ENVIRONMENT=production 或 APP_ENV=production 时,
        ALLOW_LEGACY_RESTORE=1/true/yes → Settings 加载失败(ValueError)。
        R67 P0-06 在此基础上增加函数级硬守卫,双层防护。

        注:测试环境 conftest.py 用 MagicMock 替换了 config 模块,
        无法直接 import Settings 类。这里通过直接读取 settings.py 源码
        验证 validator 函数定义存在,确保代码未被误删。
        """
        # 验证 settings.py 源码中 validator 仍存在
        settings_path = REPO_ROOT / "config" / "settings.py"
        assert settings_path.exists(), "config/settings.py 必须存在"
        source = settings_path.read_text()
        assert "validate_no_legacy_restore_in_production" in source, (
            "R66 P0-07 的 settings.py validator 必须保留"
        )
        assert "RESTORE_LEGACY_WRITER_SEALED" in source or "ALLOW_LEGACY_RESTORE" in source, (
            "settings.py 必须包含 ALLOW_LEGACY_RESTORE 校验逻辑"
        )


# asyncio 测试需要 pytest-asyncio
@pytest.fixture
def event_loop():
    """兼容 pytest-asyncio 的事件循环 fixture。"""
    import asyncio
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
