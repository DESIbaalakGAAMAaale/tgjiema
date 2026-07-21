"""R70 Wave 3: 测试逃生舱硬守卫 — 测试 services.escape_hatch_guard 模块。

R70 P0-08 根因修复的回归测试:
    旧版多个测试逃生舱(I18N_ALLOW_FALLBACK / ALLOW_LEGACY_RESTORE /
    TEST_ONLY / DEV_ONLY / BYPASS / SKIP_VERIFY)在 production/staging 下
    仍可通过环境变量启用,造成生产环境绕过 fail-closed 的风险。

测试矩阵(对应 R70 Wave 3 §6 要求):
    1. APP_ENV=production + I18N_ALLOW_FALLBACK=1 → raise AppError
    2. APP_ENV=production + ALLOW_LEGACY_RESTORE=1 → raise AppError
    3. APP_ENV=staging + 任何逃生舱 → raise AppError
    4. APP_ENV=test + 逃生舱 → 允许
    5. APP_ENV=development + 逃生舱 → 允许
    6. production + 多个逃生舱同时设置 → 错误信息包含全部
    7. production + 无逃生舱 → 允许
    8. 各种真值变体(1/true/yes/on 接受;其他值拒绝)
    9. 大小写不敏感(I18N_ALLOW_FALLBACK=TRUE / Yes / ON)
    10. ESCAPE_HATCH_REGISTRY 完整性
    11. list_escape_hatch_env_vars / is_escape_hatch_var 辅助函数
    12. _detect_production_like_from_os_environ 行为
    13. 别名 prod/stg 也视为 production/staging
    14. ENVIRONMENT/DEPLOY_ENV 也能触发 production 判定
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest


# ──────────────────────────────────────────────────────────────────
# 测试隔离:直接加载 config.environment 与 services.escape_hatch_guard
# (绕过 config/__init__.py 与 services/__init__.py 可能触发的副作用)
# ──────────────────────────────────────────────────────────────────
def _ensure_config_environment_module():
    """直接加载 config.environment 模块,不触发 config/__init__.py。"""
    if "config.environment" not in sys.modules or not hasattr(
        sys.modules.get("config.environment", None), "parse_app_env"
    ):
        import importlib.util

        env_path = Path(__file__).resolve().parent.parent / "config" / "environment.py"
        spec = importlib.util.spec_from_file_location("config.environment", env_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["config.environment"] = module
        spec.loader.exec_module(module)
    return sys.modules["config.environment"]


def _load_escape_hatch_guard_module():
    """加载 services.escape_hatch_guard 模块。

    该模块依赖 services.error_codes.AppError 与 ErrorCodes,需要确保
    services 包可用。我们直接通过 importlib 加载目标文件,并预注入
    services.error_codes 模块(若尚未加载)。
    """
    # 确保 services 包可导入
    if "services" not in sys.modules:
        services_pkg = types.ModuleType("services")
        services_pkg.__path__ = [
            str(Path(__file__).resolve().parent.parent / "services")
        ]
        sys.modules["services"] = services_pkg

    # 确保 services.error_codes 已加载(真实模块,不是 mock)
    if "services.error_codes" not in sys.modules:
        import importlib.util

        ec_path = Path(__file__).resolve().parent.parent / "services" / "error_codes.py"
        spec = importlib.util.spec_from_file_location("services.error_codes", ec_path)
        ec_module = importlib.util.module_from_spec(spec)
        sys.modules["services.error_codes"] = ec_module
        spec.loader.exec_module(ec_module)

    # 加载 escape_hatch_guard
    if "services.escape_hatch_guard" not in sys.modules:
        import importlib.util

        guard_path = (
            Path(__file__).resolve().parent.parent / "services" / "escape_hatch_guard.py"
        )
        spec = importlib.util.spec_from_file_location(
            "services.escape_hatch_guard", guard_path
        )
        guard_module = importlib.util.module_from_spec(spec)
        sys.modules["services.escape_hatch_guard"] = guard_module
        spec.loader.exec_module(guard_module)

    return sys.modules["services.escape_hatch_guard"]


@pytest.fixture
def guard_module():
    """提供 services.escape_hatch_guard 模块实例。"""
    _ensure_config_environment_module()
    return _load_escape_hatch_guard_module()


@pytest.fixture
def clean_env(monkeypatch):
    """清理所有环境变量,确保测试隔离。"""
    for var in (
        "APP_ENV",
        "ENVIRONMENT",
        "DEPLOY_ENV",
        "I18N_ALLOW_FALLBACK",
        "ALLOW_LEGACY_RESTORE",
        "TEST_ONLY",
        "DEV_ONLY",
        "BYPASS",
        "SKIP_VERIFY",
        "SKIP_VALIDATION",
        "ALLOW_INSECURE",
        "RELEASE_BUILD",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


# ══════════════════════════════════════════════════════════════════
# 测试 1: APP_ENV=production + I18N_ALLOW_FALLBACK=1 → raise AppError
# ══════════════════════════════════════════════════════════════════
def test_production_with_i18n_allow_fallback_rejected(guard_module, clean_env, monkeypatch):
    """APP_ENV=production + I18N_ALLOW_FALLBACK=1 → 必须 raise AppError。"""
    from services.error_codes import AppError, ErrorCodes

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("I18N_ALLOW_FALLBACK", "1")

    with pytest.raises(AppError) as exc_info:
        guard_module.assert_no_test_escape_hatches(caller="test")

    assert exc_info.value.code == ErrorCodes.PRODUCTION_ESCAPE_HATCH_DETECTED
    # AppError.params 包含 safe_params(caller / hatch_count / hatch_details / reason)
    params = exc_info.value.params
    assert "I18N_ALLOW_FALLBACK" in params.get("hatch_details", "")


# ══════════════════════════════════════════════════════════════════
# 测试 2: APP_ENV=production + ALLOW_LEGACY_RESTORE=1 → raise AppError
# ══════════════════════════════════════════════════════════════════
def test_production_with_allow_legacy_restore_rejected(guard_module, clean_env, monkeypatch):
    """APP_ENV=production + ALLOW_LEGACY_RESTORE=1 → 必须 raise AppError。"""
    from services.error_codes import AppError, ErrorCodes

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ALLOW_LEGACY_RESTORE", "1")

    with pytest.raises(AppError) as exc_info:
        guard_module.assert_no_test_escape_hatches(caller="test")

    assert exc_info.value.code == ErrorCodes.PRODUCTION_ESCAPE_HATCH_DETECTED
    params = exc_info.value.params
    assert "ALLOW_LEGACY_RESTORE" in params.get("hatch_details", "")


# ══════════════════════════════════════════════════════════════════
# 测试 3: APP_ENV=staging + 任何逃生舱 → raise AppError
# ══════════════════════════════════════════════════════════════════
@pytest.mark.parametrize(
    "hatch_var,hatch_value",
    [
        ("I18N_ALLOW_FALLBACK", "1"),
        ("ALLOW_LEGACY_RESTORE", "true"),
        ("TEST_ONLY", "yes"),
        ("DEV_ONLY", "on"),
        ("BYPASS", "1"),
        ("SKIP_VERIFY", "1"),
        ("SKIP_VALIDATION", "true"),
        ("ALLOW_INSECURE", "1"),
    ],
)
def test_staging_with_any_escape_hatch_rejected(
    guard_module, clean_env, monkeypatch, hatch_var, hatch_value
):
    """APP_ENV=staging + 任何逃生舱变量 → 必须 raise AppError。"""
    from services.error_codes import AppError, ErrorCodes

    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv(hatch_var, hatch_value)

    with pytest.raises(AppError) as exc_info:
        guard_module.assert_no_test_escape_hatches(caller="test")

    assert exc_info.value.code == ErrorCodes.PRODUCTION_ESCAPE_HATCH_DETECTED
    params = exc_info.value.params
    assert hatch_var in params.get("hatch_details", "")


# ══════════════════════════════════════════════════════════════════
# 测试 4: APP_ENV=test + 逃生舱 → 允许(测试需要)
# ══════════════════════════════════════════════════════════════════
def test_test_env_allows_escape_hatches(guard_module, clean_env, monkeypatch):
    """APP_ENV=test 下设置逃生舱 → 允许(不 raise)。"""
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("I18N_ALLOW_FALLBACK", "1")
    monkeypatch.setenv("ALLOW_LEGACY_RESTORE", "1")
    monkeypatch.setenv("TEST_ONLY", "1")

    # 不应 raise
    guard_module.assert_no_test_escape_hatches(caller="test")


# ══════════════════════════════════════════════════════════════════
# 测试 5: APP_ENV=development + 逃生舱 → 允许(本地开发需要)
# ══════════════════════════════════════════════════════════════════
def test_development_env_allows_escape_hatches(guard_module, clean_env, monkeypatch):
    """APP_ENV=development 下设置逃生舱 → 允许(不 raise)。"""
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("I18N_ALLOW_FALLBACK", "1")
    monkeypatch.setenv("BYPASS", "1")

    # 不应 raise
    guard_module.assert_no_test_escape_hatches(caller="test")


# ══════════════════════════════════════════════════════════════════
# 测试 6: production + 多个逃生舱同时设置 → 错误信息包含全部
# ══════════════════════════════════════════════════════════════════
def test_production_multiple_hatches_all_listed(guard_module, clean_env, monkeypatch):
    """APP_ENV=production + 多个逃生舱 → 错误信息必须包含全部检测到的变量。"""
    from services.error_codes import AppError

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("I18N_ALLOW_FALLBACK", "1")
    monkeypatch.setenv("ALLOW_LEGACY_RESTORE", "true")
    monkeypatch.setenv("BYPASS", "yes")
    monkeypatch.setenv("SKIP_VERIFY", "on")

    with pytest.raises(AppError) as exc_info:
        guard_module.assert_no_test_escape_hatches(caller="multi_test")

    params = exc_info.value.params
    hatch_details = params.get("hatch_details", "")
    assert "I18N_ALLOW_FALLBACK" in hatch_details
    assert "ALLOW_LEGACY_RESTORE" in hatch_details
    assert "BYPASS" in hatch_details
    assert "SKIP_VERIFY" in hatch_details
    # hatch_count 应该是 4
    assert params.get("hatch_count") == "4"


# ══════════════════════════════════════════════════════════════════
# 测试 7: production + 无逃生舱 → 允许(正常生产)
# ══════════════════════════════════════════════════════════════════
def test_production_no_escape_hatches_allowed(guard_module, clean_env, monkeypatch):
    """APP_ENV=production 且无任何逃生舱 → 允许(正常生产)。"""
    monkeypatch.setenv("APP_ENV", "production")
    # 不设置任何逃生舱变量

    # 不应 raise
    guard_module.assert_no_test_escape_hatches(caller="test")


def test_staging_no_escape_hatches_allowed(guard_module, clean_env, monkeypatch):
    """APP_ENV=staging 且无任何逃生舱 → 允许(预发环境)。"""
    monkeypatch.setenv("APP_ENV", "staging")
    # 不设置任何逃生舱变量

    # 不应 raise
    guard_module.assert_no_test_escape_hatches(caller="test")


# ══════════════════════════════════════════════════════════════════
# 测试 8: 各种真值变体(1/true/yes/on 接受;其他值拒绝)
# ══════════════════════════════════════════════════════════════════
@pytest.mark.parametrize(
    "true_value", ["1", "true", "yes", "on", "TRUE", "True", "YES", "ON"]
)
def test_true_value_variants_accepted(
    guard_module, clean_env, monkeypatch, true_value
):
    """真值变体(1/true/yes/on 及大小写) → 触发拒绝。"""
    from services.error_codes import AppError

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("I18N_ALLOW_FALLBACK", true_value)

    with pytest.raises(AppError):
        guard_module.assert_no_test_escape_hatches(caller="test")


@pytest.mark.parametrize(
    "non_true_value", ["0", "false", "no", "off", "", "random", "nope"]
)
def test_non_true_value_variants_not_triggered(
    guard_module, clean_env, monkeypatch, non_true_value
):
    """非真值(0/false/no/off/空/随机值) → 不触发拒绝(允许)。"""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("I18N_ALLOW_FALLBACK", non_true_value)

    # 不应 raise(只有真值才触发)
    guard_module.assert_no_test_escape_hatches(caller="test")


# ══════════════════════════════════════════════════════════════════
# 测试 9: 大小写与空格不敏感
# ══════════════════════════════════════════════════════════════════
def test_case_insensitive_true_values(guard_module, clean_env, monkeypatch):
    """TRUE / True / YES / On 等大小写变体 → 都视为真值。"""
    from services.error_codes import AppError

    for val in ("TRUE", "True", "YES", "On"):
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("BYPASS", val)
        with pytest.raises(AppError):
            guard_module.assert_no_test_escape_hatches(caller="test")
        monkeypatch.delenv("BYPASS", raising=False)


def test_whitespace_stripped(guard_module, clean_env, monkeypatch):
    """值前后有空格 → 去除后判定。"""
    from services.error_codes import AppError

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("I18N_ALLOW_FALLBACK", "  1  ")

    with pytest.raises(AppError):
        guard_module.assert_no_test_escape_hatches(caller="test")


# ══════════════════════════════════════════════════════════════════
# 测试 10: ESCAPE_HATCH_REGISTRY 完整性
# ══════════════════════════════════════════════════════════════════
def test_escape_hatch_registry_completeness(guard_module):
    """ESCAPE_HATCH_REGISTRY 必须包含 R70 Wave 3 §1 列出的所有变量。"""
    registry = guard_module.ESCAPE_HATCH_REGISTRY
    var_names = {name for name, _ in registry}

    # R70 Wave 3 §1 要求的硬守卫变量清单
    required_vars = {
        "I18N_ALLOW_FALLBACK",
        "ALLOW_LEGACY_RESTORE",
        "TEST_ONLY",
        "DEV_ONLY",
        "BYPASS",
        "SKIP_VERIFY",
    }
    for var in required_vars:
        assert var in var_names, f"ESCAPE_HATCH_REGISTRY 缺少必需变量: {var}"


def test_registry_entries_well_formed(guard_module):
    """每个 registry 条目必须是 (str, str) 元组,描述非空。"""
    for entry in guard_module.ESCAPE_HATCH_REGISTRY:
        assert isinstance(entry, tuple), f"条目必须是 tuple: {entry}"
        assert len(entry) == 2, f"条目必须是 2-tuple: {entry}"
        var_name, description = entry
        assert isinstance(var_name, str) and var_name, f"变量名必须非空 str: {entry}"
        assert isinstance(description, str) and description, (
            f"描述必须非空 str: {entry}"
        )


# ══════════════════════════════════════════════════════════════════
# 测试 11: 辅助函数 list_escape_hatch_env_vars / is_escape_hatch_var
# ══════════════════════════════════════════════════════════════════
def test_list_escape_hatch_env_vars(guard_module):
    """list_escape_hatch_env_vars 返回所有登记的变量名列表。"""
    var_list = guard_module.list_escape_hatch_env_vars()
    assert isinstance(var_list, list)
    assert "I18N_ALLOW_FALLBACK" in var_list
    assert "ALLOW_LEGACY_RESTORE" in var_list
    assert "BYPASS" in var_list
    # 数量应与 registry 一致
    assert len(var_list) == len(guard_module.ESCAPE_HATCH_REGISTRY)


def test_is_escape_hatch_var_true(guard_module):
    """is_escape_hatch_var 对已登记变量返回 True。"""
    assert guard_module.is_escape_hatch_var("I18N_ALLOW_FALLBACK") is True
    assert guard_module.is_escape_hatch_var("ALLOW_LEGACY_RESTORE") is True
    assert guard_module.is_escape_hatch_var("BYPASS") is True


def test_is_escape_hatch_var_false(guard_module):
    """is_escape_hatch_var 对未登记变量返回 False。"""
    assert guard_module.is_escape_hatch_var("NOT_REGISTERED") is False
    assert guard_module.is_escape_hatch_var("") is False
    assert guard_module.is_escape_hatch_var("APP_ENV") is False


# ══════════════════════════════════════════════════════════════════
# 测试 12: _detect_production_like_from_os_environ 行为
# ══════════════════════════════════════════════════════════════════
def test_detect_production_like_with_app_env_production(guard_module, clean_env, monkeypatch):
    """APP_ENV=production → _detect_production_like_from_os_environ 返回 True。"""
    monkeypatch.setenv("APP_ENV", "production")
    assert guard_module._detect_production_like_from_os_environ() is True


def test_detect_production_like_with_app_env_staging(guard_module, clean_env, monkeypatch):
    """APP_ENV=staging → _detect_production_like_from_os_environ 返回 True。"""
    monkeypatch.setenv("APP_ENV", "staging")
    assert guard_module._detect_production_like_from_os_environ() is True


def test_detect_production_like_with_app_env_test(guard_module, clean_env, monkeypatch):
    """APP_ENV=test → _detect_production_like_from_os_environ 返回 False。"""
    monkeypatch.setenv("APP_ENV", "test")
    assert guard_module._detect_production_like_from_os_environ() is False


def test_detect_production_like_with_app_env_development(guard_module, clean_env, monkeypatch):
    """APP_ENV=development → 返回 False。"""
    monkeypatch.setenv("APP_ENV", "development")
    assert guard_module._detect_production_like_from_os_environ() is False


def test_detect_production_like_with_no_env(guard_module, clean_env):
    """无任何环境变量 → 返回 False(降级为 development)。"""
    assert guard_module._detect_production_like_from_os_environ() is False


# ══════════════════════════════════════════════════════════════════
# 测试 13: 别名 prod/stg 也视为 production/staging
# ══════════════════════════════════════════════════════════════════
def test_alias_prod_treated_as_production(guard_module, clean_env, monkeypatch):
    """APP_ENV=prod 别名 → 视为 production,逃生舱拒绝。"""
    from services.error_codes import AppError

    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("I18N_ALLOW_FALLBACK", "1")

    with pytest.raises(AppError):
        guard_module.assert_no_test_escape_hatches(caller="test")


def test_alias_stg_treated_as_staging(guard_module, clean_env, monkeypatch):
    """APP_ENV=stg 别名 → 视为 staging,逃生舱拒绝。"""
    from services.error_codes import AppError

    monkeypatch.setenv("APP_ENV", "stg")
    monkeypatch.setenv("BYPASS", "1")

    with pytest.raises(AppError):
        guard_module.assert_no_test_escape_hatches(caller="test")


# ══════════════════════════════════════════════════════════════════
# 测试 14: ENVIRONMENT/DEPLOY_ENV 也能触发 production 判定
# ══════════════════════════════════════════════════════════════════
def test_environment_var_triggers_production(guard_module, clean_env, monkeypatch):
    """仅设置 ENVIRONMENT=production(legacy 变量) → 视为生产,逃生舱拒绝。"""
    from services.error_codes import AppError

    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("I18N_ALLOW_FALLBACK", "1")

    with pytest.raises(AppError):
        guard_module.assert_no_test_escape_hatches(caller="test")


def test_deploy_env_var_triggers_production(guard_module, clean_env, monkeypatch):
    """仅设置 DEPLOY_ENV=production → 视为生产,逃生舱拒绝。"""
    from services.error_codes import AppError

    monkeypatch.setenv("DEPLOY_ENV", "production")
    monkeypatch.setenv("ALLOW_LEGACY_RESTORE", "1")

    with pytest.raises(AppError):
        guard_module.assert_no_test_escape_hatches(caller="test")


# ══════════════════════════════════════════════════════════════════
# 测试 15: caller 参数正确传入诊断信息
# ══════════════════════════════════════════════════════════════════
def test_caller_in_error_message(guard_module, clean_env, monkeypatch):
    """caller 参数应出现在错误 params 中,便于诊断。"""
    from services.error_codes import AppError

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("BYPASS", "1")

    with pytest.raises(AppError) as exc_info:
        guard_module.assert_no_test_escape_hatches(caller="custom_caller_xyz")

    # caller 应该出现在 params 中(safe_params 已包含 caller)
    params = exc_info.value.params
    assert params.get("caller") == "custom_caller_xyz"


def test_caller_default_empty(guard_module, clean_env, monkeypatch):
    """不传 caller → 默认 "unknown",仍正常工作。"""
    from services.error_codes import AppError

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("BYPASS", "1")

    with pytest.raises(AppError):
        guard_module.assert_no_test_escape_hatches()


# ══════════════════════════════════════════════════════════════════
# 测试 16: RELEASE_BUILD 严格模式不影响守卫(守卫独立于 RELEASE_BUILD)
# ══════════════════════════════════════════════════════════════════
def test_release_build_does_not_bypass_guard(guard_module, clean_env, monkeypatch):
    """RELEASE_BUILD=1 不应绕过守卫 — 守卫独立于 RELEASE_BUILD。

    这里的逻辑是:守卫只关心 APP_ENV 是否为 production/staging,
    与 RELEASE_BUILD 无关。RELEASE_BUILD 只影响 i18n 内部的严格模式判定。
    守卫应保持独立,任何 production/staging + 逃生舱都拒绝。
    """
    from services.error_codes import AppError

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("RELEASE_BUILD", "1")
    monkeypatch.setenv("I18N_ALLOW_FALLBACK", "1")

    with pytest.raises(AppError):
        guard_module.assert_no_test_escape_hatches(caller="test")


# ══════════════════════════════════════════════════════════════════
# 测试 17: __all__ 导出完整
# ══════════════════════════════════════════════════════════════════
def test_module_all_exports(guard_module):
    """__all__ 必须包含所有公开 API。"""
    expected_exports = {
        "ESCAPE_HATCH_REGISTRY",
        "assert_no_test_escape_hatches",
        "list_escape_hatch_env_vars",
        "is_escape_hatch_var",
    }
    actual_exports = set(guard_module.__all__)
    for name in expected_exports:
        assert name in actual_exports, f"__all__ 缺少 {name}"


# ══════════════════════════════════════════════════════════════════
# 测试 18: 所有逃生舱变量都被 production 守卫
# ══════════════════════════════════════════════════════════════════
def test_all_registered_hatches_blocked_in_production(
    guard_module, clean_env, monkeypatch
):
    """遍历 ESCAPE_HATCH_REGISTRY 中每个变量,验证 production 下都被拒绝。"""
    from services.error_codes import AppError

    for var_name, _ in guard_module.ESCAPE_HATCH_REGISTRY:
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv(var_name, "1")

        with pytest.raises(AppError):
            guard_module.assert_no_test_escape_hatches(caller="test")

        monkeypatch.delenv(var_name, raising=False)
