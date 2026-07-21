"""R70 Wave 1: APP_ENV 单一事实源 — 测试 config.environment 模块。

R70 P0-03 根因修复的回归测试:
    旧版 Settings.APP_ENV 默认值为 "development"(非空),导致只设置
    ENVIRONMENT=production 的旧部署会静默降级为 development。
    本测试验证新的 config.environment.parse_app_env() 在以下场景的行为:

测试矩阵(对应 R70 Wave 1 §9 要求):
    1. 仅 APP_ENV=production
    2. 仅 ENVIRONMENT=production(legacy 迁移)
    3. 仅 DEPLOY_ENV=production(legacy 迁移)
    4. APP_ENV 与 ENVIRONMENT 同值(允许 + 弃用告警)
    5. APP_ENV 与 ENVIRONMENT 冲突(拒绝启动)
    6. 三变量同时冲突(拒绝启动)
    7. 三变量全部缺失 + allow_default_development=True → development
    8. 三变量全部缺失 + allow_default_development=False → 拒绝启动
    9. 未知值(拒绝启动)
    10. 大小写与空格(规范化后接受)
    11. 别名 prod/stg(规范化 + 弃用告警)
    12. _production_guard 在 legacy 变量模式下仍生效
"""
from __future__ import annotations

import os
import sys
import types

import pytest


