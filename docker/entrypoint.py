#!/usr/bin/env python3
"""R70 Wave 2: 正式容器入口 — 基于 SERVICE_ROLE 的角色映射启动器。

R70 P0-04 根因:
    Dockerfile 默认 CMD ["python", "run_all.py"] 在 APP_ENV=production 下
    会调用 run_all.py 的 main(),而 main() 在生产环境下直接 exit 1(拒绝多进程模式),
    导致容器进入 restart loop。
    旧 Compose 通过 `command: python run_all.py --standalone up` 覆盖 CMD,
    但 systemd 部署又复制了启动命令,造成多份命令定义漂移风险。

R70 Wave 2 整改:
    建立唯一容器入口 docker/entrypoint.py,通过 SERVICE_ROLE 环境变量映射到
    唯一生产命令。Dockerfile / Compose / systemd 全部只注入 SERVICE_ROLE,
    不复制启动命令。

入口契约:
    1. 读取 APP_ENV(由 config.environment.parse_app_env 解析)
    2. 读取 SERVICE_ROLE 环境变量
    3. SERVICE_ROLE 必须在显式枚举内:
         up / idx / dsp / mon / admin / admin_bot
         db_writer / crdb_sync / db_backup / migration
         prometheus_exporter / r40_scheduler
    4. 未指定或未知 SERVICE_ROLE → 输出明确错误 + exit 1
    5. production/staging 下未指定 SERVICE_ROLE → exit 1
       (避免容器空跑被误判为正常)
    6. development/test 下未指定 SERVICE_ROLE → 回退到 run_all.py 多进程模式
       (本地开发兼容)

实现说明:
    - 不直接执行业务逻辑,而是把 SERVICE_ROLE 转换成 `python run_all.py
      --standalone <role>` 调用,复用 run_all.py 中已注册的 BOT_RUNNERS
    - migration 与 prometheus_exporter 走独立模块入口(非 run_all.py 的 BOT_RUNNERS):
        migration → python -m services.migration_runner
        prometheus_exporter → python -m services.prometheus_exporter
    - exec 模式:本脚本通过 os.execvp 直接替换进程映像,确保 PID 1 是业务进程
      (而非 entrypoint 包装层),这样 SIGTERM 直接发到业务进程
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# R71 fix: 确保 /app 在 sys.path 中,使 `from config.environment import` 可用
# entrypoint.py 位于 /app/docker/,sys.path[0] 默认是 /app/docker 而非 /app
# 需要将 /app(父目录)加入 sys.path,才能导入 config / services / bots 等顶层包
_APP_ROOT = str(Path(__file__).resolve().parent.parent)
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)


# ──────────────────────────────────────────────────────────────────
# SERVICE_ROLE 显式枚举
# ──────────────────────────────────────────────────────────────────
# Wave 2 §3 要求的 SERVICE_ROLE 角色:
#   up / idx / dsp / mon / admin / admin_bot / db_writer / crdb_sync
#   / db_backup / migration / prometheus_exporter
# 额外允许:r40_scheduler(已在 run_all.py BOT_RUNNERS 中)
# R76 O6: 新增 provider_sim(secretless CI Provider 协议模拟器,
# 仅在 APP_ENV=test/development 下允许,生产环境禁止启动)
SERVICE_ROLE_RUN_ALL = frozenset({
    "up", "idx", "dsp", "mon", "admin", "admin_bot",
    "db_writer", "crdb_sync", "db_backup", "r40_scheduler",
})

# 走独立模块入口的角色(非 run_all.py BOT_RUNNERS)
SERVICE_ROLE_MODULE = {
    "migration": "services.migration_runner",
    "prometheus_exporter": "services.prometheus_exporter",
    # R76 O6: secretless CI Provider 模拟器入口
    # 仅在 SECRETLESS_MODE=true 下可用,生产环境 fail-closed
    "provider_sim": "tests.support.provider_simulator",
}

# 全部允许的 SERVICE_ROLE
ALLOWED_SERVICE_ROLES = SERVICE_ROLE_RUN_ALL | frozenset(SERVICE_ROLE_MODULE.keys())


def _log_error(msg: str) -> None:
    """输出错误到 stderr(不依赖 loguru,确保最早期可用)。"""
    print(f"[docker/entrypoint] ERROR: {msg}", file=sys.stderr, flush=True)


def _log_info(msg: str) -> None:
    """输出信息到 stderr(避免污染 stdout 业务输出)。"""
    print(f"[docker/entrypoint] {msg}", file=sys.stderr, flush=True)


def _resolve_app_env() -> str:
    """R70 Wave 2: 调用 config.environment.parse_app_env() 解析 APP_ENV。

    在容器入口最早期调用,如果 APP_ENV 配置错误(冲突/未知值/缺失),立即 fail-closed。
    """
    # 容器入口是生产入口,三变量全缺失时 fail-closed
    # (allow_default_development=False)
    try:
        from config.environment import (
            AppEnvironment,
            EnvironmentResolutionError,
            parse_app_env,
        )
    except ImportError as e:
        _log_error(f"无法导入 config.environment 模块: {e}")
        sys.exit(2)

    try:
        resolved = parse_app_env(allow_default_development=False)
    except EnvironmentResolutionError as e:
        _log_error(f"APP_ENV 解析失败: {e}")
        sys.exit(2)

    return resolved.value


def _build_command(service_role: str) -> list[str]:
    """根据 SERVICE_ROLE 构造启动命令。

    返回的是 argv list,将通过 os.execvp 执行。
    """
    if service_role in SERVICE_ROLE_MODULE:
        # 走独立模块入口
        module = SERVICE_ROLE_MODULE[service_role]
        return [sys.executable, "-m", module]

    if service_role in SERVICE_ROLE_RUN_ALL:
        # 走 run_all.py --standalone <role>
        run_all_path = str(Path(__file__).resolve().parent.parent / "run_all.py")
        return [sys.executable, run_all_path, "--standalone", service_role]

    # 不应该到这里(_validate_service_role 已经过滤)
    _log_error(f"未知 SERVICE_ROLE: {service_role!r}")
    sys.exit(2)


def _assert_no_test_escape_hatches() -> None:
    """R70 Wave 3: 调用统一逃生舱守卫(在 exec 业务进程前)。

    在容器入口最早期阶段(已解析 APP_ENV 但还未 exec 业务进程)调用,
    检测到任何 production/staging 下的逃生舱变量立即 fail-closed。

    这是 entrypoint 的第二道防线:
      - 第一道防线:Settings 加载时 after-validator 中的守卫
      - 第二道防线:此处 docker/entrypoint 的守卫(确保 PID 1 启动前就阻断)
      - 第三道防线:业务进程内的 services/_production_guard 守卫
    """
    try:
        from services.escape_hatch_guard import assert_no_test_escape_hatches
    except ImportError as e:
        _log_error(f"无法导入 services.escape_hatch_guard 模块: {e}")
        sys.exit(2)
    try:
        assert_no_test_escape_hatches(caller="docker/entrypoint")
    except Exception as e:
        _log_error(f"检测到生产环境测试逃生舱,R70 Wave 3 守卫拒绝启动: {e}")
        sys.exit(3)


def _run_readiness_gate(app_env: str, service_role: str) -> None:
    """R71 Wave 1 / R73 §5.10 (P1-06): 启动前 readiness gate — production/staging 强制。

    在 exec 业务进程前,依次执行两道门禁:
      1. **startup gate**(R73 §5.10):调用 services.health.check_startup(role)
         验证必需初始化步骤(settings_loaded / bot_token_configured /
         redis_connected / writer_group_created / schema_migrated 等)已完成。
         未通过 → sys.exit(4)(fail-closed)。
      2. **readiness gate**(R71 Wave 1):调用 services.health.check_readiness(role)
         执行真实依赖检查。任一 critical 检查失败 → sys.exit(4)(fail-closed)。

    R73 §5.10 整改:启动前必须先验证 startup,再验证 readiness。
    顺序:startup gate → readiness gate(前者失败立即 exit,不进入 readiness)。

    production/staging 强制执行;development/test 跳过(本地开发兼容)。

    退出码:
        0: 通过(隐式,函数返回)
        4: 未通过(startup gate 失败 / readiness gate 失败 / 模块崩溃)

    Args:
        app_env: 已解析的 APP_ENV(production/staging/development/test)
        service_role: 规范化后的 SERVICE_ROLE(如 "up_bot" / "db_writer")
    """
    # R72 P0-01: 删除通用 CI 变量 bypass — RC Runtime E2E 必须执行与生产
    # 完全相同的 readiness 语义。CI 环境缺少真实依赖时门禁必须失败(fail-closed),
    # 不得通过环境变量跳过。单元测试使用 mock;Compose RC 使用真实 production profile。
    if app_env not in ("production", "staging"):
        # development/test 跳过 readiness gate(本地开发兼容)
        return

    _log_info(
        f"R71 Wave 1 / R73 §5.10: 启动前 gate(app_env={app_env}, "
        f"role={service_role})"
    )

    # R71 RC25 fix: 标记启动前 readiness gate 模式。
    # services.health 中的 _check_admin_web_port() 和 _check_metrics_endpoint()
    # 检测此环境变量,在启动前跳过自身端口检查(进程还没 exec,端口自然没监听 —
    # 先有鸡还是先有蛋)。运行时 healthcheck 不设置此变量,正常执行端口检查。
    # R73 §5.10: 同样用于 check_startup 的 in_startup_grace 判定(启动宽限期内
    # started=True,避免容器编排 kill 尚未完成启动的进程)。
    os.environ["READINESS_GATE_PRE_LAUNCH"] = "1"

    try:
        import asyncio

        from services.health import check_readiness, check_startup
    except ImportError as e:
        # readiness 模块本身不可用 → fail-closed
        # 不允许跳过(pretend healthy),否则容器会以不健康状态启动
        _log_error(
            f"R71 readiness gate 失败: 无法导入 services.health 模块: {e}"
        )
        sys.exit(4)

    # ── R73 §5.10: 第一道门禁 — startup gate ──
    # 验证必需初始化步骤已完成(settings_loaded / bot_token_configured 等)。
    # 启动宽限期内(READINESS_GATE_PRE_LAUNCH=1)check_startup 总是 started=True,
    # 此门禁等价于"声明所有启动步骤的预期状态",便于审计 pending_initializations。
    # 运行态下若 startup_completed_at 未记录或有 pending 步骤,直接 fail-closed。
    try:
        startup_result = check_startup(service_role)
    except Exception as e:
        # check_startup 崩溃 → fail-closed(不允许跳过)
        _log_error(
            f"R73 §5.10 startup gate 崩溃: {type(e).__name__}: {e}"
        )
        sys.exit(4)

    if not startup_result["started"]:
        # startup 未完成 → fail-closed(R73 §5.10 / §5.17)
        # 记录详细 pending_initializations,便于运维排查
        pending = startup_result.get("pending_initializations", [])
        _log_error(
            f"R73 §5.10 startup gate 未通过: role={startup_result.get('role')} "
            f"started=False, pending_initializations={pending}"
        )
        sys.exit(4)

    _log_info(
        f"R73 §5.10 startup gate 通过: role={startup_result['role']} "
        f"in_startup_grace={startup_result['in_startup_grace']} "
        f"pending={startup_result.get('pending_initializations', [])}"
    )

    # ── R73 §5.10: 第二道门禁 — readiness gate ──
    # 验证依赖健康(critical 检查全通过)。
    try:
        result = asyncio.run(check_readiness(service_role))
    except Exception as e:
        # check_readiness 崩溃 → fail-closed(不允许跳过)
        _log_error(
            f"R71 readiness gate 崩溃: {type(e).__name__}: {e}"
        )
        sys.exit(4)

    # RC25: 清除启动前标记,避免影响业务进程
    del os.environ["READINESS_GATE_PRE_LAUNCH"]

    if not result.healthy:
        # critical 检查失败 → fail-closed
        # 列出所有失败项,便于运维排查
        failed_critical = [
            chk for chk in result.checks
            if chk.critical and not chk.healthy
        ]
        failed_names = ", ".join(
            f"{chk.name}({chk.error})" for chk in failed_critical
        )
        _log_error(
            f"R71 readiness gate 未通过: role={result.role} "
            f"有 {len(failed_critical)} 个 critical 检查失败: {failed_names}"
        )
        sys.exit(4)

    # 通过 → 记录通过信息(包含通过项数,便于审计)
    passed_count = sum(1 for chk in result.checks if chk.healthy)
    total_count = len(result.checks)
    _log_info(
        f"R71 readiness gate 通过: role={result.role} "
        f"({passed_count}/{total_count} 项健康)"
    )


def main() -> None:
    """R70 Wave 2: 容器入口主逻辑。"""
    # 1. 解析 APP_ENV(fail-closed,错误立即退出)
    app_env = _resolve_app_env()
    _log_info(f"APP_ENV={app_env}")

    # 1b. R70 Wave 3: 逃生舱硬守卫(在 exec 业务进程前)
    # 必须在解析 APP_ENV 之后(确认是 production/staging)才能检查
    _assert_no_test_escape_hatches()

    # 2. 读取 SERVICE_ROLE
    service_role = os.environ.get("SERVICE_ROLE", "").strip().lower()

    # 3. SERVICE_ROLE 校验
    if not service_role:
        # production/staging 下未指定 → fail-closed
        if app_env in ("production", "staging"):
            _log_error(
                f"APP_ENV={app_env} 下未指定 SERVICE_ROLE。"
                "生产环境必须通过 Compose environment 或 systemd 注入 SERVICE_ROLE。"
                f"允许值: {sorted(ALLOWED_SERVICE_ROLES)}"
            )
            sys.exit(1)
        # development/test 下未指定 → 回退到 run_all.py 多进程模式(本地开发兼容)
        _log_info(
            f"APP_ENV={app_env} 且未指定 SERVICE_ROLE,回退到 run_all.py 多进程模式"
            "(仅本地开发用,生产环境禁止)"
        )
        run_all_path = str(Path(__file__).resolve().parent.parent / "run_all.py")
        os.execvp(sys.executable, [sys.executable, run_all_path] + sys.argv[1:])
        return  # execvp 不返回,这只是类型提示

    if service_role not in ALLOWED_SERVICE_ROLES:
        _log_error(
            f"未知 SERVICE_ROLE: {service_role!r}。"
            f"允许值: {sorted(ALLOWED_SERVICE_ROLES)}"
        )
        sys.exit(1)

    # 3b. R76 O6: provider_sim 角色边界守卫 — 仅允许在 test/development 下启动
    # 生产环境(staging/production)严禁启动 secretless Provider 模拟器,
    # 否则会导致生产流量被拦截到测试服务,造成数据泄露/服务不可用。
    if service_role == "provider_sim" and app_env in ("production", "staging"):
        _log_error(
            f"R76 O6 边界守卫: SERVICE_ROLE=provider_sim 仅允许在 "
            f"APP_ENV=test/development 下启动,当前 APP_ENV={app_env!r}。"
            "生产环境严禁运行 secretless Provider 模拟器。"
        )
        sys.exit(1)

    # 4. 构造启动命令
    cmd = _build_command(service_role)
    _log_info(f"SERVICE_ROLE={service_role}, exec: {' '.join(cmd)}")

    # 4b. R71 Wave 1: 启动前 readiness gate(production/staging 强制,
    # development/test 跳过)。任一 critical 检查失败 → exit 4(fail-closed)。
    # 注意:此处传入的 service_role 是 entrypoint 别名(如 "up"/"idx"),
    # services.health._canonicalize_role 会自动规范化为 "up_bot" 等。
    _run_readiness_gate(app_env, service_role)

    # 5. exec 替换进程映像 — PID 1 直接是业务进程,SIGTERM 直达
    # sys.argv[1:] 透传给业务进程(如 prometheus_exporter 的 --port 等)
    extra_args = sys.argv[1:]
    if extra_args:
        cmd = cmd + extra_args

    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    main()
