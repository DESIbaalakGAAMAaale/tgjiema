"""R67 P0-06: 生产环境硬隔离守卫 — 物理阻止 legacy restore 公共入口。

R67 审计背景:
    旧直接 restore writer(``run_restore()`` / ``restore_from_backup()`` /
    ``_restore_from_backup_data()``)虽已通过 R65 P0-07 的 capability-seal
    封存,但 seal 仍可通过 ``ALLOW_LEGACY_RESTORE=1`` 环境变量解封(逃生舱)。
    逃生舱仅用于 tests/ 与 scripts/ 兼容场景,但生产镜像中仍存在这些入口,
    一旦攻击者/误操作设置了环境变量,旧 writer 即被解封 — 违反 R67 P0-06:
    "生产镜像不得包含可调用的 legacy restore public entrypoint"。

    R67 P0-06 整改:
      1. 在 ``services/db_restore.py`` 与 ``services/db_backup.py`` 的 legacy
         公共入口(``run_restore`` / ``restore_from_backup``)中增加硬守卫:
         - 若 ``APP_ENV`` 为 ``production`` 或 ``staging``,**无条件** raise
           AppError(不允许环境变量解封)
         - 守卫直接读取 ``APP_ENV`` 环境变量,不依赖 Settings 实例化(避免
           "未加载 Settings 即可绕过"的漏洞)
      2. 守卫检查在 capability-seal 之前,确保即使设置了 ``ALLOW_LEGACY_RESTORE``
         也无法解封生产环境的 legacy writer
      3. 守卫使用与 ``config/settings.py`` 的
         ``validate_no_legacy_restore_in_production`` 相同的生产环境判定逻辑
         (``APP_ENV`` 与 ``ENVIRONMENT`` 两种标识),但作为函数级守卫,可在
         CLI 直接调用 ``run_restore()`` 时生效(Settings 可能未实例化)

设计原则:
    - **fail-closed**:任何不确定状态(包括无法读取环境变量)均视为非生产,
      但若显式设置为 production/staging 则强制 fail
    - **不可绕过**:生产环境无逃生舱,``ALLOW_LEGACY_RESTORE`` 被忽略
    - **机器可验证**:守卫 raise 的异常含明确的 error_code 与 diagnostics
"""
from __future__ import annotations

import os

from services.error_codes import AppError, ErrorCodes
from services.i18n import translate as _i18n_t


# 生产环境标识集合(小写比较)
_PRODUCTION_ENVS = frozenset({"production", "staging", "prod", "stg"})


def _detect_production_environment() -> tuple[bool, str]:
    """检测当前是否为生产/staging 环境。

    R69 P0-1: APP_ENV 是单一权威源,Dockerfile/Compose/run_all.py/Settings
    全部以 APP_ENV 为事实源。ENVIRONMENT / DEPLOY_ENV 作为历史兼容降级读取
    (旧部署可能未迁移到 APP_ENV),但 APP_ENV 优先级最高。

    直接读取环境变量,不依赖 Settings 实例化(R67 P0-06 关键设计)。

    检查顺序:
        1. ``APP_ENV``(R69 P0-1 单一权威源,Dockerfile 设置的 ENV APP_ENV=production)
        2. ``ENVIRONMENT``(历史兼容,Settings 字段对应的 env var)
        3. ``DEPLOY_ENV``(常见部署工具标识,历史兼容)

    Returns:
        (is_production, source_env_var):若为生产环境,返回 (True, env_var_name);
        否则返回 (False, "")。
    """
    for env_var in ("APP_ENV", "ENVIRONMENT", "DEPLOY_ENV"):
        val = os.environ.get(env_var, "").strip().lower()
        if val in _PRODUCTION_ENVS:
            return True, env_var
    return False, ""


def assert_no_legacy_restore_in_production(
    *, entry_point: str, caller: str,
) -> None:
    """R67 P0-06 硬守卫:生产环境禁止调用 legacy restore 公共入口。

    本守卫在 capability-seal 之前执行,确保即使设置了 ``ALLOW_LEGACY_RESTORE``
    也无法解封生产环境的 legacy writer。

    Args:
        entry_point: 被调用的入口名称(如 "run_restore()", "db_backup.restore_from_backup()")
        caller: 调用方标识(如 "run_restore", "command_bus.make_restore_backup_command")

    Raises:
        AppError(ErrorCodes.RESTORE_LEGACY_WRITER_SEALED): 当检测到生产环境时
            无条件 raise(不允许 ``ALLOW_LEGACY_RESTORE`` 解封)。

    安全保障:
        - 守卫直接读取 ``APP_ENV`` / ``ENVIRONMENT`` / ``DEPLOY_ENV`` 环境变量,
          不依赖 Settings 实例化(避免 "未加载 Settings 即可绕过" 的漏洞)
        - 守卫在 capability-seal 之前执行,即使 ``ALLOW_LEGACY_RESTORE=1`` 也无法解封
        - 生产环境无逃生舱 — tests/ 与 scripts/ 必须在非生产环境(不设置 APP_ENV 或
          设置为 development/test)下运行才能使用 legacy writer
    """
    is_production, source_env = _detect_production_environment()
    if not is_production:
        return  # 非生产环境,允许 capability-seal 的逃生舱逻辑继续判断

    # 生产环境:无条件拒绝,不允许 ALLOW_LEGACY_RESTORE 解封
    allow_legacy = os.environ.get("ALLOW_LEGACY_RESTORE", "").lower()
    allow_legacy_set = allow_legacy in ("1", "true", "yes")

    # 构造详细的诊断信息
    raise AppError(
        ErrorCodes.RESTORE_LEGACY_WRITER_SEALED,
        params={
            "caller": caller,
            "reason": (
                "r67_p0_06_production_hard_guard"
                if not allow_legacy_set
                else "r67_p0_06_production_hard_guard_allow_legacy_ignored"
            ),
            "entry_point": entry_point,
            "source_env_var": source_env,
            "allow_legacy_restore_set": str(allow_legacy_set),
        },
    )


def is_production_environment() -> bool:
    """R67 P0-06: 返回当前是否为生产/staging 环境(供其他模块复用)。"""
    is_prod, _ = _detect_production_environment()
    return is_prod
