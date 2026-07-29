"""R76 O2: Provider 工厂测试。

验证 ``build_provider_client(settings, token)`` 根据 ``PROVIDER_BACKEND`` 选择
正确的实现:
    - ``telegram`` backend → 返回 ``telegram.Bot`` 实例(生产路径)
    - ``contract`` backend → 返回 ``ContractProviderClient`` 实例(secretless CI)

负向验收:
    - 不支持的 backend 抛 ValueError;
    - contract 模式缺 PROVIDER_BASE_URL / PROVIDER_CONTRACT_TOKEN 抛 ValueError。

测试不连接真实 Telegram 或 provider-sim,只验证工厂返回类型和参数校验。
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ════════════════════════════════════════════════════════════════
# 测试辅助:加载真实 settings.py(绕过 conftest.py 的 mock config)
# ════════════════════════════════════════════════════════════════
def _load_real_settings():
    """加载真实 ``config/settings.py`` Settings 类(绕过 conftest mock)。

    conftest.py 在 sys.modules 注入 mock config,导致
    ``from config.settings import Settings`` 拿到 MagicMock。本函数通过
    ``importlib.util.spec_from_file_location`` 直接加载文件,绕过 mock。
    """
    settings_path = Path(__file__).resolve().parent.parent / "config" / "settings.py"
    spec = importlib.util.spec_from_file_location(
        "_r76_test_settings_module", settings_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 settings.py: {settings_path}")
    module = importlib.util.module_from_spec(spec)
    # config.environment 是 settings.py 的依赖,需要先加载
    env_path = Path(__file__).resolve().parent.parent / "config" / "environment.py"
    env_spec = importlib.util.spec_from_file_location("config.environment", env_path)
    if env_spec is None or env_spec.loader is None:
        raise RuntimeError(f"无法加载 environment.py: {env_path}")
    env_module = importlib.util.module_from_spec(env_spec)
    sys.modules["config.environment"] = env_module
    env_spec.loader.exec_module(env_module)
    # 注入 config 包(空模块,settings.py 内部 ``from config.environment import ...``
    # 会从 sys.modules["config.environment"] 拿到 env_module)
    config_pkg = types.ModuleType("config")
    config_pkg.environment = env_module
    sys.modules["config"] = config_pkg
    spec.loader.exec_module(module)
    return module.Settings


# ════════════════════════════════════════════════════════════════
# 测试辅助:构造 mock settings
# ════════════════════════════════════════════════════════════════
def _make_mock_settings(
    *,
    provider_backend: str = "telegram",
    provider_base_url: str = "https://api.telegram.org",
    provider_contract_token: str = "",
    secretless_mode: bool = False,
):
    """构造 mock settings 对象(用于工厂测试)。

    工厂只读取 ``PROVIDER_BACKEND`` / ``PROVIDER_BASE_URL`` /
    ``PROVIDER_CONTRACT_TOKEN`` 三个字段,不需要完整 Settings 实例。
    """
    s = MagicMock(name="mock_settings")
    s.PROVIDER_BACKEND = provider_backend
    s.PROVIDER_BASE_URL = provider_base_url
    s.PROVIDER_CONTRACT_TOKEN = provider_contract_token
    s.SECRETLESS_MODE = secretless_mode
    return s


# ════════════════════════════════════════════════════════════════
# 正向测试
# ════════════════════════════════════════════════════════════════
def _is_real_telegram_available() -> bool:
    """检测 telegram 包是否真实可用(非 conftest.py 注入的 MagicMock)。

    conftest.py 在 telegram 未安装时注入 MagicMock 作为 telegram 模块,
    此时 ``telegram.Bot`` 是 MagicMock 属性(非 type),无法用于 isinstance。
    本函数帮助跳过依赖真实 telegram.Bot 类的测试。
    """
    try:
        import telegram
        return isinstance(getattr(telegram, "Bot", None), type)
    except ImportError:
        return False


@pytest.mark.skipif(
    not _is_real_telegram_available(),
    reason="telegram 包未安装(conftest.py 注入 MagicMock),"
           "无法验证 isinstance(client, telegram.Bot) — 跳过至生产环境验证",
)
def test_factory_telegram_backend_returns_telegram_bot():
    """telegram backend 应返回 telegram.Bot 实例。"""
    # R76 fix: 运行时重新检查 telegram 是否被其他测试文件 mock
    # (test_r41_p1_12 / test_r53_p0_3 在模块级 sys.modules["telegram"] = MagicMock()
    # 会污染整个会话,导致 collection-time skipif 通过但 run-time Bot 不是 type)
    if not _is_real_telegram_available():
        pytest.skip(
            "telegram 包在运行时被 mock(其他测试文件模块级注入),"
            "跳过 isinstance(client, telegram.Bot) 验证"
        )
    from telegram import Bot

    from services.sink_adapters.telegram_adapter import build_provider_client

    settings = _make_mock_settings(
        provider_backend="telegram",
        provider_base_url="https://api.telegram.org",
    )
    client = build_provider_client(settings, "123456:fake-token-for-test")

    assert isinstance(client, Bot), (
        f"telegram backend 应返回 telegram.Bot 实例,实际得到 {type(client).__name__}"
    )


def test_factory_contract_backend_returns_contract_client():
    """contract backend 应返回 ContractProviderClient 实例。"""
    from services.sink_adapters.contract_adapter import ContractProviderClient
    from services.sink_adapters.telegram_adapter import build_provider_client

    settings = _make_mock_settings(
        provider_backend="contract",
        provider_base_url="http://provider-sim:8088",
        provider_contract_token="ci-test-token-abc123",
    )
    client = build_provider_client(settings, "ci-local-token")

    assert isinstance(client, ContractProviderClient), (
        f"contract backend 应返回 ContractProviderClient 实例,"
        f"实际得到 {type(client).__name__}"
    )


def test_factory_contract_backend_propagates_base_url():
    """contract backend 应把 PROVIDER_BASE_URL 传递给 ContractProviderClient。"""
    from services.sink_adapters.telegram_adapter import build_provider_client

    expected_url = "http://provider-sim:8088"
    settings = _make_mock_settings(
        provider_backend="contract",
        provider_base_url=expected_url,
        provider_contract_token="ci-test-token-abc123",
    )
    client = build_provider_client(settings, "ci-local-token")

    # base_url 在 ContractProviderClient 内部去掉末尾 /
    assert client._base_url == expected_url, (
        f"base_url 应为 {expected_url},实际为 {client._base_url}"
    )


def test_factory_contract_backend_propagates_token():
    """contract backend 应把 token 传递给 ContractProviderClient。"""
    from services.sink_adapters.telegram_adapter import build_provider_client

    expected_token = "ci-local-token"
    settings = _make_mock_settings(
        provider_backend="contract",
        provider_base_url="http://provider-sim:8088",
        provider_contract_token="ci-test-token-abc123",
    )
    client = build_provider_client(settings, expected_token)

    assert client._token == expected_token, (
        f"token 应为 {expected_token},实际为 {client._token}"
    )


def test_factory_contract_backend_propagates_contract_token():
    """contract backend 应把 PROVIDER_CONTRACT_TOKEN 传递给 ContractProviderClient。"""
    from services.sink_adapters.telegram_adapter import build_provider_client

    expected_contract_token = "ci-test-token-abc123"
    settings = _make_mock_settings(
        provider_backend="contract",
        provider_base_url="http://provider-sim:8088",
        provider_contract_token=expected_contract_token,
    )
    client = build_provider_client(settings, "ci-local-token")

    assert client._contract_token == expected_contract_token, (
        f"contract_token 应为 {expected_contract_token},"
        f"实际为 {client._contract_token}"
    )


def test_factory_contract_backend_strips_trailing_slash():
    """contract backend 应去掉 base_url 末尾的 /。"""
    from services.sink_adapters.telegram_adapter import build_provider_client

    settings = _make_mock_settings(
        provider_backend="contract",
        provider_base_url="http://provider-sim:8088/",
        provider_contract_token="ci-test-token-abc123",
    )
    client = build_provider_client(settings, "ci-local-token")

    assert client._base_url == "http://provider-sim:8088", (
        f"base_url 应去掉末尾 /,实际为 {client._base_url}"
    )


# ════════════════════════════════════════════════════════════════
# 负向测试
# ════════════════════════════════════════════════════════════════
def test_factory_unsupported_backend_raises_value_error():
    """不支持的 PROVIDER_BACKEND 应抛 ValueError。"""
    from services.sink_adapters.telegram_adapter import build_provider_client

    settings = _make_mock_settings(provider_backend="invalid-backend")
    with pytest.raises(ValueError, match="不支持的 PROVIDER_BACKEND"):
        build_provider_client(settings, "some-token")


def test_factory_contract_backend_missing_base_url_raises():
    """contract 模式缺 PROVIDER_BASE_URL 应抛 AppError(VALIDATION_FAILED)。

    注:实现使用 AppError(ErrorCodes.VALIDATION_FAILED) 而非 ValueError,
    safe_params 仅允许 ``field``(reason 被过滤),故仅校验 code。
    """
    from services.error_codes import AppError, ErrorCodes
    from services.sink_adapters.telegram_adapter import build_provider_client

    settings = _make_mock_settings(
        provider_backend="contract",
        provider_base_url="",
        provider_contract_token="ci-test-token-abc123",
    )
    with pytest.raises(AppError) as exc_info:
        build_provider_client(settings, "ci-local-token")
    assert exc_info.value.code == ErrorCodes.VALIDATION_FAILED


def test_factory_contract_backend_missing_contract_token_raises():
    """contract 模式缺 PROVIDER_CONTRACT_TOKEN 应抛 AppError(VALIDATION_FAILED)。

    注:实现使用 AppError(ErrorCodes.VALIDATION_FAILED) 而非 ValueError,
    safe_params 仅允许 ``field``(reason 被过滤),故仅校验 code。
    """
    from services.error_codes import AppError, ErrorCodes
    from services.sink_adapters.telegram_adapter import build_provider_client

    settings = _make_mock_settings(
        provider_backend="contract",
        provider_base_url="http://provider-sim:8088",
        provider_contract_token="",
    )
    with pytest.raises(AppError) as exc_info:
        build_provider_client(settings, "ci-local-token")
    assert exc_info.value.code == ErrorCodes.VALIDATION_FAILED


# ════════════════════════════════════════════════════════════════
# 协议结构测试
# ════════════════════════════════════════════════════════════════
def test_contract_provider_client_satisfies_provider_protocol():
    """ContractProviderClient 应结构化满足 ProviderClient 协议。"""
    from services.sink_adapters.contract_adapter import ContractProviderClient
    from services.sink_adapters.provider_protocol import ProviderClient

    client = ContractProviderClient(
        base_url="http://provider-sim:8088",
        token="ci-local-token",
        contract_token="ci-test-token",
    )

    # ProviderClient 是 runtime_checkable Protocol,可用 isinstance 检查
    assert isinstance(client, ProviderClient), (
        "ContractProviderClient 应满足 ProviderClient 协议"
    )


def test_contract_provider_file_has_required_attributes():
    """ContractProviderFile 应具有 ProviderFile 协议要求的属性。"""
    from services.sink_adapters.contract_adapter import ContractProviderFile
    from services.sink_adapters.provider_protocol import ProviderFile

    f = ContractProviderFile(
        file_id="sha256:abc123",
        file_unique_id="sha256:abc123",
        file_size=1024,
        file_path="sha256:abc123",
    )

    assert isinstance(f, ProviderFile), (
        "ContractProviderFile 应满足 ProviderFile 协议"
    )
    assert f.file_id == "sha256:abc123"
    assert f.file_unique_id == "sha256:abc123"
    assert f.file_size == 1024
    assert f.file_path == "sha256:abc123"


def test_contract_provider_message_has_required_attributes():
    """ContractProviderMessage 应具有 ProviderMessage 协议要求的属性。"""
    from services.sink_adapters.contract_adapter import ContractProviderMessage
    from services.sink_adapters.provider_protocol import ProviderMessage

    m = ContractProviderMessage(message_id=42, chat_id=12345)

    assert isinstance(m, ProviderMessage), (
        "ContractProviderMessage 应满足 ProviderMessage 协议"
    )
    assert m.message_id == 42
    assert m.chat_id == 12345


# ════════════════════════════════════════════════════════════════
# 后端常量测试
# ════════════════════════════════════════════════════════════════
def test_provider_backend_constants():
    """验证 PROVIDER_BACKEND 常量值。"""
    from services.sink_adapters.provider_protocol import (
        PROVIDER_BACKEND_CONTRACT,
        PROVIDER_BACKEND_TELEGRAM,
        VALID_PROVIDER_BACKENDS,
    )

    assert PROVIDER_BACKEND_TELEGRAM == "telegram"
    assert PROVIDER_BACKEND_CONTRACT == "contract"
    assert VALID_PROVIDER_BACKENDS == frozenset({"telegram", "contract"})
