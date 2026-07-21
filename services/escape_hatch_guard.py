"""R70 Wave 3: 测试逃生舱硬守卫 — 在应用启动最早期阶段拒绝所有 production 逃生舱。

R70 P0-08 根因:
    旧版多个测试逃生舱(I18N_ALLOW_FALLBACK / ALLOW_LEGACY_RESTORE /
    TEST_ONLY / DEV_ONLY / BYPASS / SKIP_VERIFY)在 production/staging 下
    仍可通过环境变量启用,造成生产环境绕过 fail-closed 的风险。

    例如 services/i18n.py 的 _get_i18n_allow_fallback() 优先级:
      1. RELEASE_BUILD → 严格
      2. I18N_ALLOW_FALLBACK=1 → 允许 fallback(测试逃生舱)
      3. production/staging → 严格
    第 2 步在第 3 步之前执行,意味着 production 下设置 I18N_ALLOW_FALLBACK=1
    会绕过严格模式。

R70 Wave 3 整改:
    建立统一 escape_hatch_guard.assert_no_test_escape_hatches() 守卫,
    在应用启动最早期阶段(应用进程入口、Settings 加载后、业务循环启动前)
    调用,检测到任何 production/staging 下的逃生舱变量立即 raise(进程 exit)。

设计原则:
    - **fail-closed**:任何 production/staging 下设置的逃生舱变量 → 拒绝启动
    - **不可绕过**:守卫在 _production_guard 之外加第二道防线,即使单点守卫被
      绕过,本守卫仍会拒绝
    - **机器可验证**:守卫 raise 的异常含明确的 error_code 与诊断
    - **完整覆盖**:扫描所有已知逃生舱变量,新增变量必须在此登记

逃生舱变量清单(随发现新增):
    1. I18N_ALLOW_FALLBACK — i18n locale fallback 逃生舱(services/i18n.py)
    2. ALLOW_LEGACY_RESTORE — legacy restore writer 解封(services/db_restore.py)
    3. TEST_ONLY / DEV_ONLY — 测试/开发模式标记(扫描全仓)
    4. BYPASS / SKIP_VERIFY — 跳过校验(扫描全仓)

调用时机:
    - docker/entrypoint.py:在 exec 业务进程前调用(可阻止镜像启动)
    - services/_production_guard.py:在 assert_no_legacy_restore_in_production 之前
    - config/settings.py 的 after-validator:在 Settings 实例化时调用
    - run_all.py 的 main():在启动业务循环前调用

测试要求:
    - APP_ENV=test 下设置逃生舱变量 → 允许(测试需要)
    - APP_ENV=development 下设置逃生舱变量 → 允许(本地开发需要)
    - APP_ENV=production/staging 下设置逃生舱变量 → 拒绝启动
"""
from __future__ import annotations

import os
from typing import Optional

from services.error_codes import AppError, ErrorCodes


# ──────────────────────────────────────────────────────────────────
# 逃生舱变量登记表(随发现新增)
# ──────────────────────────────────────────────────────────────────
# 每个条目:(env_var_name, 描述, 真值集合)
# 真值集合:这些值代表"启用逃生舱"(任何其他值视为未设置)
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

ESCAPE_HATCH_REGISTRY: list[tuple[str, str]] = [
    # i18n locale fallback 逃生舱(services/i18n.py)
    (
        "I18N_ALLOW_FALLBACK",
        "i18n locale fallback 测试逃生舱(允许在 locale 未绑定时静默 fallback)",
    ),
    # legacy restore writer 解封(services/db_restore.py)
    (
        "ALLOW_LEGACY_RESTORE",
        "legacy restore writer 解封(绕过 capability-seal 调用旧 writer)",
    ),
    # 测试/开发模式标记(扫描全仓)
    (
        "TEST_ONLY",
        "测试专用模式标记(production/staging 下禁止)",
    ),
    (
        "DEV_ONLY",
        "开发专用模式标记(production/staging 下禁止)",
    ),
    # 跳过校验(扫描全仓)
    (
        "BYPASS",
        "通用绕过标记(production/staging 下禁止)",
    ),
    (
        "SKIP_VERIFY",
        "跳过校验标记(production/staging 下禁止)",
    ),
    # 补充:其他可能被用作逃生舱的变量
    (
        "SKIP_VALIDATION",
        "跳过校验标记(production/staging 下禁止)",
    ),
    (
        "ALLOW_INSECURE",
        "允许不安全模式(production/staging 下禁止)",
    ),
]


