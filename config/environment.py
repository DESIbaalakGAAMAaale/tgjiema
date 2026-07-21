"""R70 Wave 1: APP_ENV 单一事实源 — 唯一环境解析模块。

R70 P0-03 根因:
    Settings.APP_ENV 默认值为 "development" (非空)。
    validator 只在 APP_ENV 为空时才读取 ENVIRONMENT/DEPLOY_ENV,
    但因默认值非空,只设置 ENVIRONMENT=production 的旧部署会:
      1. APP_ENV 保持 "development" (Pydantic 用默认值填充)
      2. validator 反向把 ENVIRONMENT 覆盖成 "development"
      3. 生产 guard / legacy restore seal / 加密强制等逻辑按 development 执行
    这造成生产部署静默降级。

R70 Wave 1 整改:
    建立唯一环境解析入口,Settings / run_all / _production_guard /
    i18n / restore_guard / migration / Compose / systemd / workflow
    全部调用同一解析逻辑。

允许值(显式枚举):
    - development
    - test
    - staging
    - production

历史兼容变量(降级读取):
    - ENVIRONMENT (Settings 字段对应的 env var)
    - DEPLOY_ENV (常见部署工具标识)

解析规则(优先级从高到低):
    1. APP_ENV 显式存在 → 作为权威值
    2. APP_ENV 缺失但 ENVIRONMENT 或 DEPLOY_ENV 存在 → 明确迁移 + 弃用告警
    3. 三者都缺失 → 仅显式本地开发命令(如 run_all.py 无 --standalone)
       可选择 development;生产入口(Settings 加载 / 镜像启动)必须 fail-closed

冲突检测:
    - 多变量存在且值不同 → 拒绝启动(ValueError)
    - 多变量存在且值相同 → 允许但输出弃用告警
    - 大小写/空格差异 → 视为相同(规范化比较)

别名规范化:
    - prod → production (并输出弃用告警)
    - stg → staging  (并输出弃用告警)

禁止值:
    - 未知值 / 拼写错误 → 拒绝启动
    - 空字符串 + 生产入口 → 拒绝启动
"""
from __future__ import annotations

import os
from enum import Enum
from typing import Optional


class AppEnvironment(str, Enum):
    """R70 Wave 1: 应用环境显式枚举。

    取值只能为 development / test / staging / production。
    其他任何值(含 prod/stg 别名)在 parse_app_env() 内规范化,
    但规范化会输出弃用告警 — 生产应直接使用规范值。
    """
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


_ALLOWED_ENVS: frozenset[str] = frozenset({
    e.value for e in AppEnvironment
})

# 历史兼容别名(规范化映射)
_ALIAS_MAP: dict[str, str] = {
    "prod": AppEnvironment.PRODUCTION.value,
    "stg": AppEnvironment.STAGING.value,
}

# 被识别为"生产/staging"的环境集合(用于 _production_guard / i18n / migration 等)
_PRODUCTION_LIKE_ENVS: frozenset[str] = frozenset({
    AppEnvironment.PRODUCTION.value,
    AppEnvironment.STAGING.value,
})

# 三变量名(用于冲突检测与弃用告警)
_ENV_VAR_NAMES: tuple[str, ...] = ("APP_ENV", "ENVIRONMENT", "DEPLOY_ENV")


class EnvironmentResolutionError(ValueError):
    """R70 Wave 1: 环境解析失败(冲突 / 未知值 / 生产入口缺值)。

    本异常在 Settings 加载阶段抛出,直接导致进程启动失败(fail-closed)。
    """

    def __init__(
        self,
        reason: str,
        *,
        app_env: str = "",
        environment: str = "",
        deploy_env: str = "",
        conflict_vars: Optional[list[str]] = None,
    ):
        self.reason = reason
        self.app_env = app_env
        self.environment = environment
        self.deploy_env = deploy_env
        self.conflict_vars = conflict_vars or []
        details = (
            f"[R70-P0-03/Wave1] {reason} "
            f"(APP_ENV={app_env or '<unset>'!r}, "
            f"ENVIRONMENT={environment or '<unset>'!r}, "
            f"DEPLOY_ENV={deploy_env or '<unset>'!r})"
        )
        if conflict_vars:
            details += f" conflict_vars={conflict_vars}"
        super().__init__(details)


