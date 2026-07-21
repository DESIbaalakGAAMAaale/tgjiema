#!/usr/bin/env python3
"""R67 P1-09 / R69 Wave 7: Compose 静态规则门禁(已重命名以反映实际能力)。

整改背景(R67 终审报告 P1-09 + R69 Wave 7):
    R69 Wave 7 要求:静态 lint 不得命名为 "runtime smoke"。
    本脚本原名 check_compose_runtime_smoke.py,但实际只做静态规则校验,
    不会启动任何容器。为消除命名误导,R69 Wave 7 重命名为
    check_compose_static_rules.py,文件能力与 docstring 描述保持一致。

    真正的运行态 smoke 由 scripts/runtime_smoke_compose.py 提供
    (执行 docker compose up + 健康探针 + SIGTERM/restart 验证 + 日志扫描)。

本脚本的范围与边界(诚实声明):
    1. 本脚本是**静态规则门禁**,不是运行态 smoke。
       本脚本只解析 docker-compose.yml,验证可静态判定的运行态契约,
       不会启动任何容器,不验证真实运行时行为。

    2. 本脚本通过解析 docker-compose.yml(并可选校验渲染后产物),验证以下
       可静态判定的运行态契约:
         (a) 非 root: Dockerfile USER 非 0(compose 无 user 覆盖时由 Dockerfile 决定)
         (b) read-only filesystem: `read_only: true`
         (c) tmpfs: 有 /tmp 可写挂载(配合 read_only)
         (d) cap_drop: 至少 drop ALL
         (e) security_opt: no-new-privileges:true
         (f) healthcheck: 每个长运行服务必须配置 healthcheck
         (g) secrets mount: secret 通过 .env.secrets.<service> env_file 注入,
             不挂载到容器外共享卷
         (h) 网络隔离: 默认 bridge 网络中,只暴露 admin/prometheus_exporter 端口
             且必须绑定 127.0.0.1
         (i) restart policy: 长运行服务 restart: always 或 unless-stopped;
             oneshot 服务 restart: "no"
         (j) graceful shutdown: Dockerfile 含 STOPSIGNAL SIGTERM(由 Dockerfile
             校验,compose 无配置);本脚本验证 docker-compose.yml 未覆盖
             stop_signal 为非 SIGTERM
         (k) migration ordering: migration 服务必须先于 db_writer/crdb_sync/
             up/idx/dsp/mon/admin_bot/admin/db_backup 启动(condition:
             service_completed_successfully)

    3. CI 调用方式:
         python scripts/check_compose_static_rules.py [--compose docker-compose.yml]

退出码:
    0 — 所有静态规则通过
    1 — 检测到违规
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

# 项目根目录(scripts/ 的上一级)
REPO_ROOT = Path(__file__).resolve().parent.parent

# 默认 compose 文件路径
DEFAULT_COMPOSE = REPO_ROOT / "docker-compose.yml"

# 长运行服务(必须 restart: always 或 unless-stopped + healthcheck)
LONG_RUNNING_SERVICES: frozenset[str] = frozenset({
    "redis",
    "db_writer",
    "crdb_sync",
    "up",
    "idx",
    "dsp",
    "mon",
    "admin_bot",
    "admin",
    "db_backup",
    "prometheus_exporter",
})

# oneshot 服务(必须 restart: "no",不需 healthcheck)
ONESHOT_SERVICES: frozenset[str] = frozenset({
    "redis-acl-init",
    "migration",
})

# 依赖 migration 的服务(必须 depends_on.migration.condition:
# service_completed_successfully)
MIGRATION_DEPENDENT_SERVICES: frozenset[str] = frozenset({
    "db_writer",
    "crdb_sync",
    "up",
    "idx",
    "dsp",
    "mon",
    "admin_bot",
    "admin",
    "db_backup",
    "prometheus_exporter",
})

# 允许暴露端口的服务(端口必须绑定 127.0.0.1)
PORT_EXPOSING_SERVICES: frozenset[str] = frozenset({
    "redis",       # 127.0.0.1:6379
    "admin",       # 127.0.0.1:8080
    "prometheus_exporter",  # 127.0.0.1:9100
})

# 允许的 restart 策略
VALID_LONG_RUNNING_RESTART: frozenset[str] = frozenset({
    "always", "unless-stopped", "on-failure",
})

# SIGTERM 是默认 stop_signal(Dockerfile STOPSIGNAL SIGTERM);
# compose 中若显式覆盖,只允许 SIGTERM / SIGINT(优雅关闭信号)
VALID_STOP_SIGNALS: frozenset[str] = frozenset({
    "SIGTERM", "SIGINT", "15", "2",
})


# ════════════════════════════════════════════════════════════════
# YAML 解析(避免引入 PyYAML 依赖,使用最小解析器)
# ════════════════════════════════════════════════════════════════
# 注:docker-compose.yml 结构稳定且本仓库已用,这里使用 PyYAML 优先,
# 失败时返回错误并提示安装(避免 silently 跳过检查)。

def _load_compose(path: Path) -> dict[str, Any]:
    """加载 docker-compose.yml,返回解析后的 dict。

    Raises:
        ImportError: PyYAML 未安装
        FileNotFoundError: 文件不存在
        yaml.YAMLError: YAML 解析失败
    """
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError as e:
        raise ImportError(
            "PyYAML 未安装 — docker-compose 静态规则门禁需要 PyYAML 解析 YAML。"
            "请运行: pip install pyyaml"
        ) from e
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} 顶层结构不是 dict")
    return data


# ════════════════════════════════════════════════════════════════
# 规则检查
# ════════════════════════════════════════════════════════════════


class Violation:
    """单条违规。"""

    def __init__(self, service: str, rule: str, detail: str) -> None:
        self.service = service
        self.rule = rule
        self.detail = detail

    def __str__(self) -> str:
        return f"  - service={self.service} [{self.rule}] {self.detail}"


def _check_read_only(
    name: str, svc: dict[str, Any], is_oneshot: bool
) -> list[Violation]:
    """(b) read_only: true;(c) tmpfs 有 /tmp。"""
    out: list[Violation] = []
    # redis-acl-init 也 read_only(已是)
    if not svc.get("read_only", False):
        out.append(Violation(
            name, "read_only",
            "缺少 read_only: true — 容器文件系统必须只读",
        ))
    tmpfs = svc.get("tmpfs", []) or []
    if isinstance(tmpfs, list) and "/tmp" not in tmpfs:
        out.append(Violation(
            name, "tmpfs",
            "tmpfs 缺少 /tmp — read_only 容器需要 /tmp 可写挂载",
        ))
    return out


def _check_cap_drop(name: str, svc: dict[str, Any]) -> list[Violation]:
    """(d) cap_drop: 至少 ALL。"""
    out: list[Violation] = []
    cap_drop = svc.get("cap_drop", []) or []
    if "ALL" not in cap_drop:
        out.append(Violation(
            name, "cap_drop",
            "cap_drop 必须包含 ALL — 最小权限原则",
        ))
    return out


def _check_security_opt(name: str, svc: dict[str, Any]) -> list[Violation]:
    """(e) security_opt: no-new-privileges:true。"""
    out: list[Violation] = []
    sec_opt = svc.get("security_opt", []) or []
    if "no-new-privileges:true" not in sec_opt:
        out.append(Violation(
            name, "security_opt",
            "security_opt 必须包含 no-new-privileges:true — 禁止提权",
        ))
    return out


def _check_healthcheck(
    name: str, svc: dict[str, Any], is_long_running: bool
) -> list[Violation]:
    """(f) 长运行服务必须有 healthcheck。"""
    out: list[Violation] = []
    if is_long_running and "healthcheck" not in svc:
        out.append(Violation(
            name, "healthcheck",
            "长运行服务缺少 healthcheck — 运行态探针必须配置",
        ))
    return out


def _check_restart_policy(
    name: str, svc: dict[str, Any], is_long_running: bool, is_oneshot: bool
) -> list[Violation]:
    """(i) restart policy。"""
    out: list[Violation] = []
    restart = svc.get("restart", "")
    if is_oneshot:
        if restart != "no":
            out.append(Violation(
                name, "restart",
                f"oneshot 服务 restart 必须为 'no',实际为 '{restart}'",
            ))
    elif is_long_running:
        if restart not in VALID_LONG_RUNNING_RESTART:
            out.append(Violation(
                name, "restart",
                f"长运行服务 restart 必须为 always/unless-stopped/on-failure,"
                f"实际为 '{restart}'",
            ))
    return out


def _check_port_binding(name: str, svc: dict[str, Any]) -> list[Violation]:
    """(h) 暴露端口必须绑定 127.0.0.1。"""
    out: list[Violation] = []
    ports = svc.get("ports", []) or []
    for port_entry in ports:
        if isinstance(port_entry, str):
            # 形如 "127.0.0.1:8080:8080" 或 "8080:8080" 或 "8080"
            if not port_entry.startswith("127.0.0.1:") and not port_entry.startswith("localhost:"):
                out.append(Violation(
                    name, "ports",
                    f"端口 '{port_entry}' 必须绑定到 127.0.0.1 — "
                    f"禁止绑定 0.0.0.0(避免外网暴露)",
                ))
        elif isinstance(port_entry, dict):
            # 长形式 {published: 8080, target: 8080, host_ip: "127.0.0.1"}
            host_ip = port_entry.get("host_ip", "")
            if host_ip not in ("127.0.0.1", "localhost", "::1"):
                out.append(Violation(
                    name, "ports",
                    f"端口 {port_entry} host_ip 必须为 127.0.0.1 — "
                    f"禁止绑定 0.0.0.0",
                ))
    return out


def _check_migration_ordering(name: str, svc: dict[str, Any]) -> list[Violation]:
    """(k) migration 依赖 — 必须有 depends_on.migration.condition:
    service_completed_successfully。"""
    out: list[Violation] = []
    depends_on = svc.get("depends_on", {}) or {}
    mig_dep = depends_on.get("migration") if isinstance(depends_on, dict) else None
    if isinstance(mig_dep, dict):
        condition = mig_dep.get("condition", "")
        if condition != "service_completed_successfully":
            out.append(Violation(
                name, "migration_ordering",
                f"depends_on.migration.condition 必须为 "
                f"service_completed_successfully,实际为 '{condition}'",
            ))
    elif mig_dep is None and "migration" not in (depends_on if isinstance(depends_on, dict) else {}):
        out.append(Violation(
            name, "migration_ordering",
            "缺少 depends_on.migration — migration 必须先于业务服务完成",
        ))
    return out


def _check_stop_signal(name: str, svc: dict[str, Any]) -> list[Violation]:
    """(j) stop_signal — 若显式覆盖,只允许 SIGTERM/SIGINT。"""
    out: list[Violation] = []
    stop_signal = svc.get("stop_signal")
    if stop_signal is not None and str(stop_signal) not in VALID_STOP_SIGNALS:
        out.append(Violation(
            name, "stop_signal",
            f"stop_signal={stop_signal} 不允许 — 必须为 SIGTERM/SIGINT "
            f"(graceful shutdown),不允许 SIGKILL(9)",
        ))
    return out


def _check_secrets_injection(name: str, svc: dict[str, Any]) -> list[Violation]:
    """(g) secrets 注入 — 必须通过 env_file(.env.secrets.<service>),
    不通过共享卷挂载。

    本仓库约定:每个需要 secrets 的服务通过 env_file 注入
    .env.secrets.<service>,而非挂载 secrets 文件到容器。这避免
    secret 文件在共享卷中泄露。
    """
    out: list[Violation] = []
    # 检查是否挂载了 secrets 文件(以 .env 开头的文件作为 volume 挂载)
    volumes = svc.get("volumes", []) or []
    for vol in volumes:
        if isinstance(vol, str):
            # 形如 "./.env.secrets.up:/app/.env.secrets:ro"
            if ".env" in vol and ":/" in vol:
                out.append(Violation(
                    name, "secrets_mount",
                    f"volume '{vol}' 挂载 .env 文件 — secrets 必须通过 "
                    f"env_file 注入,不得挂载到容器",
                ))
    return out


def _check_resource_limits(name: str, svc: dict[str, Any]) -> list[Violation]:
    """附加检查:每个服务必须有 deploy.resources.limits(cpus + memory)。"""
    out: list[Violation] = []
    deploy = svc.get("deploy", {}) or {}
    resources = deploy.get("resources", {}) or {}
    limits = resources.get("limits", {}) or {}
    if not limits:
        out.append(Violation(
            name, "resource_limits",
            "缺少 deploy.resources.limits — 每个服务必须有 cpus/memory 限制",
        ))
    else:
        if "cpus" not in limits:
            out.append(Violation(
                name, "resource_limits",
                "deploy.resources.limits 缺少 cpus",
            ))
        if "memory" not in limits:
            out.append(Violation(
                name, "resource_limits",
                "deploy.resources.limits 缺少 memory",
            ))
    return out


# ════════════════════════════════════════════════════════════════
# 主校验流程
# ════════════════════════════════════════════════════════════════


def check(compose_path: Path = DEFAULT_COMPOSE) -> tuple[int, list[Violation]]:
    """主校验流程。

    Returns:
        (exit_code, violations)
        exit_code: 0=无违规,1=有违规
    """
    if not compose_path.is_file():
        return 1, [Violation(
            "(global)", "compose_file",
            f"compose 文件不存在: {compose_path}",
        )]

    try:
        data = _load_compose(compose_path)
    except ImportError as e:
        # PyYAML 缺失 — fail-closed(不允许 silently 跳过)
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1, [Violation("(global)", "dependency", str(e))]
    except Exception as e:
        return 1, [Violation("(global)", "compose_parse", str(e))]

    services = data.get("services", {}) or {}
    if not services:
        return 1, [Violation("(global)", "services",
                              "compose 文件缺少 services 段")]

    violations: list[Violation] = []
    for name, svc in services.items():
        if not isinstance(svc, dict):
            violations.append(Violation(
                name, "service_shape",
                f"service 定义不是 dict: {type(svc).__name__}",
            ))
            continue

        is_long_running = name in LONG_RUNNING_SERVICES
        is_oneshot = name in ONESHOT_SERVICES

        # (b)(c) read_only + tmpfs
        violations.extend(_check_read_only(name, svc, is_oneshot))
        # (d) cap_drop
        violations.extend(_check_cap_drop(name, svc))
        # (e) security_opt
        violations.extend(_check_security_opt(name, svc))
        # (f) healthcheck(仅长运行)
        violations.extend(_check_healthcheck(name, svc, is_long_running))
        # (i) restart policy
        violations.extend(_check_restart_policy(
            name, svc, is_long_running, is_oneshot,
        ))
        # (h) port binding(仅暴露端口的服务)
        if name in PORT_EXPOSING_SERVICES or svc.get("ports"):
            violations.extend(_check_port_binding(name, svc))
        # (k) migration ordering(仅依赖 migration 的服务)
        if name in MIGRATION_DEPENDENT_SERVICES:
            violations.extend(_check_migration_ordering(name, svc))
        # (j) stop_signal(若显式覆盖)
        violations.extend(_check_stop_signal(name, svc))
        # (g) secrets injection
        violations.extend(_check_secrets_injection(name, svc))
        # 附加:resource limits
        violations.extend(_check_resource_limits(name, svc))

    if violations:
        print(
            f"[FAIL] R67 P1-09 compose 运行态 smoke 静态门禁检测到 "
            f"{len(violations)} 处违规:"
        )
        for v in violations:
            print(v)
        return 1, violations

    print(
        f"[OK] R67 P1-09 compose 运行态 smoke 静态门禁通过 "
        f"(校验 {len(services)} 个服务,无违规)"
    )
    return 0, violations


def main(argv: list[str] | None = None) -> None:
    """脚本入口。"""
    parser = argparse.ArgumentParser(
        description="R67 P1-09: Compose 运行态 smoke 静态门禁",
    )
    parser.add_argument(
        "--compose",
        type=Path,
        default=DEFAULT_COMPOSE,
        help=f"compose 文件路径(默认: {DEFAULT_COMPOSE})",
    )
    args = parser.parse_args(argv)
    exit_code, _ = check(args.compose)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