def _detect_production_like_from_os_environ() -> bool:
    """R70 Wave 3: 直接从 os.environ 检测 production/staging(不依赖 Settings)。

    使用 config.environment.detect_production_from_os_environ() 的逻辑,
    但只返回 bool(简化调用方)。
    """
    try:
        from config.environment import detect_production_from_os_environ
        is_prod, _ = detect_production_from_os_environ()
        return is_prod
    except ImportError:
        # config.environment 不可用时,降级直接检查 APP_ENV/ENVIRONMENT/DEPLOY_ENV
        for var in ("APP_ENV", "ENVIRONMENT", "DEPLOY_ENV"):
            val = os.environ.get(var, "").strip().lower()
            if val in ("production", "staging", "prod", "stg"):
                return True
        return False


def _detect_test_or_development_from_os_environ() -> bool:
    """R70 Wave 3: 检测是否为 test / development 环境(允许逃生舱)。"""
    for var in ("APP_ENV", "ENVIRONMENT", "DEPLOY_ENV"):
        val = os.environ.get(var, "").strip().lower()
        # 别名规范化
        if val in ("prod",):
            val = "production"
        elif val in ("stg",):
            val = "staging"
        if val in ("test", "development"):
            return True
    return False


def assert_no_test_escape_hatches(*, caller: str = "") -> None:
    """R70 Wave 3: 硬守卫 — production/staging 下禁止任何测试逃生舱。

    在应用启动最早期阶段调用。检测到任何逃生舱变量在 production/staging 下
    被设置时立即 raise AppError。

    Args:
        caller: 调用方标识(如 "docker/entrypoint", "Settings.after_validator",
            "run_all.main")。用于诊断。

    Raises:
        AppError(ErrorCodes.PRODUCTION_ESCAPE_HATCH_DETECTED): 当检测到
            production/staging 下设置了逃生舱变量时。

    允许的场景:
        - APP_ENV=test 或 development 下设置逃生舱 → 允许(测试/开发需要)
        - APP_ENV=production/staging 下未设置逃生舱 → 允许(正常生产)
        - 三变量全缺失(本地开发) → 允许(视为 development)

    拒绝的场景:
        - APP_ENV=production/staging 下设置了任何逃生舱变量 → 拒绝启动
    """
    # 1. 检测当前环境
    is_production_like = _detect_production_like_from_os_environ()
    if not is_production_like:
        # 非生产环境,允许逃生舱(测试/开发需要)
        return

    # 2. 扫描所有逃生舱变量
    detected_hatches: list[tuple[str, str, str]] = []  # (var_name, var_value, description)
    for var_name, description in ESCAPE_HATCH_REGISTRY:
        raw_value = os.environ.get(var_name, "").strip().lower()
        if raw_value in _TRUE_VALUES:
            detected_hatches.append((var_name, raw_value, description))

    if not detected_hatches:
        # 生产环境且无逃生舱 → 允许
        return

    # 3. 构造诊断信息
    # 注意: services.error_codes.is_safe_param 会过滤长度 > 100 的字符串值,
    # 因此 hatch_details 必须简短(只列变量名,不含完整描述)。
    # 完整描述已在 ESCAPE_HATCH_REGISTRY 中,可通过 code 审计查询。
    hatch_details = ",".join(
        f"{var_name}={var_value}" for var_name, var_value, _ in detected_hatches
    )

    raise AppError(
        ErrorCodes.PRODUCTION_ESCAPE_HATCH_DETECTED,
        params={
            "caller": caller or "unknown",
            "hatch_count": str(len(detected_hatches)),
            "hatch_details": hatch_details,
            "reason": "r70_wave3_production_escape_hatch_hard_guard",
        },
    )


def list_escape_hatch_env_vars() -> list[str]:
    """R70 Wave 3: 列出所有已登记的逃生舱环境变量名。

    供测试与文档使用,确保登记表完整可审计。
    """
    return [var_name for var_name, _ in ESCAPE_HATCH_REGISTRY]


def is_escape_hatch_var(var_name: str) -> bool:
    """R70 Wave 3: 判断变量是否为已登记的逃生舱。

    供扫描器与 CI 检查使用。
    """
    return var_name in {name for name, _ in ESCAPE_HATCH_REGISTRY}


__all__ = [
    "ESCAPE_HATCH_REGISTRY",
    "assert_no_test_escape_hatches",
    "list_escape_hatch_env_vars",
    "is_escape_hatch_var",
]