def _read_env_raw(name: str) -> str:
    """读取环境变量的原始值(不去除空白),仅返回 str。"""
    val = os.environ.get(name)
    if val is None:
        return ""
    return val


def _normalize(value: str) -> str:
    """规范化环境变量值:去除首尾空白 + 转小写。"""
    return value.strip().lower()


def _emit_deprecation_warning(msg: str) -> None:
    """输出弃用告警到 stderr(避免循环导入 loguru)。

    使用 print 到 stderr 而非 loguru,因为本模块可能在 loguru 初始化前被调用。
    """
    import sys
    print(f"[R70-Wave1][DEPRECATION] {msg}", file=sys.stderr, flush=True)


def parse_app_env(
    *,
    allow_default_development: bool = False,
    raw_overrides: Optional[dict[str, str]] = None,
) -> AppEnvironment:
    """R70 Wave 1: 唯一环境解析入口。

    所有入口(Settings / run_all / _production_guard / i18n / restore_guard /
    migration / Compose / systemd / workflow)必须调用此函数获取权威环境值。

    Args:
        allow_default_development:
            True — 三变量全部缺失时允许回退到 development(仅限本地开发命令,
            如 `python run_all.py` 无 --standalone)。默认 False。
        raw_overrides:
            可选的显式覆盖字典(主要供 Settings 的 before-validator 使用,
            把 Pydantic 已解析的原始值传入而非重新读取环境变量)。
            键: APP_ENV / ENVIRONMENT / DEPLOY_ENV
            值: 对应的原始字符串(未规范化)
            若未提供,则直接从 os.environ 读取。

    Returns:
        AppEnvironment 枚举值(已规范化 + 已校验)

    Raises:
        EnvironmentResolutionError: 当
            - 多变量冲突(值不同)
            - 未知值 / 拼写错误
            - 三变量全部缺失且 allow_default_development=False (生产 fail-closed)

    Side effects:
        - 若使用别名(prod/stg),输出弃用告警
        - 若使用历史变量(ENVIRONMENT/DEPLOY_ENV),输出弃用告警
        - 若多变量同值,输出弃用告警
    """
    # 1. 读取三变量原始值(优先用 raw_overrides,其次 os.environ)
    if raw_overrides is not None:
        app_env_raw = raw_overrides.get("APP_ENV", "") or _read_env_raw("APP_ENV")
        environment_raw = raw_overrides.get("ENVIRONMENT", "") or _read_env_raw("ENVIRONMENT")
        deploy_env_raw = raw_overrides.get("DEPLOY_ENV", "") or _read_env_raw("DEPLOY_ENV")
    else:
        app_env_raw = _read_env_raw("APP_ENV")
        environment_raw = _read_env_raw("ENVIRONMENT")
        deploy_env_raw = _read_env_raw("DEPLOY_ENV")

    app_env_norm = _normalize(app_env_raw)
    environment_norm = _normalize(environment_raw)
    deploy_env_norm = _normalize(deploy_env_raw)

    # 2. 冲突检测:多变量存在但值不同 → 拒绝启动
    present_vars: dict[str, str] = {}
    if app_env_norm:
        present_vars["APP_ENV"] = app_env_norm
    if environment_norm:
        present_vars["ENVIRONMENT"] = environment_norm
    if deploy_env_norm:
        present_vars["DEPLOY_ENV"] = deploy_env_norm

    if len(present_vars) >= 2:
        unique_values = set(present_vars.values())
        if len(unique_values) > 1:
            raise EnvironmentResolutionError(
                "环境变量冲突: APP_ENV / ENVIRONMENT / DEPLOY_ENV 同时存在但值不同,"
                "拒绝启动(防止生产环境静默降级)。"
                "请只配置 APP_ENV,或确保三者值一致。",
                app_env=app_env_raw,
                environment=environment_raw,
                deploy_env=deploy_env_raw,
                conflict_vars=list(present_vars.keys()),
            )
        # 多变量同值:允许,但输出弃用告警
        _emit_deprecation_warning(
            f"多环境变量同时设置且值一致({present_vars})。"
            "R70 Wave 1 推荐:仅配置 APP_ENV,ENVIRONMENT/DEPLOY_ENV 将在后续版本移除。"
        )

    # 3. 确定权威值(优先级:APP_ENV > ENVIRONMENT > DEPLOY_ENV)
    auth_value = app_env_norm or environment_norm or deploy_env_norm

    # 4. 三变量全部缺失
    if not auth_value:
        if allow_default_development:
            return AppEnvironment.DEVELOPMENT
        raise EnvironmentResolutionError(
            "环境变量全部缺失:APP_ENV / ENVIRONMENT / DEPLOY_ENV 均未设置,"
            "生产入口拒绝启动(fail-closed)。"
            "本地开发请显式设置 APP_ENV=development;生产环境必须显式设置 APP_ENV=production。"
        )

    # 5. 别名规范化(prod → production, stg → staging)
    if auth_value in _ALIAS_MAP:
        canonical = _ALIAS_MAP[auth_value]
        _emit_deprecation_warning(
            f"环境别名 '{auth_value}' 已规范化为 '{canonical}'。"
            "R70 Wave 1 推荐:直接使用规范值,别名将在后续版本移除。"
        )
        auth_value = canonical

    # 6. 显式枚举校验(未知值 / 拼写错误 fail-closed)
    if auth_value not in _ALLOWED_ENVS:
        raise EnvironmentResolutionError(
            f"APP_ENV='{auth_value}' 不在允许枚举内({sorted(_ALLOWED_ENVS)})。"
            "缺值 / 未知值 / 拼写错误均不允许启动(fail-closed)。",
            app_env=app_env_raw,
            environment=environment_raw,
            deploy_env=deploy_env_raw,
        )

    # 7. 若仅通过 ENVIRONMENT / DEPLOY_ENV 读取(非 APP_ENV),输出弃用告警
    if not app_env_norm and (environment_norm or deploy_env_norm):
        legacy_var = "ENVIRONMENT" if environment_norm else "DEPLOY_ENV"
        _emit_deprecation_warning(
            f"环境值来自历史变量 {legacy_var}(APP_ENV 未设置)。"
            "R70 Wave 1 推荐:迁移到 APP_ENV,ENVIRONMENT/DEPLOY_ENV 将在后续版本移除。"
        )

    return AppEnvironment(auth_value)