# ──────────────────────────────────────────────────────────────────
# 测试隔离:直接加载 config.environment 模块,绕过 config/__init__.py
# (config/__init__.py 会触发 Settings 实例化,依赖 .env 与 secrets)
# ──────────────────────────────────────────────────────────────────
def _load_environment_module():
    """直接加载 config.environment 模块,不触发 config/__init__.py。"""
    if "config.environment" in sys.modules:
        # 若已加载(可能被 conftest.py 安装的 fake config 影响),先清理
        # 但保留 fake config 主模块,避免破坏其他测试
        pass

    # 构造独立的 config 包占位对象(不执行 __init__.py)
    if "config.environment" not in sys.modules or not hasattr(
        sys.modules.get("config.environment", None), "parse_app_env"
    ):
        import importlib.util
        from pathlib import Path

        env_path = Path(__file__).resolve().parent.parent / "config" / "environment.py"
        spec = importlib.util.spec_from_file_location("config.environment", env_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["config.environment"] = module
        spec.loader.exec_module(module)
    return sys.modules["config.environment"]


@pytest.fixture
def env_module():
    """提供 config.environment 模块实例。"""
    return _load_environment_module()


@pytest.fixture
def clean_env(monkeypatch):
    """清理环境变量,确保测试隔离。"""
    for var in ("APP_ENV", "ENVIRONMENT", "DEPLOY_ENV"):
        monkeypatch.delenv(var, raising=False)
    yield
    # monkeypatch 会自动恢复


# ══════════════════════════════════════════════════════════════════
# 测试 1:仅 APP_ENV=production
# ══════════════════════════════════════════════════════════════════
def test_only_app_env_production(env_module, clean_env, monkeypatch):
    """仅设置 APP_ENV=production → 解析为 PRODUCTION。"""
    monkeypatch.setenv("APP_ENV", "production")
    result = env_module.parse_app_env(allow_default_development=False)
    assert result == env_module.AppEnvironment.PRODUCTION
    assert result.value == "production"


def test_only_app_env_staging(env_module, clean_env, monkeypatch):
    """仅设置 APP_ENV=staging → 解析为 STAGING。"""
    monkeypatch.setenv("APP_ENV", "staging")
    result = env_module.parse_app_env(allow_default_development=False)
    assert result == env_module.AppEnvironment.STAGING


def test_only_app_env_test(env_module, clean_env, monkeypatch):
    """仅设置 APP_ENV=test → 解析为 TEST。"""
    monkeypatch.setenv("APP_ENV", "test")
    result = env_module.parse_app_env(allow_default_development=False)
    assert result == env_module.AppEnvironment.TEST


def test_only_app_env_development(env_module, clean_env, monkeypatch):
    """仅设置 APP_ENV=development → 解析为 DEVELOPMENT。"""
    monkeypatch.setenv("APP_ENV", "development")
    result = env_module.parse_app_env(allow_default_development=False)
    assert result == env_module.AppEnvironment.DEVELOPMENT


# ══════════════════════════════════════════════════════════════════
# 测试 2 & 3:仅 ENVIRONMENT / DEPLOY_ENV(legacy 迁移)
# ══════════════════════════════════════════════════════════════════
def test_only_environment_production(env_module, clean_env, monkeypatch, capsys):
    """仅设置 ENVIRONMENT=production(legacy)→ 解析为 PRODUCTION + 弃用告警。"""
    monkeypatch.setenv("ENVIRONMENT", "production")
    result = env_module.parse_app_env(allow_default_development=False)
    assert result == env_module.AppEnvironment.PRODUCTION
    captured = capsys.readouterr()
    assert "DEPRECATION" in captured.err
    assert "ENVIRONMENT" in captured.err


def test_only_deploy_env_staging(env_module, clean_env, monkeypatch, capsys):
    """仅设置 DEPLOY_ENV=staging(legacy)→ 解析为 STAGING + 弃用告警。"""
    monkeypatch.setenv("DEPLOY_ENV", "staging")
    result = env_module.parse_app_env(allow_default_development=False)
    assert result == env_module.AppEnvironment.STAGING
    captured = capsys.readouterr()
    assert "DEPRECATION" in captured.err
    assert "DEPLOY_ENV" in captured.err


# ══════════════════════════════════════════════════════════════════
# 测试 4:APP_ENV 与 ENVIRONMENT 同值(允许 + 弃用告警)
# ══════════════════════════════════════════════════════════════════
def test_app_env_and_environment_same_value(env_module, clean_env, monkeypatch, capsys):
    """APP_ENV=production + ENVIRONMENT=production → 允许,但输出弃用告警。"""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ENVIRONMENT", "production")
    result = env_module.parse_app_env(allow_default_development=False)
    assert result == env_module.AppEnvironment.PRODUCTION
    captured = capsys.readouterr()
    assert "DEPRECATION" in captured.err


# ══════════════════════════════════════════════════════════════════
# 测试 5:APP_ENV 与 ENVIRONMENT 冲突(拒绝启动)
# ══════════════════════════════════════════════════════════════════
def test_app_env_environment_conflict(env_module, clean_env, monkeypatch):
    """APP_ENV=production + ENVIRONMENT=staging → 冲突 → 拒绝启动。"""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ENVIRONMENT", "staging")
    with pytest.raises(env_module.EnvironmentResolutionError) as exc_info:
        env_module.parse_app_env(allow_default_development=False)
    assert "冲突" in str(exc_info.value) or "conflict" in str(exc_info.value).lower()
    assert "APP_ENV" in exc_info.value.conflict_vars
    assert "ENVIRONMENT" in exc_info.value.conflict_vars


# ══════════════════════════════════════════════════════════════════
# 测试 6:三变量同时冲突(拒绝启动)
# ══════════════════════════════════════════════════════════════════
def test_three_way_conflict(env_module, clean_env, monkeypatch):
    """APP_ENV=production + ENVIRONMENT=staging + DEPLOY_ENV=test → 冲突 → 拒绝启动。"""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("DEPLOY_ENV", "test")
    with pytest.raises(env_module.EnvironmentResolutionError):
        env_module.parse_app_env(allow_default_development=False)


# ══════════════════════════════════════════════════════════════════
# 测试 7:三变量全部缺失 + allow_default_development=True → development
# ══════════════════════════════════════════════════════════════════
def test_all_missing_allow_default_dev(env_module, clean_env):
    """三变量全缺失 + allow_default_development=True → 回退 development(本地开发命令)。"""
    result = env_module.parse_app_env(allow_default_development=True)
    assert result == env_module.AppEnvironment.DEVELOPMENT


# ══════════════════════════════════════════════════════════════════
# 测试 8:三变量全部缺失 + allow_default_development=False → 拒绝启动
# ══════════════════════════════════════════════════════════════════
def test_all_missing_production_fail_closed(env_module, clean_env):
    """三变量全缺失 + allow_default_development=False → 生产入口 fail-closed。"""
    with pytest.raises(env_module.EnvironmentResolutionError) as exc_info:
        env_module.parse_app_env(allow_default_development=False)
    assert "全部缺失" in str(exc_info.value) or "fail-closed" in str(exc_info.value)


# ══════════════════════════════════════════════════════════════════
# 测试 9:未知值(拒绝启动)
# ══════════════════════════════════════════════════════════════════
def test_unknown_value_rejected(env_module, clean_env, monkeypatch):
    """APP_ENV=prodution(拼写错误)→ 拒绝启动。"""
    monkeypatch.setenv("APP_ENV", "prodution")  # typo
    with pytest.raises(env_module.EnvironmentResolutionError):
        env_module.parse_app_env(allow_default_development=False)


def test_unknown_value_empty_string(env_module, clean_env, monkeypatch):
    """APP_ENV='' (空字符串)→ 视为缺失 → 由 allow_default_development 决定。"""
    monkeypatch.setenv("APP_ENV", "")
    with pytest.raises(env_module.EnvironmentResolutionError):
        env_module.parse_app_env(allow_default_development=False)


# ══════════════════════════════════════════════════════════════════
# 测试 10:大小写与空格(规范化后接受)
# ══════════════════════════════════════════════════════════════════
def test_case_insensitive(env_module, clean_env, monkeypatch):
    """APP_ENV=PRODUCTION(大写)→ 规范化为 production → 接受。"""
    monkeypatch.setenv("APP_ENV", "PRODUCTION")
    result = env_module.parse_app_env(allow_default_development=False)
    assert result == env_module.AppEnvironment.PRODUCTION


def test_whitespace_trimmed(env_module, clean_env, monkeypatch):
    """APP_ENV='  production  '(带空格)→ 规范化为 production → 接受。"""
    monkeypatch.setenv("APP_ENV", "  production  ")
    result = env_module.parse_app_env(allow_default_development=False)
    assert result == env_module.AppEnvironment.PRODUCTION


def test_mixed_case_with_spaces(env_module, clean_env, monkeypatch):
    """APP_ENV='  Production '(混合大小写 + 空格)→ 规范化为 production → 接受。"""
    monkeypatch.setenv("APP_ENV", "  Production ")
    result = env_module.parse_app_env(allow_default_development=False)
    assert result == env_module.AppEnvironment.PRODUCTION


# ══════════════════════════════════════════════════════════════════
# 测试 11:别名 prod/stg(规范化 + 弃用告警)
# ══════════════════════════════════════════════════════════════════
def test_alias_prod_normalized(env_module, clean_env, monkeypatch, capsys):
    """APP_ENV=prod → 规范化为 production + 弃用告警。"""
    monkeypatch.setenv("APP_ENV", "prod")
    result = env_module.parse_app_env(allow_default_development=False)
    assert result == env_module.AppEnvironment.PRODUCTION
    captured = capsys.readouterr()
    assert "DEPRECATION" in captured.err
    assert "prod" in captured.err


def test_alias_stg_normalized(env_module, clean_env, monkeypatch, capsys):
    """APP_ENV=stg → 规范化为 staging + 弃用告警。"""
    monkeypatch.setenv("APP_ENV", "stg")
    result = env_module.parse_app_env(allow_default_development=False)
    assert result == env_module.AppEnvironment.STAGING
    captured = capsys.readouterr()
    assert "DEPRECATION" in captured.err


def test_alias_prod_uppercase(env_module, clean_env, monkeypatch):
    """APP_ENV=PROD(大写别名)→ 规范化为 production → 接受。"""
    monkeypatch.setenv("APP_ENV", "PROD")
    result = env_module.parse_app_env(allow_default_development=False)
    assert result == env_module.AppEnvironment.PRODUCTION


# ══════════════════════════════════════════════════════════════════
# 测试 12:_production_guard 在 legacy 变量模式下仍生效
# ══════════════════════════════════════════════════════════════════
def test_detect_production_from_os_environ_app_env(env_module, clean_env, monkeypatch):
    """detect_production_from_os_environ() 检测 APP_ENV=production。"""
    monkeypatch.setenv("APP_ENV", "production")
    is_prod, source = env_module.detect_production_from_os_environ()
    assert is_prod is True
    assert source == "APP_ENV"


def test_detect_production_from_os_environ_environment(env_module, clean_env, monkeypatch):
    """detect_production_from_os_environ() 检测 ENVIRONMENT=production(legacy)。"""
    monkeypatch.setenv("ENVIRONMENT", "production")
    is_prod, source = env_module.detect_production_from_os_environ()
    assert is_prod is True
    assert source == "ENVIRONMENT"


def test_detect_production_from_os_environ_deploy_env(env_module, clean_env, monkeypatch):
    """detect_production_from_os_environ() 检测 DEPLOY_ENV=production(legacy)。"""
    monkeypatch.setenv("DEPLOY_ENV", "production")
    is_prod, source = env_module.detect_production_from_os_environ()
    assert is_prod is True
    assert source == "DEPLOY_ENV"


def test_detect_production_from_os_environ_staging(env_module, clean_env, monkeypatch):
    """detect_production_from_os_environ() 检测 staging。"""
    monkeypatch.setenv("APP_ENV", "staging")
    is_prod, source = env_module.detect_production_from_os_environ()
    assert is_prod is True
    assert source == "APP_ENV"


def test_detect_production_from_os_environ_alias(env_module, clean_env, monkeypatch):
    """detect_production_from_os_environ() 检测别名 prod/stg。"""
    monkeypatch.setenv("APP_ENV", "prod")
    is_prod, source = env_module.detect_production_from_os_environ()
    assert is_prod is True
    assert source == "APP_ENV"


def test_detect_production_from_os_environ_dev(env_module, clean_env, monkeypatch):
    """detect_production_from_os_environ() 在 development 下返回 False。"""
    monkeypatch.setenv("APP_ENV", "development")
    is_prod, source = env_module.detect_production_from_os_environ()
    assert is_prod is False
    assert source == ""


def test_detect_production_from_os_environ_empty(env_module, clean_env):
    """detect_production_from_os_environ() 在三变量全缺失时返回 False(不抛异常)。"""
    is_prod, source = env_module.detect_production_from_os_environ()
    assert is_prod is False
    assert source == ""


def test_detect_production_from_os_environ_unknown_value(env_module, clean_env, monkeypatch):
    """detect_production_from_os_environ() 在未知值时返回 False(不抛异常,守卫模式)。"""
    monkeypatch.setenv("APP_ENV", "prodution")  # typo
    is_prod, source = env_module.detect_production_from_os_environ()
    assert is_prod is False
    assert source == ""


# ══════════════════════════════════════════════════════════════════
# 测试 13:is_production / is_production_like 辅助函数
# ══════════════════════════════════════════════════════════════════
def test_is_production_strict(env_module):
    """is_production() 严格判定 production(不含 staging)。"""
    assert env_module.is_production(env_module.AppEnvironment.PRODUCTION) is True
    assert env_module.is_production(env_module.AppEnvironment.STAGING) is False
    assert env_module.is_production(env_module.AppEnvironment.DEVELOPMENT) is False
    assert env_module.is_production(env_module.AppEnvironment.TEST) is False


def test_is_production_like_includes_staging(env_module):
    """is_production_like() 包含 production + staging。"""
    assert env_module.is_production_like(env_module.AppEnvironment.PRODUCTION) is True
    assert env_module.is_production_like(env_module.AppEnvironment.STAGING) is True
    assert env_module.is_production_like(env_module.AppEnvironment.DEVELOPMENT) is False
    assert env_module.is_production_like(env_module.AppEnvironment.TEST) is False


def test_is_production_accepts_string(env_module):
    """is_production() / is_production_like() 接受字符串参数。"""
    assert env_module.is_production("production") is True
    assert env_module.is_production("staging") is False
    assert env_module.is_production_like("staging") is True
    assert env_module.is_production_like("PRODUCTION") is True  # 大小写不敏感
    assert env_module.is_production_like("  staging  ") is True  # 空格


def test_is_production_rejects_alias(env_module):
    """is_production() 不接受别名 prod/stg(只接受规范值)。

    别名规范化由 parse_app_env() 处理,辅助函数只看规范值。
    """
    assert env_module.is_production("prod") is False
    assert env_module.is_production_like("stg") is False


# ══════════════════════════════════════════════════════════════════
# 测试 14:raw_overrides 参数(供 Settings 的 before-validator 使用)
# ══════════════════════════════════════════════════════════════════
def test_raw_overrides_takes_precedence(env_module, clean_env, monkeypatch):
    """raw_overrides 优先于 os.environ(供 Settings before-validator 使用)。"""
    # os.environ 设置了 development,但 raw_overrides 设置 production → 用 raw_overrides
    monkeypatch.setenv("APP_ENV", "development")
    result = env_module.parse_app_env(
        allow_default_development=False,
        raw_overrides={"APP_ENV": "production", "ENVIRONMENT": "", "DEPLOY_ENV": ""},
    )
    assert result == env_module.AppEnvironment.PRODUCTION


def test_raw_overrides_conflict_detected(env_module, clean_env):
    """raw_overrides 中的冲突也被检测。"""
    with pytest.raises(env_module.EnvironmentResolutionError):
        env_module.parse_app_env(
            allow_default_development=False,
            raw_overrides={"APP_ENV": "production", "ENVIRONMENT": "staging", "DEPLOY_ENV": ""},
        )


def test_raw_overrides_falls_back_to_environ(env_module, clean_env, monkeypatch):
    """raw_overrides 中某变量为空时,降级读取 os.environ。"""
    monkeypatch.setenv("APP_ENV", "production")
    result = env_module.parse_app_env(
        allow_default_development=False,
        raw_overrides={"APP_ENV": "", "ENVIRONMENT": "", "DEPLOY_ENV": ""},
    )
    assert result == env_module.AppEnvironment.PRODUCTION


# ══════════════════════════════════════════════════════════════════
# 测试 15:AppEnvironment 枚举完整性
# ══════════════════════════════════════════════════════════════════
def test_app_environment_enum_values(env_module):
    """AppEnvironment 枚举只有 4 个值:development/test/staging/production。"""
    values = {e.value for e in env_module.AppEnvironment}
    assert values == {"development", "test", "staging", "production"}


def test_app_environment_str_enum(env_module):
    """AppEnvironment 是 str Enum,可直接比较字符串。"""
    assert env_module.AppEnvironment.PRODUCTION == "production"
    assert env_module.AppEnvironment.STAGING == "staging"
