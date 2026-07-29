"""R76 O1: Secretless Settings 矩阵测试。

覆盖 docs/tgjiema R76 整改报告 10.O-O1 要求的 12+ 组矩阵:
  - ci+contract 成功
  - ci+minio 成功
  - production+contract 失败
  - production+minio 失败
  - 缺 base URL 失败
  - secretless 误指 Telegram 失败
  - 缺临时 S3 key 失败
  - secretless 在 production 失败
  - secretless 缺 contract token 失败
  - secretless 配置 R2 endpoint 失败
  - staging+minio 失败
  - telegram+default 生产配置成功

Settings 通过环境变量加载,测试使用 monkeypatch.setenv 精确控制环境。
所有测试必须 fail-closed:不允许的配置组合必须抛 ValueError。

注意: tests/conftest.py 会向 sys.modules 注入 mock config 模块,覆盖真实
config 包。本测试必须用 importlib.util.spec_from_file_location 直接按文件
路径加载 config/settings.py,绕过 mock 注入,确保测试的是真实 Settings 类。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


_SETTINGS_PATH = Path(__file__).resolve().parent.parent / "config" / "settings.py"
_ENVIRONMENT_PATH = Path(__file__).resolve().parent.parent / "config" / "environment.py"


def _load_real_settings_class():
    """直接按文件路径加载 config/settings.py 的 Settings 类。

    每次 reload 确保 Settings 类重新执行 module-level 代码(包括
    `settings = Settings()` 单例构造),从而精确反映当前环境变量。

    注意: module-level `settings = Settings()` 会在 exec_module 时执行,
    此时如果 APP_ENV 等未设置,会触发 fail-closed ValueError。
    因此在加载前临时设置 APP_ENV=development 让 module-level 通过,
    返回的 Settings 类供测试重新构造实例(此时再使用测试 monkeypatch 的环境)。
    """
    import os

    # 先确保 config.environment 真实模块加载(被 conftest mock 的可能存在)
    env_spec = importlib.util.spec_from_file_location(
        "config.environment", _ENVIRONMENT_PATH
    )
    env_module = importlib.util.module_from_spec(env_spec)
    sys.modules["config.environment"] = env_module
    env_spec.loader.exec_module(env_module)

    # 加载前临时设置 APP_ENV=development 让 module-level `settings = Settings()` 通过
    saved = {}
    bootstrap_env = {
        "APP_ENV": "development",
        "ENVIRONMENT": "development",
        "DEPLOY_ENV": "",
        "ALLOW_LEGACY_RESTORE": "",
        # Settings 的 module-level 单例仍执行角色级必填字段校验；测试加载阶段
        # 必须显式进入 CI 模式，之后 finally 会恢复，由 monkeypatch 控制用例环境。
        "CI": "true",
        "GITHUB_ACTIONS": "",
    }
    for var, value in bootstrap_env.items():
        saved[var] = os.environ.get(var)
        os.environ[var] = value

    try:
        # 加载真实 config.settings 模块(独立命名空间,避免与 conftest mock 冲突)
        spec = importlib.util.spec_from_file_location(
            "_r76_real_config_settings", _SETTINGS_PATH
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        # 恢复环境变量(让测试 monkeypatch 控制)
        for var, val in saved.items():
            if val is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = val

    return module.Settings


def _clear_secretless_env(monkeypatch):
    """清除所有 secretless 相关环境变量,确保测试隔离。"""
    for var in (
        "APP_ENV", "ENVIRONMENT", "DEPLOY_ENV",
        "PROVIDER_BACKEND", "PROVIDER_BASE_URL", "PROVIDER_CONTRACT_TOKEN",
        "OBJECT_STORAGE_BACKEND", "S3_ENDPOINT_URL",
        "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "S3_BUCKET_NAME",
        "SECRETLESS_MODE", "R2_ENDPOINT", "R2_ACCOUNT_ID",
        "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME",
        "SERVICE_ROLE", "CI", "GITHUB_ACTIONS",
        "ALLOW_LEGACY_RESTORE",
    ):
        monkeypatch.delenv(var, raising=False)


def _make_settings(monkeypatch, env_overrides: dict[str, str]):
    """用指定环境变量构造 Settings 实例。

    APP_ENV 必须显式设置(parse_app_env 要求),test/development 允许。
    """
    Settings = _load_real_settings_class()
    _clear_secretless_env(monkeypatch)
    for k, v in env_overrides.items():
        monkeypatch.setenv(k, v)
    return Settings()


# ── 正向用例 ──────────────────────────────────────────────────────────────

def test_ci_contract_minio_success(monkeypatch):
    """矩阵 1: CI+contract+minio 全部配置正确 → 成功。"""
    s = _make_settings(monkeypatch, {
        "APP_ENV": "test",
        "CI": "true",
        "SECRETLESS_MODE": "true",
        "PROVIDER_BACKEND": "contract",
        "PROVIDER_BASE_URL": "http://provider-sim:8088",
        "PROVIDER_CONTRACT_TOKEN": "ci-test-token-abc123",
        "OBJECT_STORAGE_BACKEND": "minio",
        "S3_ENDPOINT_URL": "http://minio:9000",
        "S3_ACCESS_KEY_ID": "ci_key",
        "S3_SECRET_ACCESS_KEY": "ci_secret",
        "S3_BUCKET_NAME": "tgjiema-test",
    })
    assert s.SECRETLESS_MODE is True
    assert s.PROVIDER_BACKEND == "contract"
    assert s.OBJECT_STORAGE_BACKEND == "minio"


def test_development_contract_minio_success(monkeypatch):
    """矩阵 2: development+contract+minio 成功(本地开发)。"""
    s = _make_settings(monkeypatch, {
        "APP_ENV": "development",
        "CI": "true",
        "SECRETLESS_MODE": "true",
        "PROVIDER_BACKEND": "contract",
        "PROVIDER_BASE_URL": "http://localhost:8088",
        "PROVIDER_CONTRACT_TOKEN": "dev-token",
        "OBJECT_STORAGE_BACKEND": "minio",
        "S3_ENDPOINT_URL": "http://localhost:9000",
        "S3_ACCESS_KEY_ID": "dev_key",
        "S3_SECRET_ACCESS_KEY": "dev_secret",
        "S3_BUCKET_NAME": "dev-bucket",
    })
    assert s.SECRETLESS_MODE is True


def test_production_telegram_r2_default_success(monkeypatch):
    """矩阵 3: production+telegram+r2 默认值 → 成功(无 secretless)。

    production 要求 BACKUP_ENCRYPTION_REQUIRED 等被强制,但不要求 secretless。
    """
    # production 走 _validate_all_fields 会要求很多 secrets,
    # 用 SERVICE_ROLE=admin_bot 只校验该角色
    s = _make_settings(monkeypatch, {
        "APP_ENV": "production",
        "SERVICE_ROLE": "admin_bot",
        "ADMIN_BOT_TOKEN": "placeholder:prod",
        "ADMIN_TELEGRAM_ID": "123456",
        "PROVIDER_BACKEND": "telegram",
        "OBJECT_STORAGE_BACKEND": "r2",
        "SECRETLESS_MODE": "false",
    })
    assert s.PROVIDER_BACKEND == "telegram"
    assert s.OBJECT_STORAGE_BACKEND == "r2"


# ── 负向用例 ──────────────────────────────────────────────────────────────

def test_production_secretless_mode_fails(monkeypatch):
    """矩阵 4: production+SECRETLESS_MODE=true → 失败。"""
    with pytest.raises(ValueError, match="production 环境禁止 SECRETLESS_MODE"):
        _make_settings(monkeypatch, {
            "APP_ENV": "production",
            "SERVICE_ROLE": "admin_bot",
            "ADMIN_BOT_TOKEN": "placeholder:prod",
            "ADMIN_TELEGRAM_ID": "123456",
            "SECRETLESS_MODE": "true",
            "PROVIDER_BACKEND": "contract",
            "PROVIDER_BASE_URL": "http://provider-sim:8088",
            "PROVIDER_CONTRACT_TOKEN": "x",
            "OBJECT_STORAGE_BACKEND": "minio",
            "S3_ENDPOINT_URL": "http://minio:9000",
            "S3_ACCESS_KEY_ID": "k",
            "S3_SECRET_ACCESS_KEY": "s",
            "S3_BUCKET_NAME": "b",
        })


def test_production_contract_backend_fails(monkeypatch):
    """矩阵 5: production+PROVIDER_BACKEND=contract → 失败(即使非 secretless)。"""
    with pytest.raises(ValueError, match="production 环境禁止 PROVIDER_BACKEND=contract"):
        _make_settings(monkeypatch, {
            "APP_ENV": "production",
            "SERVICE_ROLE": "admin_bot",
            "ADMIN_BOT_TOKEN": "placeholder:prod",
            "ADMIN_TELEGRAM_ID": "123456",
            "PROVIDER_BACKEND": "contract",
            "PROVIDER_BASE_URL": "http://provider-sim:8088",
            "PROVIDER_CONTRACT_TOKEN": "x",
            "SECRETLESS_MODE": "false",
        })


def test_production_minio_backend_fails(monkeypatch):
    """矩阵 6: production+OBJECT_STORAGE_BACKEND=minio → 失败。"""
    with pytest.raises(ValueError, match="production 环境禁止 OBJECT_STORAGE_BACKEND=minio"):
        _make_settings(monkeypatch, {
            "APP_ENV": "production",
            "SERVICE_ROLE": "admin_bot",
            "ADMIN_BOT_TOKEN": "placeholder:prod",
            "ADMIN_TELEGRAM_ID": "123456",
            "OBJECT_STORAGE_BACKEND": "minio",
            "S3_ENDPOINT_URL": "http://minio:9000",
            "S3_ACCESS_KEY_ID": "k",
            "S3_SECRET_ACCESS_KEY": "s",
            "S3_BUCKET_NAME": "b",
            "SECRETLESS_MODE": "false",
        })


def test_secretless_mode_in_staging_fails(monkeypatch):
    """矩阵 7: staging+SECRETLESS_MODE=true → 失败(staging 不在允许集合)。"""
    with pytest.raises(ValueError, match="SECRETLESS_MODE=true 仅允许"):
        _make_settings(monkeypatch, {
            "APP_ENV": "staging",
            "CI": "true",
            "SECRETLESS_MODE": "true",
            "PROVIDER_BACKEND": "contract",
            "PROVIDER_BASE_URL": "http://provider-sim:8088",
            "PROVIDER_CONTRACT_TOKEN": "x",
            "OBJECT_STORAGE_BACKEND": "minio",
            "S3_ENDPOINT_URL": "http://minio:9000",
            "S3_ACCESS_KEY_ID": "k",
            "S3_SECRET_ACCESS_KEY": "s",
            "S3_BUCKET_NAME": "b",
        })


def test_secretless_with_telegram_backend_fails(monkeypatch):
    """矩阵 8: SECRETLESS_MODE=true 但 PROVIDER_BACKEND=telegram → 失败。"""
    with pytest.raises(ValueError, match="PROVIDER_BACKEND 必须为 'contract'"):
        _make_settings(monkeypatch, {
            "APP_ENV": "test",
            "CI": "true",
            "SECRETLESS_MODE": "true",
            "PROVIDER_BACKEND": "telegram",
            "OBJECT_STORAGE_BACKEND": "minio",
            "S3_ENDPOINT_URL": "http://minio:9000",
            "S3_ACCESS_KEY_ID": "k",
            "S3_SECRET_ACCESS_KEY": "s",
            "S3_BUCKET_NAME": "b",
        })


def test_secretless_with_r2_storage_fails(monkeypatch):
    """矩阵 9: SECRETLESS_MODE=true 但 OBJECT_STORAGE_BACKEND=r2 → 失败。"""
    with pytest.raises(ValueError, match="OBJECT_STORAGE_BACKEND 必须为 'minio'"):
        _make_settings(monkeypatch, {
            "APP_ENV": "test",
            "CI": "true",
            "SECRETLESS_MODE": "true",
            "PROVIDER_BACKEND": "contract",
            "PROVIDER_BASE_URL": "http://provider-sim:8088",
            "PROVIDER_CONTRACT_TOKEN": "x",
            "OBJECT_STORAGE_BACKEND": "r2",
        })


def test_secretless_with_telegram_url_fails(monkeypatch):
    """矩阵 10: SECRETLESS_MODE=true 但 PROVIDER_BASE_URL=api.telegram.org → 失败。"""
    with pytest.raises(ValueError, match="禁止指向 api.telegram.org"):
        _make_settings(monkeypatch, {
            "APP_ENV": "test",
            "CI": "true",
            "SECRETLESS_MODE": "true",
            "PROVIDER_BACKEND": "contract",
            "PROVIDER_BASE_URL": "https://api.telegram.org",
            "PROVIDER_CONTRACT_TOKEN": "x",
            "OBJECT_STORAGE_BACKEND": "minio",
            "S3_ENDPOINT_URL": "http://minio:9000",
            "S3_ACCESS_KEY_ID": "k",
            "S3_SECRET_ACCESS_KEY": "s",
            "S3_BUCKET_NAME": "b",
        })


def test_secretless_with_r2_endpoint_fails(monkeypatch):
    """矩阵 11: SECRETLESS_MODE=true 但配置了 R2_ENDPOINT → 失败。"""
    with pytest.raises(ValueError, match="禁止配置 R2_ENDPOINT"):
        _make_settings(monkeypatch, {
            "APP_ENV": "test",
            "CI": "true",
            "SECRETLESS_MODE": "true",
            "PROVIDER_BACKEND": "contract",
            "PROVIDER_BASE_URL": "http://provider-sim:8088",
            "PROVIDER_CONTRACT_TOKEN": "x",
            "OBJECT_STORAGE_BACKEND": "minio",
            "S3_ENDPOINT_URL": "http://minio:9000",
            "S3_ACCESS_KEY_ID": "k",
            "S3_SECRET_ACCESS_KEY": "s",
            "S3_BUCKET_NAME": "b",
            "R2_ENDPOINT": "abc.r2.cloudflarestorage.com",
        })


def test_contract_mode_missing_base_url_fails(monkeypatch):
    """矩阵 12: PROVIDER_BACKEND=contract 但缺 PROVIDER_BASE_URL → 失败。

    注意: PROVIDER_BASE_URL 默认值为 'https://api.telegram.org',
    必须显式设置为空字符串才能测试"缺失"场景。
    """
    with pytest.raises(ValueError, match="PROVIDER_BASE_URL 必须配置"):
        _make_settings(monkeypatch, {
            "APP_ENV": "test",
            "CI": "true",
            "PROVIDER_BACKEND": "contract",
            "PROVIDER_BASE_URL": "",  # 显式清空,覆盖默认值
            "PROVIDER_CONTRACT_TOKEN": "x",
            "OBJECT_STORAGE_BACKEND": "minio",
            "S3_ENDPOINT_URL": "http://minio:9000",
            "S3_ACCESS_KEY_ID": "k",
            "S3_SECRET_ACCESS_KEY": "s",
            "S3_BUCKET_NAME": "b",
        })


def test_contract_mode_missing_token_fails(monkeypatch):
    """矩阵 13: contract 模式缺 PROVIDER_CONTRACT_TOKEN → 失败。"""
    with pytest.raises(ValueError, match="PROVIDER_CONTRACT_TOKEN 必须配置"):
        _make_settings(monkeypatch, {
            "APP_ENV": "test",
            "CI": "true",
            "PROVIDER_BACKEND": "contract",
            "PROVIDER_BASE_URL": "http://provider-sim:8088",
            "OBJECT_STORAGE_BACKEND": "minio",
            "S3_ENDPOINT_URL": "http://minio:9000",
            "S3_ACCESS_KEY_ID": "k",
            "S3_SECRET_ACCESS_KEY": "s",
            "S3_BUCKET_NAME": "b",
        })


def test_minio_mode_missing_endpoint_fails(monkeypatch):
    """矩阵 14: minio 模式缺 S3_ENDPOINT_URL → 失败。"""
    with pytest.raises(ValueError, match="S3_ENDPOINT_URL 必须配置"):
        _make_settings(monkeypatch, {
            "APP_ENV": "test",
            "CI": "true",
            "OBJECT_STORAGE_BACKEND": "minio",
            "S3_ACCESS_KEY_ID": "k",
            "S3_SECRET_ACCESS_KEY": "s",
            "S3_BUCKET_NAME": "b",
        })


def test_minio_mode_missing_access_key_fails(monkeypatch):
    """矩阵 15: minio 模式缺 S3_ACCESS_KEY_ID → 失败。"""
    with pytest.raises(ValueError, match="S3_ACCESS_KEY_ID 必须配置"):
        _make_settings(monkeypatch, {
            "APP_ENV": "test",
            "CI": "true",
            "OBJECT_STORAGE_BACKEND": "minio",
            "S3_ENDPOINT_URL": "http://minio:9000",
            "S3_SECRET_ACCESS_KEY": "s",
            "S3_BUCKET_NAME": "b",
        })


def test_minio_mode_missing_secret_fails(monkeypatch):
    """矩阵 16: minio 模式缺 S3_SECRET_ACCESS_KEY → 失败。"""
    with pytest.raises(ValueError, match="S3_SECRET_ACCESS_KEY 必须配置"):
        _make_settings(monkeypatch, {
            "APP_ENV": "test",
            "CI": "true",
            "OBJECT_STORAGE_BACKEND": "minio",
            "S3_ENDPOINT_URL": "http://minio:9000",
            "S3_ACCESS_KEY_ID": "k",
            "S3_BUCKET_NAME": "b",
        })


def test_minio_mode_missing_bucket_fails(monkeypatch):
    """矩阵 17: minio 模式缺 S3_BUCKET_NAME → 失败。"""
    with pytest.raises(ValueError, match="S3_BUCKET_NAME 必须配置"):
        _make_settings(monkeypatch, {
            "APP_ENV": "test",
            "CI": "true",
            "OBJECT_STORAGE_BACKEND": "minio",
            "S3_ENDPOINT_URL": "http://minio:9000",
            "S3_ACCESS_KEY_ID": "k",
            "S3_SECRET_ACCESS_KEY": "s",
        })


def test_staging_minio_backend_fails(monkeypatch):
    """矩阵 18: staging+minio → 失败(staging 不允许 minio)。"""
    with pytest.raises(ValueError, match="仅允许在 APP_ENV=test/development/CI"):
        _make_settings(monkeypatch, {
            "APP_ENV": "staging",
            "CI": "true",
            "OBJECT_STORAGE_BACKEND": "minio",
            "S3_ENDPOINT_URL": "http://minio:9000",
            "S3_ACCESS_KEY_ID": "k",
            "S3_SECRET_ACCESS_KEY": "s",
            "S3_BUCKET_NAME": "b",
            "SECRETLESS_MODE": "false",
        })


def test_no_secretless_with_telegram_default_success(monkeypatch):
    """矩阵 19: 默认 production 不启用 secretless,使用 telegram+r2 → 成功。"""
    # 测试默认值通过(不触发 secretless 校验)
    s = _make_settings(monkeypatch, {
        "APP_ENV": "test",
        "CI": "true",
        "SERVICE_ROLE": "admin_bot",
        "ADMIN_BOT_TOKEN": "test",
        "ADMIN_TELEGRAM_ID": "123",
        # 不设置 SECRETLESS_MODE → 默认 False
        # 不设置 PROVIDER_BACKEND → 默认 telegram
        # 不设置 OBJECT_STORAGE_BACKEND → 默认 r2
    })
    assert s.SECRETLESS_MODE is False
    assert s.PROVIDER_BACKEND == "telegram"
    assert s.OBJECT_STORAGE_BACKEND == "r2"