def is_production_like(env: AppEnvironment | str) -> bool:
    """R70 Wave 1: 判断环境是否为 production 或 staging。

    供 _production_guard / i18n / restore_guard / migration 等模块复用。
    接受 AppEnvironment 枚举或已规范化的字符串。
    """
    if isinstance(env, AppEnvironment):
        return env in (AppEnvironment.PRODUCTION, AppEnvironment.STAGING)
    val = _normalize(env) if isinstance(env, str) else ""
    return val in _PRODUCTION_LIKE_ENVS


def is_production(env: AppEnvironment | str) -> bool:
    """R70 Wave 1: 判断环境是否为 production(不含 staging)。

    严格生产判定(如 backup_encryption_required 强制加密)用此函数。
    """
    if isinstance(env, AppEnvironment):
        return env == AppEnvironment.PRODUCTION
    val = _normalize(env) if isinstance(env, str) else ""
    return val == AppEnvironment.PRODUCTION.value


def detect_production_from_os_environ() -> tuple[bool, str]:
    """R70 Wave 1: 直接从 os.environ 检测生产环境(不依赖 Settings 实例化)。

    供 _production_guard 等需要在 Settings 加载前判定的场景使用。

    与 parse_app_env() 的区别:
        - 本函数不抛异常,只返回 (is_production, source_env_var)
        - 用于"守卫"场景(即使环境配置错误也只拒绝,不崩溃)
        - 用于 R67 P0-06 / R66 P0-07 的 legacy restore 硬守卫

    Returns:
        (is_production_like, source_env_var):若为 production/staging,
        返回 (True, env_var_name);否则返回 (False, "")。
    """
    for env_var in _ENV_VAR_NAMES:
        val = _normalize(_read_env_raw(env_var))
        if val in _PRODUCTION_LIKE_ENVS:
            return True, env_var
        # 别名规范化
        if val in _ALIAS_MAP and _ALIAS_MAP[val] in _PRODUCTION_LIKE_ENVS:
            return True, env_var
    return False, ""


__all__ = [
    "AppEnvironment",
    "EnvironmentResolutionError",
    "parse_app_env",
    "is_production",
    "is_production_like",
    "detect_production_from_os_environ",
]
